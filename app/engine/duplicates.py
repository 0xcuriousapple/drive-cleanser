"""Duplicate detection.

- Exact: identical sha256 (or provider md5).
- Near (photos): perceptual-hash hamming distance <= PHASH_HAMMING_MAX.
  Candidate pairs come from 8-bit chunk bucketing (pigeonhole: any pair within
  hamming distance 7 shares at least one of 8 chunks), so we never do O(n^2)
  over the whole library.
- Videos: fraction of sampled frames (full duration) with a close hash match
  in the other video >= VIDEO_OVERLAP_MIN.

For each group we recommend keeping the best file (quality, resolution,
original timestamp, size) — recommendation only, never an action.
"""
from collections import defaultdict

from .. import config, db
from ..pipeline import media, video


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self):
        out = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return [g for g in out.values() if len(g) > 1]


def rebuild_all():
    db.execute("DELETE FROM dup_members")
    db.execute("DELETE FROM dup_groups")
    stats = {"exact": 0, "near": 0, "video": 0}

    files = db.rows("SELECT id, kind, sha256, md5, phash, quality, size, width, height, "
                    "taken_time, created_time, name, source FROM files WHERE status != 'trashed'")
    photos = [f for f in files if f["kind"] == "photo"]
    videos = [f for f in files if f["kind"] == "video"]

    # exact (photos and videos alike)
    by_hash = defaultdict(list)
    for f in files:
        # md5 preferred: Drive provides it in metadata (no download needed) and
        # local analysis computes it too, so the key is consistent across sources
        key = (f"md5:{f['md5']}" if f["md5"] else f["sha256"])
        if key:
            by_hash[key].append(f)
    exact_ids = set()          # non-keep members of exact groups
    for group in by_hash.values():
        if len(group) > 1:
            keep_id = _store_group(group, "exact", sim=1.0)
            stats["exact"] += 1
            # the keeper still participates in near-dup matching, so a resized
            # variant of an exactly-duplicated photo is still caught
            exact_ids.update(f["id"] for f in group if f["id"] != keep_id)

    # near (photos; exact-group non-keepers excluded to avoid double-flagging)
    uf = UnionFind()
    byid = {f["id"]: f for f in photos}
    buckets = defaultdict(list)
    for f in photos:
        if not f["phash"] or f["id"] in exact_ids:
            continue
        v = int(f["phash"], 16)
        for i in range(8):
            buckets[(i, (v >> (8 * i)) & 0xFF)].append(f["id"])
    seen_pairs = set()
    for ids in buckets.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair = (min(ids[i], ids[j]), max(ids[i], ids[j]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                d = media.hamming(byid[pair[0]]["phash"], byid[pair[1]]["phash"])
                if d <= config.PHASH_HAMMING_MAX:
                    uf.union(*pair)
    for g in uf.groups():
        _store_group([byid[i] for i in g], "near",
                     sim=round(1 - config.PHASH_HAMMING_MAX / 64, 3))
        stats["near"] += 1

    # video near-dups: frame-set overlap across full duration
    vhashes = {f["id"]: video.frame_hashes(f["id"]) for f in videos if f["id"] not in exact_ids}
    vids = [i for i, hs in vhashes.items() if hs]
    vuf = UnionFind()
    for i in range(len(vids)):
        for j in range(i + 1, len(vids)):
            ov = _overlap(vhashes[vids[i]], vhashes[vids[j]])
            if ov >= config.VIDEO_OVERLAP_MIN:
                vuf.union(vids[i], vids[j])
    vbyid = {f["id"]: f for f in videos}
    for g in vuf.groups():
        _store_group([vbyid[i] for i in g], "video", sim=config.VIDEO_OVERLAP_MIN)
        stats["video"] += 1
    return stats


def _overlap(h1: list[str], h2: list[str]) -> float:
    if not h1 or not h2:
        return 0.0
    short, long_ = (h1, h2) if len(h1) <= len(h2) else (h2, h1)
    long_ints = [int(h, 16) for h in long_]
    matched = 0
    for h in short:
        v = int(h, 16)
        if any(bin(v ^ w).count("1") <= config.VIDEO_FRAME_HAMMING_MAX for w in long_ints):
            matched += 1
    return matched / len(short)


COPY_NAME = None  # compiled lazily below


def _keep_score(f: dict) -> tuple:
    import re
    global COPY_NAME
    if COPY_NAME is None:
        COPY_NAME = re.compile(r"(copy|copie|duplicate|\(\d+\)| \d+\.\w+$)", re.I)
    mp = (f["width"] or 0) * (f["height"] or 0)
    looks_original = 0 if COPY_NAME.search(f["name"] or "") else 1
    # resolution dominates (the original is almost always the largest),
    # then quality, size, original-looking name, earliest timestamp
    return (mp, round(f["quality"] or 0, 2), f["size"] or 0, looks_original,
            -(_ts_rank(f)))


def _ts_rank(f: dict) -> float:
    t = f["taken_time"] or f["created_time"] or "9999"
    return sum(ord(c) for c in t[:10])  # cheap monotonic-ish ordering on ISO prefix


def _store_group(group: list[dict], kind: str, sim: float):
    keep = max(group, key=_keep_score)
    reasons = []
    if keep["quality"] is not None:
        reasons.append(f"highest quality score ({keep['quality']:.2f})")
    if keep["width"] and keep["height"]:
        reasons.append(f"largest resolution ({keep['width']}x{keep['height']})")
    if keep["taken_time"]:
        reasons.append("has original capture timestamp")
    expl = (f"{len(group)} {kind}-duplicate {'videos' if kind == 'video' else 'files'}; "
            f"suggest keeping '{keep['name']}' — " + ", ".join(reasons or ["largest file"]))
    cur = db.execute("INSERT INTO dup_groups(kind, keep_file_id, explanation) VALUES(?,?,?)",
                     (kind, keep["id"], expl))
    gid = cur.lastrowid
    for f in group:
        db.execute("INSERT INTO dup_members(group_id, file_id, similarity) VALUES(?,?,?)",
                   (gid, f["id"], 1.0 if f["id"] == keep["id"] else sim), commit=False)
    db.get_db().commit()
    return keep["id"]
