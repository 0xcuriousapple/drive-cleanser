"""Background scan job. Single worker thread; progress in the jobs table.

Stages:
 1. index    — list media from the source into files (read-only)
 2. analyze  — per file: fetch bytes, thumb, exif, hashes, quality,
               CLIP embedding, faces, classification; videos get
               full-duration frame sampling
 3. group    — duplicate groups, face clustering
 4. recommend— rebuild pending recommendations

Analysis never mutates the source in any way.
"""
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from .. import config, db
from ..sources import get_source
from . import media, classify, clip_embed, faces, video

_lock = threading.Lock()
_current = {"job_id": None, "cancel": False}


def start_scan(source_name: str, max_files: int | None = None) -> int:
    with _lock:
        if _current["job_id"] and db.row("SELECT 1 FROM jobs WHERE id=? AND status='running'",
                                         (_current["job_id"],)):
            raise RuntimeError("a scan is already running")
        cur = db.execute("INSERT INTO jobs(type, message) VALUES('scan', 'starting')")
        job_id = cur.lastrowid
        _current.update(job_id=job_id, cancel=False)
    t = threading.Thread(target=_run, args=(job_id, source_name, max_files), daemon=True)
    t.start()
    return job_id


def cancel():
    _current["cancel"] = True


def _msg(job_id, message, progress=None):
    if progress is None:
        db.execute("UPDATE jobs SET message=? WHERE id=?", (message, job_id))
    else:
        db.execute("UPDATE jobs SET message=?, progress=? WHERE id=?", (message, progress, job_id))


def _run(job_id: int, source_name: str, max_files: int | None):
    try:
        source = get_source(source_name)
        max_files = max_files or config.MAX_FILES

        # 1. index
        _msg(job_id, "indexing (read-only)…")
        n = 0
        for item in source.list_media():
            db.execute(
                """INSERT INTO files(source, source_id, name, path, mime, kind, size, md5,
                       created_time, modified_time, taken_time, camera, gps_lat, gps_lon,
                       width, height, duration)
                   VALUES(:source,:source_id,:name,:path,:mime,:kind,:size,:md5,
                       :created_time,:modified_time,:taken_time,:camera,:gps_lat,:gps_lon,
                       :width,:height,:duration)
                   ON CONFLICT(source, source_id) DO UPDATE SET
                       name=excluded.name, path=excluded.path, size=excluded.size,
                       md5=coalesce(excluded.md5, files.md5),
                       modified_time=excluded.modified_time""",
                {"source": source_name, "taken_time": None, "camera": None,
                 "gps_lat": None, "gps_lon": None, "width": None, "height": None,
                 "duration": None, **item,
                 "kind": "video" if (item.get("mime") or "").startswith("video/") else "photo"},
                commit=False)
            n += 1
            if n % 200 == 0:
                db.get_db().commit()
                _msg(job_id, f"indexing… {n} files found")
            if _current["cancel"] or (max_files and n >= max_files):
                break
        db.get_db().commit()

        # 2. analyze — but first split off exact copies known from provider
        # checksums: identical bytes need analyzing only once, the copies
        # inherit the results (zero downloads for duplicate videos/photos).
        todo = db.rows("SELECT * FROM files WHERE source=? AND status='indexed'", (source_name,))
        reps, clones = _partition_exact(todo)
        total = len(reps) or 1
        ml = clip_embed.available()
        fc = faces.available()
        _msg(job_id, f"analyzing {len(reps)} unique files "
                     f"({len(clones)} exact copies detected via checksums — analysis shared) "
                     f"(CLIP: {'on' if ml else 'off'}, faces: {'on' if fc else 'off'})", 0)
        workers = max(1, min(8, int(os.environ.get("DC_SCAN_WORKERS", "4"))))
        done = [0]
        plock = threading.Lock()

        def _one(f):
            if _current["cancel"]:
                return
            try:
                _analyze_one(source, f, ml, fc)
            except Exception as e:
                db.execute("UPDATE files SET status='error', error=? WHERE id=?",
                           (f"{type(e).__name__}: {e}", f["id"]))
            with plock:
                done[0] += 1
                if done[0] % 5 == 0:
                    _msg(job_id, f"analyzing {done[0]}/{total} ({workers} workers): {f['name']}",
                         round(done[0] / total * 0.8, 3))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_one, reps))
        if _current["cancel"]:
            raise InterruptedError
        for clone, rep_id in clones:
            if _current["cancel"]:
                raise InterruptedError
            try:
                _inherit_analysis(source, clone, rep_id, ml, fc)
            except Exception as e:
                db.execute("UPDATE files SET status='error', error=? WHERE id=?",
                           (f"{type(e).__name__}: {e}", clone["id"]))

        # 3. group
        _msg(job_id, "detecting duplicates…", 0.85)
        from ..engine import duplicates, recommend
        dstats = duplicates.rebuild_all()
        if fc:
            _msg(job_id, "clustering faces…", 0.9)
            faces.recluster()

        # 4. recommend
        _msg(job_id, "building recommendations…", 0.95)
        rstats = recommend.rebuild_all()

        db.execute("UPDATE jobs SET status='done', progress=1, finished_at=datetime('now'), message=? WHERE id=?",
                   (f"done — duplicates: {dstats}, recommendations: {rstats}", job_id))
    except InterruptedError:
        db.execute("UPDATE jobs SET status='cancelled', finished_at=datetime('now') WHERE id=?", (job_id,))
    except Exception:
        db.execute("UPDATE jobs SET status='error', finished_at=datetime('now'), message=? WHERE id=?",
                   (traceback.format_exc()[-1000:], job_id))


