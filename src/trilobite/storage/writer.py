"""On-device storage.

Two rules encoded here, both learned the expensive way:

1. **Never save pixels without their metadata.** Every image gets a JSON
   sidecar carrying the sensor settings, the full pipeline parameter set, the
   camera description and the timestamps. An uncalibrated image with no record
   of how it was taken is not data.

2. **Default to lossless and unprocessed.** .npy holds the native dtype with
   no compression artefacts and loads in one line on the desktop. PNG and TIFF
   are offered for interchange. JPEG is deliberately not an option for the
   science path.

Sessions group captures. A session directory is created once per run and
everything from that run lands in it, so a day's work is one folder you can
copy off the Pi in a single scp.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import StorageConfig
from ..types import Frame

log = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    """libcamera metadata contains tuples, numpy scalars and enums."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class SessionWriter:
    def __init__(self, cfg: StorageConfig, root: Path) -> None:
        self.cfg = cfg
        self.root = root
        self.session_dir = root / datetime.now().strftime("session_%Y%m%d_%H%M%S")
        self._lock = threading.Lock()
        self._counter = 0
        self.session_dir.mkdir(parents=True, exist_ok=True)
        log.info("session directory: %s", self.session_dir)

    def write_session_manifest(self, payload: dict[str, Any]) -> Path:
        """Record the rig configuration once, at startup."""
        path = self.session_dir / "session.json"
        path.write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
        return path

    def save_still(
        self,
        frame: Frame,
        pipeline_settings: dict[str, Any] | None = None,
        camera_info: dict[str, Any] | None = None,
        tag: str = "still",
        label: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._counter += 1
            n = self._counter

        # cam_id leads the filename after the tag, so a directory listing sorts
        # by what the file *is* and names which physical camera produced it
        # without opening the sidecar.
        stem = f"{tag}_{frame.cam_id}_{n:06d}_{datetime.now().strftime('%H%M%S_%f')}"
        cam_dir = self.session_dir / frame.cam_id
        cam_dir.mkdir(parents=True, exist_ok=True)

        fmt = self.cfg.still_format
        img_path = cam_dir / f"{stem}.{ 'npy' if fmt == 'npy' else fmt }"

        if fmt == "npy":
            np.save(img_path, frame.data)
        else:
            try:
                import cv2  # noqa: PLC0415

                if not cv2.imwrite(str(img_path), frame.data):
                    raise RuntimeError(f"cv2.imwrite failed for {img_path}")
            except ImportError:
                from PIL import Image  # noqa: PLC0415

                Image.fromarray(frame.data).save(img_path)

        sidecar = {
            "file": img_path.name,
            "cam_id": frame.cam_id,
            "camera_label": label or frame.cam_id,
            "tag": tag,
            "seq": frame.seq,
            "t_monotonic": frame.t_mono,
            "t_wall": frame.t_wall,
            "t_iso": datetime.fromtimestamp(frame.t_wall).isoformat(),
            "space": frame.space,
            "dtype": str(frame.data.dtype),
            "shape": list(frame.data.shape),
            "sensor_metadata": _jsonable(frame.meta),
            "pipeline": _jsonable(pipeline_settings or {}),
            "camera": _jsonable(camera_info or {}),
        }
        meta_path = img_path.with_suffix(".json")
        meta_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

        log.info("saved %s (%s %s)", img_path.name, frame.data.shape, frame.data.dtype)
        return {"image": str(img_path), "metadata": str(meta_path), **sidecar}
