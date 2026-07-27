#!/usr/bin/env python3
"""
Auto-label video frames into a YOLO-format dataset using a YOLO model.

Two entry points:

- auto_label_video(...): labels only the manually-labeled regions of a single video,
  driven by a label_with_mouse.py CSV (frame -> primary + extra object boxes). Each
  region is cropped (with a margin) before running detection, and results are mapped
  back to full-frame-normalized YOLO coordinates. Meant to be imported and called by
  label_with_mouse.py after a manual session, via --auto-label.

- Running this file directly (`python auto_label.py --model-path ...`) batch-labels
  every frame of every video under --data-root using full-frame detection, with no
  manual ROI restriction - for the future no-manual-intervention pipeline.

Both write into the same output/labeled_data/ dataset (train/val/test/review splits,
plus a resumable master_pipeline_log.json), so results accumulate across videos and
across the two modes.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import random
from datetime import datetime
from pathlib import Path

import cv2

OUTPUT_ROOT = Path(__file__).resolve().parent / "output"
LABELED_DATA_DIR = OUTPUT_ROOT / "labeled_data"

DEFAULT_DATA_ROOT = "./data"
DEFAULT_HIGH_CONF = 0.8       # all detections in a frame must meet this to go to train/val/test
DEFAULT_TARGET_WIDTH = 1280   # downscale saved images to this width, keeping aspect ratio
DEFAULT_CROP_MARGIN = 0.5     # fraction of each ROI's own width/height added as padding per side
JPEG_QUALITY = 85

SPLITS = ("train", "val", "test", "review")


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
    high_conf: float,
    target_width: int,
    execution_log: dict,
    session_log: dict,
) -> None:
    """Apply the confidence-based routing decision, write the resized image + YOLO
    label file, and update execution_log/session_log in place.
    - ALL detections >= high_conf  -> train/val/test (clean positive)
    - ANY detection  <  high_conf  -> review (ambiguous, needs manual check)
    - No detections at all         -> review (possible missed object)
    """
    h_img, w_img = frame.shape[:2]
    has_detections = len(confidences_in_frame) > 0
    all_above_threshold = has_detections and all(c >= high_conf for c in confidences_in_frame)
    any_below_threshold = has_detections and any(c < high_conf for c in confidences_in_frame)

    if all_above_threshold:
        chosen_split = random.choices(["train", "val", "test"], weights=[0.70, 0.20, 0.10])[0]
        lines_to_write = yolo_lines
    elif any_below_threshold:
        chosen_split = "review"
        lines_to_write = yolo_lines
    else:
        chosen_split = "review"
        lines_to_write = []

    target_img_path = output_dir / chosen_split / "images" / f"{frame_id}.jpg"
    target_lbl_path = output_dir / chosen_split / "labels" / f"{frame_id}.txt"

    scale_factor = target_width / w_img
    resized_frame = cv2.resize(
        frame, (target_width, max(1, int(h_img * scale_factor))), interpolation=cv2.INTER_AREA
    )
    cv2.imwrite(str(target_img_path), resized_frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    with target_lbl_path.open("w") as f:
        f.writelines(lines_to_write)

    # A frame promoted out of review/ leaves behind stale copies there - remove them.
    prev = execution_log.get(frame_id)
    if prev and prev["dataset_split"] == "review" and chosen_split in ("train", "val", "test"):
        for old_path in (prev.get("image_path"), prev.get("label_path")):
            if old_path and os.path.exists(old_path):
                os.remove(old_path)

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


def auto_label_video(
    video_path: str | Path,
    csv_path: str | Path,
    model_path: str | None = None,
    output_dir: str | Path = LABELED_DATA_DIR,
    crop_margin: float = DEFAULT_CROP_MARGIN,
    high_conf: float = DEFAULT_HIGH_CONF,
    target_width: int = DEFAULT_TARGET_WIDTH,
    model=None,
) -> dict[str, str]:
    """Run YOLO detection restricted to the manually-labeled ROIs of each frame in
    csv_path (a label_with_mouse.py output CSV), writing a YOLO-format dataset under
    output_dir. Frames with no ROI in the CSV (no primary label and no extra objects)
    are skipped entirely. Label coordinates are normalized to the full original frame,
    not the cropped region used for inference. Returns the session log (frame_id ->
    split) for frames processed in this call. Either model_path or a pre-loaded model
    must be given.
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

    log_path = output_dir / "master_pipeline_log.json"
    execution_log = _load_json(log_path)

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_log: dict[str, str] = {}
    session_log_path = output_dir / f"session_{session_id}.json"

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    frame_idx = -1
    processed = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1

        # frame_idx is 0-indexed to match label_with_mouse.py's CSV "frame" column.
        rois = rois_by_frame.get(frame_idx)
        if not rois:
            continue

        frame_id = f"{video_name}_frame_{frame_idx:06d}"
        if frame_id in execution_log and execution_log[frame_id]["dataset_split"] in ("train", "val", "test"):
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

        _route_and_save(
            frame=frame,
            frame_id=frame_id,
            video_source=str(video_path),
            frame_number=frame_idx,
            yolo_lines=yolo_lines,
            confidences_in_frame=confidences_in_frame,
            output_dir=output_dir,
            high_conf=high_conf,
            target_width=target_width,
            execution_log=execution_log,
            session_log=session_log,
        )
        processed += 1

    cap.release()

    _save_json(log_path, execution_log)
    if session_log:
        _save_json(session_log_path, session_log)

    print(f"Auto-labeled {processed} frame(s) from {video_name}.")
    print(f"Master log : {log_path} ({len(execution_log)} total frames)")
    if session_log:
        print(f"Session log: {session_log_path} ({len(session_log)} frames)")
        print(f"To verify only new frames: python3 verify.py --session {session_log_path}")

    return session_log


