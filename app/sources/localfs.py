"""Local folder source — useful for testing the pipeline and for analyzing
folders synced locally (e.g. an iCloud Drive or Google Drive sync folder).
'Trash' moves files into data/local_trash preserving relative paths, so it is
always undoable."""
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Iterator

from .. import config, db
from .base import Source

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tiff",
             ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}


class LocalFSSource(Source):
    name = "localfs"

    def _root(self) -> Path:
        root = db.get_setting("localfs_root")
        if not root:
            raise RuntimeError("localfs root not set — POST /api/sources/localfs with {\"root\": \"/path\"}")
        return Path(root)

    def list_media(self) -> Iterator[dict]:
        root = self._root()
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in MEDIA_EXT:
                continue
            st = p.stat()
            yield {
                "source_id": str(p),
                "name": p.name,
                "path": str(p.relative_to(root)),
                "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                "size": st.st_size,
                "md5": None,
                "created_time": _iso(st.st_ctime),
                "modified_time": _iso(st.st_mtime),
            }

    def download(self, source_id: str, dest: Path) -> Path:
        # File is already local; analyze it in place instead of copying.
        return Path(source_id)

    def trash(self, source_id: str) -> dict:
        src = Path(source_id)
        rel = src.relative_to(self._root())
        dest = config.LOCAL_TRASH_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return {"trashed_to": str(dest), "original": str(src)}

    def restore(self, source_id: str, undo_info: dict) -> None:
        src = Path(undo_info["trashed_to"])
        dest = Path(undo_info["original"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
