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

    # -- orientation -----------------------------------------------------
    #
    # Rotation and mirroring are applied ONCE, here, at acquisition, before
    # anything else sees the pixels -- so the preview, the saved raw, the
    # sub-aperture crops and the recorded corners cannot disagree about which
    # way round the image is. An orientation that lived in the display pipeline
    # would turn what you look at and not what you measure, which is a
    # difference nobody notices until the calibration is finished and mirrored.
    #
    # Order: rotate, THEN mirror. That makes the two flip checkboxes mean "flip
    # the image I am looking at", which is the only reading an operator can
    # verify by eye. The other order would make them mean "flip the sensor",
    # and which screen axis that corresponds to would depend on the rotation.

    @property
    def quarter_turns(self) -> int:
        """Rotation as a count of 90-degree steps for np.rot90 (0-3).

        `rotate_deg` is clockwise on screen; np.rot90's positive direction on
        an image whose rows run downward is counter-clockwise, so the sign
        flips exactly here and nowhere else.
        """
        return (-int(getattr(self.cfg, "rotate_deg", 0) or 0) // 90) % 4

    def oriented_size(self, size: tuple[int, int]) -> tuple[int, int]:
        """(width, height) as delivered, given a sensor-native (width, height).

        The one function that knows a quarter turn swaps the axes. Every
        `describe()` runs its resolutions through this, which is what keeps the
        MLA reference frame, the readiness arithmetic and the session manifest
        from having to know about rotation at all.
        """
        w, h = int(size[0]), int(size[1])
        return (h, w) if self.quarter_turns % 2 else (w, h)

    def _orient(self, data: np.ndarray) -> np.ndarray:
        """Apply the configured rotation and mirroring. Every frame passes here.

        np.rot90 and np.flip both return views with permuted or negative
        strides; the copy back to contiguous is what makes the result a real
        array again, and costs about 0.3 ms for a 1456x1088 uint8 frame (0.9 ms
        for a quarter turn, which cannot be done by striding alone).
        """
        if data is None or data.ndim < 2:
            return data
        turns = self.quarter_turns
        if turns:
            data = np.rot90(data, k=turns)
        if self.cfg.flip_horizontal:
            data = np.flip(data, axis=1)
        if self.cfg.flip_vertical:
            data = np.flip(data, axis=0)
        if turns or self.cfg.flip_horizontal or self.cfg.flip_vertical:
            return np.ascontiguousarray(data)
        return data

    @property
    def orientation(self) -> dict[str, Any]:
        """Recorded in every sidecar: a frame has to say which way round it is.

        `rotate_deg` is here as well as the flips because a reader holding a
        portrait .npy cannot otherwise tell a rotated landscape sensor from a
        portrait one, and the difference decides whether the recorded MLA
        offsets need their axes swapped.
        """
        return {
            "flip_horizontal": bool(self.cfg.flip_horizontal),
            "flip_vertical": bool(self.cfg.flip_vertical),
            "rotate_deg": int(getattr(self.cfg, "rotate_deg", 0) or 0),
        }

    def skip_preview(self) -> None:
        """Consume one frame's worth of the source without processing it.

        The capture loop calls this when the pipeline is already up to rate.
        The frame still has to be taken -- a picamera2 request left unreleased
        starves a four-deep pool and stalls the sensor -- but nothing needs to
        be decoded, oriented or copied. The default is the honest slow version;
        backends that can release a request without converting it override
        this and save the copy.
        """
        self.read_preview()

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
