"""Source interface. Analysis only ever calls list_media/download — both
read-only. trash/restore are invoked exclusively by the execution engine on
user-approved recommendations, and must be reversible."""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator


class Source(ABC):
    name: str

    @abstractmethod
    def list_media(self) -> Iterator[dict]:
        """Yield dicts with keys: source_id, name, path, mime, size, md5,
        created_time, modified_time (subset ok)."""

    @abstractmethod
    def download(self, source_id: str, dest: Path) -> Path:
        """Fetch the file's bytes into dest. Never mutates the remote."""

    def fetch_preview(self, source_id: str, dest: Path) -> Path | None:
        """Fetch a reduced-size preview good enough for analysis (hashing,
        CLIP, faces). Return None if the source has no cheap preview —
        callers then fall back to download(). Never mutates the remote."""
        return None

    def stream_spec(self, source_id: str) -> tuple[str, dict] | None:
        """(url, headers) that ffmpeg can read the file from directly, or
        None if the source can't stream — callers then fall back to
        download(). Read-only."""
        return None

    @abstractmethod
    def trash(self, source_id: str) -> dict:
        """Reversibly trash the file. Returns undo_info dict."""

    @abstractmethod
    def restore(self, source_id: str, undo_info: dict) -> None:
        """Undo a previous trash()."""
