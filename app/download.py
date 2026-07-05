"""Download ONE Google Drive folder to disk as-is (no zip chunking).

    python -m app.download "Folder Name"  /local/dest
    python -m app.download <folder-id>    /local/dest
    python -m app.download --list                     # show your top-level folders
    python -m app.download --size <folder>            # measure only, no download

Why this instead of drive.google.com's Download button: the web UI zips big
folders into multiple ~2GB archives you then have to unpack and merge. This
talks to the Drive API directly (using the credentials.json you already set
up) and:

- downloads with PARALLEL workers (default 12, --workers N) — essential for
  photo libraries: small files are API-latency-bound, not bandwidth-bound;
- preserves the folder structure exactly;
- is RESUMABLE — re-run after any interruption and it skips files that are
  already complete (same size on disk);
- skips EXACT DUPLICATES without downloading them, using the md5 checksum
  Drive reports in metadata (ledger: download-ledger.jsonl; report of what
  was skipped and where its kept copy lives: skipped-duplicates.jsonl);
- sets each file's modified time to the Drive modifiedTime, so the year
  organizer's mtime fallback stays meaningful;
- retries each file up to 3x with backoff (incl. rate-limit 403/429);
- Google-native files (Docs/Sheets/Slides…) have no binary form — they are
  counted and listed at the end, not downloaded (--export-docs saves PDFs).

Read-only scope is enough; nothing in your Drive is modified.
"""
import argparse
import json
import queue
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .sources.gdrive import GDriveSource

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
FIELDS = "nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum)"
CHUNK = 4 * 1024 * 1024

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp",
             ".tiff", ".tif", ".dng", ".raw", ".cr2", ".nef", ".arw"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".wmv", ".mpg"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr"}


