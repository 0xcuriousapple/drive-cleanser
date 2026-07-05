"""Recommendation builder + feedback learning.

Collections: Keep, Review, Duplicate Candidates, Screenshots, Documents, Memes.
Every recommendation carries an action, a confidence, and a human-readable
explanation. Nothing here touches files — it only fills review queues.

Learning: per-collection accept/reject history shifts that collection's
confidence threshold. If the user keeps rejecting "Screenshots" suggestions,
the bar for future ones rises (and vice versa), and the applied adjustment is
mentioned in the explanation.
"""
from .. import db

BASE_THRESHOLDS = {"Screenshots": 0.6, "Documents": 0.6, "Memes": 0.65,
                   "Duplicate Candidates": 0.0, "Review": 0.0, "Keep": 0.0}
LABEL_TO_COLLECTION = {"screenshot": "Screenshots", "document": "Documents",
                       "receipt": "Documents", "meme": "Memes", "whiteboard": "Documents"}


def threshold(collection: str) -> tuple[float, str]:
    """Base threshold adjusted by user feedback. Returns (threshold, note)."""
    base = BASE_THRESHOLDS.get(collection, 0.6)
    r = db.row("SELECT sum(status='approved') a, sum(status='rejected') j FROM recommendations "
               "WHERE collection=? AND status IN ('approved','rejected','executed')", (collection,))
    a, j = (r["a"] or 0), (r["j"] or 0)
    if a + j < 5:
        return base, ""
    reject_rate = j / (a + j)
    adj = round((reject_rate - 0.5) * 0.3, 2)  # +/- 0.15 max
    if abs(adj) < 0.03:
        return base, ""
    note = (f" (threshold {'raised' if adj > 0 else 'lowered'} to {base + adj:.2f} based on "
            f"your past {a + j} decisions in this collection)")
    return base + adj, note


def rebuild_all():
    # keep decided recs (they are the learning signal + audit); rebuild pending ones
    db.execute("DELETE FROM recommendations WHERE status='pending'")
    stats = {}

    already = {r["file_id"] for r in db.rows(
        "SELECT DISTINCT file_id FROM recommendations WHERE status IN ('approved','rejected','executed','undone')")}

    # 1. duplicate candidates: every non-keep member of every group
    groups = db.rows("SELECT * FROM dup_groups")
    for g in groups:
        members = db.rows(
            "SELECT m.file_id, m.similarity, f.kind, f.name FROM dup_members m "
            "JOIN files f ON f.id=m.file_id WHERE m.group_id=? AND f.status != 'trashed'", (g["id"],))
        for m in members:
            if m["file_id"] == g["keep_file_id"] or m["file_id"] in already:
                continue
            conf = 0.99 if g["kind"] == "exact" else round(m["similarity"], 2)
            note = " Videos always require explicit review before any action." if m["kind"] == "video" else ""
            _add(m["file_id"], "Duplicate Candidates", "trash", conf,
                 f"{g['explanation']}. This copy ('{m['name']}') is the "
                 f"{'identical' if g['kind'] == 'exact' else 'near-duplicate'} version.{note}")
            stats["Duplicate Candidates"] = stats.get("Duplicate Candidates", 0) + 1

    dup_flagged = {r["file_id"] for r in db.rows(
        "SELECT file_id FROM recommendations WHERE collection='Duplicate Candidates' AND status='pending'")}

    # 2. classification-driven collections
    for coll in ("Screenshots", "Documents", "Memes"):
        thr, note = threshold(coll)
        labels = [l for l, c in LABEL_TO_COLLECTION.items() if c == coll]
        rows = db.rows(
            f"SELECT c.file_id, c.label, c.score, c.method FROM classifications c "
            f"JOIN files f ON f.id=c.file_id "
            f"WHERE c.label IN ({','.join('?' * len(labels))}) AND c.score >= ? AND f.status != 'trashed'",
            (*labels, thr))
        for r in rows:
            if r["file_id"] in already or r["file_id"] in dup_flagged:
                continue
            _add(r["file_id"], coll, "review", r["score"],
                 f"Classified as {r['label']} with {r['score']:.0%} confidence "
                 f"({'filename/metadata heuristic' if r['method'] == 'heuristic' else 'AI visual classification'})."
                 + note)
            stats[coll] = stats.get(coll, 0) + 1

    flagged = dup_flagged | {r["file_id"] for r in db.rows(
        "SELECT file_id FROM recommendations WHERE status='pending'")}

    # 3. Keep / Review for the rest
    files = db.rows("SELECT f.id, f.kind, f.quality, f.name, "
                    "(SELECT count(*) FROM faces WHERE file_id=f.id) nfaces "
                    "FROM files f WHERE f.status != 'trashed'")
    for f in files:
        if f["id"] in flagged or f["id"] in already:
            continue
        q = f["quality"]
        if f["nfaces"] > 0:
            _add(f["id"], "Keep", "keep", min(0.95, 0.7 + 0.05 * f["nfaces"]),
                 f"Contains {f['nfaces']} face(s) — likely a personal memory.")
            stats["Keep"] = stats.get("Keep", 0) + 1
        elif q is not None and q < 0.25:
            _add(f["id"], "Review", "review", round(1 - q, 2),
                 f"Low quality score ({q:.2f}) — possibly blurry, dark, or an accidental capture.")
            stats["Review"] = stats.get("Review", 0) + 1
        else:
            _add(f["id"], "Keep", "keep", 0.6, "No issues detected.")
            stats["Keep"] = stats.get("Keep", 0) + 1
    return stats


def _add(file_id, collection, action, confidence, explanation):
    db.execute("INSERT INTO recommendations(file_id, collection, action, confidence, explanation) "
               "VALUES(?,?,?,?,?)", (file_id, collection, action, round(confidence, 3), explanation),
               commit=False)
    db.get_db().commit()
