# Drive Cleanser — AI-Powered Personal Media Organizer

Connects to Google Drive (or any local folder, e.g. a synced iCloud folder), builds a
read-only catalog of your photos and videos, detects duplicates, recognizes faces,
classifies content, and lets you search it all in natural language — while **never
touching a single file without your explicit approval**.

## Safety model (read this first)

- **Analysis is 100% read-only.** Scanning indexes and downloads *copies* for analysis;
  it never moves, renames, modifies, or deletes anything. The default Google Drive
  connection uses the `drive.readonly` OAuth scope, so writes are impossible at the
  API level.
- **Everything is a recommendation.** Duplicates, screenshots, low-quality shots — all
  land in review queues with a confidence score and a plain-English explanation.
- **Execution requires three explicit steps**: you approve individual recommendations,
  you click Execute, and (for Google Drive) you must first deliberately re-connect with
  write access. Videos get an extra confirmation dialog.
- **"Delete" always means reversible trash** — Google Drive trash (recoverable for
  30 days) or a local trash folder. Nothing is ever permanently deleted by this app.
- **Complete audit trail** with one-click Undo for every executed action.
- **It learns from you**: rejecting recommendations in a collection raises the
  confidence bar for future suggestions there (and approvals lower it). The applied
  adjustment is shown in each explanation.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # core (small)
.venv/bin/pip install -r requirements-ml.txt       # optional: CLIP + face recognition (~2GB)
brew install ffmpeg                                # optional: full video analysis

.venv/bin/uvicorn app.main:app --port 8500
# open http://localhost:8500
```

The app is fully functional without the ML extras (indexing, exact/near duplicate
detection, screenshot heuristics, review workflow). Installing `requirements-ml.txt`
adds semantic search, zero-shot classification, and face recognition; models download
on first scan.

### Connect Google Drive

1. In [Google Cloud Console](https://console.cloud.google.com/): create a project,
   enable the **Google Drive API**, configure the OAuth consent screen (add yourself
   as a test user), and create an **OAuth client ID → Desktop app**.
2. Download the client JSON and save it as `credentials.json` in the repo root.
3. In the app dashboard, click **Connect (read-only)** — a browser window opens for
   consent. Then **Start scan**.

Only click **Enable write access** when you're ready to execute approved trash
actions; until then the app cannot modify your Drive even in principle.

### Or point it at a local folder

Set a folder path on the dashboard (works great on a locally-synced iCloud Drive /
Google Drive folder) and scan with source "Local folder".

## Big libraries: bandwidth & disk

Scanning does **not** mirror your library locally. For a ~300GB Drive:

- **Photos** are analyzed from Drive's ~1600px previews (a few hundred KB each,
  fetched read-only) — originals are never downloaded. Exact-duplicate detection
  uses the MD5 checksum Drive already reports in metadata.
- **Videos** must be downloaded for full-duration analysis, but they're processed
  one at a time and **deleted immediately after** — peak disk usage is one video,
  not the library. Files over `DC_MAX_DOWNLOAD_MB` (default 2048) are skipped and
  flagged.
- What's kept locally: thumbnails, hashes, embeddings, and the catalog DB — roughly
  2–4GB for a 50k-item library. Set `DC_KEEP_ORIGINALS=1` if you *want* full local
  copies retained.

Expect total transfer of roughly: (number of photos × ~0.3MB) + (total size of your
videos, streamed once). You can cap a first pass with the "max files" field on the
dashboard to see results quickly before committing to the full scan.

## What a scan does

1. **Index** — lists all images/videos into a SQLite catalog (`data/catalog.db`).
2. **Analyze** each file:
   - SHA-256 + perceptual hash, EXIF (capture time, camera, GPS), thumbnail,
     quality score (sharpness / exposure / resolution)
   - CLIP embedding + zero-shot classification (screenshot, document, receipt, meme,
     people, pet, food, nature, travel, celebration, …) *(ML extras)*
   - Face detection + embeddings *(ML extras)*
   - **Videos**: frames are sampled across the entire duration (up to 300), each
     hashed and scored; scene-distinct representative frames get face detection and
     CLIP analysis; a summary is generated. Video near-duplicates are detected by
     frame-set overlap, so a re-encoded or resized copy is still caught.
3. **Group** — exact duplicates (identical hash), near-duplicate photos (perceptual
   hash, bucketed so it scales), near-duplicate videos, face clusters.
4. **Recommend** — fills the review collections: **Keep**, **Review**,
   **Duplicate Candidates**, **Screenshots**, **Documents**, **Memes**. Each duplicate
   group recommends keeping the best version (resolution → quality → size → original
   filename → earliest timestamp) and explains why; you can override the keeper.

## Using the app

- **People**: face clusters appear unlabeled; type a name once ("Mom") and every
  photo/video of that person — past and future — is tagged automatically.
- **Search**: "birthday cake", "Mom cooking", "beach 2022" — person names and years
  are parsed as filters, the rest is CLIP semantic search (keyword fallback without ML).
- **Collections**: approve/reject each recommendation (or in bulk), then
  **Execute approved trash actions** when ready.
- **Activity**: full audit log with Undo.

## Architecture

```
app/
  main.py            FastAPI app + all API routes
  config.py, db.py   settings, SQLite schema/access
  sources/           gdrive.py (OAuth, read-only default), localfs.py
  pipeline/          runner.py (background scan job)
                     media.py (hash/EXIF/quality/thumbs)
                     clip_embed.py, faces.py, classify.py  (lazy ML)
                     video.py (full-duration ffmpeg sampling)
  engine/            duplicates.py, recommend.py (+feedback learning),
                     search.py, actions.py (execute/undo, audit)
  web/               single-page UI (no build step)
```

All state lives in `data/` (git-ignored): catalog DB, cached originals, thumbnails,
local trash. Delete `data/` to start fresh — your cloud files are unaffected.

## Roadmap

- iCloud support (via locally-synced Photos library; no official Apple API)
- Richer video/photo captions via a vision LLM (e.g. Claude) as an opt-in layer
- Google Photos connector (separate API from Drive)
- Storage-savings report and burst-photo detection
