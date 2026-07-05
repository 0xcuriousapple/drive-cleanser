"""CLIP embeddings (OpenCLIP ViT-B/32). Lazy-loaded; everything degrades
gracefully when the ML extras aren't installed."""
import numpy as np

import threading

_state = {"tried": False, "model": None, "preprocess": None, "tokenizer": None, "torch": None,
          "error": None, "device": "cpu"}
_infer_lock = threading.Lock()   # MPS/torch inference serialized across scan workers
MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
DIM = 512


def available() -> bool:
    _load()
    return _state["model"] is not None


def _load():
    if _state["tried"]:
        return
    _state["tried"] = True
    try:
        import torch
        import open_clip
        model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=PRETRAINED)
        model.eval()
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        model.to(device)
        _state.update(model=model, preprocess=preprocess, device=device,
                      tokenizer=open_clip.get_tokenizer(MODEL_NAME), torch=torch)
    except ImportError:
        _state["error"] = "ML extras not installed (pip install -r requirements-ml.txt)"
    except Exception as e:  # a broken ML install must not kill scans
        _state["error"] = f"{type(e).__name__}: {e}"


def load_error() -> str | None:
    return _state["error"]


def embed_images(pil_images) -> np.ndarray:
    """Return L2-normalized float32 embeddings, shape (n, 512)."""
    _load()
    torch = _state["torch"]
    batch = torch.stack([_state["preprocess"](im.convert("RGB")) for im in pil_images])
    with _infer_lock, torch.no_grad():
        feats = _state["model"].encode_image(batch.to(_state["device"]))
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().numpy().astype(np.float32)


def embed_text(texts: list[str]) -> np.ndarray:
    _load()
    torch = _state["torch"]
    toks = _state["tokenizer"](texts)
    with _infer_lock, torch.no_grad():
        feats = _state["model"].encode_text(toks.to(_state["device"]))
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().numpy().astype(np.float32)


def to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
