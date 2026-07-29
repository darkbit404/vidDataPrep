"""
review_per_video.py
-------------------
Review auto-labeled frames for a single video folder produced by
auto_label_per_video.py.

Controls:
    Space / d / ->    Advance to next frame
    a / <-            Go back one frame
    c                 Clear label for current frame (empty txt)
    Right-click       Same as c - clear label
    Left-click+drag   Draw new bounding box (replaces existing label)
    u                 Undo last action
    q / Esc           Quit

Usage:
    python3 review_per_video.py <video_folder>
    python3 review_per_video.py /path/to/autolabel_per_video/DJI_xxx
"""

import os
import sys
import argparse
import importlib.util as _ilu

# Must be set BEFORE cv2 is imported
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false;qt.text.font.*=false"
os.environ["QT_QPA_FONTDIR"]   = "/usr/share/fonts/truetype"

# Create the missing cv2 Qt fonts dir so Qt can initialize properly
_cv2_spec = _ilu.find_spec("cv2")
if _cv2_spec and _cv2_spec.origin:
    _fonts_dir = os.path.join(os.path.dirname(_cv2_spec.origin), "qt", "fonts")
    os.makedirs(_fonts_dir, exist_ok=True)

import cv2
import numpy as np

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("video_folder", help="Per-video output folder (contains images/ and labels/)")
parser.add_argument("--class", dest="cls", type=int, default=0, help="Class index for drawn boxes (default 0)")
args = parser.parse_args()

VIDEO_FOLDER = args.video_folder
CLS          = args.cls
IMG_DIR      = os.path.join(VIDEO_FOLDER, "images")
LBL_DIR      = os.path.join(VIDEO_FOLDER, "labels")

if not os.path.isdir(IMG_DIR):
    print(f"Error: images/ folder not found in {VIDEO_FOLDER}")
    sys.exit(1)
os.makedirs(LBL_DIR, exist_ok=True)

# ── Load frames ───────────────────────────────────────────────────────────────
frames = sorted([f for f in os.listdir(IMG_DIR) if f.lower().endswith(".jpg")])
if not frames:
    print("No .jpg frames found in images/")
    sys.exit(0)

print(f"Loaded {len(frames)} frames from {IMG_DIR}")
print("Space/d=next  a=back  c=clear  drag=draw box  u=undo  q=quit")

# ── State ─────────────────────────────────────────────────────────────────────
draw_start  = None
draw_cur    = None
is_drawing  = False
scale       = 1.0
img_w = img_h = 0
undo_stack  = []

current_idx = [0]   # list so closure can write to it
cb_set      = [False]

# ── Label helpers ─────────────────────────────────────────────────────────────
def lbl_path(fname):
    return os.path.join(LBL_DIR, os.path.splitext(fname)[0] + ".txt")

def read_label(fname):
    p = lbl_path(fname)
    return open(p).read() if os.path.exists(p) else ""

def write_label(fname, content):
    with open(lbl_path(fname), "w") as f:
        f.write(content)

def push_undo(fname):
    undo_stack.append((fname, read_label(fname)))
    if len(undo_stack) > 50:
        undo_stack.pop(0)

# ── Drawing helpers ───────────────────────────────────────────────────────────
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
    global scale, img_w, img_h
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
    status_col  = (0, 220, 0) if lbl.strip() else (30, 30, 220)
    cv2.putText(out, f"[{idx+1}/{len(frames)}] {fname} [{status}]",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 220, 255), 2)
    cv2.putText(out, "Space/d=next  a=back  c=clear  drag=draw  u=undo  q=quit",
                (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 200), 1)
    return out

# ── Mouse callback ────────────────────────────────────────────────────────────
def on_mouse(event, x, y, flags, param):
    global draw_start, draw_cur, is_drawing
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
            fname = frames[current_idx[0]]
            push_undo(fname)
            cx = ((x1 + x2) / 2) / img_w
            cy = ((y1 + y2) / 2) / img_h
            bw = (x2 - x1) / img_w
            bh = (y2 - y1) / img_h
            write_label(fname, f"{CLS} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n")
        draw_start = draw_cur = None

    elif event == cv2.EVENT_RBUTTONDOWN:
        fname = frames[current_idx[0]]
        push_undo(fname)
        write_label(fname, "")

# ── Main loop ─────────────────────────────────────────────────────────────────
WIN = "Review -- " + os.path.basename(VIDEO_FOLDER)   # ASCII only
cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)

idx = 0
while True:
    current_idx[0] = idx
    fname = frames[idx]
    img = cv2.imread(os.path.join(IMG_DIR, fname))
    if img is None:
        idx = min(idx + 1, len(frames) - 1)
        continue

    shown = build_display(img, fname, idx)
    cv2.imshow(WIN, shown)

    # Set mouse callback only after the first successful imshow
    if not cb_set[0]:
        cv2.setMouseCallback(WIN, on_mouse)
        cb_set[0] = True

    delay = 15 if is_drawing else 30
    key = cv2.waitKey(delay) & 0xFF

    if key in (ord('q'), 27):
        break
    elif key in (ord(' '), ord('d'), ord('D'), 83):   # next
        idx = min(idx + 1, len(frames) - 1)
    elif key in (ord('a'), ord('A'), 81):             # back
        idx = max(idx - 1, 0)
    elif key in (ord('c'), ord('C')):                 # clear
        push_undo(fname)
        write_label(fname, "")
    elif key in (ord('u'), ord('U')):                 # undo
        if undo_stack:
            pf, pc = undo_stack.pop()
            write_label(pf, pc)
            if pf in frames:
                idx = frames.index(pf)
            print(f"  Undo: {pf}")
        else:
            print("  Nothing to undo.")

cv2.destroyAllWindows()
print(f"\nDone.")
