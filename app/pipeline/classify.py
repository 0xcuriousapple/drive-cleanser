"""Media classification: cheap heuristics always run; zero-shot CLIP refines
when the ML stack is installed. Every label gets a score and a method so the
UI can explain where it came from."""
import re

import numpy as np

from .. import db
from . import clip_embed

# label -> CLIP text prompt
CATEGORIES = {
    "screenshot": "a screenshot of a phone or computer screen with UI elements",
    "document": "a photo or scan of a text document or paper form",
    "receipt": "a photo of a shopping receipt or invoice",
    "meme": "a meme image with overlaid caption text",
    "people": "a photo of people, family or friends posing or candid",
    "pet": "a photo of a pet dog or cat",
    "food": "a photo of a plate of food or a meal",
    "nature": "a landscape photo of nature, mountains, beach or scenery",
    "travel": "a travel photo of a city, landmark or tourist attraction",
    "celebration": "a photo of a birthday party, wedding or celebration with cake or decorations",
    "vehicle": "a photo of a car, motorcycle or vehicle",
    "whiteboard": "a photo of a whiteboard, slide or work presentation",
}

SCREENSHOT_NAME = re.compile(r"(screen\s?shot|screencap|screen_recording)", re.I)
SCREEN_SIZES = {(1170, 2532), (1179, 2556), (1080, 2340), (1284, 2778), (1290, 2796),
                (750, 1334), (828, 1792), (1125, 2436), (1440, 3200), (1080, 2400),
                (2532, 1170), (2340, 1080)}
_text_cache = {}


def heuristics(file_row: dict, has_exif: bool) -> list[tuple[str, float]]:
    out = []
    name = file_row.get("name") or ""
    w, h = file_row.get("width") or 0, file_row.get("height") or 0
    if SCREENSHOT_NAME.search(name):
        out.append(("screenshot", 0.97))
    elif (w, h) in SCREEN_SIZES and not has_exif:
        out.append(("screenshot", 0.75))
    elif (file_row.get("mime") == "image/png" and not has_exif and w and h
          and 0.4 < w / h < 0.6):
        out.append(("screenshot", 0.6))
    if re.search(r"(whatsapp|wa\d{4}|telegram|IMG-\d{8}-WA)", name, re.I):
        out.append(("messaging_download", 0.8))
    return out


def clip_classify(image_emb: np.ndarray) -> list[tuple[str, float]]:
    """Zero-shot over CATEGORIES. image_emb: (512,) normalized."""
    if "vecs" not in _text_cache:
        _text_cache["labels"] = list(CATEGORIES.keys())
        _text_cache["vecs"] = clip_embed.embed_text(list(CATEGORIES.values()))
    sims = _text_cache["vecs"] @ image_emb
    probs = np.exp(sims * 20) / np.exp(sims * 20).sum()  # temperature-scaled softmax
    order = np.argsort(-probs)
    return [(_text_cache["labels"][i], float(probs[i])) for i in order[:3] if probs[i] > 0.15]


def store(file_id: int, labels: list[tuple[str, float]], method: str):
    for label, score in labels:
        db.execute(
            "INSERT INTO classifications(file_id,label,score,method) VALUES(?,?,?,?) "
            "ON CONFLICT(file_id,label) DO UPDATE SET score=max(score,excluded.score), method=excluded.method",
            (file_id, label, round(score, 3), method), commit=False)
    db.get_db().commit()
