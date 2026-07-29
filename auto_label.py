#!/usr/bin/env python3
"""
Auto-label video frames into a YOLO-format dataset using a YOLO model.

Four entry points:

- auto_label_video(...): labels only the manually-labeled regions of a single video,
  driven by a label_with_mouse.py CSV (frame -> primary + extra object boxes). Each
  region is cropped (with a margin) before running detection, and results are mapped
  back to full-frame-normalized YOLO coordinates. Meant to be imported and called by
  label_with_mouse.py after a manual session, via --auto-label.

- `python auto_label.py --model-path ... --csv output/manual_labels/video.csv`: the
  same ROI-restricted labeling as above, but as a CLI - the video itself is located by
  recursively searching --data-root for a file whose name matches the CSV's, so you
  don't need to know/type its path or subfolder depth.

- `python auto_label.py --model-path ... --video /path/to/video.mp4`: full-frame
  detection (no ROI restriction) on exactly one named video file.

- `python auto_label.py --model-path ...` (no --csv, no --video): batch-labels every
  frame of every video under --data-root using full-frame detection, with no manual
  ROI restriction - for the future no-manual-intervention pipeline.

All four write into the same output/labeled_data/ dataset (plus a resumable
master_pipeline_log.json), so results accumulate across videos and across modes.

Every frame this script processes lands in labeled_data/review/ unconditionally -
confidence plays no role in where a frame goes, and this script never auto-promotes
a frame into train/val/test itself. Promotion out of review/ is a human-in-the-loop
step done separately with reviewer.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

import cv2

OUTPUT_ROOT = Path(__file__).resolve().parent / "output"
LABELED_DATA_DIR = OUTPUT_ROOT / "labeled_data"

DEFAULT_DATA_ROOT = "./data"
DEFAULT_TARGET_WIDTH = 1280   # downscale saved images to this width, keeping aspect ratio
DEFAULT_CROP_MARGIN = 4     # fraction of each ROI's own width/height added as padding per side
JPEG_QUALITY = 85
PROGRESS_INTERVAL = 100       # print a progress heartbeat every this many processed frames

SPLITS = ("train", "val", "test", "review")
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mkv")


def _ensure_split_dirs(output_dir: Path) -> None:
    for s in SPLITS:
        (output_dir / s / "images").mkdir(parents=True, exist_ok=True)
        (output_dir / s / "labels").mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict:
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _log_progress(processed: int, total: int, split_counts: dict[str, int], start_time: float) -> None:
    """Print a periodic progress heartbeat - called every PROGRESS_INTERVAL processed
    frames, not per frame, so it doesn't drown out everything else in noise."""
    elapsed = time.monotonic() - start_time
    rate = processed / elapsed if elapsed > 0 else 0.0
    remaining = (total - processed) / rate if rate > 0 else 0.0
    counts_str = " ".join(f"{k}={v}" for k, v in split_counts.items())
    pct = (processed / total * 100) if total else 100.0
    print(
        f"  [progress] {processed}/{total} ({pct:.1f}%) | {counts_str} | "
        f"{rate:.1f} fps | elapsed {_format_duration(elapsed)} | ETA {_format_duration(remaining)}"
    )


def _read_rois_by_frame(csv_path: Path) -> dict[int, list[tuple[int, int, int, int]]]:
    """Parse a label_with_mouse.py CSV into frame_idx -> [(x1,y1,x2,y2), ...] - the
    primary row (unless it's an explicit "no object" / NaN row) plus any extra-object
    rows sharing that frame index, since both are written as separate rows per frame."""
    rois: dict[int, list[tuple[int, int, int, int]]] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["source"] == "none" or row["x1"] == "NaN":
                continue
            frame_idx = int(row["frame"])
            box = (int(float(row["x1"])), int(float(row["y1"])), int(float(row["x2"])), int(float(row["y2"])))
            rois.setdefault(frame_idx, []).append(box)
    return rois


def _expand_roi(
    box: tuple[int, int, int, int], margin: float, width: int, height: int
) -> tuple[int, int, int, int]:
    """Pad box by `margin` (fraction of its own width/height) per side, clamped to the frame."""
    x1, y1, x2, y2 = box
    pad_x, pad_y = (x2 - x1) * margin / 2.0, (y2 - y1) * margin / 2.0
    ex1 = max(0, int(round(x1 - pad_x)))
    ey1 = max(0, int(round(y1 - pad_y)))
    ex2 = min(width, int(round(x2 + pad_x)))
    ey2 = min(height, int(round(y2 + pad_y)))
    return ex1, ey1, ex2, ey2


