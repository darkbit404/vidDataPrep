#!/usr/bin/env python3
"""
Download an entire (shared) Google Drive folder to a local directory,
recursively, using the Drive API — bypassing the browser's 2GB zip export limit.

Usage:
    python download_drive_folder.py <FOLDER_ID_OR_URL> [--dest ./downloaded_data]

First run will open a browser window for OAuth login (needs credentials.json
in the same directory — see setup instructions).

Handles:
  - Nested subfolders (recreated locally)
  - Large files via resumable/chunked download
  - Google Docs/Sheets/Slides (native Google formats) via export
  - Retries on transient network errors
  - Resuming: skips files that already exist locally with the same size
"""

import argparse
import io
import os
import re
import sys
import time

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

# Google-native formats need to be *exported* to a real file format, not downloaded directly.
GOOGLE_EXPORT_MAP = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

FOLDER_MIME = "application/vnd.google-apps.folder"


def extract_folder_id(id_or_url: str) -> str:
    """Accept either a raw folder ID or a full Drive URL."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", id_or_url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", id_or_url)
    if match:
        return match.group(1)
    return id_or_url  # assume it's already a bare ID


def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                sys.exit(
                    f"Missing {CREDENTIALS_FILE}. Download OAuth client credentials "
                    "from Google Cloud Console (Desktop app type) and place them here."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def list_children(service, folder_id):
    """Yield all direct children (files+folders) of a folder, handling pagination."""
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        resp = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, size, md5Checksum)",
            pageToken=page_token,
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get("files", []):
            yield f
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def download_file(service, file_id, mime_type, dest_path, expected_size=None, retries=5):
    if os.path.exists(dest_path) and expected_size:
        try:
            if os.path.getsize(dest_path) == int(expected_size):
                print(f"  [skip, exists] {dest_path}")
                return
        except (ValueError, TypeError):
            pass

    if mime_type in GOOGLE_EXPORT_MAP:
        export_mime, ext = GOOGLE_EXPORT_MAP[mime_type]
        if not dest_path.endswith(ext):
            dest_path += ext
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    elif mime_type == FOLDER_MIME:
        return  # handled elsewhere
    else:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    for attempt in range(1, retries + 1):
        try:
            fh = io.FileIO(dest_path, "wb")
            downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"\r  {os.path.basename(dest_path)}: {pct}%", end="", flush=True)
            print()  # newline after progress
            fh.close()
            return
        except HttpError as e:
            fh.close()
            wait = min(2 ** attempt, 60)
            print(f"\n  HttpError ({e}) on attempt {attempt}/{retries}, retrying in {wait}s...")
            time.sleep(wait)
        except Exception as e:
            fh.close()
            wait = min(2 ** attempt, 60)
            print(f"\n  Error ({e}) on attempt {attempt}/{retries}, retrying in {wait}s...")
            time.sleep(wait)
    print(f"  [FAILED after {retries} attempts] {dest_path}")


def walk_and_download(service, folder_id, local_path, stats):
    os.makedirs(local_path, exist_ok=True)
    for item in list_children(service, folder_id):
        name = sanitize(item["name"])
        item_path = os.path.join(local_path, name)
        if item["mimeType"] == FOLDER_MIME:
            print(f"\n[dir] {item_path}")
            walk_and_download(service, item["id"], item_path, stats)
        else:
            stats["count"] += 1
            print(f"[{stats['count']}] {item_path}")
            download_file(
                service, item["id"], item["mimeType"], item_path,
                expected_size=item.get("size"),
            )


def run(folder: str, dest: str = "./downloaded_data") -> None:
    """Download `folder` (Drive folder ID or URL) into local directory `dest`."""
    folder_id = extract_folder_id(folder)
    service = get_service()

    # Verify access + get folder name for a nicer root directory name
    try:
        meta = service.files().get(fileId=folder_id, fields="name", supportsAllDrives=True).execute()
        root_name = sanitize(meta.get("name", "downloaded_data"))
    except HttpError as e:
        sys.exit(f"Could not access folder {folder_id}: {e}\n"
                  "Make sure the folder is shared with the Google account you authenticated as.")

    dest_root = os.path.join(dest, root_name) if dest == "./downloaded_data" else dest
    print(f"Downloading '{root_name}' -> {dest_root}\n")

    stats = {"count": 0}
    start = time.time()
    walk_and_download(service, folder_id, dest_root, stats)
    elapsed = time.time() - start
    print(f"\nDone. {stats['count']} files processed in {elapsed/60:.1f} minutes.")


def main():
    parser = argparse.ArgumentParser(description="Recursively download a shared Google Drive folder.")
    parser.add_argument("folder", help="Google Drive folder ID or full URL")
    parser.add_argument(
        "--dest",
        default=os.getenv("DATA_DIR", "./downloaded_data"),
        help="Local destination directory (default: $DATA_DIR from .env, or ./downloaded_data)",
    )
    args = parser.parse_args()
    run(args.folder, args.dest)


if __name__ == "__main__":
    main()