def _partition_exact(todo: list[dict]) -> tuple[list[dict], list[tuple[dict, int]]]:
    """Split files into representatives to analyze and exact copies that can
    inherit results, using provider checksums available before any transfer.
    Returns (reps, [(clone_row, rep_id), ...])."""
    import re
    from collections import defaultdict
    copy_pat = re.compile(r"(copy|copie|duplicate|\(\d+\))", re.I)
    groups = defaultdict(list)
    reps, clones = [], []
    for f in todo:
        if f["md5"] and f["size"]:
            groups[(f["md5"], f["size"])].append(f)
        else:
            reps.append(f)
    for group in groups.values():
        # bytes are identical, so prefer the original-looking name / earliest
        # timestamp purely for which row carries the analysis
        group.sort(key=lambda f: (bool(copy_pat.search(f["name"] or "")),
                                  f["created_time"] or "9999"))
        reps.append(group[0])
        clones.extend((c, group[0]["id"]) for c in group[1:])
    return reps, clones


def _inherit_analysis(source, clone: dict, rep_id: int, ml: bool, fc: bool):
    """Copy analysis results from an identical-bytes representative."""
    rep = db.row("SELECT * FROM files WHERE id=?", (rep_id,))
    if not rep or rep["status"] != "analyzed":
        _analyze_one(source, clone, ml, fc)   # rep failed; analyze normally
        return
    db.execute(
        "UPDATE files SET status='analyzed', sha256=?, phash=?, quality=?, width=?, height=?, "
        "duration=?, thumb_path=?, taken_time=coalesce(taken_time, ?), summary=? WHERE id=?",
        (rep["sha256"], rep["phash"], rep["quality"], rep["width"], rep["height"],
         rep["duration"], rep["thumb_path"], rep["taken_time"],
         f"Exact copy of '{rep['name']}' (checksum match) — analysis shared, no download needed.",
         clone["id"]))
    for c in db.rows("SELECT label, score, method FROM classifications WHERE file_id=?", (rep_id,)):
        classify.store(clone["id"], [(c["label"], c["score"])], c["method"])


