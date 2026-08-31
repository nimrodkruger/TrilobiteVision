"""Backend lookup and hardware discovery."""

from __future__ import annotations

import logging
from typing import Any

from ..config import CameraConfig
from .base import CameraSource
from .offline import ReplaySource, SyntheticSource
from .picam import Picamera2Source

log = logging.getLogger(__name__)

BACKENDS: dict[str, type[CameraSource]] = {
    "picamera2": Picamera2Source,
    "synthetic": SyntheticSource,
    "replay": ReplaySource,
}


def build_camera(cfg: CameraConfig) -> CameraSource:
    try:
        cls = BACKENDS[cfg.backend]
    except KeyError:
        raise ValueError(
            f"unknown camera backend {cfg.backend!r}; known: {sorted(BACKENDS)}"
        ) from None
    return cls(cfg)


def discover() -> list[dict[str, Any]]:
    """Enumerate cameras libcamera can see, without opening them.

    Returns an empty list on a machine with no libcamera, which is the normal
    case on the development desktop.
    """
    try:
        from picamera2 import Picamera2  # noqa: PLC0415
    except ImportError:
        log.info("picamera2 not available on this host; no hardware discovery")
        return []
    try:
        return list(Picamera2.global_camera_info())
    except Exception:  # pragma: no cover - driver-dependent
        log.exception("libcamera enumeration failed")
        return []
