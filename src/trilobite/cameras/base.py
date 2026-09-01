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
import threading
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
        # Full-frame handshake; see the block below read_preview.
        self._full_pending = threading.Event()
        self._full_ready = threading.Event()
        self._full_lock = threading.Lock()
        self._full_frame: Frame | None = None

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

    # -- full frames, served by the capture thread -----------------------
    #
    # **One consumer.** An earlier design had a second thread calling into the
    # camera on its own schedule to fetch full-resolution frames for corner
    # detection. Two threads pulling from a four-deep request pool, one at 30 Hz
    # and one at 1 Hz, took the Pi down repeatedly -- and a CPU stress test at
    # four cores did not, which is how the camera path rather than the load was
    # identified as the cause.
    #
    # So nothing outside the capture loop ever touches the device. A caller that
    # wants a full-resolution frame raises a flag; the capture thread, which is
    # already holding a request containing every stream, pulls `main` out of
    # *that same request* before releasing it, and publishes the result here.
    #
    # Two things fall out of that beyond not crashing. The full frame is the
    # same exposure as the preview frame it arrived with, not a later one. And
    # a request that arrives while the camera is stopped simply never completes,
    # instead of raising from inside a thread that has no way to report.

    def request_full_frame(self) -> None:
        """Ask the capture loop for a full-resolution frame. Non-blocking."""
        self._full_pending.set()

    def take_full_frame(self) -> Frame | None:
        """Consume the most recent full frame, if one has been delivered."""
        with self._full_lock:
            frame, self._full_frame = self._full_frame, None
        return frame

    @property
    def full_frame_pending(self) -> bool:
        return self._full_pending.is_set()

    def _serve_full_frame(self, frame: Frame | None) -> None:
        """Called by the backend from inside its capture call."""
        if frame is None:
            return
        with self._full_lock:
            self._full_frame = frame
        self._full_pending.clear()
        self._full_ready.set()

    def wait_full_frame(self, timeout: float = 3.0) -> Frame | None:
        """Request a full frame and block until the capture loop delivers it.

        Used once per pose, not in a loop. Returns None on timeout, which for a
        camera that has stopped producing frames is the honest answer.
        """
        self._full_ready.clear()
        self.request_full_frame()
        if not self._full_ready.wait(timeout):
            self._full_pending.clear()
            return None
        return self.take_full_frame()

    @staticmethod
    def _to_mono(frame: Frame) -> Frame:
        """Reduce an ISP frame to one channel.

        A mono sensor through the ISP arrives as XBGR or RGB with every channel
        carrying the same luma, so taking one plane is a copy rather than a
        colour conversion, and gives an identical result.
        """
        data = frame.data
        if data.ndim == 3:
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
