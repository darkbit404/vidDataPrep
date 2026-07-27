#!/usr/bin/env python3
"""
Download raw footage from a Google Drive folder.

Local path (download destination) is read from .env (DATA_DIR) — only the
Drive folder link/ID is passed as an argument, since that's the one thing
that changes on every run.

Usage:
    python main.py <FOLDER_ID_OR_URL>

download_drive_folder.py can still be run on its own with the same effect.
"""

import argparse
import os

from dotenv import load_dotenv

import download_drive_folder

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Download a Google Drive folder to local disk."
    )
    parser.add_argument("drive_folder", help="Google Drive folder ID or full URL")
    args = parser.parse_args()

    data_dir = os.getenv("DATA_DIR", "./data")

    print("=== Downloading from Google Drive ===")
    download_drive_folder.run(args.drive_folder, data_dir)


if __name__ == "__main__":
    main()
