import os
import sys
import json
import shutil
import random

# Must be set BEFORE cv2 is imported — Qt backend reads these at module load time
os.environ["QT_LOGGING_RULES"]  = "*.debug=false;qt.qpa.*=false;qt.text.font.*=false"
os.environ["QT_QPA_FONTDIR"]    = "/usr/share/fonts/truetype"

import cv2

DATASET_ROOT = "./output/labeled_data"
LOG_PATH     = os.path.join(DATASET_ROOT, "master_pipeline_log.json")
SESSION_PATH = os.path.join(DATASET_ROOT, "verify_session.json")

if not os.path.exists(LOG_PATH):
    print(f"Error: Log file not found at {LOG_PATH}. Run the pipeline first.")
    exit()

with open(LOG_PATH, "r") as f:
    master_log = json.load(f)

print(f"Loaded records for {len(master_log)} processed frames.")

# ── Session file mode (--session path/to/session_*.json) ─────────────────────
_session_file  = None
_session_split = None   # optional: 'active' or 'review'

if "--session" in sys.argv:
    _si = sys.argv.index("--session")
    if _si + 1 < len(sys.argv):
        _session_file = sys.argv[_si + 1]

if "--session-split" in sys.argv:
    _ssi = sys.argv.index("--session-split")
    if _ssi + 1 < len(sys.argv):
        _session_split = sys.argv[_ssi + 1].lower()  # 'active' or 'review'

if _session_file:
    if not os.path.exists(_session_file):
        print(f"Error: session file not found: {_session_file}")
        exit()
    with open(_session_file) as _sf:
        _session_map = json.load(_sf)   # {frame_id: split}

    # Apply optional split filter within the session
    ACTIVE_SPLITS = {"train", "val", "test"}
    if _session_split == "active":
        _session_ids = {fid for fid, sp in _session_map.items() if sp in ACTIVE_SPLITS}
        print(f"Session mode [active only]: {len(_session_ids)} promoted frames from {_session_file}")
    elif _session_split == "review":
        _session_ids = {fid for fid, sp in _session_map.items() if sp == "review"}
        print(f"Session mode [review only]: {len(_session_ids)} review frames from {_session_file}")
    else:
        _session_ids = set(_session_map.keys())
        print(f"Session mode: {len(_session_ids)} frames from {_session_file}")

    review_queue = [
        (fid, meta) for fid, meta in master_log.items()
        if fid in _session_ids and os.path.exists(meta["image_path"])
    ]
    split_choice  = "session"
    min_thresh, max_thresh = 0.0, 1.0
    SESSION_KEY   = f"session|{_session_file}|{_session_split}"

if not _session_file:
    # ── Step 1: Split selector ────────────────────────────────────────────────
    SPLIT_COUNTS = {}
    for meta in master_log.values():
        s = meta["dataset_split"]
        SPLIT_COUNTS[s] = SPLIT_COUNTS.get(s, 0) + 1

    print("\nAvailable splits:")
    print("  [1] review      — ambiguous detections pending manual approval",
          f"({SPLIT_COUNTS.get('review', 0)} frames)")
    print("  [2] train/val/test — manually promoted frames",
          f"({SPLIT_COUNTS.get('train', 0) + SPLIT_COUNTS.get('val', 0) + SPLIT_COUNTS.get('test', 0)} frames)")
    print("  [3] background  — empty-label hard negatives",
          f"({sum(1 for m in master_log.values() if m['max_confidence'] == 0.0 and m['dataset_split'] != 'review')} frames)")
    print("  [4] all         — everything in the log")

    split_choice = input("\nSelect filter [1/2/3/4, default=1]: ").strip() or "1"

    if split_choice == "1":
        allowed_splits = {"review"}
        split_label = "review"
    elif split_choice == "2":
        allowed_splits = {"train", "val", "test"}
        split_label = "accepted (train/val/test)"
    elif split_choice == "3":
        allowed_splits = {"train", "val", "test"}
        split_label = "background (empty label)"
    else:
        allowed_splits = None
        split_label = "all splits"

    # ── Step 2: Confidence window ─────────────────────────────────────────────
    print(f"\nFiltering within: {split_label}")
    print("Enter confidence window (press Enter to accept defaults shown).")
    min_thresh = input("  Min confidence [default 0.0]: ").strip()
    max_thresh = input("  Max confidence [default 1.0]: ").strip()
    min_thresh = float(min_thresh) if min_thresh else 0.0
    max_thresh = float(max_thresh) if max_thresh else 1.0

    # ── Build review queue ────────────────────────────────────────────────────
    def _matches(fid, meta):
        if allowed_splits and meta["dataset_split"] not in allowed_splits:
            return False
        conf = meta["max_confidence"]
        if not (min_thresh <= conf <= max_thresh):
            return False
        if split_choice == "3" and conf != 0.0:
            return False
        return True

    review_queue = [(fid, meta) for fid, meta in master_log.items() if _matches(fid, meta)]
    SESSION_KEY  = f"{split_choice}|{min_thresh}|{max_thresh}"

