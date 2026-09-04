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


def _is_compressed_raw(name: str) -> bool:
    """Is this raw format one of the Pi 5 pipeline's COMPRESSED transports?

    `MONO_PISP_COMP1`, `PISP_COMP1_*` and friends carry compressed bytes, not
    pixel values. Matched by name because that is what libcamera exposes, and
    the naming is stable: everything compressed carries PISP and COMP.
    """
    u = name.upper()
    return "PISP" in u or "COMP" in u


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
        chosen = self._choose_raw_format(picam)
        if chosen:
            raw_stream["format"] = chosen

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
        want_full = self.full_frame_pending
        with self._lock:
            request = self._picam.capture_request()
            try:
                yuv = request.make_array("lores")
                meta = dict(request.get_metadata())
                # The full-resolution stream comes out of the SAME request, so
                # it is the same exposure as the preview and costs no extra
                # camera access. This is the only place `main` is ever read in
                # the streaming path -- see the note in cameras/base.py about
                # why a second consumer is not allowed to exist.
                full = request.make_array("main") if want_full else None
            finally:
                # Requests are a finite pool. Holding one starves the sensor.
                request.release()

        # One sequence number for both, because they are one exposure. That is
        # the property the whole handshake exists to preserve: a pose's full
        # frame and the presence map that triggered it describe the same
        # instant, not two instants a frame apart.
        seq = self._next_seq()

        if full is not None:
            if full.ndim == 3:
                full = full[..., 0]
            self._serve_full_frame(Frame.now(
                self._orient(np.ascontiguousarray(full)), self.cam_id, seq,
                space="mono8", stream="main", mono_sensor=self._mono,
                **self.orientation, **meta,
            ))

        # lores is YUV420; the first height rows are the luma plane. For a mono
        # sensor that plane *is* the image, and for a colour sensor it is a
        # perfectly good preview. Slicing beats a colour conversion.
        h = int(self.cfg.preview_resolution[1])
        luma = np.ascontiguousarray(yuv[:h, : self.cfg.preview_resolution[0]])
        # Kept so that turning auto-exposure off can pin the values AE just
        # chose -- see set_controls.
        self._last_meta = meta
        return Frame.now(self._orient(luma), self.cam_id, seq, space="mono8",
                         **self.orientation, **meta)


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
        # What the pixels ARE. Without this a .npy does not say whether its
        # values are linear sensor counts or a compressed transport format,
        # and those are indistinguishable by inspection -- which is exactly how
        # a session was recorded in MONO_PISP_COMP1 and only noticed later.
        meta["raw_format"] = self._raw_format_name()
        meta["raw_format_choice"] = getattr(self, "_raw_choice", "unknown")
        if raw:
            # Trim BEFORE orienting. The stride padding is on the right of the
            # buffer as it comes off the sensor; flip first and it would be on
            # the left, and cropping the right would then remove real image.
            data, pad = self._trim_stride(data)
            meta.update(pad)
        meta.update(self.orientation)
        return Frame.now(
            np.ascontiguousarray(self._orient(data)), self.cam_id,
            self._next_seq(), space=space, **meta
        )

    def _choose_raw_format(self, picam: Any) -> str | None:
        """Pick an uncompressed raw format, and never a PiSP-compressed one.

        **This is the bug that corrupted a whole recording session.** On a Pi 5
        libcamera's default raw format for the mono IMX296 is
        `MONO_PISP_COMP1`. PISP_COMP1 is not raw sensor data in any useful
        sense: it is the Pi 5 imaging pipeline's *compressed* transport format,
        one byte per pixel, produced to save memory bandwidth. The same code
        on a Pi 4 got `R10` and worked, which is why this only appeared after
        the move to the 5.

        `make_array` hands those bytes back as a plain uint8 image, and nothing
        anywhere says they are compressed. The result has the right shape, the
        right size, and obvious structure in it -- so it looks like a picture
        that has gone slightly wrong rather than like a decode failure, which
        is the worst possible failure mode. Every value is wrong, so anything
        fitted to those pixels is fitted to an artefact.

        So the format is chosen here rather than left to libcamera:

          * an explicit `raw_format` in the config always wins, but is checked
            and warned about if it looks compressed;
          * otherwise the sensor's advertised modes are searched for an
            uncompressed one, preferring the **unpacked** variant (`R10` over
            `R10_CSI2P`) so the array comes back as plain uint16 with no
            bit-unpacking left to do;
          * if nothing suitable is advertised, libcamera's default is used and
            a loud warning is logged, because silence is what caused this.

        Returns the format string to request, or None to accept the default.
        """
        configured = (self.cfg.raw_format or "").strip()
        if configured:
            if _is_compressed_raw(configured):
                log.error(
                    "%s: raw_format is set to %r, which is a PiSP COMPRESSED "
                    "format. Captures will contain compressed bytes, not pixel "
                    "values -- they will look like a damaged image. Set an "
                    "uncompressed format (run scripts/probe_cameras.py to see "
                    "what this sensor offers) or remove the setting to let one "
                    "be chosen automatically.",
                    self.cam_id, configured,
                )
            self._raw_choice = f"{configured} (from config)"
            return configured

        candidates: list[str] = []
        try:
            for mode in picam.sensor_modes or []:
                # picamera2 gives both the packed transport format and the
                # unpacked name; the unpacked one is what we want to be handed.
                for key in ("unpacked", "format"):
                    name = str(mode.get(key) or "").strip()
                    if name and not _is_compressed_raw(name) and name not in candidates:
                        candidates.append(name)
        except Exception as exc:                       # pragma: no cover - driver
            log.debug("%s: cannot read sensor_modes: %s", self.cam_id, exc)

        # Prefer unpacked (no _CSI2P suffix), then the widest bit depth, so a
        # 10-bit sensor is not quietly recorded at 8.
        def rank(name: str) -> tuple[int, int]:
            packed = 1 if name.upper().endswith("_CSI2P") else 0
            digits = "".join(c for c in name if c.isdigit())
            return (packed, -int(digits or 0))

        for name in sorted(candidates, key=rank):
            log.info("%s: raw format %s (chosen; uncompressed)", self.cam_id, name)
            self._raw_choice = f"{name} (auto)"
            return name

        log.error(
            "%s: no uncompressed raw format is advertised by this sensor, so "
            "libcamera's default will be used -- on a Pi 5 that is very likely "
            "MONO_PISP_COMP1, which is COMPRESSED and will make every capture "
            "look like a corrupted image. Run scripts/probe_cameras.py and set "
            "'raw_format' in the config by hand.",
            self.cam_id,
        )
        self._raw_choice = "libcamera default (UNCHECKED)"
        return None

    def _trim_stride(self, data: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
        """Turn a raw buffer into an image: strip the row padding, and get the
        pixel size right.

        A raw buffer's rows are padded out to a hardware-friendly stride --
        64 bytes on this pipeline -- and `make_array` shapes the array by that
        stride **in bytes**, not in pixels, and hands it back as uint8 whatever
        the real pixel size is. Two things therefore have to be undone, and the
        second one is the one that was missed:

            8-bit  (R8)   1456 px = 1456 bytes -> stride 1472 -> array 1472 wide
            10-bit (R10)  1456 px = 2912 bytes -> stride 2944 -> array 2944 wide

        In the second case the array is not "1456 pixels plus padding", it is
        1472 *uint16* pixels laid out as 2944 bytes. Treating its width as a
        pixel count and cropping to 1456 would keep the first 728 pixels and
        half of the 729th -- structure, at the wrong scale, which is what a
        2.022:1.000 aspect ratio in the reader was reporting.

        So: work out the bytes per pixel from the row length, re-view the
        buffer at that pixel size, and only then remove the padding columns.

        Left undone, the padding does two things, both silent: it makes any
        rescale of the MLA grid onto the frame anisotropic, and it moves the
        frame *centre* -- which the whole grid hangs off -- by half the padding,
        putting every micro-image out by a quarter of a checkerboard square.

        A format whose row length matches no whole bytes-per-pixel is left
        alone and flagged. Packed 10-bit (5 bytes per 4 pixels) is the case
        that matters: its width is not a pixel count at any scale, and cropping
        it would be nonsense.
        """
        full_w, full_h = self._full_res
        if data.ndim < 2:
            return data, {}
        h, w = data.shape[:2]
        note: dict[str, Any] = {"image_width": int(full_w), "image_height": int(full_h)}

        if w == full_w and h == full_h:
            return data, note
        if h != full_h:
            return data, {**note, **self._unexpected(data)}

        row_bytes = w * data.dtype.itemsize
        # 64-byte stride alignment means at most 63 bytes of padding; the
        # allowance is generous because the alignment is not ours to promise.
        for bpp in (1, 2):
            want = full_w * bpp
            if not (want <= row_bytes <= want + 256):
                continue
            out = data
            if bpp == 2 and data.dtype.itemsize == 1:
                if w % 2:
                    break                       # cannot be whole uint16 pixels
                # A C-contiguous uint8 row of 2*N bytes IS N little-endian
                # uint16 pixels. This is a re-view, not a conversion: no copy,
                # no arithmetic, and the values become the 10-bit counts the
                # sensor actually produced instead of their halves.
                out = np.ascontiguousarray(data).view(np.uint16)
            px_w = out.shape[1]
            note.update(
                raw_stride_bytes=int(row_bytes),
                raw_bytes_per_pixel=int(bpp),
                raw_stride_px=int(px_w),
                raw_padding_px=int(px_w - full_w),
            )
            if px_w < full_w:
                return data, {**note, **self._unexpected(data)}
            return out[:, :full_w], note

        return data, {**note, **self._unexpected(data)}

    def _unexpected(self, data: np.ndarray) -> dict[str, Any]:
        """Record and complain about a buffer shape we cannot interpret."""
        h, w = data.shape[:2]
        full_w, full_h = self._full_res
        log.warning(
            "%s: raw buffer is %dx%d of %s (%d bytes per row) and the sensor is "
            "%dx%d. That is not a whole number of bytes per pixel plus a stride "
            "pad, so it is being saved untouched -- most likely a PACKED format "
            "(10-bit as 5 bytes per 4 pixels), whose width is not a pixel count "
            "at any scale. Set 'raw_format' to an unpacked one; "
            "scripts/probe_cameras.py lists what this sensor offers.",
            self.cam_id, w, h, data.dtype, w * data.dtype.itemsize, full_w, full_h,
        )
        return {"raw_buffer_shape": [int(h), int(w)], "raw_unexpected_shape": True}

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
