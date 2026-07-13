# vidDataPrep

Two-stage pipeline for turning a shared Google Drive folder of raw footage into a frame-level dataset:

1. **Download** — pulls an entire (possibly huge, nested) Drive folder to local disk via the Google Drive API, bypassing the web UI's 2GB zip-export limit.
2. **Extract frames** — recursively walks the downloaded tree, regardless of depth or naming convention, finds every video, and extracts every frame into a mirrored output tree.

Both stages can be run together as a single pipeline, or independently.

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Implementation Steps](#implementation-steps)
- [Usage](#usage)
- [How It Works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Security Notes](#security-notes)

---

## Why This Exists

Google Drive's web UI only lets you download folders as `.zip` archives, capped at roughly 2GB each. For folders containing hundreds of gigabytes — common with drone footage, datasets, video archives, or research data — that means dozens of unlabeled zip files to download one by one and merge back together by hand. `download_drive_folder.py` bypasses that entirely by talking to the Google Drive API directly, downloading each file individually and recreating the original folder structure locally — no zipping, no size cap, no manual reassembly.

Once footage is downloaded, it rarely arrives tidily organized. Source footage for this project was dumped into a root folder with no consistent naming convention, no consistent folder depth (some videos sit directly in the root, others nested 3–4 subfolders deep), and no consistent tree shape between sibling branches. Manually hunting down every video and extracting frames one by one doesn't scale, so `extract_frames.py` walks the entire tree regardless of depth or naming, finds every video file it encounters, and extracts its frames — without touching or reorganizing the original data.

The two scripts started out as independent tools, each run by hand with its own arguments. In practice they're always run back-to-back on the same data — whatever gets downloaded is exactly what needs frames extracted from it — so `main.py` and `.env` tie them into a single pipeline: local paths configured once, one command for the full run. Neither script lost its standalone usability in the process.

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

### Frame extraction stage
- Depth-agnostic recursion — descends into every subfolder no matter how deeply nested, with no assumptions about tree shape
- Non-destructive — reads from the input tree only; all output is written to a separate output tree
- Mirrored output structure — the output tree reproduces the input tree's folder layout, with each video replaced by a same-named folder of its extracted frames
- Automatic video detection — matches by file extension (`.mp4`, `.avi`, `.mov`, `.mkv`, `.flv`, `.wmv`, `.webm`, `.m4v`), case-insensitive
- Every frame extracted — no subsampling; every frame in the video is written out
- Sequential frame naming — frames are numbered `1.jpg`, `2.jpg`, `3.jpg`, ... within each video's folder
- Handles duplicate video names safely — two videos named `clip.mp4` in different subfolders never collide
- Mixed folders supported — a folder containing both videos and subfolders has both processed
- Graceful failure handling — unreadable folders and unopenable/corrupt videos are logged as warnings and skipped, not fatal errors

### Pipeline
- One command for the full pipeline — `python3 main.py <FOLDER_ID_OR_URL>` downloads then extracts, back-to-back
- Each stage still fully standalone — either script can be run alone, unchanged, for one-off or partial runs
- Paths configured once, in `.env` — only the Drive folder link (the one thing that changes every run) needs to be typed
- CLI flags still override `.env` — passing `--dest`, `root_folder`, or `-o` explicitly takes priority over the `.env` default
- `DATA_DIR` chains the two stages together — it's simultaneously the download destination and the frame-extraction input

## Prerequisites

- Python 3.8 or higher
- `venv` module (standard library — no separate install needed)
- A Google account with access to the shared Drive folder (i.e. the folder was shared with your email, or shared as "Anyone with the link")
- ~10 minutes for a one-time Google Cloud OAuth client setup
- Enough free disk space for **both** the downloaded footage and the extracted frames — every-frame JPEG extraction can easily produce more data on disk than the source videos, especially for long or high-resolution footage, and source footage for this project can run into the hundreds of GB

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
4. **Configure `.env`** with the local paths the pipeline should use (already present in this repo with these defaults — edit if you want different locations):
   ```
   DATA_DIR=./data
   FRAMES_OUTPUT_DIR=./data_frames
   ```
   - `DATA_DIR` is where the Drive folder gets downloaded to, and also where `extract_frames.py` looks for videos by default.
   - `FRAMES_OUTPUT_DIR` is where extracted frames are written. If unset, it defaults to `<DATA_DIR>_frames`.
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

**Full pipeline** — only the Drive folder link changes between runs, so it's the only argument:

```bash
python3 main.py <FOLDER_ID_OR_URL>
```

This downloads into `DATA_DIR`, then extracts frames from everything in it into `FRAMES_OUTPUT_DIR`.

**Download stage alone:**

```bash
python3 download_drive_folder.py <FOLDER_ID_OR_URL> [--dest DEST_DIR]
```

| Argument | Description |
|---|---|
| `folder` | Required. The Google Drive folder ID, or a full `https://drive.google.com/drive/folders/...` URL. |
| `--dest` | Optional. Local directory to download into. Defaults to `$DATA_DIR` from `.env` if set, otherwise `./downloaded_data`. |

The script prints progress per file (`[n] path — xx%`) and logs `[skip, exists]` for files already fully downloaded in a previous run, making it safe to interrupt (`Ctrl+C`) and resume at any time.

**Frame extraction stage alone:**

```bash
python3 extract_frames.py [root_folder] [-o OUTPUT_DIR]
```

| Argument | Required | Description |
|---|---|---|
| `root_folder` | No | Path to the top of the folder tree to scan for videos. Defaults to `$DATA_DIR` from `.env` if set; required if `DATA_DIR` isn't set. |
| `-o`, `--output` | No | Path to the output root. Defaults to `$FRAMES_OUTPUT_DIR` from `.env` if set, otherwise `<root_folder>_frames` alongside the input. |

Example, given:

```
raw_footage/
├── video1.mp4
├── site_a/
│   └── cam2.avi
└── site_b/
    └── 2024-batch/
        └── unlabeled.mkv
```

```bash
python3 extract_frames.py raw_footage -o raw_footage_frames
```

produces:

```
raw_footage_frames/
├── video1/
│   ├── 1.jpg
│   ├── 2.jpg
│   └── ...
├── site_a/
│   └── cam2/
│       ├── 1.jpg
│       └── ...
└── site_b/
    └── 2024-batch/
        └── unlabeled/
            ├── 1.jpg
            └── ...
```

## How It Works

### Pipeline orchestration
- `main.py` loads `.env`, then calls each stage's `run(...)` function directly (`download_drive_folder.run(drive_folder, data_dir)`, then `extract_frames.run(data_dir, frames_output)`) — it does not shell out or re-parse command-line arguments.
- Both `download_drive_folder.py` and `extract_frames.py` are split into a `run(...)` function (the actual logic) and a thin `main()` CLI wrapper (argument parsing only). This is what lets `main.py` call the real logic directly while each script's own `argparse`-based `main()` keeps working unchanged for standalone use.
- Priority order for local paths on each stage: an explicit CLI flag always wins, otherwise the `.env` value is used, otherwise a hardcoded fallback (e.g. `./downloaded_data`) applies.
- `python-dotenv`'s `load_dotenv()` locates `.env` relative to the *script file's* own location, not your current shell directory — so the `.env` in the project root is always found regardless of where you invoke `python3` from.

### Download stage
- Uses the official [Google Drive API v3](https://developers.google.com/drive/api/guides/about-sdk) via `google-api-python-client`.
- Authenticates via OAuth 2.0 (Installed App flow), acting on your behalf with your own Google account's access — no service account or admin permissions needed.
- Recursively lists all children of the target folder (`files.list` with a `parents` query), rebuilding the same folder tree locally.
- Downloads each file with `MediaIoBaseDownload` in 50MB chunks, so even huge files stream to disk without holding everything in memory.
- Google-native file types (Docs, Sheets, Slides, Drawings) are converted via the Drive API's `export_media` endpoint into standard formats (`.docx`, `.xlsx`, `.pptx`, `.pdf`) since they have no native binary form to download.
- Automatically retries with exponential backoff on transient HTTP or network errors.

### Frame extraction stage
1. `process_folder(input_dir, output_dir)` lists the immediate contents of `input_dir`.
2. For each entry:
   - **It's a directory** → recurse into it, passing the matching subfolder under `output_dir` so the tree shape is mirrored.
   - **It's a video file** (matched by extension via `is_video_file`) → a folder named after the video (its filename without extension) is created under the *current* output directory, and `extract_frames` is called on it.
   - **Anything else** (non-video files) → ignored.
3. `extract_frames(video_path, frames_dir)` opens the video with OpenCV's `cv2.VideoCapture`, reads frames one at a time in a loop until the video is exhausted, and writes each one as `<frame_number>.jpg` into `frames_dir`.
4. Because recursion always passes the *matching* subfolder path down, and each video gets its own subfolder (`output_dir / entry.stem`), the output tree ends up structurally identical to the input tree, except every video is replaced by a folder of its frames.
5. Output directories are only created lazily, at the point a video is actually found — branches of the input tree with no videos in them don't produce empty folders in the output.

## Troubleshooting

### Pipeline

**`ModuleNotFoundError: No module named 'dotenv'` (or `cv2`, or `googleapiclient`)**
→ Dependencies aren't installed in the active environment. Run `pip install -r requirements.txt` inside your activated virtual environment.

**Changing `.env` doesn't seem to affect a run**
→ Confirm you're editing the `.env` in the project root (next to `main.py`) — that's the one `load_dotenv()` finds regardless of your current directory.

**`main.py` exits with "the following arguments are required: drive_folder"**
→ `main.py` only accepts the Drive folder ID/URL as an argument by design; pass it explicitly each run, e.g. `python3 main.py "https://drive.google.com/drive/folders/..."`.

**Frame extraction runs against the wrong (or an empty) folder**
→ Check `DATA_DIR` in `.env` — it must point at the same directory `download_drive_folder.py` downloaded into, since that's what chains the two stages together.

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

### Frame extraction stage

**`pip freeze` shows unrelated packages (e.g. ROS2 packages) that aren't in `requirements.txt`**
→ This happens if your shell profile sets a `PYTHONPATH` environment variable (common with ROS/ROS2 setups) — it leaks system-level packages into `pip freeze` output even inside an activated venv, since `PYTHONPATH` isn't cleared by venv activation. Check with `echo $PYTHONPATH`. Doesn't affect what the script actually imports, but verify with `PYTHONPATH= pip freeze` if you ever regenerate `requirements.txt`.

**`[WARN] Could not open video: ...`**
→ OpenCV couldn't open that file — it may be corrupted, use an unsupported codec, or not actually be a valid video despite the extension. The script logs it and continues to the next file rather than stopping the whole run.

**`[WARN] Skipping unreadable folder: ...`**
→ A `PermissionError` occurred while listing that folder's contents. Check ownership/permissions on that folder if you need it processed.

**Run seems slow / takes a long time**
→ Every-frame extraction of long or high-resolution videos is disk- and CPU-bound. There's no parallelism currently — folders and videos are processed strictly one at a time.

**Re-running the script**
→ It does not check for or skip already-extracted videos — re-running overwrites existing frame files in the output tree. Delete or move the previous output first if you want a clean slate, or point `-o` / `FRAMES_OUTPUT_DIR` at a new location.

## Security Notes

- **Never commit `credentials.json` or `token.json`.** `credentials.json` identifies your OAuth client, and `token.json` contains an active access/refresh token tied to your Google account — both are already excluded via this repo's `.gitignore`. If either is accidentally published, revoke access immediately from your [Google Account permissions page](https://myaccount.google.com/permissions) and delete/regenerate the OAuth client in Google Cloud Console.
- **Drive access is read-only.** `download_drive_folder.py` requests only the `drive.readonly` scope — it cannot modify, delete, or upload anything to your Drive.
- **`.env` holds only local paths, not secrets** — but it's still excluded from version control as machine-specific configuration.
- **`extract_frames.py` has no network activity of its own** — only the download stage talks to the network. Frame extraction only reads from and writes to the local filesystem.
- **Runs with your user's filesystem permissions.** Neither stage escalates privileges, but neither is stopped from overwriting files in an output path that already contains unrelated data with matching names.
- **Untrusted video files.** Video decoding is handled by OpenCV, which in turn relies on underlying media libraries (e.g. FFmpeg-based backends) that have historically had memory-safety vulnerabilities in malformed-file parsing. If any videos in `DATA_DIR` come from an untrusted or external source, consider running frame extraction in a sandboxed/disposable environment rather than directly on a machine with sensitive access.
- **Disk exhaustion.** Downloaded footage plus every-frame JPEG extraction can consume significant disk space quickly — there's no built-in disk-space check or quota in either stage, so monitor available space on large batches.
- **Dependencies are pinned** in `requirements.txt` to specific versions, so re-creating the environment from this repo gives a reproducible, isolated dependency set rather than relying on whatever is globally installed.