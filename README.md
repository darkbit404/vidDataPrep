# vidDataPrep

Downloads raw footage from a shared Google Drive folder to local disk (`download_drive_folder.py`, or `main.py` as a thin wrapper reading local paths from `.env`) — pulling an entire, possibly huge and deeply nested folder via the Google Drive API, bypassing the web UI's 2GB zip-export limit.

On top of that, this repo includes a separate, standalone toolset for turning that footage into a YOLO training dataset: `label_with_mouse.py` (manual mouse-driven labeling) and `auto_label.py` (YOLO-based auto-labeling, optionally restricted to the regions a human already labeled). Both operate directly on video files. These are not wired into `main.py` — see [Labeling Tools](#labeling-tools).

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Implementation Steps](#implementation-steps)
- [Usage](#usage)
- [How It Works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Labeling Tools](#labeling-tools)
- [FAQ](#faq)
- [Security Notes](#security-notes)

---

## Why This Exists

Google Drive's web UI only lets you download folders as `.zip` archives, capped at roughly 2GB each. For folders containing hundreds of gigabytes — common with drone footage, datasets, video archives, or research data — that means dozens of unlabeled zip files to download one by one and merge back together by hand. `download_drive_folder.py` bypasses that entirely by talking to the Google Drive API directly, downloading each file individually and recreating the original folder structure locally — no zipping, no size cap, no manual reassembly.

`main.py` is a thin convenience wrapper around it: it reads the local download destination from `.env` (`DATA_DIR`) so only the Drive folder link — the one thing that changes every run — needs to be typed each time. `download_drive_folder.py` remains fully usable on its own, unchanged.

## Features

### Download stage
- Recursively downloads all files and subfolders from a shared Google Drive folder
- No 2GB or file-count limit — works for folders of any size
- Preserves the original folder/subfolder structure locally
- Resumable — safely stop and rerun; it skips files already downloaded
- Automatic retries with exponential backoff on network errors
- Supports Google-native files (Docs, Sheets, Slides) by exporting them to `.docx`, `.xlsx`, `.pptx`
- Works with both personal Drive folders and Shared Drives (Team Drives)
- 100% free — no billing account or paid API tier required

### main.py wrapper
- `python3 main.py <FOLDER_ID_OR_URL>` — same effect as running `download_drive_folder.py` directly, but reads the destination path from `.env` (`DATA_DIR`) instead of requiring `--dest` each time
- `download_drive_folder.py` remains fully standalone — `main.py` calls its `run(...)` function directly rather than wrapping/shelling out to it

## Prerequisites

- Python 3.8 or higher
- `venv` module (standard library — no separate install needed)
- A Google account with access to the shared Drive folder (i.e. the folder was shared with your email, or shared as "Anyone with the link")
- ~10 minutes for a one-time Google Cloud OAuth client setup
- Enough free disk space for the downloaded footage — source footage for this project can run into the hundreds of GB

## Implementation Steps

One-time setup to get the pipeline ready to run:

1. **Create and activate a virtual environment** so dependencies don't mix with system/global Python packages:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. **Install dependencies** from the pinned requirements file:
   ```bash
   pip install -r requirements.txt
   ```
3. **Set up Google Drive OAuth credentials** — see [Google Cloud OAuth Setup](#google-cloud-oauth-setup) below.
4. **Configure `.env`** with the local path `main.py` should use (already present in this repo with this default — edit if you want a different location):
   ```
   DATA_DIR=./data
   ```
   `DATA_DIR` is where the Drive folder gets downloaded to. It's also the default `--data-root` for `auto_label.py`'s standalone batch mode (see [Labeling Tools](#labeling-tools)).
5. **Verify the install:**
   ```bash
   python3 -c "import cv2; print(cv2.__version__)"
   ```

### Google Cloud OAuth Setup

Needed once, before the download stage can authenticate.

**1. Create a Google Cloud Project**
- Go to the [Google Cloud Console](https://console.cloud.google.com/).
- Click the project dropdown at the top → **New Project**.
- Give it any name (e.g., `drive-downloader`) → **Create**.
- Make sure the new project is selected in the top project dropdown before continuing.
- This is free — you do **not** need to enable billing; the Google Drive API has no usage cost for this kind of read-only access.

**2. Enable the Google Drive API**
- In the left sidebar, go to **APIs & Services → Library**.
- Search for **Google Drive API**, click it, then click **Enable**.

**3. Configure the OAuth Consent Screen**
- Go to **APIs & Services → OAuth consent screen** (may appear as **Google Auth Platform → Overview / Branding / Audience** in newer Cloud Console layouts).
- Choose **User type: External**, then **Create**.
- Fill in the required fields (App name, User support email, Developer contact email) — these can be anything reasonable; this app will never be submitted for public verification.
- Leave the app in **Testing** publishing status. Do **not** click "Publish app" — Testing mode is all you need for personal use.

**4. Add the Drive Scope**
- Go to **Data Access** in the left sidebar (under Google Auth Platform).
- Click **Add or remove scopes**, search for **Google Drive API**.
- Check the scope: `.../auth/drive.readonly` — *"See and download all your Google Drive files"*.
- Click **Update**, then **Save**.

**5. Add Yourself as a Test User**
- Go to **Audience** in the left sidebar → **Test users** → **+ Add users**.
- Enter the Google account email you'll use to authenticate (the one with access to the shared folder) → **Save**.
- **This step is the one people most often miss.** Skipping it causes a `403: access_denied — has not completed the Google verification process` error when you try to log in.

**6. Create OAuth Client Credentials**
- Go to **APIs & Services → Credentials** (or **Clients** under Google Auth Platform).
- Click **+ Create Credentials → OAuth client ID**. Application type: **Desktop app**. Give it any name → **Create**.
- Click **Download JSON** on the newly created client, rename it to `credentials.json`, and place it in the project root (next to `download_drive_folder.py`).
- ⚠️ Never commit `credentials.json` or `token.json` to a public Git repository — see [Security Notes](#security-notes).

**First run:** the first time `download_drive_folder.py` (or `main.py`) runs, a URL is printed in the terminal and/or a browser window opens automatically. Sign in with the Google account added as a test user in step 5; you'll see an **"unverified app"** warning — this is expected, click **Advanced → Go to [app name] (unsafe)**, then approve the read-only Drive permission. A `token.json` is saved locally and reused on future runs, so you won't be asked to log in again until the token expires or is revoked.

## Usage

**Via `main.py`** — only the Drive folder link changes between runs, so it's the only argument:

```bash
python3 main.py <FOLDER_ID_OR_URL>
```

This downloads into `DATA_DIR` (from `.env`).

**`download_drive_folder.py` directly** — same logic, with an explicit destination override:

```bash
python3 download_drive_folder.py <FOLDER_ID_OR_URL> [--dest DEST_DIR]
```

| Argument | Description |
|---|---|
| `folder` | Required. The Google Drive folder ID, or a full `https://drive.google.com/drive/folders/...` URL. |
| `--dest` | Optional. Local directory to download into. Defaults to `$DATA_DIR` from `.env` if set, otherwise `./downloaded_data`. |

The script prints progress per file (`[n] path — xx%`) and logs `[skip, exists]` for files already fully downloaded in a previous run, making it safe to interrupt (`Ctrl+C`) and resume at any time.

## How It Works

### main.py wrapper
- `main.py` loads `.env`, then calls `download_drive_folder.run(drive_folder, data_dir)` directly — it does not shell out or re-parse command-line arguments.
- `download_drive_folder.py` is split into a `run(...)` function (the actual logic) and a thin `main()` CLI wrapper (argument parsing only). This is what lets `main.py` call the real logic directly while the script's own `argparse`-based `main()` keeps working unchanged for standalone use.
- Priority order for the local download path: an explicit `--dest` flag always wins, otherwise the `.env` value (`DATA_DIR`) is used, otherwise a hardcoded fallback (`./downloaded_data`) applies.
- `python-dotenv`'s `load_dotenv()` locates `.env` relative to the *script file's* own location, not your current shell directory — so the `.env` in the project root is always found regardless of where you invoke `python3` from.

### Download stage
- Uses the official [Google Drive API v3](https://developers.google.com/drive/api/guides/about-sdk) via `google-api-python-client`.
- Authenticates via OAuth 2.0 (Installed App flow), acting on your behalf with your own Google account's access — no service account or admin permissions needed.
- Recursively lists all children of the target folder (`files.list` with a `parents` query), rebuilding the same folder tree locally.
- Downloads each file with `MediaIoBaseDownload` in 50MB chunks, so even huge files stream to disk without holding everything in memory.
- Google-native file types (Docs, Sheets, Slides, Drawings) are converted via the Drive API's `export_media` endpoint into standard formats (`.docx`, `.xlsx`, `.pptx`, `.pdf`) since they have no native binary form to download.
- Automatically retries with exponential backoff on transient HTTP or network errors.

## Troubleshooting

### General

**`ModuleNotFoundError: No module named 'dotenv'` (or `cv2`, or `googleapiclient`)**
→ Dependencies aren't installed in the active environment. Run `pip install -r requirements.txt` inside your activated virtual environment.

**Changing `.env` doesn't seem to affect a run**
→ Confirm you're editing the `.env` in the project root (next to `main.py`) — that's the one `load_dotenv()` finds regardless of your current directory.

**`main.py` exits with "the following arguments are required: drive_folder"**
→ `main.py` only accepts the Drive folder ID/URL as an argument by design; pass it explicitly each run, e.g. `python3 main.py "https://drive.google.com/drive/folders/..."`.

### Download stage

**`Error 403: access_denied` — "has not completed the Google verification process"**
→ You (or the account you're signing in with) haven't been added as a **Test user** on the OAuth consent screen. See [step 5](#google-cloud-oauth-setup).

**`Missing credentials.json`**
→ You haven't downloaded and placed your OAuth client JSON file in the project root. See [step 6](#google-cloud-oauth-setup).

**`HttpError 404` when accessing the folder**
→ The folder ID is wrong, or the Google account you authenticated with does not actually have access to that folder. Confirm the folder is shared with that exact email.

**Downloads stall or fail partway through a huge file**
→ Just rerun the same command. Completed files are skipped automatically; the script does not restart the whole folder from scratch.

**It's really slow**
→ Speed is limited by your internet connection and Drive's per-user API throughput, not by the script. Large binary files (video, etc.) will always take a while.

**"This app isn't verified" warning won't go away**
→ Expected and harmless for personal-use OAuth apps in Testing mode. Click **Advanced → Go to [app name] (unsafe)** — "unsafe" just means Google hasn't manually reviewed it.

**`pip freeze` shows unrelated packages (e.g. ROS2 packages) that aren't in `requirements.txt`**
→ This happens if your shell profile sets a `PYTHONPATH` environment variable (common with ROS/ROS2 setups) — it leaks system-level packages into `pip freeze` output even inside an activated venv, since `PYTHONPATH` isn't cleared by venv activation. Check with `echo $PYTHONPATH`. Doesn't affect what the script actually imports, but verify with `PYTHONPATH= pip freeze` if you ever regenerate `requirements.txt`.

## Labeling Tools

A separate, standalone toolset for turning video into a YOLO training dataset — independent of the download step above (it operates on video files directly, e.g. from `DATA_DIR`, not on any intermediate output tree):

- **`label_with_mouse.py`** — a human plays back a video and follows the object of interest with the mouse (or clicks explicitly), producing a per-frame CSV of bounding-box coordinates.
- **`auto_label.py`** — runs a YOLO model to produce a YOLO-format labeled dataset (images + label files, split into `train`/`val`/`test`/`review` by confidence). Can be driven by a `label_with_mouse.py` CSV (detection restricted to the manually-labeled region of each frame) or run standalone in full-frame batch mode over a folder of videos.

### Features

**Manual labeling (`label_with_mouse.py`)**
- Mouse-follow labeling — move the mouse over the object while playing; its position is recorded per frame
- Pause/step controls (`n`/`p`) to review and correct individual frames
- Explicit "no object" marking (`x`) for stretches where the object leaves frame — a hard boundary that blocks interpolation/carry-forward across it
- Interpolation and carry-forward fill in frames between sparse manual points (each independently toggleable via `--no-interpolate`/`--no-carry-forward`)
- Zoom/pan (mouse wheel + `i`/`j`/`k`/`l`) for precise clicking on small or distant objects — saved coordinates always use the video's original resolution regardless of zoom level
- Extra objects — Ctrl+click adds a second (or third, ...) object on the current frame, independent of the primary tracked point and not interpolated across frames
- Configurable bounding box size around the tracked point (`--bbox-w`/`--bbox-h`, default 640×320)
- `--auto-label` chains straight into auto-labeling on the just-saved CSV, in the same run

**Auto-labeling (`auto_label.py`)**
- ROI-restricted mode (`auto_label_video()`, used via `--auto-label`) — for each manually-labeled frame, crops to the labeled box(es) (the primary object plus any extra objects), padded by `--crop-margin`, and runs YOLO only on that crop, instead of searching the full frame
- Detections are remapped from crop-local coordinates back to full-frame-normalized YOLO coordinates before being written out
- Frames with no manual label (source `none`, and no extra objects) are skipped entirely — never sent to `review`
- Confidence-based routing: frames where every detection meets `--high-conf` go to `train`/`val`/`test` (70/20/10 split); anything with a low-confidence or missing detection goes to `review`
- Resumable — `master_pipeline_log.json` tracks every frame ever processed. `train`/`val`/`test` frames are never reprocessed; `review` frames are retried on the next run (a better/re-trained model may clear the threshold later), and promoted frames have their stale `review` copies removed automatically
- Standalone batch mode (`python3 auto_label.py --model-path ...`) — full-frame detection over every video under `--data-root`, with no manual labeling involved; the entry point for a future no-manual-intervention pipeline
- `auto_label.py` has no side effects on import — `label_with_mouse.py` only imports it lazily, inside `run()`, when `--auto-label` is actually passed, so a manual-only session never loads `ultralytics` or a model

### Usage

**Manual labeling alone:**
```bash
python3 label_with_mouse.py /path/to/video.mp4
```
Produces `output/manual_labels/video.csv`. Playback starts paused; press Space to begin. See the script's module docstring (or run with the window focused) for the full key reference — pause/step, zoom/pan, marking "no object", extra objects, etc.

**Manual labeling + auto-labeling in one session:**
```bash
python3 label_with_mouse.py /path/to/video.mp4 --auto-label --model-path /path/to/yolo_weights.pt
```

| Flag | Description |
|---|---|
| `--bbox-w`, `--bbox-h` | Bounding box size in pixels around the tracked point (default: 640×320). |
| `--playback-speed` | Initial playback speed multiplier (default: 0.4); adjustable live with `+`/`-`. |
| `--start-frame` | Frame index to start annotation from (default: 0). |
| `--no-interpolate` / `--no-carry-forward` | Disable filling in frames between sparse manual points. |
| `--auto-label` | Run auto-labeling on the manually-labeled regions after saving the CSV. Requires `--model-path`. |
| `--model-path` | Path to YOLO model weights (required with `--auto-label`). |
| `--crop-margin` | Padding added around each manually-labeled box before detection, as a fraction of the box's own width/height (default: 0.5). |

**Auto-labeling alone (batch, full-frame, no manual CSV):**
```bash
python3 auto_label.py --model-path /path/to/yolo_weights.pt [--data-root ./data] [--output-dir ./output/labeled_data] [--high-conf 0.8] [--target-width 1280]
```
Recursively finds every video under `--data-root` (`.mp4`, `.MP4`, `.avi`, `.mkv`) and runs full-frame YOLO detection on every frame of every video.

### How It Works

- Both `label_with_mouse.py` and `auto_label.py` resolve `output/` relative to their own script's location, not the current working directory, so results land in the same place regardless of where you invoke them from.
- `label_with_mouse.py`'s CSV has one row per frame — `frame, time_sec, cx, cy, x1, y1, x2, y2, source` — where `source` is `manual`, `interp`, `carry`, or `none`; extra (Ctrl-click) objects get additional rows tagged `manual` on the same frame index.
- `auto_label.py`'s ROI mode (`auto_label_video`) parses that CSV into `frame_idx -> [ROIs]` (the primary box, if not `none`, plus any extra-object rows) and only processes frames that appear in that map. Frame numbering is 0-indexed, matching the CSV's `frame` column.
- Both label-producing paths — ROI-restricted (from a CSV) and full-frame (batch mode) — funnel through the same routing/save logic (`_route_and_save`), so `train`/`val`/`test`/`review` behavior is identical either way; only how detections are found differs.

**Output layout:**
```
output/
├── manual_labels/
│   └── <video_name>.csv                 # from label_with_mouse.py
└── labeled_data/                        # from auto_label.py — shared/accumulated across videos
    ├── master_pipeline_log.json
    ├── session_<timestamp>.json         # frames added in one run, for targeted review
    ├── train/{images,labels}/
    ├── val/{images,labels}/
    ├── test/{images,labels}/
    └── review/{images,labels}/
```

### Troubleshooting

**`--auto-label requires --model-path`**
→ Auto-labeling needs YOLO model weights; pass `--model-path /path/to/weights.pt`.

**Auto-labeling reports "nothing to auto-label"**
→ The CSV for that video has no non-`none` rows — nothing was manually labeled, or every frame was explicitly marked "no object".

**`ModuleNotFoundError: No module named 'ultralytics'`**
→ Install it via `pip install -r requirements.txt` (added specifically for the auto-labeling path — not needed for manual-only labeling).

## Security Notes

- **Never commit `credentials.json` or `token.json`.** `credentials.json` identifies your OAuth client, and `token.json` contains an active access/refresh token tied to your Google account — both are already excluded via this repo's `.gitignore`. If either is accidentally published, revoke access immediately from your [Google Account permissions page](https://myaccount.google.com/permissions) and delete/regenerate the OAuth client in Google Cloud Console.
- **Drive access is read-only.** `download_drive_folder.py` requests only the `drive.readonly` scope — it cannot modify, delete, or upload anything to your Drive.
- **`.env` holds only local paths, not secrets** — but it's still excluded from version control as machine-specific configuration.
- **Runs with your user's filesystem permissions.** Nothing here escalates privileges, but nothing is stopped from overwriting files in an output path that already contains unrelated data with matching names.
- **Untrusted video files.** Video decoding (in `label_with_mouse.py` and `auto_label.py`) is handled by OpenCV, which in turn relies on underlying media libraries (e.g. FFmpeg-based backends) that have historically had memory-safety vulnerabilities in malformed-file parsing. If any videos in `DATA_DIR` come from an untrusted or external source, consider running the labeling tools in a sandboxed/disposable environment rather than directly on a machine with sensitive access.
- **Disk exhaustion.** Downloaded footage plus the labeled-image dataset auto-labeling writes out can consume significant disk space quickly — there's no built-in disk-space check or quota, so monitor available space on large batches.
- **Dependencies are pinned** in `requirements.txt` to specific versions, so re-creating the environment from this repo gives a reproducible, isolated dependency set rather than relying on whatever is globally installed.
- **Labeling tools have no network activity.** `label_with_mouse.py` and `auto_label.py` only read local video files and a user-supplied local model weights path (`--model-path`) — no data leaves the machine.