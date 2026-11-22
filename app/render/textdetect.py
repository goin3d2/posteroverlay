"""Finds text on a poster (titles, taglines) so badges can be placed where they don't cover it.

Uses the PaddleOCR PP-OCRv3 text detection model (DBNet, Apache-2.0, models/text-detect.onnx, 2.3 MB) through
onnxruntime. Only detection runs: no characters are read. About 0.05 s per poster on a CPU.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Tuple

from PIL import Image

MODEL = os.path.join(os.path.dirname(__file__), "models", "text-detect.onnx")
log = logging.getLogger("posteroverlay")
_lock = threading.Lock()
_session = None
_failed = False


def _get():
    global _session, _failed
    if _session is None and not _failed:
        with _lock:
            if _session is None and not _failed:
                try:
                    import onnxruntime as ort
                    opts = ort.SessionOptions()
                    opts.intra_op_num_threads = 2
                    _session = ort.InferenceSession(MODEL, opts, providers=["CPUExecutionProvider"])
                except Exception as e:  # noqa: BLE001
                    _failed = True
                    log.warning("Text detection unavailable (%s); automatic badge placement keeps the top-left", e)
    return _session


def text_mask(img: Image.Image, long_side: int = 640):
    """(probability map that each pixel is text, x scale, y scale) or None when detection is unavailable."""
    import numpy as np
    sess = _get()
    if sess is None:
        return None
    w, h = img.size
    s = long_side / max(w, h)
    nw, nh = max(32, round(w * s / 32) * 32), max(32, round(h * s / 32) * 32)
    x = np.asarray(img.convert("RGB").resize((nw, nh), Image.BILINEAR), dtype=np.float32) / 255.0
    x = (x - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([0.229, 0.224, 0.225], dtype=np.float32)
    x = x.transpose(2, 0, 1)[None].astype(np.float32)
    prob = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    return prob, nw / w, nh / h


def text_share(mask, box: Tuple[float, float, float, float]) -> float:
    """Share of the box covered by text."""
    prob, sx, sy = mask
    x0, y0, x1, y1 = box
    r = prob[max(0, int(y0 * sy)):max(0, int(y1 * sy)), max(0, int(x0 * sx)):max(0, int(x1 * sx))]
    return float((r > 0.3).mean()) if r.size else 0.0