def _route_and_save(
    frame,
    frame_id: str,
    video_source: str,
    frame_number: int,
    yolo_lines: list[str],
    confidences_in_frame: list[float],
    output_dir: Path,
    target_width: int,
    execution_log: dict,
    session_log: dict,
) -> str:
    """Write the resized image + YOLO label file into review/, and update
    execution_log/session_log in place. Returns the chosen split (always "review")
    so callers can tally progress without hardcoding the string themselves. Every
    frame lands here unconditionally, regardless of detection confidence (including
    frames with no detections at all, which get an empty label file) - this script
    never decides a frame is good enough for train/val/test itself; only reviewer.py
    promotes frames out of review/, as a deliberate human-in-the-loop step."""
    h_img, w_img = frame.shape[:2]
    chosen_split = "review"

    target_img_path = output_dir / chosen_split / "images" / f"{frame_id}.jpg"
    target_lbl_path = output_dir / chosen_split / "labels" / f"{frame_id}.txt"

    scale_factor = target_width / w_img
    resized_frame = cv2.resize(
        frame, (target_width, max(1, int(h_img * scale_factor))), interpolation=cv2.INTER_AREA
    )
    cv2.imwrite(str(target_img_path), resized_frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    with target_lbl_path.open("w") as f:
        f.writelines(yolo_lines)

    execution_log[frame_id] = {
        "video_source": video_source,
        "frame_number": frame_number,
        "dataset_split": chosen_split,
        "max_confidence": max(confidences_in_frame) if confidences_in_frame else 0.0,
        "all_confidences": confidences_in_frame,
        "image_path": str(target_img_path),
        "label_path": str(target_lbl_path),
    }
    session_log[frame_id] = chosen_split
    return chosen_split


def auto_label_video(
    video_path: str | Path,
    csv_path: str | Path,
    model_path: str | None = None,
    output_dir: str | Path = LABELED_DATA_DIR,
    crop_margin: float = DEFAULT_CROP_MARGIN,
    target_width: int = DEFAULT_TARGET_WIDTH,
    model=None,
) -> dict[str, str]:
    """Run YOLO detection restricted to the manually-labeled ROIs of each frame in
    csv_path (a label_with_mouse.py output CSV). Every processed frame lands in
    output_dir/review/ unconditionally, regardless of detection confidence - see
    _route_and_save. It's never train/val/test; nothing is auto-promoted there,
    that's reviewer.py's job. Frames with no ROI in the CSV (no primary label and no
    extra objects) are skipped entirely. Label coordinates are normalized to the full
    original frame, not the cropped region used for inference. Returns the session
    log (frame_id -> split) for frames processed in this call. Either model_path or a
    pre-loaded model must be given.
    """
    if model is None:
        if not model_path:
            raise ValueError("auto_label_video requires either model_path or a pre-loaded model")
        from ultralytics import YOLO
        print(f"Loading model weights from {model_path}...")
        model = YOLO(model_path)

    video_path = Path(video_path)
    csv_path = Path(csv_path)
    output_dir = Path(output_dir)
    video_name = video_path.stem

    _ensure_split_dirs(output_dir)

    rois_by_frame = _read_rois_by_frame(csv_path)
    if not rois_by_frame:
        print(f"No manually-labeled frames found in {csv_path}; nothing to auto-label.")
        return {}
    print(f"Found {len(rois_by_frame)} labeled frame(s) in {csv_path}.")

    log_path = output_dir / "master_pipeline_log.json"
    log_existed = log_path.exists()
    execution_log = _load_json(log_path)
    if log_existed:
        print(f"Resuming - {len(execution_log)} frame(s) already in master log, will skip finalized ones.")
    else:
        print("No existing master log found. Starting fresh.")

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_log: dict[str, str] = {}
    session_log_path = output_dir / f"session_{session_id}.json"

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS)
    video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    video_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"Opened video: {video_w}x{video_h} @ {video_fps:.1f}fps, {video_frame_count} frame(s) total.")

    already_finalized = sum(
        1 for idx in rois_by_frame
        if execution_log.get(f"{video_name}_frame_{idx:06d}", {}).get("dataset_split") in ("train", "val", "test")
    )
    to_process = len(rois_by_frame) - already_finalized
    print(f"Starting: {to_process} frame(s) queued for detection ({already_finalized} already finalized, will skip).")

    frame_idx = -1
    processed = 0
    skipped_finalized = 0
    skipped_no_roi = 0
    has_label_count = 0
    no_label_count = 0
    split_counts: dict[str, int] = {"review": 0}
    start_time = time.monotonic()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1

        # frame_idx is 0-indexed to match label_with_mouse.py's CSV "frame" column.
        rois = rois_by_frame.get(frame_idx)
        if not rois:
            skipped_no_roi += 1
            continue

        frame_id = f"{video_name}_frame_{frame_idx:06d}"
        if frame_id in execution_log and execution_log[frame_id]["dataset_split"] in ("train", "val", "test"):
            skipped_finalized += 1
            continue

        h_img, w_img = frame.shape[:2]
        yolo_lines: list[str] = []
        confidences_in_frame: list[float] = []

        for roi in rois:
            ex1, ey1, ex2, ey2 = _expand_roi(roi, crop_margin, w_img, h_img)
            if ex2 <= ex1 or ey2 <= ey1:
                continue
            crop = frame[ey1:ey2, ex1:ex2]

            results = model(crop, verbose=False)[0]
            for box in results.boxes:
                conf = box.conf[0].item()
                cls = int(box.cls[0].item())
                confidences_in_frame.append(conf)

                # Detection coords are crop-local - shift back into full-frame pixel space.
                cx1, cy1, cx2, cy2 = box.xyxy[0].tolist()
                fx1, fy1, fx2, fy2 = cx1 + ex1, cy1 + ey1, cx2 + ex1, cy2 + ey1

                box_w = (fx2 - fx1) / w_img
                box_h = (fy2 - fy1) / h_img
                x_center = (fx1 + (fx2 - fx1) / 2) / w_img
                y_center = (fy1 + (fy2 - fy1) / 2) / h_img
                yolo_lines.append(f"{cls} {x_center:.6f} {y_center:.6f} {box_w:.6f} {box_h:.6f}\n")

        if yolo_lines:
            has_label_count += 1
        else:
            no_label_count += 1

        chosen_split = _route_and_save(
            frame=frame,
            frame_id=frame_id,
            video_source=str(video_path),
            frame_number=frame_idx,
            yolo_lines=yolo_lines,
            confidences_in_frame=confidences_in_frame,
            output_dir=output_dir,
            target_width=target_width,
            execution_log=execution_log,
            session_log=session_log,
        )
        split_counts[chosen_split] = split_counts.get(chosen_split, 0) + 1
        processed += 1
        if processed % PROGRESS_INTERVAL == 0:
            _log_progress(processed, to_process, split_counts, start_time)

    cap.release()

    _save_json(log_path, execution_log)
    if session_log:
        _save_json(session_log_path, session_log)

    elapsed_total = time.monotonic() - start_time
    print(
        f"Auto-labeled {processed} frame(s) from {video_name} in {_format_duration(elapsed_total)} "
        f"(skipped_finalized={skipped_finalized}, skipped_no_roi={skipped_no_roi})."
    )
    print(f"Labels: {has_label_count} frame(s) with at least one detection, {no_label_count} frame(s) with none.")
    print(f"Master log : {log_path} ({len(execution_log)} total frames)")
    if session_log:
        print(f"Session log: {session_log_path} ({len(session_log)} frames)")
        print(f"To review: python3 reviewer.py")

    return session_log


