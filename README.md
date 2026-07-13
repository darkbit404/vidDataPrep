# download_drive_folder.py

Recursively download an entire (shared) Google Drive folder to a local directory using the Drive API v3 — bypassing the web UI's 2GB `.zip` export cap.

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Google Cloud OAuth Setup](#google-cloud-oauth-setup)
- [Usage](#usage)
- [How It Works](#how-it-works)
- [Output Structure](#output-structure)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)

---

## Why This Exists

Google Drive's web UI only lets you download folders as `.zip` archives, capped at roughly 2GB each. For folders containing hundreds of gigabytes — common with drone footage, datasets, video archives, or research data — that means dozens of unlabeled zip files downloaded one by one and reassembled by hand. `download_drive_folder.py` bypasses that entirely by talking to the Google Drive API directly: it downloads each file individually and recreates the original folder structure locally — no zipping, no size cap, no manual reassembly.

## Features

- Recursively downloads all files and subfolders from a shared Google Drive folder
- No 2GB or file-count limit — works for folders of any size
- Preserves the original folder/subfolder structure locally
- Resumable — safely stop (`Ctrl+C`) and rerun; it skips files already downloaded in full
- Automatic retries with exponential backoff on transient network/HTTP errors
- Supports Google-native files (Docs, Sheets, Slides, Drawings) by exporting them to `.docx`, `.xlsx`, `.pptx`, `.pdf`
- Works with both personal Drive folders and Shared Drives (Team Drives)
- Streams downloads in 50MB chunks, so even huge files never need to fit in memory
- 100% free — no billing account or paid API tier required

## Prerequisites

- Python 3.8+
- A Google account with access to the target Drive folder (shared with your email, or shared as "Anyone with the link")
- ~10 minutes for a one-time Google Cloud OAuth client setup

## Installation

1. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. **Install dependencies:**
   ```bash
   pip install python-dotenv google-auth google-auth-oauthlib google-api-python-client
   ```
3. **Set up OAuth credentials** — see [Google Cloud OAuth Setup](#google-cloud-oauth-setup) below.
4. **(Optional) Configure `.env`** in the project root to set a default download destination:
   ```
   DATA_DIR=./data
   ```
   If present, `DATA_DIR` becomes the default `--dest` whenever it's omitted on the command line. `load_dotenv()` locates `.env` next to the script itself, so it's found regardless of which directory you run the script from.

## Google Cloud OAuth Setup

Needed once, before the script can authenticate against your Google account.

**1. Create a Google Cloud Project**
- Go to the [Google Cloud Console](https://console.cloud.google.com/).
- Click the project dropdown at the top → **New Project**.
- Give it any name (e.g., `drive-downloader`) → **Create**, and select it.
- This is free — no billing account is required for this kind of read-only access.

**2. Enable the Google Drive API**
- Go to **APIs & Services → Library**.
- Search for **Google Drive API**, click it, then click **Enable**.

**3. Configure the OAuth Consent Screen**
- Go to **APIs & Services → OAuth consent screen** (may appear as **Google Auth Platform → Overview / Branding / Audience** in newer layouts).
- Choose **User type: External** → **Create**.
- Fill in the required fields (App name, support/contact email) — any reasonable values work; the app never needs Google's public verification.
- Leave the app in **Testing** publishing status — do **not** click "Publish app".

**4. Add the Drive Scope**
- Go to **Data Access** → **Add or remove scopes** → search **Google Drive API**.
- Check `.../auth/drive.readonly` — *"See and download all your Google Drive files"*.
- **Update**, then **Save**.

**5. Add Yourself as a Test User**
- Go to **Audience** → **Test users** → **+ Add users**.
- Enter the Google account email you'll authenticate with (the one with access to the target folder) → **Save**.
- **Most commonly missed step.** Skipping it causes `403: access_denied — has not completed the Google verification process` on login.

**6. Create OAuth Client Credentials**
- Go to **APIs & Services → Credentials** (or **Clients**) → **+ Create Credentials → OAuth client ID**.
- Application type: **Desktop app** → **Create**.
- Click **Download JSON**, rename it to `credentials.json`, and place it next to `download_drive_folder.py`.
- ⚠️ Never commit `credentials.json` or `token.json` — see [Security Notes](#security-notes).

**First run:** a URL is printed and/or a browser window opens automatically. Sign in with the test-user account; you'll see an **"unverified app"** warning — expected, click **Advanced → Go to [app name] (unsafe)**, then approve the read-only Drive permission. A `token.json` is written locally and reused on subsequent runs, so you won't be prompted again until it expires or is revoked.

## Usage

```bash
python download_drive_folder.py <FOLDER_ID_OR_URL> [--dest ./downloaded_data]
```

| Argument | Required | Description |
|---|---|---|
| `folder` | Yes | The Google Drive folder ID, or a full `https://drive.google.com/drive/folders/...` URL. |
| `--dest` | No | Local directory to download into. Defaults to `$DATA_DIR` from `.env` if set, otherwise `./downloaded_data`. |

**Examples:**

```bash
# Using a full Drive URL
python download_drive_folder.py "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz/view" --dest ./footage

# Using a bare folder ID
python download_drive_folder.py 1AbCdEfGhIjKlMnOpQrStUvWxYz

# Relying on DATA_DIR from .env instead of --dest
python download_drive_folder.py 1AbCdEfGhIjKlMnOpQrStUvWxYz
```

The script prints progress per file (`[n] path` followed by a `xx%` progress line) and logs `[skip, exists]` for files already fully downloaded, so it's safe to interrupt with `Ctrl+C` and rerun — completed files aren't re-downloaded, and the run picks up where it left off.

## How It Works

- Uses the official [Google Drive API v3](https://developers.google.com/drive/api/guides/about-sdk) via `google-api-python-client`, authenticating with OAuth 2.0 (Installed App flow) under your own account — no service account or admin permissions needed.
- `extract_folder_id()` accepts either a bare folder ID or a full Drive URL (`/folders/<id>` or `?id=<id>` forms) and normalizes it to just the ID.
- `get_service()` handles the OAuth dance: loads `token.json` if present, refreshes it if expired, or runs the full `credentials.json`-based browser login flow if no valid token exists yet, then returns an authenticated Drive API client.
- `list_children()` queries `files.list` with `'<folder_id>' in parents and trashed = false`, paginating through results (`pageSize=1000`) until exhausted. `supportsAllDrives` / `includeItemsFromAllDrives` are set so folders inside Shared Drives (Team Drives) work too.
- `walk_and_download()` recurses depth-first: for each child, a subfolder triggers `os.makedirs` + recursion into it; a file is downloaded directly into the current local directory. This rebuilds the exact remote folder tree locally.
- `sanitize()` replaces characters illegal in filenames on common filesystems (`< > : " / \ | ? *`) with `_`, so unusual Drive item names never break local path creation.
- `download_file()` does the actual transfer:
  - Skips the file entirely if it already exists locally with a byte size matching Drive's reported `size` (resumability).
  - Google-native files (Docs/Sheets/Slides/Drawings) are exported via `export_media` into a standard format, per this mapping:

    | Google MIME type | Exported as | Extension |
    |---|---|---|
    | `application/vnd.google-apps.document` | Word | `.docx` |
    | `application/vnd.google-apps.spreadsheet` | Excel | `.xlsx` |
    | `application/vnd.google-apps.presentation` | PowerPoint | `.pptx` |
    | `application/vnd.google-apps.drawing` | PDF | `.pdf` |

  - All other files are streamed with `get_media` via `MediaIoBaseDownload` in 50MB chunks, printing a live `xx%` progress indicator.
  - On `HttpError` or any other exception mid-download, retries up to 5 times with exponential backoff (`2^attempt` seconds, capped at 60s) before giving up and logging `[FAILED after N attempts]`.
- `run()` ties it together: resolves the folder ID, authenticates, fetches the target folder's own name (used to name the local root directory when `--dest` is left at its default), then calls `walk_and_download()` and prints a final summary (file count, elapsed time).

## Output Structure

Given a Drive folder named `raw_footage` shaped like:

```
raw_footage/  (Drive)
├── clip1.mp4
├── site_a/
│   └── clip2.mov
└── notes.gdoc   (Google Doc)
```

Running:

```bash
python download_drive_folder.py <raw_footage_folder_id> --dest ./downloaded_data
```

produces:

```
downloaded_data/
├── clip1.mp4
├── site_a/
│   └── clip2.mov
└── notes.docx
```

Google Docs/Sheets/Slides/Drawings are converted to their standard-format equivalents; every other file type is downloaded byte-for-byte unchanged.

## Troubleshooting

**`Error 403: access_denied` — "has not completed the Google verification process"**
→ Your account hasn't been added as a **Test user** on the OAuth consent screen. See [step 5](#google-cloud-oauth-setup).

**`Missing credentials.json`**
→ You haven't downloaded and placed your OAuth client JSON in the project root. See [step 6](#google-cloud-oauth-setup).

**`HttpError 404` when accessing the folder**
→ The folder ID is wrong, or the account you authenticated with doesn't actually have access to it. Confirm the folder is shared with that exact email.

**Downloads stall or fail partway through a huge file**
→ Just rerun the same command. Completed files are skipped automatically; the whole folder isn't restarted from scratch.

**It's slow**
→ Speed is limited by your internet connection and Drive's per-user API throughput, not the script. Large binary files (video, etc.) will always take a while.

**"This app isn't verified" warning won't go away**
→ Expected and harmless for personal-use OAuth apps in Testing mode. Click **Advanced → Go to [app name] (unsafe)** — "unsafe" just means Google hasn't manually reviewed it.

**`ModuleNotFoundError: No module named 'dotenv'` (or `googleapiclient`, `google.oauth2`, etc.)**
→ Dependencies aren't installed in the active environment. Re-run the `pip install` command from [Installation](#installation) inside your activated virtual environment.

## Security Notes

- **Never commit `credentials.json` or `token.json`.** `credentials.json` identifies your OAuth client; `token.json` contains an active access/refresh token tied to your Google account. If either is accidentally published, revoke access immediately from your [Google Account permissions page](https://myaccount.google.com/permissions) and delete/regenerate the OAuth client in Google Cloud Console.
- **Access is read-only.** The script requests only the `drive.readonly` scope — it cannot modify, delete, or upload anything to your Drive.
- **Runs with your user's filesystem permissions.** The script doesn't escalate privileges, but it also won't stop itself from overwriting files in a destination path that already contains unrelated data with matching names.
- **Disk exhaustion.** There's no built-in disk-space check or quota — monitor available space when downloading large folders.
