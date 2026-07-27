#!/usr/bin/env python3
"""
Play a video and let a human label the object of interest by following it
with the mouse (or explicitly clicking), with pause/step controls to review
and correct individual frames, plus an explicit "no object" state for
stretches where the object isn't in frame.

For an input video named clip.mp4, this produces:
    output/manual_labels/clip.csv - frame, time_sec, cx, cy, x1, y1, x2, y2, source

output/ is created next to this script, regardless of where the input video
lives or which directory the script is run from.

With --auto-label, after the manual CSV is saved, auto_label.py is invoked to
run YOLO detection restricted to the manually-labeled regions of each frame,
producing a YOLO-format dataset under output/labeled_data/ (see auto_label.py).

This is a standalone labeling tool, independent of the download/extract
pipeline (main.py, download_drive_folder.py, extract_frames.py) - it is not
wired into that pipeline.

Usage:
    python label_with_mouse.py /path/to/video.mp4 [options]

Playback starts paused on the first frame; press Space to begin.

Controls:
    Space         pause/resume
    q or Esc      save and quit
    + / -         increase/decrease playback speed
    c             clear all recorded points (primary and extra objects)
    r             remove primary point/mark at current frame
    x             mark current frame "no object" and stop labeling (use when the object
                  leaves frame, then resume playback yourself to skip forward unlabeled)
    s             toggle stop/resume labeling without marking a frame (auto-marks frames
                  "no object" going forward while stopped; an explicit click always resumes too)
    n             next frame (while paused)
    p             previous frame (while paused)
    Mouse wheel   zoom in/out, centered on the cursor
    i / k         pan up / down while zoomed in
    j / l         pan left / right while zoomed in
    0             reset zoom/pan to fit-to-screen
    Left click    set primary point explicitly for current frame
    Right click   remove primary point/mark for current frame
    Ctrl+Left     add an extra object point for the current frame (independent per
                  frame - not tracked/interpolated across frames)
    Ctrl+Right    remove whichever point (primary or extra) is nearest the click

Zoom/pan only affect the live preview - the saved CSV always uses the video's original
resolution regardless of zoom level.
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import cv2
import numpy as np

LABEL_COLOR = (0, 255, 0)  # green, BGR - the recorded primary point for the current frame
NO_OBJECT_COLOR = (0, 0, 255)  # red, BGR - explicit "no object" marker on a frame
EXTRA_OBJECT_COLOR = (255, 200, 0)  # blue, BGR - extra (non-primary) object points
SCREEN_FIT_MARGIN = 0.9  # fit the preview within 90% of screen size, leaving room for window chrome/taskbar
DEFAULT_SCREEN_SIZE = (1280, 720)  # fallback if screen resolution can't be detected
MIN_ZOOM = 1.0
MAX_ZOOM = 8.0
ZOOM_STEP = 1.15  # per wheel notch, same growth rate as the existing +/- speed controls
PAN_FRACTION = 0.2  # arrow-key pan step, as a fraction of the current viewport size

OUTPUT_ROOT = Path(__file__).resolve().parent / "output"

Point = Tuple[int, int]


@dataclass
class FrameLabel:
    cx: int
    cy: int
    source: str


def get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the primary monitor, falling back to a default if undetectable."""
    try:
        from screeninfo import get_monitors
        monitor = get_monitors()[0]
        return monitor.width, monitor.height
    except Exception as e:
        print(f"[WARN] Could not detect screen resolution ({e}); "
              f"defaulting to {DEFAULT_SCREEN_SIZE[0]}x{DEFAULT_SCREEN_SIZE[1]}.", file=sys.stderr)
        return DEFAULT_SCREEN_SIZE


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _wheel_delta(flags: int) -> int:
    """Extract the signed wheel-notch delta from a mouse-callback flags value (OpenCV packs
    it into the upper 16 bits as a signed short); positive = scrolled up/away from the user."""
    raw = (flags >> 16) & 0xFFFF
    return raw - 0x10000 if raw >= 0x8000 else raw


