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
        self._dropped_controls: list[str] = []
        self._last_meta: dict[str, Any] = {}

    @staticmethod
    def _split_controls(picam: Any, controls: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Partition requested controls into (supported, unsupported names)."""
        advertised = set(picam.camera_controls)
        supported = {k: v for k, v in controls.items() if k in advertised}
        dropped = sorted(set(controls) - advertised)
        return supported, dropped

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
        if not available:
            # Zero cameras is a different failure from "index too high", and it
            # has a different first suspect. libcamera opens the media devices
            # exclusively while enumerating, so another process holding them
            # makes the cameras disappear entirely rather than fail to acquire
            # -- which reads as absent hardware and sends you to the cables
            # when the real cause is a stale process.
            raise RuntimeError(
                f"{self.cam_id}: libcamera reports no cameras at all. Most likely "
                f"another process is holding them (a previous run, or the "
                f"trilobite service) -- check with "
                f"\"pgrep -af 'trilobite|rpicam'\". Otherwise the overlays may be "
                f"missing from /boot/firmware/config.txt, or a ribbon is loose. "
                f"Run 'bash scripts/diagnose_cameras.sh' for a full diagnosis."
            )
        if self.cfg.index >= len(available):
            raise RuntimeError(
                f"{self.cam_id}: camera index {self.cfg.index} requested but "
                f"libcamera reports {len(available)}: {available}. Check the "
                f"'index' values in the config against "
                f"'python scripts/probe_cameras.py'."
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
        self._mono = raw_fmt.startswith(("R", "Y", "MONO")) and not any(
            p in raw_fmt for p in ("RGGB", "BGGR", "GRBG", "GBRG")
        )

        raw_stream: dict[str, Any] = {"size": sensor_res}
        if self.cfg.raw_format:
            # Explicit format, because libcamera's default on a Pi 5 is
            # MONO_PISP_COMP1 -- companded, not linear, and wrong for anything
            # that fits a model to pixel values.
            raw_stream["format"] = self.cfg.raw_format

        config = picam.create_video_configuration(
            main={"size": self._full_res},
            lores={"size": tuple(self.cfg.preview_resolution), "format": "YUV420"},
            raw=raw_stream,
            buffer_count=4,
        )
        picam.configure(config)

        # A configured mono sensor reports a MONO_* raw format. That is a
        # firmer signal than the pre-configure sensor_format string, so let it
        # override.
        try:
            if "MONO" in str(picam.camera_configuration()["raw"]["format"]).upper():
                self._mono = True
        except (KeyError, TypeError):
            pass

        controls: dict[str, Any] = {}
        if self.cfg.fps:
            # libcamera takes a frame duration range in microseconds.
            dur = int(1_000_000 / self.cfg.fps)
            controls["FrameDurationLimits"] = (dur, dur)
        controls.update(self.cfg.controls)

        # Not every sensor advertises every control, and picamera2 raises on an
        # unknown name rather than ignoring it. A mono IMX296 has no colour
        # processing at all, so AwbEnable, ColourGains and Saturation simply do
        # not exist on it -- setting one aborts startup. Rather than encode
        # per-sensor knowledge here, ask the camera what it supports and drop
        # the rest with a warning. That is what lets one config file serve both
        # a mono and a colour rig.
        supported, dropped = self._split_controls(picam, controls)
        if dropped:
            log.warning(
                "%s: sensor does not advertise these controls, ignoring: %s",
                self.cam_id,
                ", ".join(dropped),
            )
        self._dropped_controls = dropped
        if supported:
            picam.set_controls(supported)

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
        # Kept so that turning auto-exposure off can pin the values AE just
        # chose -- see set_controls.
        self._last_meta = meta
        return Frame.now(luma, self.cam_id, self._next_seq(), space="mono8", **meta)

    def read_full_mono(self) -> Frame | None:
        """Full-resolution luma from the `main` stream, for corner detection.

        Pulls from the same request pool as the preview, so it costs one extra
        buffer copy and no sensor reconfiguration -- which is the only reason
        this can run in a loop at all. A mode switch per detection pass would
        stall the preview for hundreds of milliseconds each time.

        `main` is configured without an explicit format, so on a Pi 5 it comes
        back as XBGR8888: four channels, all carrying the same luma for a mono
        sensor. Slicing one plane is a 1.5 MB copy; a colour conversion would
        be several times that for an identical result.
        """
        if not self._open:
            return None
        with self._lock:
            request = self._picam.capture_request()
            try:
                data = request.make_array("main")
                meta = dict(request.get_metadata())
            finally:
                request.release()
        if data.ndim == 3:
            data = data[..., 0]
        meta["stream"] = "main"
        meta["mono_sensor"] = self._mono
        return Frame.now(
            np.ascontiguousarray(data), self.cam_id, self._next_seq(), space="mono8", **meta
        )

    def capture_full(self, raw: bool = True) -> Frame:
        if not self._open:
            raise RuntimeError(f"{self.cam_id}: camera not open")
        stream = "raw" if raw else "main"
        with self._lock:
            request = self._picam.capture_request()
            try:
                try:
                    data = request.make_array(stream)
                except Exception as exc:
                    # picamera2 cannot decode every raw format into an array --
                    # notably MONO_PISP_COMP1, the Pi 5 default. Say exactly
                    # what happened and what to do, rather than surfacing a
                    # bare "format not supported" from three layers down.
                    fmt = self._raw_format_name()
                    raise RuntimeError(
                        f"{self.cam_id}: cannot decode the {stream!r} stream "
                        f"(format {fmt!r}). If this is a PiSP compressed format, "
                        f"set 'raw_format' in the camera config to an uncompressed "
                        f"one from 'probe_cameras.py' (e.g. R10), or capture the "
                        f"processed stream instead."
                    ) from exc
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
            detail={
                **{k: str(v) for k, v in self._info.items()},
                "dropped_controls": ", ".join(self._dropped_controls) or "none",
            },
        )

    def set_controls(self, controls: dict[str, Any]) -> None:
        if not self._open:
            raise RuntimeError(f"{self.cam_id}: camera not open")

        controls = self._resolve_ae(dict(controls))

        with self._lock:
            # Unlike startup, a runtime request naming an unsupported control
            # is a mistake worth reporting: the caller asked for something
            # specific and silently ignoring it would be misleading. The web
            # layer turns this into a 422 naming what is actually available.
            supported, dropped = self._split_controls(self._picam, controls)
            if dropped:
                raise ValueError(
                    f"{self.cam_id}: control(s) not supported by this sensor: "
                    f"{', '.join(dropped)}. Available: "
                    f"{', '.join(sorted(self._picam.camera_controls))}"
                )
            self._picam.set_controls(supported)
        self._requested.update(controls)

    def _resolve_ae(self, controls: dict[str, Any]) -> dict[str, Any]:
        """Make auto-exposure transitions actually take effect.

        Two libcamera behaviours make a bare AeEnable toggle unreliable, and
        both show up as "auto-exposure will not turn off":

        1. Switching AE off does not by itself pin the exposure. The AE
           algorithm stops updating, but ExposureTime and AnalogueGain are left
           at whatever they were, and some pipelines then drift or revert to a
           default. The fix is to send the *current* values -- the ones AE just
           converged on -- in the same call. That is also the behaviour you
           want: "stop here", not "stop and jump somewhere else".

        2. Setting ExposureTime while AE is on is contradictory; AE overwrites
           it on the next frame, so the control appears dead. Asking for a
           manual exposure therefore implies AE off, and we make that explicit
           rather than letting the request silently evaporate.
        """
        ae = controls.get("AeEnable")

        if ae is False:
            meta = self._last_meta
            if "ExposureTime" not in controls and "ExposureTime" in meta:
                controls["ExposureTime"] = int(meta["ExposureTime"])
            if "AnalogueGain" not in controls and "AnalogueGain" in meta:
                controls["AnalogueGain"] = float(meta["AnalogueGain"])
            log.info(
                "%s: auto-exposure off, pinning ExposureTime=%s AnalogueGain=%s",
                self.cam_id,
                controls.get("ExposureTime"),
                controls.get("AnalogueGain"),
            )
        elif ae is None and self.auto_exposure and (
            "ExposureTime" in controls or "AnalogueGain" in controls
        ):
            controls["AeEnable"] = False
            log.info("%s: manual exposure requested, turning auto-exposure off", self.cam_id)
        elif ae is True:
            # Do not send a manual exposure alongside a request to automate it.
            for key in ("ExposureTime", "AnalogueGain"):
                controls.pop(key, None)
        return controls

    def _raw_format_name(self) -> str:
        try:
            return str(self._picam.camera_configuration()["raw"]["format"])
        except Exception:
            return "unknown"

    def control_spec(self) -> dict[str, dict[str, Any]]:
        if not self._open:
            return {}
        spec: dict[str, dict[str, Any]] = {}
        for name, limits in self._picam.camera_controls.items():
            # libcamera reports each control as (min, max, default); default
            # may be None for controls with no defined resting value.
            try:
                lo, hi, default = limits
            except (TypeError, ValueError):
                continue
            if isinstance(lo, bool) or isinstance(hi, bool):
                spec[name] = {
                    "type": "boolean",
                    "default": bool(default) if default is not None else False,
                }
            elif isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                spec[name] = {
                    "type": "integer" if isinstance(lo, int) and isinstance(hi, int) else "number",
                    "minimum": lo,
                    "maximum": hi,
                    "default": default,
                }
        return spec

    def get_controls(self) -> dict[str, Any]:
        if not self._open:
            return {}
        return {k: str(v) for k, v in self._picam.camera_controls.items()}
