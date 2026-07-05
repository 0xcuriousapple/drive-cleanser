"""Per-image analysis that needs no ML: sha256, perceptual hash, EXIF,
thumbnails, and a composite quality score (sharpness/exposure/resolution)."""
import hashlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, ExifTags

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

import imagehash
from .. import config

Image.MAX_IMAGE_PIXELS = None  # trust local library sizes


def file_hashes(path: Path) -> tuple[str, str]:
    """(sha256, md5) in one pass. md5 matches Drive's md5Checksum, so exact
    duplicates can be found across sources without downloading Drive bytes."""
    h256, hmd5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h256.update(chunk)
            hmd5.update(chunk)
    return h256.hexdigest(), hmd5.hexdigest()


def open_image(path: Path) -> Image.Image:
    img = Image.open(path)
    return ImageOps.exif_transpose(img)


def make_thumb(img: Image.Image, file_id: int) -> str:
    thumb = img.convert("RGB").copy()
    thumb.thumbnail((config.THUMB_SIZE, config.THUMB_SIZE))
    out = config.THUMB_DIR / f"{file_id}.jpg"
    thumb.save(out, "JPEG", quality=85)
    return str(out)


def phash(img: Image.Image) -> str:
    return str(imagehash.phash(img))


def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def extract_exif(img: Image.Image) -> dict:
    out = {}
    try:
        exif = img.getexif()
    except Exception:
        return out
    if not exif:
        return out
    tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
    dt = tags.get("DateTimeOriginal") or tags.get("DateTime")
    if dt:
        out["taken_time"] = str(dt).replace(":", "-", 2).replace(" ", "T")
    make, model = tags.get("Make"), tags.get("Model")
    if make or model:
        out["camera"] = " ".join(str(x).strip("\x00 ") for x in (make, model) if x)
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if gps:
            lat = _dms(gps.get(2), gps.get(1))
            lon = _dms(gps.get(4), gps.get(3))
            if lat is not None and lon is not None:
                out["gps_lat"], out["gps_lon"] = lat, lon
    except Exception:
        pass
    out["has_exif"] = True
    return out


def _dms(vals, ref):
    if not vals:
        return None
    try:
        d, m, s = (float(v) for v in vals)
        sign = -1 if ref in ("S", "W") else 1
        return sign * (d + m / 60 + s / 3600)
    except Exception:
        return None


def sharpness(gray: np.ndarray) -> float:
    """Variance of Laplacian, computed with numpy (no cv2 dependency)."""
    lap = (-4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1]
           + gray[1:-1, :-2] + gray[1:-1, 2:])
    return float(lap.var())


def quality_score(img: Image.Image, true_mp: float | None = None) -> tuple[float, dict]:
    """Composite 0..1: sharpness + exposure + resolution. Returns (score, parts).
    true_mp: pass the original's megapixels when analyzing a reduced preview."""
    small = img.convert("L").copy()
    small.thumbnail((512, 512))
    gray = np.asarray(small, dtype=np.float32)

    sharp_raw = sharpness(gray)
    sharp = min(1.0, sharp_raw / 500.0)          # ~500+ laplacian var = crisp

    mean = float(gray.mean()) / 255.0
    exposure = 1.0 - min(1.0, abs(mean - 0.45) / 0.45)  # penalize very dark/blown

    mp = true_mp if true_mp else (img.width * img.height) / 1e6
    res = min(1.0, mp / 8.0)                      # 8MP+ = full marks

    score = float(0.5 * sharp + 0.25 * exposure + 0.25 * res)
    return round(score, 3), {"sharpness": round(float(sharp), 3), "exposure": round(float(exposure), 3),
                             "resolution": round(res, 3), "megapixels": round(mp, 2)}