def _compute_viewport(
    width: int, height: int, zoom: float, cx: float, cy: float
) -> tuple[float, float, float, float, float, float]:
    """Given a desired zoom level and pan center (in original-frame coordinates), return the
    (x0, y0, view_w, view_h) crop rectangle to read from the original frame, clamped so it never
    runs past the frame edges - along with the resulting (possibly re-clamped) center."""
    view_w = width / zoom
    view_h = height / zoom
    half_w, half_h = view_w / 2.0, view_h / 2.0
    cx = clamp(cx, half_w, width - half_w) if view_w < width else width / 2.0
    cy = clamp(cy, half_h, height - half_h) if view_h < height else height / 2.0
    return cx - half_w, cy - half_h, view_w, view_h, cx, cy


@contextmanager
def _uninterruptible_save():
    """Block Ctrl+C for the duration of the save phase, so a partial write (fewer
    saved frames than the CSV claims) can't happen from an accidental interrupt.
    SIGINT is caught and only printed as a warning; the save keeps running."""
    def _on_sigint(_signum, _frame):
        print("\n[WARN] Frames still saving; Ctrl+C is disabled until the save finishes.", file=sys.stderr)

    old_handler = signal.signal(signal.SIGINT, _on_sigint)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old_handler)


def open_video(path: Path) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print(f"Error: could not open video: {path}", file=sys.stderr)
        sys.exit(1)
    return cap


