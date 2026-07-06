"""FastAPI app. Run:  uvicorn app.main:app --port 8500"""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db
from .engine import actions, search as search_engine
from .pipeline import runner, faces, clip_embed, video as video_mod

app = FastAPI(title="Drive Cleanser")
db.init_db()
# a scan thread dies with its server process — mark any orphaned job rows so
# the dashboard doesn't show a dead scan as still running
db.execute("UPDATE jobs SET status='interrupted', finished_at=datetime('now'), "
           "message=coalesce(message,'') || ' (server restarted)' WHERE status='running'")


# ---------- status / sources ----------
@app.get("/api/status")
def status():
    counts = db.row(
        "SELECT count(*) total, sum(kind='photo') photos, sum(kind='video') videos, "
        "sum(status='analyzed') analyzed, sum(status='error') errors, "
        "sum(status='trashed') trashed FROM files") or {}
    job = db.row("SELECT * FROM jobs ORDER BY id DESC LIMIT 1")
    recs = {r["collection"]: r["n"] for r in db.rows(
        "SELECT collection, count(*) n FROM recommendations WHERE status='pending' GROUP BY collection")}
    gd = {"connected": False, "write": False}
    try:
        from .sources.gdrive import GDriveSource
        gd = GDriveSource().status()
    except Exception:
        pass
    return {"files": counts, "job": job, "pending_recommendations": recs,
            "capabilities": {"clip": clip_embed.available(), "faces": faces.available(),
                             "ffmpeg": video_mod.ffmpeg_available(),
                             "clip_error": clip_embed.load_error(),
                             "faces_error": faces.load_error()},
            "gdrive": gd, "localfs_root": db.get_setting("localfs_root")}


class ConnectBody(BaseModel):
    write: bool = False


@app.post("/api/sources/gdrive/connect")
def gdrive_connect(body: ConnectBody):
    from .sources.gdrive import GDriveSource
    try:
        return GDriveSource().connect(write=body.write)
    except Exception as e:
        raise HTTPException(400, str(e))


class LocalFSBody(BaseModel):
    root: str


@app.post("/api/sources/localfs")
def set_localfs(body: LocalFSBody):
    p = Path(body.root).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {p}")
    db.set_setting("localfs_root", str(p))
    return {"root": str(p)}


# ---------- scan ----------
class ScanBody(BaseModel):
    source: str = "gdrive"
    max_files: int | None = None


