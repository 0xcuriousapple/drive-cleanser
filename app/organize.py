"""On-disk media organizer: MOVE photos/videos into per-year folders.

    python -m app.organize SRC DEST [--dry-run]
    python -m app.organize --undo MANIFEST.jsonl

- Only image/video files are touched; everything else stays exactly where it is.
- Year = when the media was created, resolved in priority order:
    1. Google Takeout sidecar JSON (photoTakenTime / creationTime)
    2. Embedded metadata (EXIF DateTimeOriginal for photos,
       container creation_time via ffprobe for videos)
    3. A date in the filename (IMG_20220305..., IMG-20220305-WA...,
       Screenshot 2024-01-05..., PXL_2023..., 2022-03-05 ...)
    4. File modified time (least reliable — counted separately in the report)
- Files are MOVED (not copied); name collisions get " (2)", " (3)", … suffixes;
  nothing is ever overwritten.
- Every run writes a manifest (one JSON line per move) into DEST; --undo
  replays it in reverse, restoring every file to its original location.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp",
             ".tiff", ".tif", ".dng", ".raw", ".cr2", ".nef", ".arw"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".wmv", ".mpg"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

# yyyy[-_.]mm[-_.]dd with a plausible month/day, not part of a longer digit run
FILENAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
YEAR_MIN, YEAR_MAX = 1900, time.localtime().tm_year + 1


def year_from_sidecar(path: Path) -> int | None:
    """Google Takeout writes e.g. IMG_1.jpg.json / IMG_1.jpg.supplemental-metadata.json."""
    for cand in (path.parent / f"{path.name}.json",
                 *path.parent.glob(f"{path.name}*.json")):
        try:
            meta = json.loads(cand.read_text())
        except (OSError, ValueError):
            continue
        for key in ("photoTakenTime", "creationTime"):
            ts = meta.get(key, {}).get("timestamp")
            if ts and int(ts) > 0:
                return time.gmtime(int(ts)).tm_year
    return None


def year_from_exif(path: Path) -> int | None:
    try:
        from PIL import Image
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            # 36867 DateTimeOriginal (in Exif IFD), 306 DateTime (base)
            dt = None
            try:
                dt = exif.get_ifd(0x8769).get(36867)
            except Exception:
                pass
            dt = dt or exif.get(306) or exif.get(36867)
            if dt:
                m = re.match(r"((?:19|20)\d{2})", str(dt))
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


def year_from_video_meta(path: Path) -> int | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=60)
        tags = json.loads(out.stdout).get("format", {}).get("tags", {})
        ct = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
        if ct:
            m = re.match(r"((?:19|20)\d{2})", ct)
            # cameras with unset clocks write 1904/1970 — treat as unknown
            if m and int(m.group(1)) > 1971:
                return int(m.group(1))
    except Exception:
        pass
    return None


def year_from_name(path: Path) -> int | None:
    for m in FILENAME_DATE.finditer(path.name):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if YEAR_MIN <= y <= YEAR_MAX and 1 <= mo <= 12 and 1 <= d <= 31:
            return y
    return None


def resolve_year(path: Path, is_video: bool) -> tuple[int, str]:
    """Returns (year, source-of-truth label)."""
    y = year_from_sidecar(path)
    if y:
        return y, "takeout-sidecar"
    y = year_from_video_meta(path) if is_video else year_from_exif(path)
    if y:
        return y, "embedded-metadata"
    y = year_from_name(path)
    if y:
        return y, "filename"
    return time.localtime(path.stat().st_mtime).tm_year, "file-mtime"


def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        cand = dest.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def organize(src: Path, dest: Path, dry_run: bool) -> int:
    src, dest = src.resolve(), dest.resolve()
    if not src.is_dir():
        print(f"error: source is not a directory: {src}", file=sys.stderr)
        return 1
    if dest == src or src in dest.parents:
        print("error: DEST must not be inside SRC in a way that would re-scan moved files"
              if dest != src else "error: SRC and DEST are the same folder", file=sys.stderr)
        # dest inside src is handled below by skipping dest subtree; only same-dir is fatal
        if dest == src:
            return 1

    media = []
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if dest != src and dest in p.parents:   # never re-organize already-moved files
            continue
        if p.suffix.lower() in MEDIA_EXT:
            media.append(p)

    if not media:
        print("No image/video files found — nothing to do.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    manifest_path = dest / f"organize-manifest-{stamp}.jsonl"
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    per_year, per_source = Counter(), Counter()
    errors, moved = [], 0

    print(f"{'[DRY RUN] ' if dry_run else ''}Organizing {len(media)} media files "
          f"from {src}\n  → {dest}/<year>/\n")
    manifest = None if dry_run else open(manifest_path, "a", encoding="utf-8")
    try:
        for p in media:
            try:
                year, why = resolve_year(p, p.suffix.lower() in VIDEO_EXT)
                target = unique_dest(dest / str(year) / p.name)
                per_year[year] += 1
                per_source[why] += 1
                if dry_run:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                manifest.write(json.dumps({"from": str(p), "to": str(target)}) + "\n")
                moved += 1
                if moved % 500 == 0:
                    print(f"  …{moved} moved")
            except Exception as e:
                errors.append((p, f"{type(e).__name__}: {e}"))
    finally:
        if manifest:
            manifest.close()

    print("Per year:")
    for y in sorted(per_year):
        print(f"  {y}: {per_year[y]}")
    print("Date determined by:", dict(per_source))
    if per_source.get("file-mtime"):
        print(f"  note: {per_source['file-mtime']} file(s) had no metadata/filename date — "
              f"used file modified time, which may be the download date.")
    if errors:
        print(f"\n{len(errors)} errors:")
        for p, e in errors[:20]:
            print(f"  {p}: {e}")
    if dry_run:
        print("\nDry run — nothing was moved. Re-run without --dry-run to apply.")
    else:
        print(f"\nMoved {moved} files. Non-media files were left untouched.")
        print(f"Undo anytime with:\n  python -m app.organize --undo '{manifest_path}'")
    return 0


def undo(manifest_path: Path) -> int:
    if not manifest_path.is_file():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    entries = [json.loads(l) for l in manifest_path.read_text().splitlines() if l.strip()]
    restored, errors = 0, []
    for e in reversed(entries):
        frm, to = Path(e["from"]), Path(e["to"])
        try:
            if not to.exists():
                errors.append((to, "no longer exists"))
                continue
            frm.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(to), str(unique_dest(frm)))
            restored += 1
        except Exception as ex:
            errors.append((to, f"{type(ex).__name__}: {ex}"))
    print(f"Restored {restored}/{len(entries)} files to their original locations.")
    for p, e in errors[:20]:
        print(f"  {p}: {e}")
    return 0 if not errors else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="?", type=Path, help="folder to organize (e.g. your downloaded Drive)")
    ap.add_argument("dest", nargs="?", type=Path, help="destination root; per-year folders created inside")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen without moving anything")
    ap.add_argument("--undo", type=Path, metavar="MANIFEST", help="reverse a previous run from its manifest")
    args = ap.parse_args()
    if args.undo:
        sys.exit(undo(args.undo))
    if not args.src or not args.dest:
        ap.error("SRC and DEST are required (or use --undo MANIFEST)")
    sys.exit(organize(args.src, args.dest, args.dry_run))


if __name__ == "__main__":
    main()