def category(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "photos"
    if ext in VIDEO_EXT:
        return "videos"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def _safe(name: str) -> str:
    return name.replace("/", "⁄").strip() or "_"


def _fmt_bytes(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def _fmt_eta(sec: float) -> str:
    if sec != sec or sec < 0 or sec > 604800:  # NaN / silly values
        return "—"
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {int(sec % 60):02d}s"


def _set_mtime(path: Path, modified_time: str | None):
    if not modified_time:
        return
    try:
        dt = datetime.fromisoformat(modified_time.replace("Z", "+00:00"))
        ts = dt.astimezone(timezone.utc).timestamp()
        import os
        os.utime(path, (ts, ts))
    except (ValueError, OSError):
        pass


class Status:
    """Live multi-line terminal dashboard (plain periodic lines when piped).
    Thread-safe: workers report byte chunks and file completions."""

    def __init__(self, total_files: int, total_bytes: int, counts_total: dict):
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.total_files, self.total_bytes = total_files, total_bytes
        self.counts_total = counts_total
        self.done_bytes = 0
        self.cur_dir = self.cur_file = ""
        self.inflight = 0
        self.counts = {"photos": 0, "videos": 0, "audio": 0, "other": 0}
        self.skipped = self.dups = self.errors = 0
        self.dup_bytes = 0
        self.is_tty = sys.stderr.isatty()
        self._lines = 0
        self._last = 0.0

    # -- worker-facing (all take the lock) ----------------------------------
    def start_file(self, name: str, rel: str):
        with self.lock:
            self.inflight += 1
            self.cur_file, self.cur_dir = name, rel
            self._render()

    def add_bytes(self, n: int):
        with self.lock:
            self.done_bytes += n
            self._render()

    def end_file(self, name: str, ok: bool):
        with self.lock:
            self.inflight -= 1
            self.counts[category(name)] += 1
            if not ok:
                self.errors += 1
            self._render()

    def note_skip(self, name: str, size: int, kind: str):
        """A file resolved without downloading: 'skip' (already present) or 'dup'."""
        with self.lock:
            self.counts[category(name)] += 1
            if kind == "dup":
                self.dups += 1
                self.dup_bytes += size
            else:
                self.skipped += 1
            self.done_bytes += size
            self._render()

    # -- rendering -----------------------------------------------------------
    def _render(self, force: bool = False):
        now = time.time()
        if not force and now - self._last < 0.25:
            return
        self._last = now
        elapsed = now - self.t0
        pct = min(100.0, self.done_bytes / self.total_bytes * 100) if self.total_bytes else 0
        speed = self.done_bytes / elapsed if elapsed > 1 else 0
        eta = (self.total_bytes - self.done_bytes) / speed if speed > 0 else float("nan")
        nfiles = sum(self.counts.values())

        if not self.is_tty:
            if force or now - getattr(self, "_last_plain", 0) > 30:
                self._last_plain = now
                print(f"[{pct:5.1f}%] {nfiles}/{self.total_files} files · "
                      f"{_fmt_bytes(self.done_bytes)} / {_fmt_bytes(self.total_bytes)} · "
                      f"{speed / 1e6:.1f} MB/s · ETA {_fmt_eta(eta)} · dups {self.dups} · "
                      f"errors {self.errors}", flush=True)
            return

        width = 34
        filled = int(width * pct / 100)
        bar = "█" * filled + "░" * (width - filled)
        ct = self.counts_total
        lines = [
            f" 📁 {('/' + self.cur_dir.strip('/')) or '/':<66.66}",
            f" ⬇  {self.inflight} in flight · latest: {self.cur_file:<46.46}",
            f" {bar}  {pct:5.1f}%   {_fmt_bytes(self.done_bytes)} / {_fmt_bytes(self.total_bytes)}",
            f" ⚡ {speed / 1e6:5.1f} MB/s   ETA {_fmt_eta(eta):<9} elapsed {_fmt_eta(elapsed):<9} "
            f"files {nfiles:,}/{self.total_files:,}",
            f" 📷 {self.counts['photos']:,}/{ct.get('photos', 0):,} photos   "
            f"🎬 {self.counts['videos']:,}/{ct.get('videos', 0):,} videos   "
            f"🎵 {self.counts['audio']:,}/{ct.get('audio', 0):,} audio   "
            f"📄 {self.counts['other']:,}/{ct.get('other', 0):,} other",
            f" ↻ {self.skipped:,} already present   ≡ {self.dups:,} duplicates skipped "
            f"({_fmt_bytes(self.dup_bytes)} saved)   ⚠ {self.errors} errors",
        ]
        out = ""
        if self._lines:
            out += f"\x1b[{self._lines}A"          # cursor up to redraw in place
        out += "".join(line + "\x1b[K\n" for line in lines)
        sys.stderr.write(out)
        sys.stderr.flush()
        self._lines = len(lines)

    def close(self):
        with self.lock:
            self._render(force=True)
            if self.is_tty:
                sys.stderr.write("\n")


class Downloader:
    def __init__(self, export_docs: bool = False, dedup: bool = True, workers: int = 12):
        self.src = GDriveSource()
        try:
            self.svc = self.src.service()
        except RuntimeError:
            # no token yet — run the one-time read-only OAuth flow (opens browser)
            print("First run: opening browser for Google consent (read-only scope)…")
            self.src.connect(write=False)
            self.svc = self.src.service()
        self.export_docs = export_docs
        self.dedup = dedup
        self.workers = max(1, min(32, workers))
        self.lock = threading.Lock()
        self.q: queue.Queue = queue.Queue(maxsize=400)
        self.deferred: list[tuple] = []     # dups whose kept-copy download was in flight
        self.stats = {"files": 0, "bytes": 0, "skipped_existing": 0,
                      "skipped_duplicates": 0, "dup_bytes_saved": 0,
                      "errors": [], "google_native": []}
        self.ledger: dict[str, dict] = {}   # md5 -> {"path", "size"[, "pending"]}
        self.ledger_fh = None
        self.dupreport_fh = None
        self.status: Status | None = None
        # cache the token NOW so a run never depends on the file mid-flight
        try:
            self._token_info = json.loads(config.GDRIVE_TOKEN.read_text())
        except (OSError, ValueError):
            self._token_info = None

    def _new_session(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession
        if self._token_info:
            creds = Credentials.from_authorized_user_info(self._token_info)
        else:
            creds = Credentials.from_authorized_user_file(str(config.GDRIVE_TOKEN))
        return AuthorizedSession(creds)

    # -- folder resolution / listing (main thread) ---------------------------
    def resolve_folder(self, ref: str) -> tuple[str, str]:
        if ID_RE.match(ref) and " " not in ref:
            try:
                meta = self.svc.files().get(fileId=ref, fields="id,name,mimeType",
                                            supportsAllDrives=True).execute()
                if meta["mimeType"] == FOLDER_MIME:
                    return meta["id"], meta["name"]
            except Exception:
                pass  # fall through to name search
        q = f"mimeType='{FOLDER_MIME}' and name='{ref}' and trashed=false"
        hits = self.svc.files().list(q=q, fields="files(id,name,parents)",
                                     supportsAllDrives=True,
                                     includeItemsFromAllDrives=True).execute().get("files", [])
        if not hits:
            sys.exit(f"error: no folder named '{ref}' found. Try --list, or pass the folder ID "
                     "(the long string in the folder's Drive URL).")
        if len(hits) > 1:
            print(f"Multiple folders named '{ref}' — pass one of these IDs instead:")
            for h in hits:
                print(f"  {h['id']}")
            sys.exit(1)
        return hits[0]["id"], hits[0]["name"]

    def list_children(self, folder_id: str) -> list[dict]:
        out, token = [], None
        while True:
            resp = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields=FIELDS, pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            out += resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return out

    def scan(self, folder_id: str) -> dict:
        """Metadata-only walk: totals for the progress bar (and --size).
        Shows a live one-line status so big trees visibly make progress."""
        t = {"files": 0, "bytes": 0, "native": 0, "uniq_files": 0, "uniq_bytes": 0,
             "folders": 0, "dup_bytes": 0,
             "counts": {"photos": 0, "videos": 0, "audio": 0, "other": 0}}
        seen = set()
        spin = sys.stderr.isatty()
        last = [0.0]

        def _show(where: str, force=False):
            if not spin:
                return
            now = time.time()
            if not force and now - last[0] < 0.15:
                return
            last[0] = now
            c = t["counts"]
            sys.stderr.write(
                f"\r 🔍 {t['folders']:,} folders · {t['files']:,} files "
                f"(📷 {c['photos']:,} 🎬 {c['videos']:,} 🎵 {c['audio']:,} 📄 {c['other']:,}) · "
                f"{t['bytes'] / 1e9:.1f} GB · dups {t['files'] - t['uniq_files']:,} "
                f"[{t['dup_bytes'] / 1e9:.1f} GB] · in {where:<40.40}\x1b[K")
            sys.stderr.flush()

        def _walk(fid, rel):
            t["folders"] += 1
            _show(rel or "/", force=True)
            for it in self.list_children(fid):
                if it["mimeType"] == FOLDER_MIME:
                    _walk(it["id"], f"{rel}/{_safe(it['name'])}")
                elif it["mimeType"].startswith(GOOGLE_NATIVE_PREFIX):
                    t["native"] += 1
                else:
                    size = int(it.get("size", 0) or 0)
                    t["files"] += 1
                    t["bytes"] += size
                    t["counts"][category(it["name"])] += 1
                    md5 = it.get("md5Checksum")
                    # no checksum (rare) -> must treat as unique, never as a dup
                    if md5 is None or md5 not in seen:
                        if md5 is not None:
                            seen.add(md5)
                        t["uniq_files"] += 1
                        t["uniq_bytes"] += size
                    else:
                        t["dup_bytes"] += size
                    _show(rel or "/")

        _walk(folder_id, "")
        if spin:
            sys.stderr.write("\r\x1b[K")
        return t

    # -- ledger ---------------------------------------------------------------
    def open_ledger(self, dest_root: Path):
        dest_root.mkdir(parents=True, exist_ok=True)
        ledger_path = dest_root / "download-ledger.jsonl"
        if ledger_path.exists():
            for line in ledger_path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    self.ledger[e["md5"]] = e
                except (ValueError, KeyError):
                    continue
        self.ledger_fh = open(ledger_path, "a", encoding="utf-8")
        self.dupreport_fh = open(dest_root / "skipped-duplicates.jsonl", "a", encoding="utf-8")

    def _ledger_commit(self, md5: str | None, path: Path, size: int):
        """Finalize a ledger entry after a successful download (caller holds no lock)."""
        if not md5 or not self.ledger_fh:
            return
        with self.lock:
            e = {"md5": md5, "path": str(path), "size": size}
            self.ledger[md5] = e
            self.ledger_fh.write(json.dumps(e) + "\n")
            self.ledger_fh.flush()

    # -- parallel download ------------------------------------------------------
    def _worker(self):
        sess = self._new_session()
        while True:
            task = self.q.get()
            if task is None:
                self.q.task_done()
                return
            try:
                self._process(sess, *task)
            finally:
                self.q.task_done()

    def _process(self, sess, item: dict, dest: Path, rel: str, allow_defer: bool = True):
        size = int(item.get("size", 0) or 0)
        md5 = item.get("md5Checksum")
        name = item["name"]

        if dest.exists() and dest.stat().st_size == size and size > 0:
            with self.lock:
                self.stats["skipped_existing"] += 1
                if md5 and md5 not in self.ledger and self.ledger_fh:
                    e = {"md5": md5, "path": str(dest), "size": size}
                    self.ledger[md5] = e
                    self.ledger_fh.write(json.dumps(e) + "\n")
            if self.status:
                self.status.note_skip(name, size, "skip")
            return

        if item["mimeType"].startswith(GOOGLE_NATIVE_PREFIX) and not self.export_docs:
            with self.lock:
                self.stats["google_native"].append(str(dest))
            return

        # exact-dup check + in-flight reservation (one atomic step)
        if self.dedup and md5:
            with self.lock:
                e = self.ledger.get(md5)
                if e is None:
                    self.ledger[md5] = {"md5": md5, "path": str(dest), "size": size,
                                        "pending": True}
                elif e.get("pending"):
                    # identical bytes being downloaded right now by another worker
                    if allow_defer:
                        self.deferred.append((item, dest, rel))
                        return
                    e = None  # deferred pass and still pending (owner failed): download
                else:
                    if Path(e["path"]).exists() and e["size"] == size:
                        self.stats["skipped_duplicates"] += 1
                        self.stats["dup_bytes_saved"] += size
                        if self.dupreport_fh:
                            self.dupreport_fh.write(json.dumps(
                                {"drive_path": f"{rel}/{name}", "md5": md5,
                                 "kept_copy": e["path"], "size": size}) + "\n")
                            self.dupreport_fh.flush()
                        if self.status:
                            self.status.note_skip(name, size, "dup")
                        return
                    self.ledger[md5] = {"md5": md5, "path": str(dest), "size": size,
                                        "pending": True}

        if self.status:
            self.status.start_file(name, rel)
        ok = self._fetch(sess, item, dest, size)
        with self.lock:
            if ok:
                self.stats["files"] += 1
                self.stats["bytes"] += size
            elif self.dedup and md5 and self.ledger.get(md5, {}).get("pending"):
                del self.ledger[md5]      # release reservation so dups can retry
        if ok:
            self._ledger_commit(md5, dest, size)
        if self.status:
            self.status.end_file(name, ok)

    def _fetch(self, sess, item: dict, dest: Path, size: int) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        if item["mimeType"].startswith(GOOGLE_NATIVE_PREFIX):
            url = (f"https://www.googleapis.com/drive/v3/files/{item['id']}/export"
                   f"?mimeType=application/pdf")
            dest = dest.with_suffix(".pdf")
            tmp = dest.with_name(dest.name + ".part")
        else:
            url = (f"https://www.googleapis.com/drive/v3/files/{item['id']}"
                   f"?alt=media&supportsAllDrives=true")
        for attempt in range(3):
            written = 0
            try:
                with sess.get(url, stream=True, timeout=180) as r:
                    if r.status_code in (403, 429):
                        raise IOError(f"rate limited (HTTP {r.status_code})")
                    r.raise_for_status()
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(CHUNK):
                            fh.write(chunk)
                            written += len(chunk)
                            if self.status:
                                self.status.add_bytes(len(chunk))
                tmp.rename(dest)
                _set_mtime(dest, item.get("modifiedTime"))
                if self.status and size > written:
                    self.status.add_bytes(size - written)   # metadata size mismatch guard
                return True
            except Exception as e:
                if self.status and written:
                    self.status.add_bytes(-written)
                tmp.unlink(missing_ok=True)
                if attempt == 2:
                    with self.lock:
                        self.stats["errors"].append((str(dest), f"{type(e).__name__}: {e}"))
                    if self.status:
                        self.status.add_bytes(size)   # keep the bar honest
                    return False
                backoff = (5 if "rate limited" in str(e) else 2) * (attempt + 1)
                time.sleep(backoff + random.uniform(0, 1))
        return False

    def walk(self, folder_id: str, local_dir: Path, rel: str = ""):
        """Producer: lists folders (serial) and feeds the download queue."""
        assigned: set[Path] = set()
        for item in self.list_children(folder_id):
            name = _safe(item["name"])
            if item["mimeType"] == FOLDER_MIME:
                self.walk(item["id"], local_dir / name, f"{rel}/{name}")
            else:
                target = local_dir / name
                # Drive allows same-named siblings; disambiguate with the id tail
                if target in assigned or (
                        target.exists() and int(item.get("size", 0) or 0) != target.stat().st_size):
                    alt = local_dir / f"{Path(name).stem} [{item['id'][-6:]}]{Path(name).suffix}"
                    if alt not in assigned and not alt.exists():
                        target = alt
                assigned.add(target)
                self.q.put((item, target, rel))

    def run(self, folder_id: str, root: Path):
        threads = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(self.workers)]
        for th in threads:
            th.start()
        try:
            self.walk(folder_id, root)
        finally:
            for _ in threads:
                self.q.put(None)
            for th in threads:
                th.join()
        # dups whose kept copy was in flight at the time: resolve now, serially
        if self.deferred:
            sess = self._new_session()
            for item, dest, rel in self.deferred:
                try:
                    self._process(sess, item, dest, rel, allow_defer=False)
                except Exception as e:
                    with self.lock:
                        self.stats["errors"].append((str(dest), f"{type(e).__name__}: {e}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", help="Drive folder name or folder ID")
    ap.add_argument("dest", nargs="?", type=Path, help="local destination directory")
    ap.add_argument("--list", action="store_true", help="list your top-level Drive folders and exit")
    ap.add_argument("--size", action="store_true",
                    help="measure the folder (file count + total GB) via metadata only, no download")
    ap.add_argument("--export-docs", action="store_true",
                    help="also export Google Docs/Sheets/Slides as PDF (default: skip)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="download every file even if identical bytes (same md5) were already downloaded")
    ap.add_argument("--workers", type=int, default=12,
                    help="parallel download workers (default 12)")
    args = ap.parse_args()

    dl = Downloader(export_docs=args.export_docs, dedup=not args.no_dedup,
                    workers=args.workers)

    if args.list:
        print("Top-level folders in My Drive:")
        for f in dl.svc.files().list(
                q=f"mimeType='{FOLDER_MIME}' and 'root' in parents and trashed=false",
                fields="files(id,name)", pageSize=1000).execute().get("files", []):
            print(f"  {f['name']}    (id: {f['id']})")
        return

    if args.size:
        if not args.folder:
            ap.error("--size needs a folder name or ID")
        fid, fname = dl.resolve_folder(args.folder)
        t = dl.scan(fid)
        c = t["counts"]
        dup_gb = (t["bytes"] - t["uniq_bytes"]) / 1e9
        print(f"'{fname}': {t['files']:,} files in {t['folders']:,} folders, "
              f"{t['bytes'] / 1e9:.1f} GB total (+ {t['native']} Google-native docs, not counted)")
        print(f"  📷 {c['photos']:,} photos   🎬 {c['videos']:,} videos   "
              f"🎵 {c['audio']:,} audio   📄 {c['other']:,} other")
        print(f"exact duplicates inside the folder: {t['files'] - t['uniq_files']:,} files, "
              f"{dup_gb:.1f} GB")
        print(f"→ actual download with dedup on: {t['uniq_bytes'] / 1e9:.1f} GB")
        return

    if not args.folder or not args.dest:
        ap.error("FOLDER and DEST are required (or use --list)")

    fid, fname = dl.resolve_folder(args.folder)
    root = args.dest.expanduser() / _safe(fname)
    print(f"Downloading Drive folder '{fname}' → {root}   ({dl.workers} parallel workers)")
    print("(re-run the same command anytime to resume; completed files are skipped; "
          "exact duplicates skipped via md5 — see skipped-duplicates.jsonl)\n")

    t = dl.scan(fid)
    c = t["counts"]
    print(f" found: {t['files']:,} files in {t['folders']:,} folders — "
          f"📷 {c['photos']:,} photos · 🎬 {c['videos']:,} videos · "
          f"🎵 {c['audio']:,} audio · 📄 {c['other']:,} other — "
          f"{t['bytes'] / 1e9:.1f} GB (≈{t['uniq_bytes'] / 1e9:.1f} GB unique)\n")

    dl.open_ledger(root)
    dl.status = Status(t["files"], t["bytes"], c)
    t0 = time.time()
    try:
        dl.run(fid, root)
    finally:
        dl.status.close()

    s = dl.stats
    mins = (time.time() - t0) / 60
    print(f"\nDone in {mins:.1f} min: {s['files']} files downloaded "
          f"({s['bytes'] / 1e9:.2f} GB), {s['skipped_existing']} already present.")
    if s["skipped_duplicates"]:
        print(f"{s['skipped_duplicates']} exact duplicates skipped without downloading — "
              f"saved {s['dup_bytes_saved'] / 1e9:.2f} GB. "
              f"Report: {root / 'skipped-duplicates.jsonl'}")
    if s["google_native"]:
        print(f"{len(s['google_native'])} Google-native files (Docs/Sheets/…) skipped — "
              "they have no binary form; re-run with --export-docs to save them as PDFs.")
    if s["errors"]:
        print(f"\n{len(s['errors'])} files FAILED (re-run to retry just these):")
        for p, e in s["errors"][:20]:
            print(f"  {p}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
