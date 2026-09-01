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

import numpy as np

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
        # Everything ever asked for via set_controls, plus whatever the config
        # requested at open time. This is what gets persisted.
        self._requested: dict[str, Any] = dict(cfg.controls)

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

    def read_full_mono(self) -> Frame | None:
        """A full-resolution single-channel frame, cheap enough to poll.

        Sits between the two methods above, and exists for one caller: the
        calibration corner detector. That job needs full sensor resolution --
        the preview is half-scale, so corners found on it carry half the
        precision -- but it does **not** need raw sensor data. Corner detection
        is a geometric measurement on an intensity image, and the ISP's
        companding, which is what disqualifies the processed path for
        radiometry, moves no corner.

        Distinct from capture_full(raw=True), which may have to decode a
        compressed raw format and is far too slow to run in a loop, and from
        read_preview, which is the wrong size. The base implementation takes
        the ISP output and reduces it to two dimensions; backends with a
        cheaper route override.

        Returns None when the source has no frame available.
        """
        frame = self.capture_full(raw=False)
        if frame is None:
            return None
        data = frame.data
        if data.ndim == 3:
            # A mono sensor through the ISP arrives as XBGR/RGB with every
            # channel carrying the same luma. Take one plane rather than
            # paying for a colour conversion.
            return frame.derive(np.ascontiguousarray(data[..., 0]), space="mono8")
        return frame

    # -- introspection and control --------------------------------------

    @abstractmethod
    def describe(self) -> CameraInfo:
        """What this camera is, for the UI and the session metadata."""

    def set_controls(self, controls: dict[str, Any]) -> None:
        """Apply driver-level controls (exposure, gain, ...). Optional."""
        log.debug("%s: set_controls ignored by %s", self.cam_id, type(self).__name__)

    @property
    def auto_exposure(self) -> bool:
        """Whether auto-exposure is currently on. Backends that have no AE
        report False rather than raising."""
        return bool(self._requested.get("AeEnable", False))

    def requested_controls(self) -> dict[str, Any]:
        """The controls this source has been asked for, for saving state.

        Requested, not measured: restoring a session should reinstate the
        decisions you made, not the particular exposure auto-exposure happened
        to land on in the last frame before shutdown.
        """
        return dict(self._requested)

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
