#!/usr/bin/env python3
"""
Run the full pipeline: download raw footage from a Google Drive folder, then
extract frames from every video found in it.

Local paths (download destination, frame output folder) are read from .env
(DATA_DIR, FRAMES_OUTPUT_DIR) — only the Drive folder link/ID is passed as an
argument, since that's the one thing that changes on every run.

Usage:
    python main.py <FOLDER_ID_OR_URL>

Each stage can still be run on its own — see download_drive_folder.py and
extract_frames.py.
"""

import argparse
import os

from dotenv import load_dotenv

import download_drive_folder
import extract_frames

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Run the full download -> extract-frames pipeline."
    )
    parser.add_argument("drive_folder", help="Google Drive folder ID or full URL")
    args = parser.parse_args()

    data_dir = os.getenv("DATA_DIR", "./data")
    frames_output = os.getenv("FRAMES_OUTPUT_DIR")

    print("=== Stage 1/2: downloading from Google Drive ===")
    download_drive_folder.run(args.drive_folder, data_dir)

    print("\n=== Stage 2/2: extracting frames ===")
    extract_frames.run(data_dir, frames_output)


if __name__ == "__main__":
    main()