# ── Session resume ────────────────────────────────────────────────────────────
start_idx = 0

if os.path.exists(SESSION_PATH):
    with open(SESSION_PATH, "r") as _sf:
        _saved = json.load(_sf)
    if _saved.get("session_key") == SESSION_KEY:
        _saved_fid = _saved.get("resume_frame_id")
        # Find that frame in the current queue
        _resume_candidates = [i for i, (fid, _) in enumerate(review_queue) if fid == _saved_fid]
        if _resume_candidates:
            _ans = input(
                f"\n💾 Saved session found at frame {_resume_candidates[0]+1}/{len(review_queue)} "
                f"({_saved_fid}). Resume? [Y/n]: "
            ).strip().lower()
            if _ans != "n":
                start_idx = _resume_candidates[0]
                print(f"  ↩ Resuming from frame {start_idx+1}/{len(review_queue)}")
            else:
                os.remove(SESSION_PATH)  # clear stale session
                print("  Starting from the beginning.")
        else:
            print("  ℹ Saved session frame no longer in queue — starting from beginning.")

print(f"\nFound {len(review_queue)} frames matching criteria.")
print("--- KEYBOARD CONTROLS ---")
print(" [y]  review frame  → accept: promote to train/val/test")
print("      active frame  → skip   (already accepted)")
print(" [n]  active frame  → reject: move back to review/")
print("      review frame  → skip   (stays in review)")
print(" [u]  UNDO last promote / demote")
print(" [← / a]  go BACK     (browse without deciding)")
print(" [→ / d]  go FORWARD  (browse without deciding)")
print(" [q]  PAUSE & SAVE — resume next time with same filter")
print("--------------------------\n")