def find_all_videos(data_root: Path) -> list[Path]:
    """Recursively find every video file under data_root, any depth."""
    return sorted(p for p in data_root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)


def _find_video_by_stem(data_root: Path, stem: str) -> Path:
    """Recursively search data_root for a video file whose name (minus extension)
    exactly matches stem. Errors clearly if zero or more than one match is found,
    rather than silently guessing."""
    matches = [p for p in find_all_videos(data_root) if p.stem == stem]
    if not matches:
        raise FileNotFoundError(f"No video named '{stem}.*' found under {data_root}")
    if len(matches) > 1:
        raise FileNotFoundError(
            f"Multiple videos named '{stem}.*' found under {data_root} - pass the video "
            f"path directly via auto_label_video() instead: " + ", ".join(str(m) for m in matches)
        )
    return matches[0]


def _run_from_csv(csv_path: Path, args) -> None:
    """Locate the video matching csv_path's name under --data-root, then run
    ROI-restricted auto-labeling (auto_label_video) on that pair."""
    video_path = _find_video_by_stem(Path(args.data_root), csv_path.stem)
    print(f"Found video: {video_path}")

    auto_label_video(
        video_path=video_path,
        csv_path=csv_path,
        model_path=args.model_path,
        output_dir=args.output_dir,
        crop_margin=args.crop_margin,
        target_width=args.target_width,
    )


