"""
Background removal on our own server — a person or a product cut out of their photo.

Runs an open segmentation model (the same ONNX models the rembg project publishes) with
onnxruntime directly, instead of the rembg package: rembg pulls in numba/llvmlite/scipy for
alpha matting we don't use. The photo's own pixels are kept exactly — only an alpha mask is
added — so a real person's face or a real product is never redrawn.

The model file is baked into the image at build time (see Dockerfile) at
CUTOUT_MODEL_DIR/<CUTOUT_MODEL>.onnx. One session per worker process, loaded on first use;
calls are serialised per process so a burst of cut-outs can't multiply the memory it needs.
"""
import os
import threading
from typing import Optional

import numpy as np
from PIL import Image

MODEL_NAME = os.environ.get("CUTOUT_MODEL", "isnet-general-use")
MODEL_DIR = os.environ.get("CUTOUT_MODEL_DIR", "/app/models")

# (input size, mean, std) — the preprocessing each model was trained with (as in rembg).
_SPECS = {
    "isnet-general-use": (1024, (0.5, 0.5, 0.5), (1.0, 1.0, 1.0)),
    "u2net_human_seg": (320, (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    "u2net": (320, (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    "silueta": (320, (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
}

_session = None
_lock = threading.Lock()


def model_path(name: str = MODEL_NAME) -> str:
    return os.path.join(MODEL_DIR, f"{name}.onnx")


def available(name: str = MODEL_NAME) -> bool:
    if name not in _SPECS or not os.path.exists(model_path(name)):
        return False
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _get_session():
    global _session
    if _session is None:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        # Without the arena, memory goes back after each call instead of being held at the
        # peak (measured: 1.5 GB held vs. much less) — 4 workers share one 4 GB container.
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        _session = ort.InferenceSession(model_path(), sess_options=opts, providers=["CPUExecutionProvider"])
    return _session


def mask_for(image: Image.Image, name: str = MODEL_NAME) -> Image.Image:
    """The foreground mask ("L", same size as `image`): 255 = keep, 0 = background."""
    size, mean, std = _SPECS[name]
    rgb = image.convert("RGB")
    arr = np.asarray(rgb.resize((size, size), Image.Resampling.LANCZOS), dtype=np.float32)
    arr = arr / max(float(arr.max()), 1e-6)
    arr = (arr - np.array(mean, dtype=np.float32)) / np.array(std, dtype=np.float32)
    batch = arr.transpose(2, 0, 1)[None].astype(np.float32)
    with _lock:
        session = _get_session()
        pred = session.run(None, {session.get_inputs()[0].name: batch})[0][0, 0]
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / (hi - lo) if hi > lo else np.zeros_like(pred)
    # Faint leftovers of the background (a plant, a shadow) below 10% become fully clear.
    pred = np.where(pred < 0.10, 0.0, pred)
    mask = Image.fromarray((pred * 255).clip(0, 255).astype(np.uint8), mode="L")
    return mask.resize(rgb.size, Image.Resampling.LANCZOS)


def cut_out(image: Image.Image) -> Optional[Image.Image]:
    """The photo with its background made transparent, or None when the model found no clear
    subject (an empty or near-total mask is not a cut-out)."""
    mask = mask_for(image)
    kept = np.asarray(mask, dtype=np.float32).mean() / 255
    if kept < 0.03 or kept > 0.97:
        return None
    out = image.convert("RGBA")
    out.putalpha(mask)
    return out