def draw_overlay(
    frame: np.ndarray,
    frame_idx: int,
    fps: float,
    paused: bool,
    speed: float,
    labeling_active: bool,
    zoom: float,
    chosen: FrameLabel | None,
    is_explicit_none: bool,
    bbox_w: int,
    bbox_h: int,
    extra_points: list[Point],
) -> np.ndarray:
    """Draw the HUD and the current frame's resolved box/point (or "no object" marker)
    onto a copy of frame. chosen/is_explicit_none reflect what finalize_labels would
    actually produce for this frame, not just whether it was explicitly clicked.
    extra_points are the frame's independent, non-interpolated extra objects, already
    in display-space pixel coordinates."""
    out = frame.copy()
    h, w = out.shape[:2]

    mode = "PAUSED" if paused else "PLAY"
    labeling = "ON" if labeling_active else "OFF (s to resume)"
    t_sec = frame_idx / fps if fps > 0 else 0.0
    info = f"{mode} frame={frame_idx} time={t_sec:.2f}s speed={speed:.2f}x labeling={labeling} zoom={zoom:.1f}x"
    cv2.putText(out, info, (18, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 230, 255), 2)

    hint = ("space pause/resume | q save+quit | +/- speed | c clear | r remove | x no-object | s stop/resume | "
            "n/p step | wheel zoom | i/j/k/l pan | 0 reset zoom | ctrl+click add object | ctrl+right remove nearest")
    cv2.putText(out, hint, (18, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220, 220, 220), 1)

    if is_explicit_none:
        cv2.putText(out, "NO OBJECT (marked)", (18, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.6, NO_OBJECT_COLOR, 2)

    if chosen is not None:
        cx, cy = chosen.cx, chosen.cy
        x1 = clamp(cx - bbox_w // 2, 0, w - 1)
        y1 = clamp(cy - bbox_h // 2, 0, h - 1)
        x2 = clamp(x1 + bbox_w, 0, w - 1)
        y2 = clamp(y1 + bbox_h, 0, h - 1)

        cv2.rectangle(out, (x1, y1), (x2, y2), LABEL_COLOR, 1)
        cv2.circle(out, (cx, cy), 3, LABEL_COLOR, -1)
        cv2.putText(out, f"label: {chosen.source}", (18, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.55, LABEL_COLOR, 1)

    for obj_id, (ex, ey) in enumerate(extra_points, start=1):
        x1 = clamp(ex - bbox_w // 2, 0, w - 1)
        y1 = clamp(ey - bbox_h // 2, 0, h - 1)
        x2 = clamp(x1 + bbox_w, 0, w - 1)
        y2 = clamp(y1 + bbox_h, 0, h - 1)

        cv2.rectangle(out, (x1, y1), (x2, y2), EXTRA_OBJECT_COLOR, 1)
        cv2.circle(out, (ex, ey), 3, EXTRA_OBJECT_COLOR, -1)
        cv2.putText(out, str(obj_id), (ex + 6, ey - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, EXTRA_OBJECT_COLOR, 2)

    return out


def _resolve_frame(
    raw_marks: dict[int, Point | None],
    keys: list[int],
    i: int,
    interpolate: bool,
    carry_forward: bool,
) -> FrameLabel | None:
    """Resolve what frame i's label should be, from the sparse raw_marks table (via
    interpolation/carry-forward between neighboring keys). A neighbor whose value is None
    (explicit "no object") is not a fillable point, so it blocks interpolation/carry-forward
    from crossing it - frames on the far side of it stay unresolved until the next real point."""
    if i in raw_marks:
        val = raw_marks[i]
        return None if val is None else FrameLabel(cx=val[0], cy=val[1], source="manual")

    pos = bisect_left(keys, i)
    prev_k = keys[pos - 1] if pos > 0 else None
    next_k = keys[pos] if pos < len(keys) else None
    prev_val = raw_marks.get(prev_k) if prev_k is not None else None
    next_val = raw_marks.get(next_k) if next_k is not None else None

    if interpolate and prev_val is not None and next_val is not None and prev_k != next_k:
        x0, y0 = prev_val
        x1, y1 = next_val
        alpha = (i - prev_k) / float(next_k - prev_k)
        xi = int(round((1.0 - alpha) * x0 + alpha * x1))
        yi = int(round((1.0 - alpha) * y0 + alpha * y1))
        return FrameLabel(cx=xi, cy=yi, source="interp")

    if carry_forward and prev_val is not None:
        xp, yp = prev_val
        return FrameLabel(cx=xp, cy=yp, source="carry")

    return None


def finalize_labels(
    raw_marks: dict[int, Point | None],
    frame_count: int,
    interpolate: bool,
    carry_forward: bool,
) -> dict[int, FrameLabel]:
    """Fill in every frame 0..frame_count-1 from the sparse raw_marks, via interpolation/carry-forward."""
    if not raw_marks:
        return {}

    keys = sorted(raw_marks.keys())
    final: dict[int, FrameLabel] = {}

    for i in range(frame_count):
        label = _resolve_frame(raw_marks, keys, i, interpolate, carry_forward)
        if label is not None:
            final[i] = label

    return final


def write_csv(
    csv_path: Path,
    labels: dict[int, FrameLabel],
    extra_marks: dict[int, list[Point]],
    frame_count: int,
    fps: float,
    width: int,
    height: int,
    bbox_w: int,
    bbox_h: int,
) -> None:
    """Write one row per frame for the primary object (densified via labels -
    interp/carry/manual/none), plus one row per extra object (in click order) for
    whichever frames had extra objects explicitly added - those are never interpolated,
    so frames without an extra click simply have no extra rows."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "time_sec", "cx", "cy", "x1", "y1", "x2", "y2", "source"])

        for i in range(frame_count):
            t_sec = i / fps if fps > 0 else 0.0
            label = labels.get(i)
            if label is None:
                writer.writerow([i, f"{t_sec:.6f}", "NaN", "NaN", "NaN", "NaN", "NaN", "NaN", "none"])
            else:
                x1 = clamp(label.cx - bbox_w // 2, 0, width - 1)
                y1 = clamp(label.cy - bbox_h // 2, 0, height - 1)
                x2 = clamp(x1 + bbox_w, 0, width - 1)
                y2 = clamp(y1 + bbox_h, 0, height - 1)
                writer.writerow([i, f"{t_sec:.6f}", label.cx, label.cy, x1, y1, x2, y2, label.source])

            for (ex, ey) in extra_marks.get(i, []):
                x1 = clamp(ex - bbox_w // 2, 0, width - 1)
                y1 = clamp(ey - bbox_h // 2, 0, height - 1)
                x2 = clamp(x1 + bbox_w, 0, width - 1)
                y2 = clamp(y1 + bbox_h, 0, height - 1)
                writer.writerow([i, f"{t_sec:.6f}", ex, ey, x1, y1, x2, y2, "manual"])


def run_annotation_ui(
    video_path: Path,
    start_frame: int,
    bbox_w: int,
    bbox_h: int,
    playback_speed: float,
    interpolate: bool,
    carry_forward: bool,
) -> tuple[dict[int, Point | None], dict[int, list[Point]], int, float, int, int]:
    """Interactively play/pause/step through video_path, tracking the mouse into an
    in-memory frame -> (x, y) | None mark table (None = explicit "no object") for the
    primary object, plus a frame -> [(x, y), ...] table for extra objects (added via
    Ctrl+Left click, independently per frame - no cross-frame identity or interpolation).
    Returns (raw_marks, extra_marks, frame_count, fps, width, height); nothing is written
    to disk here - see write_csv. interpolate/carry_forward only affect the primary
    object's live preview shown while reviewing - the same flags are re-applied when the
    CSV is written."""
    cap = open_video(video_path)

    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    start_frame = clamp(start_frame, 0, max(0, frame_count - 1))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    # Scale the on-screen preview to fit the display, so a high-res video doesn't
    # open a window bigger than the monitor. Saved outputs always use the source's
    # original resolution - mouse coordinates are converted from display space back
    # to original-frame space immediately in on_mouse, so everything downstream
    # (raw_marks, labels, CSV, annotated outputs) works in original-resolution units.
    screen_w, screen_h = get_screen_size()
    scale = min(1.0, (screen_w * SCREEN_FIT_MARGIN) / width, (screen_h * SCREEN_FIT_MARGIN) / height)
    disp_w, disp_h = round(width * scale), round(height * scale)
    if scale < 1.0:
        print(f"Source is {width}x{height}; previewing at {disp_w}x{disp_h} to fit your {screen_w}x{screen_h} screen.")

    window = f"Label with mouse - {video_path.name}"
    # WINDOW_GUI_NORMAL suppresses the Qt backend's default right-click context menu
    # (Pan/Zoom/Save/Properties) - without it, Ctrl+Right click can pop that menu instead
    # of reaching on_mouse below.
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE | cv2.WINDOW_GUI_NORMAL)

    mouse_pos: Point | None = None
    raw_marks: dict[int, Point | None] = {}
    extra_marks: dict[int, list[Point]] = {}
    keys: list[int] = []
    state = {"frame_idx": start_frame, "paused": True, "speed": max(0.05, playback_speed), "labeling_active": True}

    # Zoom/pan viewport, in original-frame coordinates. x0/y0/view_w/view_h/disp_scale are
    # recomputed once per render (main loop below) from zoom/view_cx/view_cy, and read here to
    # map display-space mouse coordinates back to original-frame space - so on_mouse always
    # maps through the same viewport that's currently on screen.
    zoom = MIN_ZOOM
    view_cx, view_cy = width / 2.0, height / 2.0
    x0, y0, view_w, view_h = 0.0, 0.0, float(width), float(height)
    disp_scale = scale

    def refresh_keys() -> None:
        keys[:] = sorted(raw_marks.keys())

    def _remove_nearest(idx: int, x: int, y: int) -> None:
        """Remove whichever point on frame idx - primary or extra - is closest to (x, y).
        No-op if the frame has no points at all."""
        primary = raw_marks.get(idx)
        extras = extra_marks.get(idx, [])
        candidates: list[tuple[float, str, int]] = []
        if primary is not None:
            candidates.append(((primary[0] - x) ** 2 + (primary[1] - y) ** 2, "primary", -1))
        for i, (ex, ey) in enumerate(extras):
            candidates.append(((ex - x) ** 2 + (ey - y) ** 2, "extra", i))
        if not candidates:
            return
        _, kind, i = min(candidates, key=lambda c: c[0])
        if kind == "primary":
            raw_marks.pop(idx, None)
            refresh_keys()
        else:
            extras.pop(i)

    def on_mouse(event: int, x: int, y: int, flags: int, param: object) -> None:
        nonlocal mouse_pos, zoom, view_cx, view_cy
        ox = clamp(round(x0 + x * (view_w / disp_w)), 0, width - 1)
        oy = clamp(round(y0 + y * (view_h / disp_h)), 0, height - 1)
        mouse_pos = (ox, oy)

        idx = int(state["frame_idx"])
        ctrl_held = bool(flags & cv2.EVENT_FLAG_CTRLKEY)
        if event == cv2.EVENT_LBUTTONDOWN:
            if ctrl_held:
                extra_marks.setdefault(idx, []).append((ox, oy))
            else:
                raw_marks[idx] = (ox, oy)
                state["labeling_active"] = True  # an explicit click always resumes labeling
                refresh_keys()
        elif event == cv2.EVENT_RBUTTONDOWN:
            if ctrl_held:
                _remove_nearest(idx, ox, oy)
            else:
                raw_marks.pop(idx, None)
                refresh_keys()
        elif event == cv2.EVENT_MOUSEWHEEL:
            step = ZOOM_STEP if _wheel_delta(flags) > 0 else 1.0 / ZOOM_STEP
            new_zoom = clamp(zoom * step, MIN_ZOOM, MAX_ZOOM)
            if new_zoom != zoom:
                # Zoom centered on the cursor: keep the original-frame point under the
                # cursor (ox, oy) at the same display position after the zoom change.
                new_view_w, new_view_h = width / new_zoom, height / new_zoom
                new_cx = (ox - x * (new_view_w / disp_w)) + new_view_w / 2.0
                new_cy = (oy - y * (new_view_h / disp_h)) + new_view_h / 2.0
                zoom, view_cx, view_cy = new_zoom, new_cx, new_cy

    cv2.setMouseCallback(window, on_mouse)

    current_frame: np.ndarray | None = None

    while True:
        idx = int(state["frame_idx"])

        if current_frame is None:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            current_frame = frame
            # While actively playing (not paused): auto-stamp from the live mouse position
            # if labeling is active, or explicitly mark "no object" if it's been toggled off
            # via 's'. Paused frames (including ones just stepped to via n/p) are left
            # untouched here - see the n/p handlers below for the stopped-mode auto-mark,
            # and x/left-click/right-click/r for explicit per-frame edits.
            if not state["paused"]:
                if state["labeling_active"]:
                    if mouse_pos is not None:
                        raw_marks[idx] = mouse_pos
                        refresh_keys()
                else:
                    raw_marks[idx] = None
                    refresh_keys()

        preview_label = _resolve_frame(raw_marks, keys, idx, interpolate, carry_forward)
        is_explicit_none = idx in raw_marks and raw_marks[idx] is None

        x0, y0, view_w, view_h, view_cx, view_cy = _compute_viewport(width, height, zoom, view_cx, view_cy)
        disp_scale = scale * zoom

        crop_x0 = clamp(round(x0), 0, width - 1)
        crop_y0 = clamp(round(y0), 0, height - 1)
        crop_x1 = clamp(round(x0 + view_w), crop_x0 + 1, width)
        crop_y1 = clamp(round(y0 + view_h), crop_y0 + 1, height)
        cropped = current_frame[crop_y0:crop_y1, crop_x0:crop_x1]
        if (crop_x1 - crop_x0, crop_y1 - crop_y0) == (disp_w, disp_h):
            disp_frame = cropped
        else:
            interp = cv2.INTER_AREA if (crop_x1 - crop_x0) >= disp_w else cv2.INTER_LINEAR
            disp_frame = cv2.resize(cropped, (disp_w, disp_h), interpolation=interp)

        disp_label = None
        if preview_label is not None:
            disp_label = FrameLabel(
                cx=round((preview_label.cx - x0) * disp_scale),
                cy=round((preview_label.cy - y0) * disp_scale),
                source=preview_label.source,
            )

        disp_extra_points = [
            (round((ex - x0) * disp_scale), round((ey - y0) * disp_scale))
            for (ex, ey) in extra_marks.get(idx, [])
        ]

        shown = draw_overlay(
            frame=disp_frame,
            frame_idx=idx,
            fps=fps,
            paused=bool(state["paused"]),
            speed=float(state["speed"]),
            labeling_active=bool(state["labeling_active"]),
            zoom=zoom,
            chosen=disp_label,
            is_explicit_none=is_explicit_none,
            bbox_w=max(1, round(bbox_w * disp_scale)),
            bbox_h=max(1, round(bbox_h * disp_scale)),
            extra_points=disp_extra_points,
        )
        cv2.imshow(window, shown)

        delay_ms = 30 if state["paused"] else max(1, round(1000.0 / (fps * float(state["speed"]))))
        key = cv2.waitKey(delay_ms) & 0xFF

        if key in (27, ord("q")):
            print("Stopped by user; saving...")
            break
        if key == ord(" "):
            state["paused"] = not state["paused"]
            continue
        if key in (ord("+"), ord("=")):
            state["speed"] = min(4.0, float(state["speed"]) * 1.15)
            continue
        if key == ord("-"):
            state["speed"] = max(0.05, float(state["speed"]) / 1.15)
            continue
        if key == ord("c"):
            raw_marks.clear()
            extra_marks.clear()
            refresh_keys()
            continue
        if key == ord("r"):
            raw_marks.pop(idx, None)
            refresh_keys()
            continue
        if key == ord("x"):
            raw_marks[idx] = None
            state["labeling_active"] = False
            refresh_keys()
            continue
        if key == ord("s"):
            state["labeling_active"] = not state["labeling_active"]
            continue
        if key == ord("i"):
            view_cy -= view_h * PAN_FRACTION
            continue
        if key == ord("k"):
            view_cy += view_h * PAN_FRACTION
            continue
        if key == ord("j"):
            view_cx -= view_w * PAN_FRACTION
            continue
        if key == ord("l"):
            view_cx += view_w * PAN_FRACTION
            continue
        if key == ord("0"):
            zoom, view_cx, view_cy = MIN_ZOOM, width / 2.0, height / 2.0
            continue

        if state["paused"] and key == ord("n"):
            if idx < frame_count - 1:
                new_idx = idx + 1
                state["frame_idx"] = new_idx
                cap.set(cv2.CAP_PROP_POS_FRAMES, new_idx)
                current_frame = None
                if not state["labeling_active"]:
                    raw_marks[new_idx] = None
                    refresh_keys()
            continue

        if state["paused"] and key == ord("p"):
            if idx > 0:
                state["frame_idx"] = idx - 1
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(state["frame_idx"]))
                current_frame = None
            continue

        if not state["paused"]:
            state["frame_idx"] = idx + 1
            current_frame = None
            if int(state["frame_idx"]) >= frame_count:
                break

    cv2.destroyAllWindows()
    cap.release()
    return raw_marks, extra_marks, frame_count, fps, width, height


def run(
    video_path: str,
    bbox_w: int = 640,
    bbox_h: int = 320,
    playback_speed: float = 0.4,
    start_frame: int = 0,
    interpolate: bool = True,
    carry_forward: bool = True,
    auto_label: bool = False,
    model_path: str | None = None,
    crop_margin: float = 0.5,
) -> Path:
    """Run the interactive labeling session for video_path, then save
    output/manual_labels/<name>.csv. If auto_label is True, also invoke
    auto_label.auto_label_video() on the manually-labeled regions once the CSV is
    saved, writing a YOLO-format dataset under output/labeled_data/."""
    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        print(f"Error: {video_path} is not a valid file.", file=sys.stderr)
        sys.exit(1)
    if auto_label and not model_path:
        print("Error: --auto-label requires --model-path.", file=sys.stderr)
        sys.exit(1)

    output_csv_path = OUTPUT_ROOT / "manual_labels" / f"{video_path.stem}.csv"

    print(f"Input:      {video_path}")
    print(f"CSV output: {output_csv_path}")

    raw_marks, extra_marks, frame_count, fps, width, height = run_annotation_ui(
        video_path=video_path,
        start_frame=start_frame,
        bbox_w=max(4, bbox_w),
        bbox_h=max(4, bbox_h),
        playback_speed=max(0.05, playback_speed),
        interpolate=interpolate,
        carry_forward=carry_forward,
    )

    labels = finalize_labels(raw_marks, frame_count, interpolate=interpolate, carry_forward=carry_forward)

    with _uninterruptible_save():
        write_csv(
            output_csv_path, labels, extra_marks, frame_count, fps, width, height, max(4, bbox_w), max(4, bbox_h)
        )

    manual_count = sum(1 for v in raw_marks.values() if v is not None)
    none_count = sum(1 for v in raw_marks.values() if v is None)
    extra_count = sum(len(v) for v in extra_marks.values())
    print(f"Recorded manual points: {manual_count} (explicit no-object marks: {none_count})")
    print(f"Recorded extra object points: {extra_count}")
    print(f"Saved labels CSV: {output_csv_path}")

    if auto_label:
        from auto_label import auto_label_video
        print("\nRunning auto-label on manually-labeled regions...")
        auto_label_video(
            video_path=video_path,
            csv_path=output_csv_path,
            model_path=model_path,
            crop_margin=crop_margin,
        )

    return output_csv_path


def main():
    parser = argparse.ArgumentParser(
        description="Label an object across a video by following it with the mouse; pause/step/click to correct."
    )
    parser.add_argument("video", type=str, help="Path to the input video file.")
    parser.add_argument("--bbox-w", type=int, default=640, help="Bounding box width in pixels (default: 640).")
    parser.add_argument("--bbox-h", type=int, default=320, help="Bounding box height in pixels (default: 320).")
    parser.add_argument(
        "--playback-speed", type=float, default=0.4,
        help="Initial playback speed multiplier (default: 0.4). Adjustable live with +/- while running.",
    )
    parser.add_argument(
        "--start-frame", type=int, default=0, help="Frame index to start annotation from (default: 0)."
    )
    parser.add_argument(
        "--no-interpolate", action="store_true", help="Disable interpolation between sparse manual points."
    )
    parser.add_argument(
        "--no-carry-forward", action="store_true", help="Disable carrying the last known point forward."
    )
    parser.add_argument(
        "--auto-label", action="store_true",
        help="After saving the manual CSV, run auto_label.py on the labeled regions to produce a "
             "YOLO-format dataset under output/labeled_data/. Requires --model-path.",
    )
    parser.add_argument("--model-path", type=str, default=None, help="YOLO model weights (required with --auto-label).")
    parser.add_argument(
        "--crop-margin", type=float, default=0.5,
        help="Auto-label: padding added around each manually-labeled box before detection, as a "
             "fraction of the box's own width/height (default: 0.5).",
    )
    args = parser.parse_args()

    if args.playback_speed <= 0:
        parser.error("--playback-speed must be a positive number")
    if args.auto_label and not args.model_path:
        parser.error("--auto-label requires --model-path")

    run(
        args.video,
        bbox_w=args.bbox_w,
        bbox_h=args.bbox_h,
        playback_speed=args.playback_speed,
        start_frame=args.start_frame,
        interpolate=not args.no_interpolate,
        carry_forward=not args.no_carry_forward,
        auto_label=args.auto_label,
        model_path=args.model_path,
        crop_margin=args.crop_margin,
    )


if __name__ == "__main__":
    main()
