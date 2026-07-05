"""Face detection + embedding (InsightFace buffalo_l) and clustering.
Lazy-loaded like CLIP. Clustering runs after a scan: DBSCAN over cosine
distance groups unknown faces; faces near a labeled person's centroid are
auto-assigned to that person."""
import json

import numpy as np

from .. import config, db
from .clip_embed import to_blob, from_blob

_state = {"tried": False, "app": None, "error": None}


def available() -> bool:
    _load()
    return _state["app"] is not None


def load_error() -> str | None:
    return _state["error"]


def _load():
    if _state["tried"]:
        return
    _state["tried"] = True
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _state["app"] = app
    except ImportError:
        _state["error"] = "ML extras not installed (pip install -r requirements-ml.txt)"
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"


def detect(pil_image, file_id: int, frame_time=None) -> int:
    """Detect faces in one image, store rows. Returns count."""
    img = np.asarray(pil_image.convert("RGB"))[:, :, ::-1]  # BGR for insightface
    found = _state["app"].get(img)
    n = 0
    for f in found:
        if f.det_score < 0.5:
            continue
        emb = f.normed_embedding.astype(np.float32)
        db.execute(
            "INSERT INTO faces(file_id, frame_time, bbox, det_score, vec) VALUES(?,?,?,?,?)",
            (file_id, frame_time, json.dumps([round(float(x), 1) for x in f.bbox]),
             float(f.det_score), to_blob(emb)), commit=False)
        n += 1
    db.get_db().commit()
    return n


def recluster():
    """Cluster all unlabeled faces; auto-assign faces matching labeled persons."""
    rows = db.rows("SELECT id, vec, person_id FROM faces")
    if not rows:
        return {"clusters": 0, "auto_assigned": 0}
    ids = [r["id"] for r in rows]
    X = np.stack([from_blob(r["vec"]) for r in rows])  # already L2-normalized

    # 1. auto-assign to labeled persons by centroid similarity
    assigned = 0
    persons = db.rows("SELECT id FROM persons")
    centroids = {}
    for p in persons:
        vecs = [from_blob(r["vec"]) for r in rows if r["person_id"] == p["id"]]
        if vecs:
            c = np.mean(vecs, axis=0)
            centroids[p["id"]] = c / np.linalg.norm(c)
    for i, r in enumerate(rows):
        if r["person_id"]:
            continue
        best_pid, best_sim = None, config.FACE_MATCH_SIM
        for pid, c in centroids.items():
            sim = float(X[i] @ c)
            if sim > best_sim:
                best_pid, best_sim = pid, sim
        if best_pid:
            db.execute("UPDATE faces SET person_id=? WHERE id=?", (best_pid, r["id"]), commit=False)
            assigned += 1

    # 2. cluster the rest
    unl = [i for i, r in enumerate(rows) if not r["person_id"]]
    n_clusters = 0
    if len(unl) >= 2:
        labels = _dbscan_cosine(X[unl], eps=config.FACE_CLUSTER_EPS, min_samples=2)
        for idx, lbl in zip(unl, labels):
            cid = int(lbl) + 1 if lbl >= 0 else None
            db.execute("UPDATE faces SET cluster_id=? WHERE id=?", (cid, ids[idx]), commit=False)
        n_clusters = int(labels.max()) + 1 if labels.size and labels.max() >= 0 else 0
    db.get_db().commit()
    return {"clusters": n_clusters, "auto_assigned": assigned}


def folder_like(folder: str) -> str:
    return folder.rstrip("/") + "/%"


def folder_clusters(folder: str) -> dict:
    """People (labeled + unlabeled clusters) appearing in files under a
    top-level folder (e.g. a year like '2019')."""
    like = folder_like(folder)
    persons = db.rows(
        "SELECT p.id, p.name, count(DISTINCT f.id) n_files, count(fa.id) n_faces, "
        "min(fa.id) sample_face_id "
        "FROM persons p JOIN faces fa ON fa.person_id=p.id "
        "JOIN files f ON f.id=fa.file_id "
        "WHERE f.path LIKE ? AND f.status != 'trashed' GROUP BY p.id ORDER BY n_files DESC",
        (like,))
    clusters = db.rows(
        "SELECT fa.cluster_id, count(DISTINCT f.id) n_files, count(fa.id) n_faces, "
        "min(fa.id) sample_face_id "
        "FROM faces fa JOIN files f ON f.id=fa.file_id "
        "WHERE fa.person_id IS NULL AND fa.cluster_id IS NOT NULL "
        "AND f.path LIKE ? AND f.status != 'trashed' "
        "GROUP BY fa.cluster_id ORDER BY n_files DESC", (like,))
    loners = db.row(
        "SELECT count(fa.id) n, count(DISTINCT f.id) nf FROM faces fa "
        "JOIN files f ON f.id=fa.file_id "
        "WHERE fa.person_id IS NULL AND fa.cluster_id IS NULL "
        "AND f.path LIKE ? AND f.status != 'trashed'", (like,))
    return {"persons": persons, "clusters": clusters,
            "unclustered_faces": loners["n"] or 0, "unclustered_files": loners["nf"] or 0}


