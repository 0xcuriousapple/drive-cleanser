"""Natural-language search: CLIP text embedding vs stored image/video
embeddings, hybridized with person-name and year filters parsed from the
query. Falls back to keyword search when ML isn't installed."""
import re

import numpy as np

from .. import db
from ..pipeline import clip_embed


def search(q: str, limit: int = 60) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    person_ids, year, residual = _parse(q)

    candidate_ids = None
    if person_ids:
        rows = db.rows(f"SELECT DISTINCT file_id FROM faces WHERE person_id IN "
                       f"({','.join('?' * len(person_ids))})", tuple(person_ids))
        candidate_ids = {r["file_id"] for r in rows}
    if year:
        rows = db.rows("SELECT id FROM files WHERE substr(coalesce(taken_time, created_time),1,4)=?",
                       (str(year),))
        ids = {r["id"] for r in rows}
        candidate_ids = ids if candidate_ids is None else (candidate_ids & ids)

    if clip_embed.available() and residual:
        emb_rows = db.rows("SELECT e.file_id, e.vec FROM embeddings e JOIN files f ON f.id=e.file_id "
                           "WHERE f.status != 'trashed'")
        if emb_rows:
            tvec = clip_embed.embed_text([residual])[0]
            fids = [r["file_id"] for r in emb_rows]
            X = np.stack([clip_embed.from_blob(r["vec"]) for r in emb_rows])
            sims = X @ tvec
            order = np.argsort(-sims)
            out = []
            for i in order:
                fid = fids[i]
                if candidate_ids is not None and fid not in candidate_ids:
                    continue
                if sims[i] < 0.18 and candidate_ids is None:
                    break
                out.append({"file_id": fid, "score": round(float(sims[i]), 3)})
                if len(out) >= limit:
                    break
            return _hydrate(out)

    # fallback: keyword / filter-only search
    if candidate_ids is not None and not residual:
        return _hydrate([{"file_id": i, "score": 1.0} for i in list(candidate_ids)[:limit]])
    like = f"%{residual or q}%"
    rows = db.rows(
        "SELECT DISTINCT f.id FROM files f LEFT JOIN classifications c ON c.file_id=f.id "
        "WHERE f.status != 'trashed' AND (f.name LIKE ? OR f.summary LIKE ? OR c.label LIKE ?) LIMIT ?",
        (like, like, like, limit))
    ids = [r["id"] for r in rows]
    if candidate_ids is not None:
        ids = [i for i in ids if i in candidate_ids]
    return _hydrate([{"file_id": i, "score": 0.5} for i in ids])


def _parse(q: str):
    person_ids, year = [], None
    residual = q
    m = re.search(r"\b(19\d{2}|20\d{2})\b", residual)
    if m:
        year = int(m.group(1))
        residual = (residual[:m.start()] + residual[m.end():]).strip(" ,")
    for p in db.rows("SELECT id, name FROM persons"):
        if p["name"] and re.search(rf"\b{re.escape(p['name'])}\b", residual, re.I):
            person_ids.append(p["id"])
            residual = re.sub(rf"\b{re.escape(p['name'])}\b", "person", residual, flags=re.I)
    return person_ids, year, residual.strip()


def _hydrate(hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        f = db.row("SELECT id, name, kind, taken_time, created_time, summary FROM files WHERE id=?",
                   (h["file_id"],))
        if f:
            f["score"] = h["score"]
            out.append(f)
    return out