def _batch_main(argv: list[str] | None = None) -> None:
    """Standalone CLI: scan --data-root for videos and full-frame auto-label every
    frame of every video (no manual ROI restriction) - the no-manual-intervention path."""
    parser = argparse.ArgumentParser(
        description="Batch auto-label every frame of every video under a data root (full-frame detection)."
    )
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="Folder to recursively search for videos.")
    parser.add_argument("--output-dir", default=str(LABELED_DATA_DIR), help="Output dataset root.")
    parser.add_argument("--model-path", required=True, help="Path to YOLO model weights.")
    parser.add_argument("--high-conf", type=float, default=DEFAULT_HIGH_CONF)
    parser.add_argument("--target-width", type=int, default=DEFAULT_TARGET_WIDTH)
    args = parser.parse_args(argv)

    from ultralytics import YOLO

    output_dir = Path(args.output_dir)
    _ensure_split_dirs(output_dir)

    print(f"Loading model weights from {args.model_path}...")
    model = YOLO(args.model_path)

    video_paths = []
    for ext in ("*.MP4", "*.mp4", "*.avi", "*.mkv"):
        video_paths.extend(glob.glob(os.path.join(args.data_root, "**", ext), recursive=True))
    print(f"Found {len(video_paths)} videos to process.")

    log_path = output_dir / "master_pipeline_log.json"
    execution_log = _load_json(log_path)

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_log: dict[str, str] = {}
    session_log_path = output_dir / f"session_{session_id}.json"

    for v_idx, video_path in enumerate(video_paths):
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        print(f"\n[{v_idx + 1}/{len(video_paths)}] Processing Stream: {video_name}")

        cap = cv2.VideoCapture(video_path)
        frame_idx = -1

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1

            frame_id = f"{video_name}_frame_{frame_idx:06d}"
            if frame_id in execution_log and execution_log[frame_id]["dataset_split"] in ("train", "val", "test"):
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

            _route_and_save(
                frame=frame,
                frame_id=frame_id,
                video_source=video_path,
                frame_number=frame_idx,
                yolo_lines=yolo_lines,
                confidences_in_frame=confidences_in_frame,
                output_dir=output_dir,
                high_conf=args.high_conf,
                target_width=args.target_width,
                execution_log=execution_log,
                session_log=session_log,
            )

        cap.release()
        print(f"Finished parsing video. Current total logged records: {len(execution_log)}")

    _save_json(log_path, execution_log)
    _save_json(session_log_path, session_log)

    print("\nProcess complete.")
    print(f"  Master log : {log_path}  ({len(execution_log)} total frames)")
    print(f"  Session log: {session_log_path}  ({len(session_log)} frames added this run)")
    print(f"  To verify only new frames:  python3 verify.py --session {session_log_path}")


if __name__ == "__main__":
    _batch_main()
