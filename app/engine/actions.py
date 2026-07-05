"""Execution engine — the ONLY code path that mutates anything in a source,
and it only runs on recommendation ids the user explicitly submitted for
execution after approving them. Everything is logged to actions_log with
undo_info, and 'delete' always means reversible trash (Drive trash / local
trash folder)."""
import json

from .. import db
from ..sources import get_source


def execute(rec_ids: list[int]) -> dict:
    results = {"executed": [], "skipped": [], "errors": []}
    for rid in rec_ids:
        rec = db.row("SELECT r.*, f.source, f.source_id, f.name, f.kind FROM recommendations r "
                     "JOIN files f ON f.id=r.file_id WHERE r.id=?", (rid,))
        if not rec:
            results["errors"].append({"rec_id": rid, "error": "not found"})
            continue
        if rec["status"] != "approved":
            results["skipped"].append({"rec_id": rid, "reason": f"status is '{rec['status']}', not approved"})
            continue
        if rec["action"] != "trash":
            results["skipped"].append({"rec_id": rid, "reason": "only 'trash' actions are executable"})
            continue
        try:
            src = get_source(rec["source"])
            undo_info = src.trash(rec["source_id"])
            db.execute("UPDATE recommendations SET status='executed' WHERE id=?", (rid,))
            db.execute("UPDATE files SET status='trashed' WHERE id=?", (rec["file_id"],))
            db.execute("INSERT INTO actions_log(rec_id, file_id, action, detail, undo_info) VALUES(?,?,?,?,?)",
                       (rid, rec["file_id"], "trash",
                        f"Trashed '{rec['name']}' ({rec['kind']}) on {rec['source']}",
                        json.dumps(undo_info)))
            results["executed"].append(rid)
        except Exception as e:
            results["errors"].append({"rec_id": rid, "error": str(e)})
    return results


def undo(action_id: int) -> dict:
    a = db.row("SELECT a.*, f.source, f.source_id FROM actions_log a "
               "JOIN files f ON f.id=a.file_id WHERE a.id=?", (action_id,))
    if not a:
        raise ValueError("action not found")
    if a["undone_at"]:
        raise ValueError("already undone")
    src = get_source(a["source"])
    src.restore(a["source_id"], json.loads(a["undo_info"] or "{}"))
    db.execute("UPDATE actions_log SET undone_at=datetime('now') WHERE id=?", (action_id,))
    db.execute("UPDATE files SET status='analyzed' WHERE id=?", (a["file_id"],))
    db.execute("UPDATE recommendations SET status='undone' WHERE id=?", (a["rec_id"],))
    return {"restored_file_id": a["file_id"]}