def draw_yolo_boxes(img, lbl_path):
    """Parse a YOLO-format label file and draw bounding boxes onto img."""
    if not os.path.exists(lbl_path):
        return img
    h, w = img.shape[:2]
    with open(lbl_path, "r") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            cls, cx, cy, bw, bh = int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            x1 = int((cx - bw / 2) * w)
            y1 = int((cy - bh / 2) * h)
            x2 = int((cx + bw / 2) * w)
            y2 = int((cy + bh / 2) * h)
            # Draw bounding box
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            # Draw class label tag
            tag = f"cls:{cls}"
            (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.rectangle(img, (x1, y1 - th - 6), (x1 + tw + 4, y1), (0, 255, 0), -1)
            cv2.putText(img, tag, (x1 + 2, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)
    return img


def move_frame(frame_id, old_img, old_lbl, new_split):
    """Move image+label to new_split folder and update master_log in place."""
    new_img_dir = os.path.join(DATASET_ROOT, new_split, "images")
    new_lbl_dir = os.path.join(DATASET_ROOT, new_split, "labels")
    os.makedirs(new_img_dir, exist_ok=True)
    os.makedirs(new_lbl_dir, exist_ok=True)
    new_img = os.path.join(new_img_dir, os.path.basename(old_img))
    new_lbl = os.path.join(new_lbl_dir, os.path.basename(old_lbl))
    if os.path.exists(old_img):
        shutil.move(old_img, new_img)
    if os.path.exists(old_lbl):
        shutil.move(old_lbl, new_lbl)
    master_log[frame_id]["dataset_split"] = new_split
    master_log[frame_id]["image_path"]    = new_img
    master_log[frame_id]["label_path"]    = new_lbl
    return new_img, new_lbl


cv2.namedWindow("Scrutinizer Tool", cv2.WINDOW_NORMAL)

promoted_count = 0   # review  → train/val/test
demoted_count  = 0   # active  → review

# History stack for undo (only moves are recorded; skips are not):
# {'action': 'promote'|'demote', 'frame_id':...,
#  'old_split':..., 'new_split':..., 'old_img':..., 'old_lbl':...,
#  'new_img':..., 'new_lbl':...}
history = []


def save_session(current_idx):
    """Persist the current queue position so the session can be resumed later."""
    if current_idx < len(review_queue):
        frame_id_at_pos = review_queue[current_idx][0]
    else:
        frame_id_at_pos = None  # finished
    with open(SESSION_PATH, "w") as _sf:
        json.dump({
            "session_key":     SESSION_KEY,
            "resume_frame_id": frame_id_at_pos,
            "saved_at_index":  current_idx,
            "queue_length":    len(review_queue),
        }, _sf, indent=2)


idx = start_idx
while idx < len(review_queue):
    frame_id, meta = review_queue[idx]
    img_path = meta["image_path"]
    lbl_path = meta["label_path"]

    if not os.path.exists(img_path):
        idx += 1
        continue

    img = cv2.imread(img_path)

    # Draw YOLO bounding boxes from label file
    img = draw_yolo_boxes(img, lbl_path)

    # Overlay HUD text
    display_txt = f"[{idx+1}/{len(review_queue)}] Conf: {meta['max_confidence']:.3f} | Split: {meta['dataset_split']}"
    cv2.putText(img, display_txt, (15, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2, cv2.LINE_AA)
    cur_split = meta["dataset_split"]
    if cur_split == "review":
        hint_txt = "y=promote->active  n=skip  u=undo  a/<-=back  d/->=fwd  q=pause"
    else:
        hint_txt = "y=skip  n=demote->review  u=undo  a/<-=back  d/->=fwd  q=pause"
    cv2.putText(img, hint_txt, (15, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 0), 1, cv2.LINE_AA)

    cv2.imshow("Scrutinizer Tool", img)

    # Catch raw keyboard hits
    key = cv2.waitKey(0) & 0xFF

    if key == ord('y') or key == ord('Y'):
        cur_split = meta["dataset_split"]
        if cur_split == "review":
            # Promote: review → train/val/test
            rv = random.random()
            new_split = "train" if rv < 0.70 else ("val" if rv < 0.90 else "test")
            new_img, new_lbl = move_frame(frame_id, img_path, lbl_path, new_split)
            history.append({'action': 'promote', 'frame_id': frame_id,
                            'old_split': cur_split, 'new_split': new_split,
                            'old_img': img_path, 'old_lbl': lbl_path,
                            'new_img': new_img,  'new_lbl': new_lbl})
            promoted_count += 1
            print(f"  ✔ Promoted → {new_split}: {frame_id}")
        else:
            # Already active — skip
            print(f"  ↷ Skip (already in {cur_split}): {frame_id}")
        idx += 1

    elif key == ord('n') or key == ord('N'):
        cur_split = meta["dataset_split"]
        if cur_split in ("train", "val", "test"):
            # Demote: active → review
            new_img, new_lbl = move_frame(frame_id, img_path, lbl_path, "review")
            history.append({'action': 'demote', 'frame_id': frame_id,
                            'old_split': cur_split, 'new_split': 'review',
                            'old_img': img_path, 'old_lbl': lbl_path,
                            'new_img': new_img,  'new_lbl': new_lbl})
            demoted_count += 1
            print(f"  ✘ Demoted → review: {frame_id}")
        else:
            # Already in review — skip
            print(f"  ↷ Skip (already in review): {frame_id}")
        idx += 1

    elif key == ord('u') or key == ord('U'):
        if not history:
            print("  ⚠ Nothing to undo.")
        else:
            last = history.pop()
            # Reverse the move: new → old
            move_frame(last['frame_id'], last['new_img'], last['new_lbl'], last['old_split'])
            if last['action'] == 'promote':
                promoted_count -= 1
                print(f"  ↩ Undo promote: {last['frame_id']} back to review")
            else:
                demoted_count -= 1
                print(f"  ↩ Undo demote:  {last['frame_id']} back to {last['old_split']}")
            idx -= 1

    elif key in (ord('a'), ord('A'), 81):  # 'a', 'A', or left-arrow
        if idx > 0:
            idx -= 1
            print(f"  ← Back to frame {idx + 1}/{len(review_queue)}")
        else:
            print("  ⚠ Already at the first frame.")

    elif key in (ord('d'), ord('D'), 83):  # 'd', 'D', or right-arrow
        if idx < len(review_queue) - 1:
            idx += 1
            print(f"  → Forward to frame {idx + 1}/{len(review_queue)}")
        else:
            print("  ⚠ Already at the last frame.")

    elif key == ord('q') or key == ord('Q'):
        save_session(idx)
        with open(LOG_PATH, "w") as _lf:
            json.dump(master_log, _lf, indent=4)
        print(f"  💾 Session paused at frame {idx+1}/{len(review_queue)} — saved to {SESSION_PATH}")
        print("  Run verify.py again with the same filter to resume from here.")
        break

cv2.destroyAllWindows()
# Always persist log changes (promotions / demotions) on exit
with open(LOG_PATH, "w") as _lf:
    json.dump(master_log, _lf, indent=4)
print(f"\nReview Session Ended.")
print(f"  Promoted (review → active) : {promoted_count}")
print(f"  Demoted  (active → review) : {demoted_count}")