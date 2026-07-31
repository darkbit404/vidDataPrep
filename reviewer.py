"""
Review auto-labeled frames from output/labeled_data/review/, the flat
images/ + labels/ pool that auto_label.py writes for every video it
processes (frame filenames are prefixed with "<video_name>_frame_NNNNNN",
so all videos share the same review/ folder rather than getting one each).

Controls:
    Space / d / ->    Advance to next frame
    a / <-            Go back one frame
    c                 Clear label for current frame (empty txt)
    Right-click       Same as c - clear label
    Left-click+drag   Draw new bounding box (replaces existing label)
    r                 Remove frame - move image+label to junk/, out of the dataset
    u                 Undo last action (label edit or remove)
    q / Esc           Quit

Usage:
    python3 reviewer.py                      # review output/labeled_data/review/
    python3 reviewer.py <folder>              # review a specific images/+labels/ folder
    python3 reviewer.py --video DJI_0001      # only frames from that video

review() is also importable and callable directly - e.g. from main.py's
--manual/--auto full-pipeline flow, as the final stage after auto-labeling.
"""

import os
import json
import shutil
import bisect
import argparse
import importlib.util as _ilu
from pathlib import Path

# Must be set BEFORE cv2 is imported
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false;qt.text.font.*=false"
os.environ["QT_QPA_FONTDIR"]   = "/usr/share/fonts/truetype"

# Create the missing cv2 Qt fonts dir so Qt can initialize properly.
# Only needed for pip-installed cv2 wheels missing this folder; a
# system-package cv2 (e.g. /usr/lib/python3/dist-packages) is owned by
# root and doesn't need it, so a permission failure here is harmless.
_cv2_spec = _ilu.find_spec("cv2")
if _cv2_spec and _cv2_spec.origin:
    _fonts_dir = os.path.join(os.path.dirname(_cv2_spec.origin), "qt", "fonts")
    try:
        os.makedirs(_fonts_dir, exist_ok=True)
    except OSError:
        pass

import cv2

DEFAULT_REVIEW_DIR = Path(__file__).resolve().parent / "output" / "labeled_data" / "review"


