"""Full-duration video analysis via ffmpeg.

Per the spec, videos are sampled across their ENTIRE duration, not just a
thumbnail: one frame per interval (interval chosen so total frames <=
VIDEO_MAX_SAMPLED_FRAMES). Every sampled frame gets a perceptual hash and
sharpness score (used for video near-dup detection and representative-moment
picking). Representative frames additionally get face detection and CLIP
classification when the ML stack is installed, and feed the generated summary.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .. import config, db
from . import media, classify, clip_embed, faces


MediaSrc = Path | tuple  # local file, or (url, headers) streamed by ffmpeg


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _input_args(src: MediaSrc) -> tuple[list[str], str]:
    """(pre-input args, input target) for ffmpeg/ffprobe."""
    if isinstance(src, Path):
        return [], str(src)
    url, headers = src
    pre = []
    if headers:
        pre += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    return pre, url


def probe(src: MediaSrc) -> dict:
    """Return {duration, width, height} (missing keys if unreadable)."""
    pre, target = _input_args(src)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", *pre, "-print_format", "json", "-show_format",
             "-show_streams", target],
            capture_output=True, text=True, timeout=120)
        data = json.loads(out.stdout)
        info = {"duration": float(data["format"]["duration"])}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"], info["height"] = s.get("width"), s.get("height")
                break
        return info
    except Exception:
        return {}


def analyze(file_row: dict, src: MediaSrc) -> dict:
    """Sample frames across the whole video (local file or streamed URL);
    store per-frame rows; return updates for the files table. On failure the
    returned dict carries _failed=True so callers can fall back (e.g. from
    streaming to download)."""
    updates = {}
    if not ffmpeg_available():
        updates["summary"] = "Video not analyzed: ffmpeg not installed (brew install ffmpeg)."
        return updates

    info = probe(src)
    duration = info.get("duration") or file_row.get("duration")
    if not duration:
        updates["summary"] = "Video not analyzed: could not read duration."
        updates["_failed"] = True
        return updates
    updates["duration"] = duration
    if info.get("width"):
        updates["width"], updates["height"] = info["width"], info["height"]

    interval = max(1.0, duration / config.VIDEO_MAX_SAMPLED_FRAMES)
    file_id = file_row["id"]
    db.execute("DELETE FROM video_frames WHERE file_id=?", (file_id,))

    with tempfile.TemporaryDirectory(prefix="dcvid_") as td:
        tdir = Path(td)
        pre, target = _input_args(src)
        if interval <= 2.0:
            # short video: one sequential pass is cheapest
            cmd = ["ffmpeg", "-nostdin", "-v", "error", *pre, "-i", target,
                   "-vf", f"fps=1/{interval:g},scale='min(960,iw)':-2",
                   "-qscale:v", "3", str(tdir / "f%05d.jpg")]
            try:
                subprocess.run(cmd, capture_output=True, timeout=1800, check=True)
            except subprocess.SubprocessError as e:
                updates["summary"] = f"Video frame extraction failed: {e}"
                updates["_failed"] = True
                return updates
        else:
            # long video: seek straight to each sample time (keyframe seek reads
            # only seconds of stream per frame — a 4h video takes ~1-2 min, not
            # a full decode). 4 extractions in parallel.
            from concurrent.futures import ThreadPoolExecutor

            def grab(i_t):
                i, t = i_t
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", *pre, "-ss", f"{t:.2f}",
                     "-i", target, "-frames:v", "1", "-vf", "scale='min(960,iw)':-2",
                     "-qscale:v", "3", "-y", str(tdir / f"f{i:05d}.jpg")],
                    capture_output=True, timeout=120)

            n_samples = int(duration // interval)
            times = [(i, min(i * interval + interval / 2, max(0.0, duration - 1)))
                     for i in range(n_samples)]
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(grab, times))
        if not any(tdir.glob("f*.jpg")):
            updates["summary"] = "Video frame extraction produced no frames."
            updates["_failed"] = True
            return updates

        frames = sorted(tdir.glob("f*.jpg"))
        rows = []
        import numpy as np
        for fp in frames:
            try:
                idx = int(fp.stem[1:])
            except ValueError:
                continue
            # single-pass names are 1-based (%05d), seek names are 0-based
            t_frame = (idx - 1) * interval if interval <= 2.0 else idx * interval + interval / 2
            with Image.open(fp) as img:
                ph = media.phash(img)
                small = img.convert("L").copy()
                small.thumbnail((512, 512))
                sharp = media.sharpness(np.asarray(small, dtype=np.float32))
            rows.append({"t": round(max(0.0, t_frame), 2), "phash": ph, "sharpness": sharp, "path": fp})

        # representative = scene-distinct (hash jump vs previous kept frame), sharpest first as tiebreak
        reps, last_ph = [], None
        for r in rows:
            if last_ph is None or media.hamming(r["phash"], last_ph) > 10:
                reps.append(r)
                last_ph = r["phash"]
        reps = sorted(reps, key=lambda r: -r["sharpness"])[:config.VIDEO_FACE_FRAMES]
        rep_ts = {r["t"] for r in reps}

        for r in rows:
            db.execute("INSERT INTO video_frames(file_id,t,phash,sharpness,is_representative) VALUES(?,?,?,?,?)",
                       (file_id, r["t"], r["phash"], round(r["sharpness"], 1), int(r["t"] in rep_ts)),
                       commit=False)
        db.get_db().commit()

        # thumbnail = sharpest representative frame
        if reps:
            with Image.open(reps[0]["path"]) as img:
                updates["thumb_path"] = media.make_thumb(img, file_id)
                updates["quality"], _ = media.quality_score(img)
            updates["phash"] = reps[0]["phash"]  # coarse; video dedup uses the frame set

        # ML pass on representative frames
        labels_acc, n_faces = {}, 0
        if reps and clip_embed.available():
            imgs = [Image.open(r["path"]) for r in reps[:8]]
            embs = clip_embed.embed_images(imgs)
            for im in imgs:
                im.close()
            vid_emb = embs.mean(axis=0)
            vid_emb /= (np.linalg.norm(vid_emb) + 1e-9)
            db.execute("INSERT OR REPLACE INTO embeddings(file_id,model,dim,vec) VALUES(?,?,?,?)",
                       (file_id, clip_embed.MODEL_NAME, clip_embed.DIM, clip_embed.to_blob(vid_emb)))
            for emb in embs:
                for label, score in classify.clip_classify(emb):
                    labels_acc[label] = max(labels_acc.get(label, 0), score)
            classify.store(file_id, list(labels_acc.items()), "clip")
        if reps and faces.available():
            for r in reps:
                with Image.open(r["path"]) as img:
                    n_faces += faces.detect(img, file_id, frame_time=r["t"])

        top = sorted(labels_acc.items(), key=lambda kv: -kv[1])[:3]
        bits = [f"{duration:.0f}s video", f"{len(rows)} frames sampled across full duration",
                f"{len(reps)} distinct scenes"]
        if top:
            bits.append("content: " + ", ".join(l for l, _ in top))
        if n_faces:
            bits.append(f"{n_faces} face detections")
        updates["summary"] = "; ".join(bits) + "."
    return updates


def frame_hashes(file_id: int) -> list[str]:
    return [r["phash"] for r in db.rows(
        "SELECT phash FROM video_frames WHERE file_id=? ORDER BY t", (file_id,))]