def _label_video_full_frame(
    video_path: Path,
    model,
    output_dir: Path,
    target_width: int,
    execution_log: dict,
    session_log: dict,
) -> dict:
    """Run full-frame YOLO detection on every frame of video_path (no ROI restriction),
    routing each through _route_and_save. Shared by both --data-root batch mode and
    the single-video --video mode, so the loop only lives in one place. Returns a
    stats dict for the caller to fold into its own summary."""
    video_name = video_path.stem
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS)
    video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    video_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"  Opened: {video_w}x{video_h} @ {video_fps:.1f}fps, {video_frame_count} frame(s) total.")

    frame_idx = -1
    processed = 0
    skipped_finalized = 0
    has_label_count = 0
    no_label_count = 0
    split_counts: dict[str, int] = {"review": 0}
    start_time = time.monotonic()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1

        frame_id = f"{video_name}_frame_{frame_idx:06d}"
        if frame_id in execution_log and execution_log[frame_id]["dataset_split"] in ("train", "val", "test"):
            skipped_finalized += 1
            continue

        h_img, w_img = frame.shape[:2]
        results = model(frame, verbose=False)[0]

        yolo_lines = []
        confidences_in_frame = []
        for box in results.boxes:
            conf = box.conf[0].item()
            cls = int(box.cls[0].item())
            confidences_in_frame.append(conf)
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            box_w = (x2 - x1) / w_img
            box_h = (y2 - y1) / h_img
            x_center = (x1 + (x2 - x1) / 2) / w_img
            y_center = (y1 + (y2 - y1) / 2) / h_img
            yolo_lines.append(f"{cls} {x_center:.6f} {y_center:.6f} {box_w:.6f} {box_h:.6f}\n")

        if yolo_lines:
            has_label_count += 1
        else:
            no_label_count += 1

        chosen_split = _route_and_save(
            frame=frame,
            frame_id=frame_id,
            video_source=str(video_path),
            frame_number=frame_idx,
            yolo_lines=yolo_lines,
            confidences_in_frame=confidences_in_frame,
            output_dir=output_dir,
            target_width=target_width,
            execution_log=execution_log,
            session_log=session_log,
        )
        split_counts[chosen_split] = split_counts.get(chosen_split, 0) + 1
        processed += 1
        if processed % PROGRESS_INTERVAL == 0:
            # total is an estimate (video_frame_count minus finalized-skips so far,
            # which only grows) - good enough for a progress percentage/ETA, not exact.
            _log_progress(processed, max(processed, video_frame_count - skipped_finalized), split_counts, start_time)

    cap.release()
    elapsed_total = time.monotonic() - start_time
    print(
        f"  Finished {video_name} in {_format_duration(elapsed_total)}: {processed} processed, "
        f"{skipped_finalized} already finalized skipped. Log now has {len(execution_log)} total frame(s)."
    )
    print(f"  Labels: {has_label_count} frame(s) with at least one detection, {no_label_count} frame(s) with none.")

    return {
        "processed": processed,
        "skipped_finalized": skipped_finalized,
        "has_label_count": has_label_count,
        "no_label_count": no_label_count,
        "split_counts": split_counts,
    }