def _download_capped(source, f: dict) -> Path | None:
    """Full download honoring the size cap; None means skipped."""
    if f["size"] and f["size"] > config.MAX_DOWNLOAD_BYTES:
        db.execute("UPDATE files SET status='analyzed', "
                   "summary='skipped: exceeds size cap (DC_MAX_DOWNLOAD_MB)' WHERE id=?", (f["id"],))
        return None
    ext = Path(f["name"] or "x").suffix or ".bin"
    return source.download(f["source_id"], config.CACHE_DIR / f"{f['id']}{ext}")


def _analyze_one(source, f: dict, ml: bool, fc: bool):
    """Analyze one file with minimal transfer/disk:
    - photos on remote sources: analyzed from a small preview when the source
      offers one (Drive: ~1600px, a few hundred KB); exact-dup detection then
      relies on the provider md5 already in metadata.
    - videos: streamed directly to ffmpeg when the source supports it (Drive
      does) — nothing touches disk; falls back to download-analyze-delete.
    """
    fid = f["id"]
    local = Path(f["local_path"]) if f["local_path"] else None
    if local and not local.exists():
        local = None
    is_local_source = source.name == "localfs"
    fetched = None          # full download we may clean up afterwards
    updates = {"status": "analyzed"}
    # analysis may be a re-run of a file interrupted mid-way in a previous scan:
    # face rows are plain inserts, so clear them to keep re-analysis idempotent
    db.execute("DELETE FROM faces WHERE file_id=?", (fid,))

    if f["kind"] == "photo":
        is_preview = False
        if not local:
            preview = source.fetch_preview(f["source_id"], config.CACHE_DIR / f"{fid}_preview.jpg")
            if preview:
                local, is_preview = preview, True
            else:
                local = fetched = _download_capped(source, f)
                if not local:
                    return
        if not is_preview:
            updates["sha256"], updates["md5"] = media.file_hashes(local)
        updates["local_path"] = str(local)
        with media.open_image(local) as img:
            if not is_preview:
                updates["width"], updates["height"] = img.width, img.height
            true_mp = ((f["width"] or updates.get("width") or 0)
                       * (f["height"] or updates.get("height") or 0)) / 1e6
            updates["thumb_path"] = media.make_thumb(img, fid)
            updates["phash"] = media.phash(img)
            updates["quality"], _parts = media.quality_score(img, true_mp=true_mp or None)
            exif = media.extract_exif(img)
            for k in ("taken_time", "camera", "gps_lat", "gps_lon"):
                if exif.get(k) and not f.get(k):
                    updates[k] = exif[k]
            labels = classify.heuristics({**f, **updates}, exif.get("has_exif", False) or bool(f.get("camera")))
            classify.store(fid, labels, "heuristic")
            if ml:
                emb = clip_embed.embed_images([img])[0]
                db.execute("INSERT OR REPLACE INTO embeddings(file_id,model,dim,vec) VALUES(?,?,?,?)",
                           (fid, clip_embed.MODEL_NAME, clip_embed.DIM, clip_embed.to_blob(emb)))
                classify.store(fid, classify.clip_classify(emb), "clip")
            if fc:
                faces.detect(img, fid)
    else:
        streamed = False
        if not local:
            spec = source.stream_spec(f["source_id"])
            if spec:
                upd = video.analyze({**f, "id": fid}, spec)
                if not upd.pop("_failed", False):
                    updates.update(upd)
                    streamed = True   # no local bytes: dedup keys off provider md5
        if not streamed:
            if not local:
                local = fetched = _download_capped(source, f)
                if not local:
                    return
            updates["sha256"], updates["md5"] = media.file_hashes(local)
            updates["local_path"] = str(local)
            upd = video.analyze({**f, "id": fid}, local)
            upd.pop("_failed", None)
            updates.update(upd)
        labels = classify.heuristics(f, False)
        classify.store(fid, labels, "heuristic")

    # free disk: drop full downloads once analysis has extracted what it needs
    if fetched and not is_local_source and not config.KEEP_ORIGINALS:
        try:
            fetched.unlink(missing_ok=True)
        except OSError:
            pass
        updates["local_path"] = None

    sets = ", ".join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE files SET {sets} WHERE id=?", (*updates.values(), fid))
