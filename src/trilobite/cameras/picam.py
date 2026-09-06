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

Nothing here decides what a raw buffer MEANS. The format is chosen at open time
and every buffer is put through `cameras/rawformat.py` before it can call
itself science data; this module only talks to the driver and applies the
orientation. That split exists so the admission rules can be tested from a byte
array with no Pi attached, which is where they get the attention they need.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

import numpy as np

from ..config import CameraConfig
from ..types import (
    DIAGNOSTIC,
    SRC_ISP_MAIN,
    SRC_ISP_PREVIEW,
    SRC_RAW,
    UNVALIDATED,
    CameraInfo,
    Frame,
)
from .base import CameraSource
from .rawformat import RawFormatError, admit, best_format, classify, describe_refusal

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
        # How the raw format was arrived at, in words, for the sidecar.
        self._raw_choice: str = "unknown"
        # True only when the format the driver NEGOTIATED was established
        # admissible after configuration. False means the escape hatch is open
        # and everything raw this source produces is `diagnostic` -- carried as
        # state rather than re-derived per capture, so a capture cannot
        # accidentally be judged by a different rule than the one the camera
        # was opened under.
        self._raw_admissible: bool = False
        # The format name asked for, kept so a driver substitution can be
        # named rather than merely absorbed.
        self._requested_raw: str | None = None
        # Read back from camera_configuration() after configure: what the raw
        # stream ACTUALLY is. Admission checks against this and nothing else.
        self._raw_negotiated: dict[str, Any] = {
            "format": "", "size": (0, 0), "stride": None}

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
        try:
            chosen = self._choose_raw_format(picam)
        except RawFormatError:
            # A refusal here means the camera must not open at all. Release the
            # device before the exception leaves, or the next attempt -- after
            # the operator has fixed the config -- finds libcamera reporting no
            # cameras and goes looking at ribbon cables.
            with contextlib.suppress(Exception):
                picam.close()
            raise
        if chosen:
            raw_stream["format"] = chosen

        config = picam.create_video_configuration(
            main={"size": self._full_res},
            lores={"size": tuple(self.cfg.preview_resolution), "format": "YUV420"},
            raw=raw_stream,
            buffer_count=4,
        )
        picam.configure(config)

        # Supervisory review R5. Everything above is a REQUEST. What the driver
        # negotiated is read back here and is the only thing admission is
        # allowed to check against, for two reasons the review names:
        #
        #   * the driver may substitute a format. Asking for R10 and being
        #     given MONO_PISP_COMP1 was the original failure, and validating
        #     the request rather than the result cannot see it;
        #   * the raw stream's size is not the main stream's. `full_resolution`
        #     in the config sizes `main`; raw is configured at the sensor's
        #     native size. Checking a raw buffer against the main resolution
        #     happens to work only while the two are equal.
        try:
            self._raw_negotiated = self._read_back_raw(picam)
        except RawFormatError:
            with contextlib.suppress(Exception):
                picam.close()
            raise

        if "MONO" in str(self._raw_negotiated.get("format", "")).upper():
            # A configured mono sensor reports a MONO_* raw format. That is a
            # firmer signal than the pre-configure sensor_format string.
            self._mono = True

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
            "%s: opened %s at %sx%s (mono=%s), preview %sx%s, raw %s -> %s",
            self.cam_id,
            self._info.get("Model", "unknown"),
            self._full_res[0],
            self._full_res[1],
            self._mono,
            *self.cfg.preview_resolution,
            self._raw_choice,
            "science" if self._raw_admissible else "DIAGNOSTIC ONLY",
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
                space="mono8",
                # ISP output. Nothing about it has been established as sensor
                # counts -- it has been through demosaic-equivalent processing,
                # gamma and whatever else the pipeline applies -- so it is
                # `unvalidated`, not `science`. It used to be `science` by
                # default, which is supervisory review R2: an ISP frame
                # inheriting the strongest claim in the system by omission.
                # Corner GEOMETRY off it is still defensible; the source kind
                # is what a reader needs to decide that, so it is recorded.
                source_kind=SRC_ISP_MAIN,
                stream="main", mono_sensor=self._mono,
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
                         source_kind=SRC_ISP_PREVIEW,
                         **self.orientation, **meta)

    def skip_preview(self) -> None:
        """Take a request and release it, decoding nothing.

        The pool has to be drained at sensor rate whatever the pipeline is
        doing, but `make_array` on the lores plane is a copy and the orient is
        another, and neither is wanted for a frame that is about to be thrown
        away. Metadata is still kept: auto-exposure state should not go stale
        just because the pipeline is running slower than the sensor.
        """
        if not self._open:
            return
        if self.full_frame_pending:
            # Somebody is waiting on this exposure. Take the slow path.
            self.read_preview()
            return
        with self._lock:
            request = self._picam.capture_request()
            try:
                self._last_meta = dict(request.get_metadata())
            finally:
                request.release()

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
        meta["raw_format_choice"] = self._raw_choice
        meta["allow_unvalidated_raw"] = bool(
            getattr(self.cfg, "allow_unvalidated_raw", False))

        if raw:
            source_kind = SRC_RAW
            data, evidence, validity = self._admit_raw(data)
            meta.update(evidence)
            # image_* describes the frame the caller ends up holding, so it is
            # the POST-rotation size, and it is derived from the RAW stream's
            # geometry rather than main's -- those are not the same stream and
            # were being conflated (R5).
            native = self._raw_negotiated.get("size") or self._full_res
            out_w, out_h = self.oriented_size(native)
            meta["image_width"] = int(out_w)
            meta["image_height"] = int(out_h)
        else:
            # The ISP path. Not admitted, not refused -- nothing was
            # established, which is exactly what `unvalidated` says.
            source_kind, validity = SRC_ISP_MAIN, UNVALIDATED
        meta.update(self.orientation)
        return Frame.now(
            np.ascontiguousarray(self._orient(data)), self.cam_id,
            self._next_seq(), space=space, validity=validity,
            source_kind=source_kind, **meta
        )

    def _choose_raw_format(self, picam: Any) -> str | None:
        """Pick a raw format that is established sensor counts, or refuse to open.

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

        The old version of this method **logged an error and continued** in
        both of the cases that matter -- a configured format that looked
        compressed, and no admissible format advertised at all. A log line is
        not a control: nobody reads the journal of a rig that appears to be
        working. So both are now refusals at open time:

          * an explicit `raw_format` in the config wins, but only if
            `rawformat.classify` says it is known, uncompressed and unpacked;
          * otherwise the sensor's advertised modes are put through
            `rawformat.best_format`, which takes the widest bit depth among the
            unpacked ones, so a 10-bit sensor is not quietly recorded at 8;
          * if neither yields an admissible format, `RawFormatError` is raised
            and the camera does not open.

        `allow_unvalidated_raw: true` in the config turns each refusal back
        into a warning, and the source then produces `diagnostic` frames for
        the rest of its life. That is the only way past this, and it is
        recorded in every sidecar it touches.

        **This method decides what to ASK FOR and nothing more.** It cannot
        establish admissibility, because the driver may negotiate something
        else entirely; that verdict belongs to `_read_back_raw`, after
        `configure`, and is supervisory review R5.

        Returns the format string to request, or None to accept the driver's
        default (only reachable with the hatch open).
        """
        hatch = bool(getattr(self.cfg, "allow_unvalidated_raw", False))

        def refuse(reason: str, choice: str) -> None:
            """Raise, or -- with the hatch open -- warn and mark diagnostic."""
            if not hatch:
                raise RawFormatError(f"{self.cam_id}: {reason}")
            log.error(
                "%s: %s -- opening anyway because allow_unvalidated_raw is set. "
                "Every raw capture from this camera will be tagged DIAGNOSTIC, "
                "named as such on disk, and refused by the offline readers.",
                self.cam_id, reason,
            )
            self._raw_choice = choice

        configured = (self.cfg.raw_format or "").strip()
        if configured:
            fmt = classify(configured)
            if fmt is None or not fmt.admissible:
                refuse(describe_refusal(configured),
                       f"{configured} (from config, UNVALIDATED)")
                return configured
            self._raw_choice = f"{fmt.name} (from config)"
            self._requested_raw = fmt.name
            log.info("%s: requesting raw format %s (from config; nominally "
                     "%d-bit, uncompressed)", self.cam_id, fmt.name, fmt.bits)
            return fmt.name

        candidates: list[str] = []
        try:
            for mode in picam.sensor_modes or []:
                # picamera2 gives both the packed transport format and the
                # unpacked name; either may be the admissible one, so both are
                # offered to best_format and it decides.
                for key in ("unpacked", "format"):
                    name = str(mode.get(key) or "").strip()
                    if name and name not in candidates:
                        candidates.append(name)
        except Exception as exc:                       # pragma: no cover - driver
            log.debug("%s: cannot read sensor_modes: %s", self.cam_id, exc)

        chosen = best_format(candidates)
        if chosen is None:
            refuse(
                f"no admissible raw format among the {len(candidates)} this "
                f"sensor advertises ({', '.join(candidates) or 'none'}), so "
                f"libcamera's default would be used -- on a Pi 5 that is very "
                f"likely MONO_PISP_COMP1, which is COMPRESSED and makes every "
                f"capture look like a corrupted image. Run "
                f"scripts/probe_cameras.py and set 'raw_format' by hand.",
                "libcamera default (UNVALIDATED)",
            )
            return None

        self._raw_choice = f"{chosen} (auto)"
        self._requested_raw = chosen
        log.info("%s: requesting raw format %s (chosen; uncompressed, unpacked)",
                 self.cam_id, chosen)
        return chosen

    def _read_back_raw(self, picam: Any) -> dict[str, Any]:
        """What the driver ACTUALLY negotiated for the raw stream (R5).

        picamera2 exposes the configured stream as a dict with `format`, `size`
        and `stride`. All three are read here, once, after `configure`, and
        every later buffer is checked against them rather than against what was
        asked for. A driver substitution -- being handed a compressed format
        after requesting an uncompressed one -- is caught here and nowhere
        else, because from that point on the buffers are self-consistent with
        the substituted format and look perfectly correct.

        `stride` may be absent on older picamera2 builds. Its absence is
        recorded so admission can say which rule it applied, rather than
        quietly falling back to a looser one.
        """
        try:
            raw_cfg = dict(picam.camera_configuration()["raw"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise RawFormatError(
                f"{self.cam_id}: the camera reports no raw stream after "
                f"configuration ({exc}). Nothing can be admitted as sensor "
                f"data without knowing what the driver negotiated."
            ) from None

        name = str(raw_cfg.get("format") or "")
        size = raw_cfg.get("size") or (0, 0)
        stride = raw_cfg.get("stride")
        negotiated = {
            "format": name,
            "size": (int(size[0]), int(size[1])),
            "stride": int(stride) if stride else None,
        }

        fmt = classify(name)
        if fmt is None or not fmt.admissible:
            reason = (f"the driver negotiated {name!r} for the raw stream, "
                      f"not what was requested ({self._raw_choice}). "
                      f"{describe_refusal(name)}")
            if not bool(getattr(self.cfg, "allow_unvalidated_raw", False)):
                raise RawFormatError(f"{self.cam_id}: {reason}")
            log.error(
                "%s: %s -- opening anyway because allow_unvalidated_raw is "
                "set; every raw capture will be DIAGNOSTIC.", self.cam_id, reason)
            self._raw_admissible = False
        else:
            if self._requested_raw and fmt.name != self._requested_raw:
                # Admissible, but not what was asked for. Not a refusal -- the
                # data is still readable -- but it must not pass unremarked,
                # because a silent downgrade from R10 to R8 throws away two
                # bits per pixel and nothing downstream would ever say so.
                log.warning(
                    "%s: requested raw format %s, driver negotiated %s. The "
                    "capture is admissible but it is not the format the config "
                    "asked for.", self.cam_id, self._requested_raw, fmt.name)
                self._raw_choice = (f"{fmt.name} (negotiated; "
                                    f"{self._requested_raw} was requested)")
            self._raw_admissible = True

        if negotiated["size"] != tuple(self._full_res):
            # Not an error. It is the ordinary case once `full_resolution` is
            # set smaller than the sensor, and the point of recording it is
            # that admission must use the raw size, not this one.
            log.info(
                "%s: raw stream is %dx%d and main is %dx%d; admission uses the "
                "raw geometry.", self.cam_id, negotiated["size"][0],
                negotiated["size"][1], self._full_res[0], self._full_res[1])
        log.info("%s: negotiated raw %s %dx%d stride %s", self.cam_id, name,
                 negotiated["size"][0], negotiated["size"][1],
                 negotiated["stride"] if negotiated["stride"] else "unreported")
        return negotiated

    def _admit_raw(self, data: np.ndarray) -> tuple[np.ndarray, dict[str, Any], str]:
        """Read the buffer as an image and grade what could be established.

        Returns (pixels, evidence, validity). Three outcomes, not two:

          * **science** -- everything reconciled.
          * **unvalidated** -- it was read, and something did not reconcile.
            The reasons are in `raw_reservations` and the pixels are the best
            reading available. No opt-in needed: the capture succeeds, because
            every failure in this class is visible on screen and an operator
            needs the frame in order to see it.
          * **diagnostic** -- the FORMAT says these bytes are not pixel values.
            Compressed, packed or unknown. Needs `allow_unvalidated_raw`,
            because there is no reading to produce and the failure is invisible
            by construction. This is the case that cost a recording session.

        The middle one is new and it fixes a real regression. The previous
        version refused on any failure and, with the hatch open, handed back
        the buffer UNTOUCHED -- so a 10-bit frame whose stride did not
        reconcile went to disk as a 2944-wide uint8 array and displayed as
        white noise with row structure. Refusing to vouch for values is not a
        reason to refuse to interpret bytes.

        Admission runs on the buffer exactly as the sensor delivered it, BEFORE
        orientation, and that order is load-bearing. The stride padding is on
        the right of the buffer as it comes off the sensor; flip first and it
        would be on the left, rotate first and it would be along the bottom --
        and cropping the right would then remove real image.
        """
        neg = self._raw_negotiated
        fmt_name = neg.get("format") or self._raw_format_name()
        try:
            got = admit(
                data, fmt_name, neg.get("size") or self._full_res,
                stride_bytes=neg.get("stride"),
                alignment=self.cfg.raw_alignment,
            )
        except RawFormatError as exc:
            hatch = bool(getattr(self.cfg, "allow_unvalidated_raw", False))
            if not hatch:
                raise RawFormatError(
                    f"{self.cam_id}: these bytes are not pixel values, so "
                    f"there is no capture to return. {exc} Set "
                    f"'allow_unvalidated_raw: true' for this camera to capture "
                    f"them as a diagnostic frame instead."
                ) from None
            log.warning("%s: raw buffer is not pixel data: %s", self.cam_id, exc)
            return data, {
                "raw_admitted": False,
                "raw_refusal": str(exc),
                "raw_buffer_shape": [int(n) for n in data.shape],
                "raw_buffer_dtype": str(data.dtype),
                "raw_negotiated_format": fmt_name,
                "raw_negotiated_size": list(neg.get("size") or self._full_res),
                "raw_negotiated_stride": neg.get("stride"),
            }, DIAGNOSTIC

        if got.reservations:
            # Once per capture, at warning level, listing every reason. Not an
            # error: the frame is usable for looking at and the operator is the
            # one who has to decide what it means.
            log.warning(
                "%s: raw frame read but NOT admitted as science data (%d "
                "reservation(s)): %s", self.cam_id, len(got.reservations),
                "; ".join(got.reservations))
        return got.array, dict(got.meta), got.validity

    def describe(self) -> CameraInfo:
        return CameraInfo(
            cam_id=self.cam_id,
            model=str(self._info.get("Model", "unknown")),
            backend="picamera2",
            # POST-rotation, deliberately. `self._full_res` is what the sensor
            # delivers and is what the stream is configured with; this is what
            # every consumer of a frame is actually holding. A quarter turn
            # makes them different, and the MLA reference frame, the readiness
            # checks and the session manifest all read from here.
            full_resolution=self.oriented_size(self._full_res),
            preview_resolution=self.oriented_size(tuple(self.cfg.preview_resolution)),
            mono=self._mono,
            detail={
                **{k: str(v) for k, v in self._info.items()},
                "dropped_controls": ", ".join(self._dropped_controls) or "none",
                # In the manifest and on the UI card, because "what are these
                # pixels" is not answerable after the fact and the answer
                # changed silently once already.
                "raw_format": self._raw_choice,
                "raw_validity": "science" if self._raw_admissible else "diagnostic",
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