def auto_label_full_frame(
    video_path: str | Path | None = None,
    data_root: str | Path = DEFAULT_DATA_ROOT,
    model_path: str | None = None,
    output_dir: str | Path = LABELED_DATA_DIR,
    target_width: int = DEFAULT_TARGET_WIDTH,
    model=None,
) -> dict[str, str]:
    """Full-frame YOLO detection, no ROI/CSV restriction - the no-manual-intervention
    path. Labels exactly one video if video_path is given, otherwise every video found
    recursively under data_root (see find_all_videos). Every processed frame lands in
    output_dir/review/ unconditionally (see _route_and_save); nothing is auto-promoted
    to train/val/test here, that's reviewer.py's job. Returns the session log (frame_id
    -> split) for frames processed in this call. Either model_path or a pre-loaded
    model must be given."""
    if model is None:
        if not model_path:
            raise ValueError("auto_label_full_frame requires either model_path or a pre-loaded model")
        from ultralytics import YOLO
        print(f"Loading model weights from {model_path}...")
        model = YOLO(model_path)

    output_dir = Path(output_dir)
    _ensure_split_dirs(output_dir)

    if video_path is not None:
        video_paths = [Path(video_path)]
    else:
        video_paths = find_all_videos(Path(data_root))
        print(f"Found {len(video_paths)} video(s) to process under {data_root}.")

    log_path = output_dir / "master_pipeline_log.json"
    log_existed = log_path.exists()
    execution_log = _load_json(log_path)
    if log_existed:
        print(f"Resuming - {len(execution_log)} frame(s) already in master log, will skip finalized ones.")
    else:
        print("No existing master log found. Starting fresh.")

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_log: dict[str, str] = {}
    session_log_path = output_dir / f"session_{session_id}.json"

    total_has_label = 0
    total_no_label = 0

    for v_idx, vp in enumerate(video_paths):
        print(f"\n[{v_idx + 1}/{len(video_paths)}] Processing: {vp.stem}")
        stats = _label_video_full_frame(
            video_path=vp,
            model=model,
            output_dir=output_dir,
            target_width=target_width,
            execution_log=execution_log,
            session_log=session_log,
        )
        total_has_label += stats["has_label_count"]
        total_no_label += stats["no_label_count"]

    _save_json(log_path, execution_log)
    if session_log:
        _save_json(session_log_path, session_log)

    print("\nProcess complete.")
    print(f"  Master log : {log_path}  ({len(execution_log)} total frames)")
    if session_log:
        print(f"  Session log: {session_log_path}  ({len(session_log)} frames added this run)")
    print(f"  Labels: {total_has_label} frame(s) with at least one detection, {total_no_label} frame(s) with none.")
    if session_log:
        print(f"  To review: python3 reviewer.py")

    return session_log


def _cli_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Auto-label videos into a YOLO-format dataset - full-frame batch mode by "
                     "default (--data-root) or on one video (--video), or ROI-restricted for "
                     "a single video via --csv."
    )
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="Folder to recursively search for videos.")
    parser.add_argument("--output-dir", default=str(LABELED_DATA_DIR), help="Output dataset root.")
    parser.add_argument("--model-path", required=True, help="Path to YOLO model weights.")
    parser.add_argument("--target-width", type=int, default=DEFAULT_TARGET_WIDTH)
    parser.add_argument(
        "--csv", type=str, default=None,
        help="Run ROI-restricted auto-labeling on a single video, driven by this "
             "label_with_mouse.py CSV (e.g. output/manual_labels/video.csv). The video is "
             "located by recursively searching --data-root for a file whose name (minus "
             "extension) matches the CSV's. Mutually exclusive with --video. If neither is "
             "given, falls back to full-frame batch mode over every video under --data-root.",
    )
    parser.add_argument(
        "--video", type=str, default=None,
        help="Run full-frame detection (no ROI/CSV restriction) on exactly this one video "
             "file, instead of scanning --data-root or using a CSV. Mutually exclusive with --csv.",
    )
    parser.add_argument(
        "--crop-margin", type=float, default=DEFAULT_CROP_MARGIN,
        help=f"Only used with --csv: padding added around each manually-labeled box before "
             f"detection, as a fraction of the box's own width/height (default: {DEFAULT_CROP_MARGIN}).",
    )
    args = parser.parse_args(argv)

    if args.csv and args.video:
        parser.error("--csv and --video are mutually exclusive")

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.is_file():
            parser.error(f"CSV not found: {csv_path}")
        _run_from_csv(csv_path, args)
    elif args.video:
        video_path = Path(args.video)
        if not video_path.is_file():
            parser.error(f"Video not found: {video_path}")
        auto_label_full_frame(
            video_path=video_path,
            model_path=args.model_path,
            output_dir=args.output_dir,
            target_width=args.target_width,
        )
    else:
        auto_label_full_frame(
            data_root=args.data_root,
            model_path=args.model_path,
            output_dir=args.output_dir,
            target_width=args.target_width,
        )


if __name__ == "__main__":
    _cli_main()
