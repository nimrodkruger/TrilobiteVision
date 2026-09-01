"""Camera backends that need no hardware.

`synthetic` generates a moving test pattern. `replay` loops a directory of
images. Both exist so the entire application -- web UI, parameter plumbing,
storage layout, calibration routines -- can be run and tested on the Windows
desktop, and so a captured session can be replayed deterministically through
the identical code path used for live capture.

Treat these as first-class. Most of the debugging you will do on this project
does not need a photon.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from ..config import CameraConfig
from ..types import CameraInfo, Frame
from .base import CameraSource

log = logging.getLogger(__name__)


class SyntheticSource(CameraSource):
    """A drifting sinusoidal target plus a static grid and noise.

    The pattern is deliberately not a natural image: it has known spatial
    frequency content, so it is useful for sanity-checking that a processing
    stage does what it claims before you point the rig at anything real.
    """

    def __init__(self, cfg: CameraConfig) -> None:
        super().__init__(cfg)
        self._full = tuple(cfg.full_resolution or (1456, 1088))
        self._prev = tuple(cfg.preview_resolution)
        self._t0 = time.monotonic()
        self._rng = np.random.default_rng(abs(hash(cfg.cam_id)) % (2**32))
        self._period = 1.0 / max(cfg.fps, 1e-3)
        self._last = 0.0
        self._controls: dict[str, Any] = {
            "ExposureTime": 5000,
            "AnalogueGain": 1.0,
            "AeEnable": False,
        }
        self._brightness = 1.0
        self._last_mean = 128.0

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _render(self, size: tuple[int, int], phase: float) -> np.ndarray:
        w, h = size
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        # Two gratings at different orientations, drifting at different rates.
        img = 0.35 * np.sin(2 * np.pi * (xx / 64.0 + phase))
        img += 0.25 * np.sin(2 * np.pi * (yy / 97.0 - 0.6 * phase))
        # A fixed grid gives a geometric reference for distortion work.
        img += 0.20 * (((xx.astype(int) % 128) < 3) | ((yy.astype(int) % 128) < 3))
        img += 0.5
        img += self._rng.normal(0.0, 0.01, size=img.shape).astype(np.float32)
        # Exposure and gain scale the scene, so the sensor controls have a
        # visible effect here too and the UI is testable without the rig.
        return np.clip(img * 255.0 * self._brightness, 0, 255).astype(np.uint8)

    def read_preview(self) -> Frame | None:
        if not self._open:
            return None
        # Pace the synthetic source to the configured fps so timing-related
        # behaviour in the rest of the stack is realistic.
        now = time.monotonic()
        wait = self._period - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self._run_ae()
        phase = (self._last - self._t0) * 0.4
        # Camera id shifts the phase so two synthetic cameras look different.
        phase += 0.3 * (abs(hash(self.cam_id)) % 7)
        image = self._render(self._prev, phase)
        self._last_mean = float(image.mean())
        return Frame.now(
            image,
            self.cam_id,
            self._next_seq(),
            space="mono8",
            Synthetic=True,
            **self._controls,
        )

    def capture_full(self, raw: bool = True) -> Frame:
        phase = (time.monotonic() - self._t0) * 0.4
        return Frame.now(
            self._render(self._full, phase),
            self.cam_id,
            self._next_seq(),
            space="raw" if raw else "mono8",
            Synthetic=True,
            **self._controls,
        )

    def describe(self) -> CameraInfo:
        return CameraInfo(
            cam_id=self.cam_id,
            model="synthetic",
            backend="synthetic",
            full_resolution=self._full,
            preview_resolution=self._prev,
            mono=True,
        )

    # Advertising a plausible control set is not decoration: it means the
    # sensor panel in the UI can be exercised on the desktop, so a change to
    # that panel is testable without the rig attached.
    def control_spec(self) -> dict[str, dict[str, Any]]:
        return {
            "ExposureTime": {"type": "integer", "minimum": 30, "maximum": 100000, "default": 5000},
            "AnalogueGain": {"type": "number", "minimum": 1.0, "maximum": 16.0, "default": 1.0},
            "AeEnable": {"type": "boolean", "default": False},
        }

    def set_controls(self, controls: dict[str, Any]) -> None:
        spec = self.control_spec()
        unknown = sorted(set(controls) - set(spec))
        if unknown:
            raise ValueError(
                f"{self.cam_id}: control(s) not supported by this sensor: "
                f"{', '.join(unknown)}. Available: {', '.join(sorted(spec))}"
            )
        controls = self._resolve_ae(dict(controls))
        self._controls.update(controls)
        self._requested.update(controls)
        self._apply_brightness()

    def _resolve_ae(self, controls: dict[str, Any]) -> dict[str, Any]:
        """Mirror the real backend's auto-exposure transition rules.

        Same logic as Picamera2Source._resolve_ae, deliberately duplicated
        rather than shared: it encodes libcamera behaviour that this backend
        only *simulates*, and a shared implementation would imply the two are
        the same mechanism. Duplicating it is what makes the UI's AE handling
        testable without the rig.
        """
        ae = controls.get("AeEnable")
        if ae is False:
            controls.setdefault("ExposureTime", int(self._controls.get("ExposureTime", 5000)))
            controls.setdefault("AnalogueGain", float(self._controls.get("AnalogueGain", 1.0)))
        elif ae is None and self.auto_exposure and (
            "ExposureTime" in controls or "AnalogueGain" in controls
        ):
            controls["AeEnable"] = False
        elif ae is True:
            for key in ("ExposureTime", "AnalogueGain"):
                controls.pop(key, None)
        return controls

    def _apply_brightness(self) -> None:
        exp = float(self._controls.get("ExposureTime", 5000))
        gain = float(self._controls.get("AnalogueGain", 1.0))
        self._brightness = min(4.0, (exp / 5000.0) * gain)

    # Target mean level for the simulated auto-exposure, in DN. Deliberately
    # not 128: the synthetic scene averages near mid-grey already, and a target
    # equal to the resting value would make AE a no-op and hide any bug in the
    # transition logic behind a control that never moves.
    AE_TARGET_MEAN = 150.0

    def _run_ae(self) -> None:
        """Simulated auto-exposure: converge the exposure toward a target mean.

        Exists so the UI's auto-exposure behaviour -- the live readback, and
        the value being pinned when AE is switched off -- can be exercised
        end to end on a desktop with no camera attached.
        """
        if not self._controls.get("AeEnable"):
            return
        measured = max(self._last_mean, 1.0)
        err = self.AE_TARGET_MEAN / measured
        exp = float(self._controls.get("ExposureTime", 5000))
        # First-order convergence, damped, with a per-step limit so it settles
        # over several frames the way a real AE loop does.
        exp *= float(np.clip(1.0 + 0.3 * (err - 1.0), 0.7, 1.4))
        self._controls["ExposureTime"] = int(np.clip(exp, 30, 100000))
        self._apply_brightness()

    def get_controls(self) -> dict[str, Any]:
        return dict(self._controls)


class ReplaySource(CameraSource):
    """Loop over image files in a directory, in sorted filename order."""

    SUFFIXES = {".npy", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".pgm"}

    def __init__(self, cfg: CameraConfig) -> None:
        super().__init__(cfg)
        if not cfg.source_dir:
            raise ValueError(f"{cfg.cam_id}: replay backend requires source_dir")
        self._dir = Path(cfg.source_dir).expanduser()
        self._files: list[Path] = []
        self._idx = 0
        self._period = 1.0 / max(cfg.fps, 1e-3)
        self._last = 0.0

    def open(self) -> None:
        self._files = sorted(p for p in self._dir.iterdir() if p.suffix.lower() in self.SUFFIXES)
        if not self._files:
            raise RuntimeError(f"{self.cam_id}: no replayable images in {self._dir}")
        log.info("%s: replaying %d files from %s", self.cam_id, len(self._files), self._dir)
        self._open = True

    def close(self) -> None:
        self._open = False

    def _load(self, path: Path) -> np.ndarray:
        if path.suffix.lower() == ".npy":
            return np.load(path)
        try:
            import cv2  # noqa: PLC0415

            img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if img is None:
                raise RuntimeError(f"cv2 could not read {path}")
            return img
        except ImportError:
            from PIL import Image  # noqa: PLC0415

            return np.asarray(Image.open(path))

    def read_preview(self) -> Frame | None:
        if not self._open:
            return None
        now = time.monotonic()
        wait = self._period - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

        path = self._files[self._idx % len(self._files)]
        self._idx += 1
        data = self._load(path)
        space = "rgb8" if data.ndim == 3 else ("mono16" if data.dtype == np.uint16 else "mono8")
        return Frame.now(data, self.cam_id, self._next_seq(), space=space, source_file=str(path))

    def capture_full(self, raw: bool = True) -> Frame:
        frame = self.read_preview()
        if frame is None:
            raise RuntimeError(f"{self.cam_id}: replay source is closed")
        return frame

    def describe(self) -> CameraInfo:
        return CameraInfo(
            cam_id=self.cam_id,
            model=f"replay:{self._dir.name}",
            backend="replay",
            full_resolution=tuple(self.cfg.full_resolution or (0, 0)),
            preview_resolution=tuple(self.cfg.preview_resolution),
            mono=True,
            detail={"files": str(len(self._files)), "dir": str(self._dir)},
        )