@app.post("/api/scan")
def scan(body: ScanBody):
    try:
        return {"job_id": runner.start_scan(body.source, body.max_files)}
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/scan/cancel")
def scan_cancel():
    runner.cancel()
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def job(job_id: int):
    j = db.row("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise HTTPException(404, "no such job")
    return j


# ---------- library ----------
@app.get("/api/files")
def files(kind: str | None = None, status: str | None = None, label: str | None = None,
          page: int = 0, page_size: int = Query(60, le=200)):
    where, params = ["1=1"], []
    if kind:
        where.append("f.kind=?"); params.append(kind)
    if status:
        where.append("f.status=?"); params.append(status)
    else:
        where.append("f.status != 'trashed'")
    join = ""
    if label:
        join = "JOIN classifications c ON c.file_id=f.id AND c.label=?"
        params.insert(0, label)
    rows = db.rows(
        f"SELECT DISTINCT f.id, f.name, f.kind, f.size, f.width, f.height, f.duration, "
        f"f.taken_time, f.created_time, f.quality, f.status, f.summary FROM files f {join} "
        f"WHERE {' AND '.join(where)} ORDER BY coalesce(f.taken_time, f.created_time) DESC "
        f"LIMIT ? OFFSET ?", (*params, page_size, page * page_size))
    return {"files": rows, "page": page}


@app.get("/api/files/{file_id}")
def file_detail(file_id: int):
    f = db.row("SELECT * FROM files WHERE id=?", (file_id,))
    if not f:
        raise HTTPException(404, "no such file")
    f.pop("local_path", None)
    f["labels"] = db.rows("SELECT label, score, method FROM classifications WHERE file_id=? ORDER BY score DESC",
                          (file_id,))
    f["faces"] = db.rows(
        "SELECT fa.id, fa.bbox, fa.cluster_id, fa.person_id, fa.frame_time, p.name person_name "
        "FROM faces fa LEFT JOIN persons p ON p.id=fa.person_id WHERE fa.file_id=?", (file_id,))
    f["recommendations"] = db.rows("SELECT * FROM recommendations WHERE file_id=? ORDER BY id DESC", (file_id,))
    if f["kind"] == "video":
        f["frames"] = db.rows("SELECT t, sharpness, is_representative FROM video_frames "
                              "WHERE file_id=? ORDER BY t", (file_id,))
    return f


@app.get("/api/thumb/{file_id}")
def thumb(file_id: int):
    f = db.row("SELECT thumb_path, kind, source, source_id, local_path FROM files WHERE id=?",
               (file_id,))
    if f and f["thumb_path"] and Path(f["thumb_path"]).exists():
        return FileResponse(f["thumb_path"], media_type="image/jpeg")
    # not analyzed yet, but the original is on disk -> make the thumb on demand
    if f and f["kind"] == "photo":
        src = f["local_path"] or (f["source_id"] if f["source"] == "localfs" else None)
        if src and Path(src).exists():
            try:
                from .pipeline import media as media_mod
                with media_mod.open_image(Path(src)) as img:
                    tp = media_mod.make_thumb(img, file_id)
                db.execute("UPDATE files SET thumb_path=? WHERE id=?", (tp, file_id))
                return FileResponse(tp, media_type="image/jpeg")
            except Exception:
                pass
    raise HTTPException(404, "no thumbnail")


@app.get("/api/media/{file_id}")
def media_file(file_id: int):
    f = db.row("SELECT local_path, mime, name FROM files WHERE id=?", (file_id,))
    if f and f["local_path"] and Path(f["local_path"]).exists():
        return FileResponse(f["local_path"], media_type=f["mime"], filename=f["name"])
    raise HTTPException(404, "no local copy cached")


# ---------- duplicates ----------
@app.get("/api/duplicates")
def duplicates(kind: str | None = None):
    where, params = "", ()
    if kind:
        where, params = "WHERE g.kind=?", (kind,)
    groups = db.rows(f"SELECT * FROM dup_groups g {where} ORDER BY g.id", params)
    out = []
    for g in groups:
        # trashed members drop out live; a group with <2 surviving copies is
        # no longer a duplicate problem and disappears from the list
        g["members"] = db.rows(
            "SELECT m.file_id, m.similarity, f.name, f.kind, f.size, f.width, f.height, "
            "f.quality, f.taken_time, f.status FROM dup_members m JOIN files f ON f.id=m.file_id "
            "WHERE m.group_id=? AND f.status != 'trashed' ORDER BY m.similarity DESC", (g["id"],))
        if len(g["members"]) >= 2:
            out.append(g)
    return {"groups": out}


def _live_members(group_id: int) -> list[dict]:
    return db.rows(
        "SELECT m.file_id, m.similarity, f.name, f.kind FROM dup_members m "
        "JOIN files f ON f.id=m.file_id "
        "WHERE m.group_id=? AND f.status != 'trashed' ORDER BY m.similarity DESC", (group_id,))


def _trash_others(group_id: int, keep_file_id: int) -> list[int]:
    """Approved+executed trash recs for every live member except the keeper."""
    rec_ids = []
    keep_name = db.row("SELECT name FROM files WHERE id=?", (keep_file_id,))["name"]
    for m in _live_members(group_id):
        if m["file_id"] == keep_file_id:
            continue
        cur = db.execute(
            "INSERT INTO recommendations(file_id, collection, action, confidence, explanation, "
            "status, decided_at) VALUES(?,?,?,?,?,'approved',datetime('now'))",
            (m["file_id"], "Duplicate Candidates", "trash", 1.0,
             f"User resolved duplicate group {group_id}: keeping '{keep_name}'."))
        rec_ids.append(cur.lastrowid)
    return rec_ids


class ResolveBody(BaseModel):
    keep_file_id: int


@app.post("/api/duplicates/{group_id}/resolve")
def resolve_group(group_id: int, body: ResolveBody):
    """Keep exactly one member; trash the rest (undoable, audited)."""
    members = _live_members(group_id)
    if len(members) < 2:
        raise HTTPException(400, "group has fewer than 2 remaining files")
    if body.keep_file_id not in {m["file_id"] for m in members}:
        raise HTTPException(400, "keep_file_id is not a live member of this group")
    db.execute("UPDATE dup_groups SET keep_file_id=? WHERE id=?", (body.keep_file_id, group_id))
    return actions.execute(_trash_others(group_id, body.keep_file_id))


class ResolveAllBody(BaseModel):
    kind: str | None = None


@app.post("/api/duplicates/resolve-all")
def resolve_all(body: ResolveAllBody):
    """Resolve every group (of the given kind) using its current keeper choice."""
    where, params = "", ()
    if body.kind:
        where, params = "WHERE kind=?", (body.kind,)
    results = {"groups_resolved": 0, "trashed": 0, "errors": 0, "skipped_groups": 0}
    for g in db.rows(f"SELECT id, keep_file_id FROM dup_groups {where}", params):
        members = _live_members(g["id"])
        if len(members) < 2:
            continue
        live_ids = {m["file_id"] for m in members}
        keeper = g["keep_file_id"] if g["keep_file_id"] in live_ids else members[0]["file_id"]
        res = actions.execute(_trash_others(g["id"], keeper))
        results["groups_resolved"] += 1
        results["trashed"] += len(res["executed"])
        results["errors"] += len(res["errors"])
    return results


class KeepBody(BaseModel):
    file_id: int


@app.post("/api/duplicates/{group_id}/keep")
def set_keep(group_id: int, body: KeepBody):
    """User overrides which file to keep — regenerates that group's pending recs."""
    g = db.row("SELECT * FROM dup_groups WHERE id=?", (group_id,))
    if not g:
        raise HTTPException(404, "no such group")
    db.execute("UPDATE dup_groups SET keep_file_id=?, explanation=explanation || ' (keep choice set by user)' "
               "WHERE id=?", (body.file_id, group_id))
    member_ids = [m["file_id"] for m in db.rows("SELECT file_id FROM dup_members WHERE group_id=?", (group_id,))]
    qmarks = ",".join("?" * len(member_ids))
    db.execute(f"DELETE FROM recommendations WHERE status='pending' AND collection='Duplicate Candidates' "
               f"AND file_id IN ({qmarks})", tuple(member_ids))
    from .engine import recommend
    recommend.rebuild_all()
    return {"ok": True}


# ---------- people ----------
@app.get("/api/people")
def people():
    persons = db.rows(
        "SELECT p.id, p.name, p.cover_face_id, count(fa.id) n_faces, "
        "count(DISTINCT fa.file_id) n_files FROM persons p "
        "LEFT JOIN faces fa ON fa.person_id=p.id GROUP BY p.id")
    clusters = db.rows(
        "SELECT cluster_id, count(*) n_faces, count(DISTINCT file_id) n_files, min(id) sample_face_id "
        "FROM faces WHERE person_id IS NULL AND cluster_id IS NOT NULL "
        "GROUP BY cluster_id ORDER BY n_faces DESC")
    for c in clusters:
        c["sample_file_id"] = db.row("SELECT file_id FROM faces WHERE id=?",
                                     (c["sample_face_id"],))["file_id"]
    return {"persons": persons, "unlabeled_clusters": clusters}


class LabelBody(BaseModel):
    cluster_id: int
    name: str


@app.post("/api/people/label")
def label_cluster(body: LabelBody):
    """Label a cluster once -> every face in it (and future matches) gets tagged."""
    p = db.row("SELECT id FROM persons WHERE name=?", (body.name,))
    if p:
        pid = p["id"]
    else:
        pid = db.execute("INSERT INTO persons(name) VALUES(?)", (body.name,)).lastrowid
    db.execute("UPDATE faces SET person_id=?, cluster_id=NULL WHERE cluster_id=?", (pid, body.cluster_id))
    cover = db.row("SELECT id FROM faces WHERE person_id=? ORDER BY det_score DESC", (pid,))
    if cover:
        db.execute("UPDATE persons SET cover_face_id=? WHERE id=?", (cover["id"], pid))
    faces.recluster()  # newly labeled centroid may absorb more faces
    n = db.row("SELECT count(DISTINCT file_id) n FROM faces WHERE person_id=?", (pid,))["n"]
    return {"person_id": pid, "files_tagged": n}


@app.get("/api/people/{person_id}/files")
def person_files(person_id: int):
    return {"files": db.rows(
        "SELECT DISTINCT f.id, f.name, f.kind, f.taken_time FROM faces fa "
        "JOIN files f ON f.id=fa.file_id WHERE fa.person_id=? AND f.status != 'trashed'", (person_id,))}


# ---------- cleanup by people (per-folder face filter) ----------
@app.get("/api/face/{face_id}/crop")
def face_crop(face_id: int):
    """Cropped face image for cluster review. Cached in data/faces/."""
    import json as _json
    import subprocess
    from PIL import Image
    from .pipeline import media as media_mod

    out = config.DATA_DIR / "faces" / f"{face_id}.jpg"
    if out.exists():
        return FileResponse(out, media_type="image/jpeg")
    fa = db.row("SELECT fa.*, f.local_path, f.thumb_path, f.kind FROM faces fa "
                "JOIN files f ON f.id=fa.file_id WHERE fa.id=?", (face_id,))
    if not fa:
        raise HTTPException(404, "no such face")
    out.parent.mkdir(parents=True, exist_ok=True)
    src_img = None
    try:
        if fa["kind"] == "video" and fa["local_path"] and Path(fa["local_path"]).exists():
            tmp = config.DATA_DIR / "faces" / f"{face_id}_frame.jpg"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(fa["frame_time"] or 0),
                            "-i", fa["local_path"], "-frames:v", "1", "-y", str(tmp)],
                           capture_output=True, timeout=60)
            if tmp.exists():
                src_img = Image.open(tmp)
        elif fa["local_path"] and Path(fa["local_path"]).exists():
            src_img = media_mod.open_image(Path(fa["local_path"]))
        if src_img is None and fa["thumb_path"] and Path(fa["thumb_path"]).exists():
            return FileResponse(fa["thumb_path"], media_type="image/jpeg")
        if src_img is None:
            raise HTTPException(404, "no source image available")
        x1, y1, x2, y2 = _json.loads(fa["bbox"])
        # pad the box 35% for context
        w, h = x2 - x1, y2 - y1
        x1, y1 = max(0, x1 - 0.35 * w), max(0, y1 - 0.35 * h)
        x2, y2 = min(src_img.width, x2 + 0.35 * w), min(src_img.height, y2 + 0.35 * h)
        crop = src_img.convert("RGB").crop((x1, y1, x2, y2))
        crop.thumbnail((160, 160))
        crop.save(out, "JPEG", quality=88)
    finally:
        if src_img:
            src_img.close()
        (config.DATA_DIR / "faces" / f"{face_id}_frame.jpg").unlink(missing_ok=True)
    return FileResponse(out, media_type="image/jpeg")


