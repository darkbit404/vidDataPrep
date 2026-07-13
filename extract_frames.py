#!/usr/bin/env python3
"""
Recursively walk an arbitrarily nested folder tree, find every video file
(regardless of depth or naming convention), and extract its frames into a
mirrored output directory tree.

For an input video found at:
    <root>/a/b/c/clip.mp4
frames are written to:
    <output_root>/a/b/c/clip/1.jpg, 2.jpg, 3.jpg, ...

Usage:
    python extract_frames.py /path/to/root_folder [-o /path/to/output_folder]
"""

import argparse
import os
import sys
from pathlib import Path

import cv2
from dotenv import load_dotenv

load_dotenv()

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".m4v"}


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def extract_frames(video_path: Path, frames_dir: Path) -> int:
    """Extract every frame of video_path as sequentially numbered jpgs into frames_dir."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"  [WARN] Could not open video: {video_path}", file=sys.stderr)
        return 0

    frames_dir.mkdir(parents=True, exist_ok=True)

    frame_count = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_count += 1
        out_path = frames_dir / f"{frame_count}.jpg"
        cv2.imwrite(str(out_path), frame)

    cap.release()
    return frame_count


def process_folder(input_dir: Path, output_dir: Path) -> None:
    """Recursively process input_dir, mirroring its structure under output_dir."""
    try:
        entries = sorted(input_dir.iterdir())
    except PermissionError as e:
        print(f"  [WARN] Skipping unreadable folder {input_dir}: {e}", file=sys.stderr)
        return

    for entry in entries:
        if entry.is_dir():
            process_folder(entry, output_dir / entry.name)
        elif is_video_file(entry):
            frames_dir = output_dir / entry.stem
            print(f"Processing video: {entry}")
            count = extract_frames(entry, frames_dir)
            print(f"  -> {count} frames written to {frames_dir}")


def run(root_folder: str, output: str | None = None) -> None:
    """Extract frames from every video under `root_folder` into a mirrored tree under `output`."""
    root_folder = Path(root_folder).resolve()
    if not root_folder.is_dir():
        print(f"Error: {root_folder} is not a valid directory.", file=sys.stderr)
        sys.exit(1)

    if output:
        output_root = Path(output).resolve()
    else:
        output_root = root_folder.parent / f"{root_folder.name}_frames"

    print(f"Input root:  {root_folder}")
    print(f"Output root: {output_root}")

    process_folder(root_folder, output_root)

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Extract frames from every video in a nested folder tree.")
    parser.add_argument(
        "root_folder",
        type=str,
        nargs="?",
        default=os.getenv("DATA_DIR"),
        help="Path to the root folder containing videos/subfolders (default: $DATA_DIR from .env).",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=os.getenv("FRAMES_OUTPUT_DIR"),
        help="Path to the output root folder (default: $FRAMES_OUTPUT_DIR from .env, "
             "or '<root_folder>_frames' next to the input).",
    )
    args = parser.parse_args()

    if not args.root_folder:
        parser.error("root_folder is required (pass it as an argument or set DATA_DIR in .env)")

    run(args.root_folder, args.output)


if __name__ == "__main__":
    main()
