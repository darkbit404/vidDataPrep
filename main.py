#!/usr/bin/env python3
"""
Entry point for vidDataPrep.

Plain usage just downloads a Google Drive folder to local disk (same as
running download_drive_folder.py directly):

    python main.py <FOLDER_ID_OR_URL>

--manual and --auto each run one of the two complete labeling pipelines,
one stage triggering the next as soon as it finishes:

    python main.py [FOLDER_ID_OR_URL] --manual --model-path best.pt
        download (optional) -> for every video found under DATA_DIR (any
        depth, skipping ones already manually labeled): manual labeling,
        then auto-labeling on the manually-labeled regions -> once every
        video is done, review

    python main.py [FOLDER_ID_OR_URL] --auto --model-path best.pt
        download (optional) -> full-frame auto-labeling (no manual step)
        over every video found under DATA_DIR -> review

FOLDER_ID_OR_URL is optional with --manual/--auto: omit it to skip the
download and run the pipeline against whatever's already under DATA_DIR.
Every stage also still runs standalone (download_drive_folder.py,
label_with_mouse.py, auto_label.py, reviewer.py) if you only want one part.

Local path (download destination / pipeline data root) is read from .env
(DATA_DIR).
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

import download_drive_folder

load_dotenv()


def _run_manual_pipeline(data_dir: str, model_path: str) -> None:
    """Data acquisition already happened (or was skipped) by the time this runs.
    Manual labeling is inherently one-video-at-a-time and interactive, so this
    finds every video under data_dir (any depth) and loops: manual label ->
    auto-label chained in via label_with_mouse.run(auto_label=True) -> repeat
    for the next video. Videos that already have a manual CSV are skipped, so
    re-running --manual after adding new footage doesn't force relabeling
    everything from scratch. Review runs once at the end, over the whole
    shared review/ pool."""
    from auto_label import find_all_videos
    import label_with_mouse
    import reviewer

    videos = find_all_videos(Path(data_dir))
    if not videos:
        print(f"No videos found under {data_dir}.")
        return

    manual_labels_dir = Path(__file__).resolve().parent / "output" / "manual_labels"
    pending = [v for v in videos if not (manual_labels_dir / f"{v.stem}.csv").exists()]
    already_done = len(videos) - len(pending)
    if already_done:
        print(f"Skipping {already_done} video(s) already manually labeled.")
    if not pending:
        print("Nothing to manually label - every video already has a manual CSV.")

    for i, video_path in enumerate(pending):
        print(f"\n=== Manual labeling [{i + 1}/{len(pending)}]: {video_path} ===")
        label_with_mouse.run(str(video_path), auto_label=True, model_path=model_path)

    print("\n=== Review ===")
    reviewer.review()


def _run_auto_pipeline(data_dir: str, model_path: str) -> None:
    """Full-frame auto-labeling (no manual step) over every video under data_dir,
    then a single review pass over the shared review/ pool."""
    from auto_label import auto_label_full_frame
    import reviewer

    auto_label_full_frame(data_root=data_dir, model_path=model_path)

    print("\n=== Review ===")
    reviewer.review()


def main():
    parser = argparse.ArgumentParser(
        description="Download a Google Drive folder, and optionally run a complete labeling pipeline."
    )
    parser.add_argument(
        "drive_folder", nargs="?", default=None,
        help="Google Drive folder ID or full URL. Optional with --manual/--auto: omit it to "
             "skip download and run the pipeline against what's already in DATA_DIR.",
    )
    parser.add_argument(
        "--manual", action="store_true",
        help="Run the full manual-labeling pipeline: manual label + auto-label (looped) for "
             "every video found under DATA_DIR, then review.",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Run the full auto-labeling pipeline: full-frame auto-label (no manual step) for "
             "every video found under DATA_DIR, then review.",
    )
    parser.add_argument(
        "--model-path", type=str, default=None,
        help="YOLO model weights, required with --manual/--auto.",
    )
    args = parser.parse_args()

    if args.manual and args.auto:
        parser.error("--manual and --auto are mutually exclusive")
    if (args.manual or args.auto) and not args.model_path:
        parser.error("--manual/--auto require --model-path")
    if not (args.manual or args.auto) and not args.drive_folder:
        parser.error("drive_folder is required unless --manual or --auto is given")

    data_dir = os.getenv("DATA_DIR", "./data")

    if args.drive_folder:
        print("=== Downloading from Google Drive ===")
        download_drive_folder.run(args.drive_folder, data_dir)

    if args.manual:
        _run_manual_pipeline(data_dir, args.model_path)
    elif args.auto:
        _run_auto_pipeline(data_dir, args.model_path)


if __name__ == "__main__":
    main()