@app.get("/api/cleanup/folders")
def cleanup_folders():
    """Top-level folders (e.g. years) under the scanned root, with face stats."""
    rows = db.rows("SELECT path, id FROM files WHERE status != 'trashed' AND path IS NOT NULL")
    folders = {}
    for r in rows:
        top = (r["path"] or "").split("/")[0]
        if top:
            folders.setdefault(top, {"files": 0})["files"] += 1
    out = []
    for name in sorted(folders):
        st = db.row(
            "SELECT count(DISTINCT fa.file_id) with_faces FROM faces fa "
            "JOIN files f ON f.id=fa.file_id WHERE f.path LIKE ? AND f.status != 'trashed'",
            (name + "/%",))
        out.append({"folder": name, "files": folders[name]["files"],
                    "files_with_faces": st["with_faces"] or 0})
    return {"folders": out}


@app.get("/api/cleanup/clusters")
def cleanup_clusters(folder: str):
    return faces.folder_clusters(folder)


class CandidatesBody(BaseModel):
    folder: str
    keep_clusters: list[int] = []
    keep_persons: list[int] = []
    margin: float = 0.45
    include_nofaces: bool = False


@app.post("/api/cleanup/candidates")
def cleanup_candidates(body: CandidatesBody):
    if not body.keep_clusters and not body.keep_persons:
        raise HTTPException(400, "select at least one known person/cluster first")
    return faces.cleanup_candidates(body.folder, body.keep_clusters, body.keep_persons,
                                    margin=body.margin, include_nofaces=body.include_nofaces)


