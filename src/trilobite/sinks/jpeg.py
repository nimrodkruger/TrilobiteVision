"""JPEG encoding for the preview stream.

The Pi 5 has no hardware JPEG or H.264 encoder -- the VideoCore VII dropped
them. Every preview frame is compressed on the CPU, and with two cameras that
cost is real. Hence: encode the small lores stream, not the full frame, and
cap the preview frame rate well below the sensor frame rate.

simplejpeg is a picamera2 dependency and is the fastest of the three options
here, so it is tried first.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

_BACKEND: str | None = None


def _pick_backend() -> str:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    for name, mod in (("simplejpeg", "simplejpeg"), ("cv2", "cv2"), ("pil", "PIL.Image")):
        try:
            __import__(mod)
            _BACKEND = name
            log.info("jpeg encoder backend: %s", name)
            return name
        except ImportError:
            continue
    raise RuntimeError("no JPEG encoder available; install simplejpeg, opencv or pillow")


def encode_jpeg(image: np.ndarray, quality: int = 80) -> bytes:
    backend = _pick_backend()

    if image.dtype != np.uint8:
        # 16-bit sensor data has to be scaled for display. Scaling by the
        # actual max rather than the dtype max keeps dim scenes visible; it
        # does mean preview brightness is not comparable between frames, which
        # is fine for a viewfinder and unacceptable for measurement.
        peak = float(image.max()) or 1.0
        image = (image.astype(np.float32) * (255.0 / peak)).astype(np.uint8)

    if backend == "simplejpeg":
        import simplejpeg  # noqa: PLC0415

        if image.ndim == 2:
            image = image[:, :, None]
            colorspace = "GRAY"
        else:
            colorspace = "RGB"
        return simplejpeg.encode_jpeg(
            np.ascontiguousarray(image), quality=quality, colorspace=colorspace
        )

    if backend == "cv2":
        import cv2  # noqa: PLC0415

        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise RuntimeError("cv2.imencode failed")
        return buf.tobytes()

    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    Image.fromarray(image.squeeze()).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