def cleanup_candidates(folder: str, keep_clusters: list[int], keep_persons: list[int],
                       margin: float = 0.45, include_nofaces: bool = False) -> dict:
    """Files under `folder` that contain NO selected (known) person.

    A face counts as 'known' if it belongs to a selected cluster/person OR its
    embedding is within `margin` cosine similarity of ANY example face of the
    selected people (library-wide exemplars, not just this folder). The margin
    check deliberately errs toward keeping files: a borderline face means the
    file stays out of the delete list.
    """
    like = folder_like(folder)
    files = {f["id"]: f for f in db.rows(
        "SELECT id, name, kind, path, taken_time, created_time, quality FROM files "
        "WHERE path LIKE ? AND status != 'trashed'", (like,))}
    face_rows = db.rows(
        "SELECT fa.id, fa.file_id, fa.vec, fa.cluster_id, fa.person_id FROM faces fa "
        "JOIN files f ON f.id=fa.file_id WHERE f.path LIKE ? AND f.status != 'trashed'", (like,))

    keep_c, keep_p = set(keep_clusters or []), set(keep_persons or [])
    # library-wide exemplars of the selected people (helps across folders/years)
    keep_vecs = []
    if keep_c:
        keep_vecs += [from_blob(r["vec"]) for r in db.rows(
            f"SELECT vec FROM faces WHERE cluster_id IN ({','.join('?' * len(keep_c))})",
            tuple(keep_c))]
    if keep_p:
        keep_vecs += [from_blob(r["vec"]) for r in db.rows(
            f"SELECT vec FROM faces WHERE person_id IN ({','.join('?' * len(keep_p))})",
            tuple(keep_p))]
    K = np.stack(keep_vecs) if keep_vecs else None

    by_file: dict[int, list] = {}
    for r in face_rows:
        by_file.setdefault(r["file_id"], []).append(r)

    candidates, kept = [], 0
    for fid, faces_ in by_file.items():
        if fid not in files:
            continue
        known = False
        best_sim = 0.0
        for r in faces_:
            if r["cluster_id"] in keep_c or r["person_id"] in keep_p:
                known = True
                break
            if K is not None:
                sim = float((K @ from_blob(r["vec"])).max())
                best_sim = max(best_sim, sim)
                if sim >= margin:
                    known = True
                    break
        if known:
            kept += 1
        else:
            f = dict(files[fid])
            f["n_faces"] = len(faces_)
            f["closest_known_sim"] = round(best_sim, 2)
            candidates.append(f)

    noface_files = []
    if include_nofaces:
        noface_files = [dict(f, n_faces=0) for fid, f in files.items() if fid not in by_file]

    candidates.sort(key=lambda f: (f["path"] or "", f["name"] or ""))
    return {"candidates": candidates, "nofaces": noface_files,
            "kept_files": kept, "total_with_faces": len(by_file)}


def _dbscan_cosine(X: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    try:
        from sklearn.cluster import DBSCAN
        return DBSCAN(eps=eps, min_samples=min_samples, metric="cosine").fit(X).labels_
    except ImportError:
        return _greedy_cluster(X, eps, min_samples)


def _greedy_cluster(X: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """Fallback: greedy centroid clustering when scikit-learn isn't installed."""
    labels = np.full(len(X), -1, dtype=int)
    centroids: list[np.ndarray] = []
    members: list[list[int]] = []
    for i, v in enumerate(X):
        best, best_d = -1, eps
        for c_i, c in enumerate(centroids):
            d = 1.0 - float(v @ c / (np.linalg.norm(c) + 1e-9))
            if d < best_d:
                best, best_d = c_i, d
        if best >= 0:
            members[best].append(i)
            centroids[best] = X[members[best]].mean(axis=0)
        else:
            centroids.append(v.copy())
            members.append([i])
    for c_i, m in enumerate(members):
        if len(m) >= min_samples:
            for i in m:
                labels[i] = c_i
    # compact label ids
    uniq = sorted(set(labels[labels >= 0]))
    remap = {old: new for new, old in enumerate(uniq)}
    return np.array([remap.get(l, -1) for l in labels])
