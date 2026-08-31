"""Picamera2 backend, targeting the IMX296 global shutter camera on a Pi 5.

Import of Picamera2 is deferred to open() so the module can be imported on a
Windows desktop where libcamera does not exist.

Stream layout, and why:

    main    full sensor resolution, ISP output
    lores   small YUV420 preview, produced by the ISP in parallel
    raw     the Bayer/mono sensor data, ISP bypassed

Requesting all three in one configuration means the preview costs no extra
sensor reads and no CPU resize, and a still capture can pull the full frame
out of the *same* request that produced the preview, so the metadata matches
the pixels exactly. That coherence matters when you are calibrating and the
exposure is being swept.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

from ..config import CameraConfig
from ..types import CameraInfo, Frame
from .base import CameraSource

log = logging.getLogger(__name__)


class Picamera2Source(CameraSource):
    def __init__(self, cfg: CameraConfig) -> None:
        super().__init__(cfg)
        self._picam: Any = None
        self._info: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._mono = False
        self._full_res: tuple[int, int] = (0, 0)

    def open(self) -> None:
        if self._open:
            return
        try:
            from picamera2 import Picamera2  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on host
            raise RuntimeError(
                "picamera2 is not importable. On the Pi install it with "
                "'sudo apt install -y python3-picamera2' and create the venv "
                "with '--system-site-packages'. On a desktop, use the "
                "'synthetic' or 'replay' backend instead."
            ) from exc

        available = Picamera2.global_camera_info()
        if self.cfg.index >= len(available):
            raise RuntimeError(
                f"camera index {self.cfg.index} requested but libcamera reports "
                f"{len(available)} camera(s): {available}. Check "
                f"/boot/firmware/config.txt and 'rpicam-hello --list-cameras'."
            )
        self._info = available[self.cfg.index]

        picam = Picamera2(self.cfg.index)
        sensor_res = picam.sensor_resolution
        full = tuple(self.cfg.full_resolution or sensor_res)
        self._full_res = (int(full[0]), int(full[1]))

        # The mono IMX296 advertises an R8/R10 raw format; the colour part
        # advertises a Bayer pattern. Detect rather than assume, because the
        # two variants share a model string.
        raw_fmt = str(picam.sensor_format or "")
        self._mono = raw_fmt.startswith("R") and not any(
            p in raw_fmt for p in ("RGGB", "BGGR", "GRBG", "GBRG")
        )

        config = picam.create_video_configuration(
            main={"size": self._full_res},
            lores={"size": tuple(self.cfg.preview_resolution), "format": "YUV420"},
            raw={"size": sensor_res},
            buffer_count=4,
        )
        picam.configure(config)

        controls: dict[str, Any] = {}
        if self.cfg.fps:
            # libcamera takes a frame duration range in microseconds.
            dur = int(1_000_000 / self.cfg.fps)
            controls["FrameDurationLimits"] = (dur, dur)
        controls.update(self.cfg.controls)
        if controls:
            picam.set_controls(controls)

        picam.start()
        self._picam = picam
        self._open = True
        log.info(
            "%s: opened %s at %sx%s (mono=%s), preview %sx%s",
            self.cam_id,
            self._info.get("Model", "unknown"),
            self._full_res[0],
            self._full_res[1],
            self._mono,
            *self.cfg.preview_resolution,
        )

    def close(self) -> None:
        if not self._open:
            return
        try:
            self._picam.stop()
            self._picam.close()
        except Exception:  # pragma: no cover - best effort teardown
            log.exception("%s: error during close", self.cam_id)
        finally:
            self._picam = None
            self._open = False

    def read_preview(self) -> Frame | None:
        if not self._open:
            return None
        with self._lock:
            request = self._picam.capture_request()
            try:
                yuv = request.make_array("lores")
                meta = dict(request.get_metadata())
            finally:
                # Requests are a finite pool. Holding one starves the sensor.
                request.release()

        # lores is YUV420; the first height rows are the luma plane. For a mono
        # sensor that plane *is* the image, and for a colour sensor it is a
        # perfectly good preview. Slicing beats a colour conversion.
        h = int(self.cfg.preview_resolution[1])
        luma = np.ascontiguousarray(yuv[:h, : self.cfg.preview_resolution[0]])
        return Frame.now(luma, self.cam_id, self._next_seq(), space="mono8", **meta)

    def capture_full(self, raw: bool = True) -> Frame:
        if not self._open:
            raise RuntimeError(f"{self.cam_id}: camera not open")
        with self._lock:
            request = self._picam.capture_request()
            try:
                stream = "raw" if raw else "main"
                data = request.make_array(stream)
                meta = dict(request.get_metadata())
            finally:
                request.release()

        if raw:
            space = "raw"
        elif data.ndim == 3:
            space = "rgb8"
        else:
            space = "mono8"
        meta["stream"] = "raw" if raw else "main"
        meta["mono_sensor"] = self._mono
        return Frame.now(
            np.ascontiguousarray(data), self.cam_id, self._next_seq(), space=space, **meta
        )

    def describe(self) -> CameraInfo:
        return CameraInfo(
            cam_id=self.cam_id,
            model=str(self._info.get("Model", "unknown")),
            backend="picamera2",
            full_resolution=self._full_res,
            preview_resolution=tuple(self.cfg.preview_resolution),
            mono=self._mono,
            detail={k: str(v) for k, v in self._info.items()},
        )

    def set_controls(self, controls: dict[str, Any]) -> None:
        if not self._open:
            raise RuntimeError(f"{self.cam_id}: camera not open")
        with self._lock:
            self._picam.set_controls(controls)

    def get_controls(self) -> dict[str, Any]:
        if not self._open:
            return {}
        return {k: str(v) for k, v in self._picam.camera_controls.items()}