class CleanupTrashBody(BaseModel):
    file_ids: list[int]


@app.post("/api/cleanup/trash")
def cleanup_trash(body: CleanupTrashBody):
    """Move selected files to the undoable local trash, with full audit trail."""
    if not body.file_ids:
        raise HTTPException(400, "file_ids is empty")
    rec_ids = []
    for fid in body.file_ids:
        f = db.row("SELECT id, source, status FROM files WHERE id=?", (fid,))
        if not f or f["status"] == "trashed":
            continue
        cur = db.execute(
            "INSERT INTO recommendations(file_id, collection, action, confidence, explanation, "
            "status, decided_at) VALUES(?,?,?,?,?,'approved',datetime('now'))",
            (fid, "People Filter", "trash", 1.0,
             "User-selected in people-filter cleanup: contains no selected known person."))
        rec_ids.append(cur.lastrowid)
    return actions.execute(rec_ids)


# ---------- search ----------
@app.get("/api/search")
def search(q: str, limit: int = 60):
    return {"results": search_engine.search(q, limit)}


# ---------- recommendations / review ----------
@app.get("/api/collections")
def collections():
    return {"collections": db.rows(
        "SELECT collection, status, count(*) n, avg(confidence) avg_confidence "
        "FROM recommendations GROUP BY collection, status")}


