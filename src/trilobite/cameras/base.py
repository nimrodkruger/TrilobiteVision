"""The camera abstraction.

This is the most important seam in the project. Everything above it -- the
pipeline, the sinks, the web layer -- knows only this interface. That buys
three things:

  1. The whole stack runs on a Windows desktop with no camera attached, using
     the synthetic or replay backends. You can develop and test the web UI,
     the parameter plumbing and the storage layout without the Pi.
  2. Recorded sessions replay through the identical code path as live capture,
     so a calibration routine can be debugged deterministically.
  3. An event camera, a machine-vision USB3 camera or a simulated plenoptic
     rig is a new subclass, not a refactor.

The dual-stream contract matters for optics work. `read_preview` returns small,
processed, cheap frames for the browser. `capture_full` returns the full
sensor frame with the ISP bypassed where possible. Never stream what you
intend to measure, and never measure what you have streamed.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from ..config import CameraConfig
from ..types import CameraInfo, Frame

log = logging.getLogger(__name__)


class CameraSource(ABC):
    """Base class for anything that produces Frames."""

    def __init__(self, cfg: CameraConfig) -> None:
        self.cfg = cfg
        self.cam_id = cfg.cam_id
        self._seq = 0
        self._open = False

    # -- lifecycle ------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Acquire the device and configure streams. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release the device. Idempotent, and safe to call after a failure."""

    def __enter__(self) -> CameraSource:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._open

    # -- capture --------------------------------------------------------

    @abstractmethod
    def read_preview(self) -> Frame | None:
        """Block for the next low-resolution frame. None if the source ended."""

    @abstractmethod
    def capture_full(self, raw: bool = True) -> Frame:
        """Grab one full-resolution frame.

        raw=True asks for sensor data with the ISP bypassed: Bayer or mono,
        native bit depth. This is what calibration and plenoptic
        reconstruction need. raw=False gives the ISP output, which is only
        useful for looking at.
        """

    # -- introspection and control --------------------------------------

    @abstractmethod
    def describe(self) -> CameraInfo:
        """What this camera is, for the UI and the session metadata."""

    def set_controls(self, controls: dict[str, Any]) -> None:
        """Apply driver-level controls (exposure, gain, ...). Optional."""
        log.debug("%s: set_controls ignored by %s", self.cam_id, type(self).__name__)

    def control_spec(self) -> dict[str, dict[str, Any]]:
        """Advertised driver controls as {name: {min, max, default, type}}.

        Structured, not stringified, so the UI can build a slider with the
        sensor's own limits rather than a hardcoded guess -- exposure range
        differs per sensor and per mode, and a wrong range makes the control
        useless at one end.
        """
        return {}

    def get_controls(self) -> dict[str, Any]:
        """Current values of the driver controls, where the backend knows them."""
        return {}

    # -- helpers for subclasses -----------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq
