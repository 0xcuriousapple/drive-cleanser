from .base import Source
from .localfs import LocalFSSource


def get_source(name: str) -> Source:
    if name == "gdrive":
        from .gdrive import GDriveSource
        return GDriveSource()
    if name == "localfs":
        return LocalFSSource()
    raise ValueError(f"unknown source: {name}")