@app.get("/api/recommendations")
def recommendations(collection: str | None = None, status: str = "pending",
                    page: int = 0, page_size: int = Query(60, le=200)):
    where, params = ["r.status=?"], [status]
    if collection:
        where.append("r.collection=?"); params.append(collection)
    if status == "pending":
        where.append("f.status != 'trashed'")   # queues shrink live as files are trashed
    rows = db.rows(
        f"SELECT r.*, f.name, f.kind, f.size, f.width, f.height, f.taken_time, f.quality "
        f"FROM recommendations r JOIN files f ON f.id=r.file_id WHERE {' AND '.join(where)} "
        f"ORDER BY r.confidence DESC LIMIT ? OFFSET ?", (*params, page_size, page * page_size))
    return {"recommendations": rows}


class DecideBody(BaseModel):
    decision: str  # 'approve' | 'reject'


@app.post("/api/recommendations/{rec_id}/decide")
def decide(rec_id: int, body: DecideBody):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    r = db.row("SELECT status FROM recommendations WHERE id=?", (rec_id,))
    if not r:
        raise HTTPException(404, "no such recommendation")
    if r["status"] not in ("pending", "approved", "rejected"):
        raise HTTPException(409, f"cannot change a recommendation in status '{r['status']}'")
    db.execute("UPDATE recommendations SET status=?, decided_at=datetime('now') WHERE id=?",
               (body.decision + "d" if body.decision == "approve" else "rejected", rec_id))
    return {"ok": True}


# ---------- execution (approved only) ----------
class ExecuteBody(BaseModel):
    rec_ids: list[int]


@app.post("/api/actions/execute")
def execute(body: ExecuteBody):
    if not body.rec_ids:
        raise HTTPException(400, "rec_ids is empty")
    return actions.execute(body.rec_ids)


@app.get("/api/actions")
def actions_log():
    return {"actions": db.rows("SELECT * FROM actions_log ORDER BY id DESC LIMIT 200")}