def review(review_folder=None, cls: int = 0, video: str | None = None, restart: bool = False) -> None:
    """Interactively review a images/+labels/ pool (default: output/labeled_data/review/),
    optionally filtered to one video's frames (see module docstring for controls).
    Blocks until the user quits (q/Esc); a no-op (prints and returns) if the folder
    doesn't exist or has no frames, so callers chaining this after auto-labeling don't
    crash the whole pipeline run on an empty pool.

    Progress (the last-viewed frame) is persisted to a small state file in
    review_folder and auto-resumed on the next call/run, so a session that gets
    interrupted (or just closed) doesn't force a re-scroll through everything
    already seen. Pass restart=True to ignore any saved state and start at frame 0."""
    review_folder = str(review_folder) if review_folder is not None else str(DEFAULT_REVIEW_DIR)
    img_dir = os.path.join(review_folder, "images")
    lbl_dir = os.path.join(review_folder, "labels")

    if not os.path.isdir(img_dir):
        print(f"Error: images/ folder not found in {review_folder}")
        return
    os.makedirs(lbl_dir, exist_ok=True)

    # Junk lives as a sibling of review_folder (e.g. output/labeled_data/junk next to
    # .../review), mirroring the images/+labels/ layout so it can itself be reviewed
    # later with this same function.
    junk_dir = os.path.join(os.path.dirname(os.path.normpath(review_folder)), "junk")
    junk_img_dir = os.path.join(junk_dir, "images")
    junk_lbl_dir = os.path.join(junk_dir, "labels")
    os.makedirs(junk_img_dir, exist_ok=True)
    os.makedirs(junk_lbl_dir, exist_ok=True)

    frames = sorted(f for f in os.listdir(img_dir) if f.lower().endswith(".jpg"))
    if video:
        frames = [f for f in frames if f.startswith(f"{video}_frame_")]

    if not frames:
        where = f" for video '{video}'" if video else ""
        print(f"No .jpg frames found{where} in {img_dir}")
        return

    # ── Resume state ─────────────────────────────────────────────────────────
    # Namespaced by video filter so a --video session and a full-pool session
    # (different frame lists) don't clobber each other's progress.
    state_suffix = f"__{video}" if video else ""
    state_path = os.path.join(review_folder, f".review_state{state_suffix}.json")

    def save_state(fname):
        try:
            with open(state_path, "w") as f:
                json.dump({"last_frame": fname}, f)
        except OSError:
            pass

    def load_resume_idx():
        if restart or not os.path.exists(state_path):
            return 0
        try:
            with open(state_path) as f:
                last_frame = json.load(f).get("last_frame")
        except (OSError, json.JSONDecodeError):
            return 0
        if last_frame in frames:
            return frames.index(last_frame)
        return 0

    video_note = f" (video={video})" if video else ""
    print(f"Loaded {len(frames)} frames from {img_dir}{video_note}")
    print("Space/d=next  a=back  c=clear  drag=draw box  r=junk  u=undo  q=quit")

    # ── State ────────────────────────────────────────────────────────────────
    draw_start  = None
    draw_cur    = None
    is_drawing  = False
    scale       = 1.0
    img_w = img_h = 0
    undo_stack  = []
    current_idx = 0
    cb_set      = False

    # ── Label helpers ────────────────────────────────────────────────────────
    def lbl_path(fname):
        return os.path.join(lbl_dir, os.path.splitext(fname)[0] + ".txt")

    def read_label(fname):
        p = lbl_path(fname)
        return open(p).read() if os.path.exists(p) else ""

    def write_label(fname, content):
        with open(lbl_path(fname), "w") as f:
            f.write(content)

    def _trim_undo():
        if len(undo_stack) > 50:
            undo_stack.pop(0)

    def push_label_undo(fname):
        undo_stack.append(("label", fname, read_label(fname)))
        _trim_undo()

    def push_remove_undo(fname):
        undo_stack.append(("remove", fname))
        _trim_undo()

    # ── Junk (removed-frame) helpers ────────────────────────────────────────
    def junk_img_path(fname):
        return os.path.join(junk_img_dir, fname)

    def junk_lbl_path(fname):
        return os.path.join(junk_lbl_dir, os.path.splitext(fname)[0] + ".txt")

    def do_remove(fname):
        """Move fname's image+label into junk/ and drop it from the active frames
        list. A label file always ends up in junk/labels/ - moved if one exists,
        otherwise created empty - mirroring auto_label.py's convention that every
        frame gets a paired .txt, so junk/ stays self-consistent with review/."""
        push_remove_undo(fname)
        shutil.move(os.path.join(img_dir, fname), junk_img_path(fname))
        src_lbl = lbl_path(fname)
        if os.path.exists(src_lbl):
            shutil.move(src_lbl, junk_lbl_path(fname))
        else:
            open(junk_lbl_path(fname), "w").close()
        frames.remove(fname)

    def undo_remove(fname):
        """Move fname's image+label back out of junk/ and reinsert it into frames,
        keeping the list sorted (frames started out sorted() by filename)."""
        shutil.move(junk_img_path(fname), os.path.join(img_dir, fname))
        jl = junk_lbl_path(fname)
        if os.path.exists(jl):
            shutil.move(jl, lbl_path(fname))
        bisect.insort(frames, fname)

    # ── Drawing helpers ──────────────────────────────────────────────────────
    def draw_yolo_boxes(img, fname):
        p = lbl_path(fname)
        if not os.path.exists(p):
            return img
        h, w = img.shape[:2]
        for line in open(p):
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            _, cx, cy, bw, bh = int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            x1 = int((cx - bw/2) * w);  y1 = int((cy - bh/2) * h)
            x2 = int((cx + bw/2) * w);  y2 = int((cy + bh/2) * h)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        return img

    def disp_to_img(x, y):
        return int(round(x / scale)), int(round(y / scale))

    def build_display(img, fname, idx):
        nonlocal scale, img_w, img_h
        img_h, img_w = img.shape[:2]
        scale = min(1.0, 1728 / img_w, 972 / img_h)
        dw, dh = round(img_w * scale), round(img_h * scale)

        out = img.copy()
        draw_yolo_boxes(out, fname)

        # Live drag preview (in original-res coords)
        if draw_start and draw_cur:
            cv2.rectangle(out, draw_start, draw_cur, (0, 120, 255), 2)

        if scale < 1.0:
            out = cv2.resize(out, (dw, dh), interpolation=cv2.INTER_AREA)

        lbl = read_label(fname)
        status      = "LABELED" if lbl.strip() else "EMPTY"
        cv2.putText(out, f"[{idx+1}/{len(frames)}] {fname} [{status}]",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 220, 255), 2)
        cv2.putText(out, "Space/d=next  a=back  c=clear  drag=draw  r=junk  u=undo  q=quit",
                    (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 200), 1)
        return out

    # ── Mouse callback ───────────────────────────────────────────────────────
    def on_mouse(event, x, y, flags, param):
        nonlocal draw_start, draw_cur, is_drawing
        ox, oy = disp_to_img(x, y)

        if event == cv2.EVENT_LBUTTONDOWN:
            is_drawing = True
            draw_start = (ox, oy)
            draw_cur   = (ox, oy)

        elif event == cv2.EVENT_MOUSEMOVE and is_drawing:
            draw_cur = (ox, oy)

        elif event == cv2.EVENT_LBUTTONUP and is_drawing:
            is_drawing = False
            draw_cur   = (ox, oy)
            x1 = min(draw_start[0], draw_cur[0]);  y1 = min(draw_start[1], draw_cur[1])
            x2 = max(draw_start[0], draw_cur[0]);  y2 = max(draw_start[1], draw_cur[1])
            if x2 - x1 >= 4 and y2 - y1 >= 4:
                fname = frames[current_idx]
                push_label_undo(fname)
                cx = ((x1 + x2) / 2) / img_w
                cy = ((y1 + y2) / 2) / img_h
                bw = (x2 - x1) / img_w
                bh = (y2 - y1) / img_h
                write_label(fname, f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n")
            draw_start = draw_cur = None

        elif event == cv2.EVENT_RBUTTONDOWN:
            fname = frames[current_idx]
            push_label_undo(fname)
            write_label(fname, "")

    # ── Main loop ────────────────────────────────────────────────────────────
    win_label = video or os.path.basename(os.path.normpath(review_folder))
    win = "Review -- " + win_label   # ASCII only
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    idx = load_resume_idx()
    if idx:
        print(f"Resuming at frame {idx+1}/{len(frames)}: {frames[idx]}")
    while True:
        current_idx = idx
        fname = frames[idx]
        img = cv2.imread(os.path.join(img_dir, fname))
        if img is None:
            idx = min(idx + 1, len(frames) - 1)
            continue

        shown = build_display(img, fname, idx)
        cv2.imshow(win, shown)

        # Set mouse callback only after the first successful imshow
        if not cb_set:
            cv2.setMouseCallback(win, on_mouse)
            cb_set = True

        delay = 15 if is_drawing else 30
        key = cv2.waitKey(delay) & 0xFF

        if key in (ord('q'), 27):
            save_state(frames[idx])
            break
        elif key in (ord(' '), ord('d'), ord('D'), 83):   # next
            idx = min(idx + 1, len(frames) - 1)
            save_state(frames[idx])
        elif key in (ord('a'), ord('A'), 81):             # back
            idx = max(idx - 1, 0)
            save_state(frames[idx])
        elif key in (ord('c'), ord('C')):                 # clear
            push_label_undo(fname)
            write_label(fname, "")
        elif key in (ord('r'), ord('R')):                 # remove -> junk/
            do_remove(fname)
            print(f"  Junked: {fname}")
            if not frames:
                print("All frames junked - nothing left to review.")
                break
            idx = min(idx, len(frames) - 1)
            save_state(frames[idx])
            continue
        elif key in (ord('u'), ord('U')):                 # undo
            if undo_stack:
                action = undo_stack.pop()
                if action[0] == "label":
                    _, pf, pc = action
                    write_label(pf, pc)
                    if pf in frames:
                        idx = frames.index(pf)
                        save_state(frames[idx])
                    print(f"  Undo: restored label for {pf}")
                else:  # ("remove", pf)
                    _, pf = action
                    undo_remove(pf)
                    idx = frames.index(pf)
                    save_state(frames[idx])
                    print(f"  Undo: restored {pf} from junk")
            else:
                print("  Nothing to undo.")

    cv2.destroyAllWindows()
    print(f"\nDone.")


def _cli_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("review_folder", nargs="?", default=str(DEFAULT_REVIEW_DIR),
                         help=f"Folder containing images/ and labels/ (default: {DEFAULT_REVIEW_DIR})")
    parser.add_argument("--class", dest="cls", type=int, default=0, help="Class index for drawn boxes (default 0)")
    parser.add_argument("--video", dest="video", default=None,
                         help="Only review frames whose filename starts with '<video>_frame_'")
    parser.add_argument("--restart", action="store_true",
                         help="Ignore any saved resume state and start over from frame 0")
    args = parser.parse_args()
    review(review_folder=args.review_folder, cls=args.cls, video=args.video, restart=args.restart)


if __name__ == "__main__":
    _cli_main()
