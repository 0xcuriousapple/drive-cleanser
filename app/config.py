"""Central paths and settings. Everything lives under DATA_DIR, which is
git-ignored and safe to delete (originals are only ever cached copies)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DC_DATA_DIR", ROOT / "data"))

DB_PATH = DATA_DIR / "catalog.db"
CACHE_DIR = DATA_DIR / "originals"      # downloaded copies for analysis
THUMB_DIR = DATA_DIR / "thumbs"
LOCAL_TRASH_DIR = DATA_DIR / "local_trash"  # undo-able trash for the localfs source
GDRIVE_CREDENTIALS = Path(os.environ.get("DC_GDRIVE_CREDENTIALS", ROOT / "credentials.json"))
GDRIVE_TOKEN = DATA_DIR / "token.json"

# Analysis limits (tune freely; None = unlimited)
MAX_DOWNLOAD_BYTES = int(os.environ.get("DC_MAX_DOWNLOAD_MB", "2048")) * 1024 * 1024
# Keep full downloaded originals in data/originals after analysis. Default off:
# photos are analyzed from small previews and videos are deleted right after
# analysis, so a 300GB library needs only a few GB of local state.
KEEP_ORIGINALS = os.environ.get("DC_KEEP_ORIGINALS", "0") == "1"
PREVIEW_SIZE = 1600                     # px, Drive preview used for photo analysis
MAX_FILES = int(os.environ.get("DC_MAX_FILES", "0")) or None
VIDEO_MAX_SAMPLED_FRAMES = 300          # cap on frames sampled across a video's full duration
VIDEO_FACE_FRAMES = 20                  # representative frames that get face/CLIP analysis

THUMB_SIZE = 384

# Near-duplicate thresholds
PHASH_HAMMING_MAX = 7                   # photos: <= this hamming distance = near-dup
VIDEO_FRAME_HAMMING_MAX = 8
VIDEO_OVERLAP_MIN = 0.7                 # fraction of matched frames to call videos near-dups
FACE_CLUSTER_EPS = 0.35                 # DBSCAN eps on cosine distance
FACE_MATCH_SIM = 0.55                   # min cosine sim to auto-assign a labeled person

IMAGE_MIMES_PREFIX = "image/"
VIDEO_MIMES_PREFIX = "video/"

for d in (DATA_DIR, CACHE_DIR, THUMB_DIR, LOCAL_TRASH_DIR):
    d.mkdir(parents=True, exist_ok=True)