@app.post("/api/actions/{action_id}/undo")
def undo(action_id: int):
    try:
        return actions.undo(action_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- deletion record (permanent registry of what was removed) ----------
def _trashed_rows():
    """One row per trashed file with its latest trash action + reason."""
    return db.rows("""
        SELECT f.name, f.path, f.kind, f.size, f.md5, f.sha256, f.phash,
               a.executed_at AS deleted_at,
               coalesce(r.collection, 'manual') AS reason,
               r.explanation AS detail
        FROM files f
        LEFT JOIN actions_log a ON a.id = (
            SELECT max(a2.id) FROM actions_log a2
            WHERE a2.file_id = f.id AND a2.undone_at IS NULL)
        LEFT JOIN recommendations r ON r.id = a.rec_id
        WHERE f.status = 'trashed'
        ORDER BY f.path""")


@app.get("/api/record/summary")
def record_summary():
    rows = _trashed_rows()
    by_reason = {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    trash_dir = config.LOCAL_TRASH_DIR
    disk = sum(p.stat().st_size for p in trash_dir.rglob("*") if p.is_file()) if trash_dir.exists() else 0
    return {"deleted_files": len(rows),
            "deleted_bytes": sum(r["size"] or 0 for r in rows),
            "by_reason": by_reason,
            "with_checksum": sum(1 for r in rows if r["md5"] or r["sha256"]),
            "local_trash_dir": str(trash_dir),
            "local_trash_bytes": disk,
            "export_dir": db.get_setting("localfs_root") or str(config.DATA_DIR)}


class RecordExportBody(BaseModel):
    download_report: str | None = None   # optional path to skipped-duplicates.jsonl


@app.post("/api/record/export")
def record_export(body: RecordExportBody):
    import csv
    import json as _json
    import sqlite3 as _sq
    root = Path(db.get_setting("localfs_root") or config.DATA_DIR)
    rows = _trashed_rows()

    # duplicates never downloaded (from the download phase), if a report is given
    skipped = []
    if body.download_report:
        rp = Path(body.download_report).expanduser()
        if not rp.exists():
            raise HTTPException(400, f"report not found: {rp}")
        for line in rp.read_text().splitlines():
            try:
                skipped.append(_json.loads(line))
            except ValueError:
                continue

    sq_path = root / "deleted-record.sqlite"
    csv_path = root / "deleted-record.csv"
    sq_path.unlink(missing_ok=True)
    con = _sq.connect(sq_path)
    con.executescript("""
        CREATE TABLE deleted (
          name TEXT, path TEXT, kind TEXT, size INTEGER,
          md5 TEXT, sha256 TEXT, phash TEXT,
          deleted_at TEXT, reason TEXT, detail TEXT);
        CREATE INDEX idx_deleted_md5 ON deleted(md5);
        CREATE INDEX idx_deleted_sha ON deleted(sha256);
        CREATE TABLE skipped_duplicates (
          drive_path TEXT, md5 TEXT, size INTEGER, kept_copy TEXT);
        CREATE INDEX idx_skip_md5 ON skipped_duplicates(md5);
    """)
    con.executemany("INSERT INTO deleted VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(r["name"], r["path"], r["kind"], r["size"], r["md5"], r["sha256"],
                      r["phash"], r["deleted_at"], r["reason"], r["detail"]) for r in rows])
    con.executemany("INSERT INTO skipped_duplicates VALUES(?,?,?,?)",
                    [(s.get("drive_path"), s.get("md5"), s.get("size"), s.get("kept_copy"))
                     for s in skipped])
    con.commit()
    con.close()

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "path", "kind", "size", "md5", "sha256", "phash",
                    "deleted_at", "reason", "detail"])
        for r in rows:
            w.writerow([r["name"], r["path"], r["kind"], r["size"], r["md5"], r["sha256"],
                        r["phash"], r["deleted_at"], r["reason"], r["detail"]])

    # duplication record: every group member ever detected, keeper + fate included
    dup_rows = db.rows("""
        SELECT g.id AS group_id, g.kind,
               (m.file_id = g.keep_file_id) AS is_keeper,
               m.similarity, f.name, f.path, f.kind AS file_kind, f.size,
               f.md5, f.sha256, f.phash,
               CASE f.status WHEN 'trashed' THEN 'trashed' ELSE 'kept' END AS fate
        FROM dup_groups g
        JOIN dup_members m ON m.group_id = g.id
        JOIN files f ON f.id = m.file_id
        ORDER BY g.id, is_keeper DESC, m.similarity DESC""")
    dsq_path = root / "duplication-record.sqlite"
    dcsv_path = root / "duplication-record.csv"
    dsq_path.unlink(missing_ok=True)
    con = _sq.connect(dsq_path)
    con.executescript("""
        CREATE TABLE duplicates (
          group_id INTEGER, kind TEXT, is_keeper INTEGER, similarity REAL,
          name TEXT, path TEXT, file_kind TEXT, size INTEGER,
          md5 TEXT, sha256 TEXT, phash TEXT, fate TEXT);
        CREATE INDEX idx_dup_md5 ON duplicates(md5);
        CREATE INDEX idx_dup_sha ON duplicates(sha256);
        CREATE INDEX idx_dup_group ON duplicates(group_id);
    """)
    con.executemany("INSERT INTO duplicates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(r["group_id"], r["kind"], r["is_keeper"], r["similarity"], r["name"],
                      r["path"], r["file_kind"], r["size"], r["md5"], r["sha256"], r["phash"],
                      r["fate"]) for r in dup_rows])
    con.commit()
    con.close()
    with open(dcsv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["group_id", "kind", "is_keeper", "similarity", "name", "path",
                    "file_kind", "size", "md5", "sha256", "phash", "fate"])
        for r in dup_rows:
            w.writerow([r["group_id"], r["kind"], r["is_keeper"], r["similarity"], r["name"],
                        r["path"], r["file_kind"], r["size"], r["md5"], r["sha256"], r["phash"],
                        r["fate"]])

    return {"deleted_rows": len(rows), "skipped_duplicate_rows": len(skipped),
            "duplication_rows": len(dup_rows),
            "sqlite": str(sq_path), "csv": str(csv_path),
            "dup_sqlite": str(dsq_path), "dup_csv": str(dcsv_path)}


# ---------- static UI (mounted last so /api wins) ----------
@app.middleware("http")
async def no_ui_cache(request, call_next):
    """UI files must never be cached — stale app.js against a new API is the
    classic 'why is the page old' bug. API responses are dynamic anyway."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
