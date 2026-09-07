"""Wiring. One CameraRuntime per camera, an Application that owns them all.

The capture thread does exactly three things: read a frame, run the pipeline,
publish to the bus. It never encodes, never writes to disk, never touches the
network. Everything expensive happens on a consumer thread, so preview or
storage problems cannot perturb capture timing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import net
from .acquisition import (
    CONTROL_DEADLINE_S,
    STILL_DEADLINE_S,
    CameraOwner,
)
from .bus import LatestFrame
from .calibration import CalibrationSettings, DerivedOptics, readiness_report
from .cameras.base import CameraSource
from .cameras.registry import build_camera
from .config import AppConfig, CameraConfig
from .health import host_health
from .optics.orientation import Orientation
from .processing.pipeline import Pipeline
from .state import StateStore
from .storage.writer import SessionWriter
from .types import DIAGNOSTIC, Frame

log = logging.getLogger(__name__)


def _round_or_none(v: float | None, places: int = 2) -> float | None:
    """Round, but keep None meaning "never happened" rather than "zero ago"."""
    return None if v is None else round(v, places)


class RateMeter:
    """Rolling frame-rate estimate over a short window, that goes to zero.

    The going-to-zero is the point, and it was missing. The estimate was
    computed from the last N samples with no reference to the present, so a
    camera that stopped an hour ago went on reporting the rate it had when it
    stopped -- for as long as the process lived. The dashboard showed 12 fps
    next to a frozen image, which is a worse answer than showing nothing: it
    actively argues the rig is fine.

    `stale_after` is generous on purpose. It has to be longer than the slowest
    legitimate gap -- a 1 Hz source, a paused pipeline -- or the number would
    flicker to zero during ordinary operation and be ignored thereafter.
    """

    def __init__(self, window: int = 60, stale_after: float = 3.0) -> None:
        self._t = deque(maxlen=window)
        self._stale_after = stale_after

    def tick(self) -> None:
        self._t.append(time.monotonic())

    @property
    def fps(self) -> float:
        if len(self._t) < 2:
            return 0.0
        if time.monotonic() - self._t[-1] > self._stale_after:
            return 0.0
        span = self._t[-1] - self._t[0]
        return (len(self._t) - 1) / span if span > 0 else 0.0

    @property
    def last(self) -> float | None:
        """`time.monotonic()` of the most recent tick, or None."""
        return self._t[-1] if self._t else None

    @property
    def age(self) -> float | None:
        """Seconds since the most recent tick. None if there has never been one."""
        return None if not self._t else time.monotonic() - self._t[-1]


class CameraRuntime:
    """A camera, its preview pipeline, its capture thread and its output slot."""

    def __init__(
        self,
        cfg: CameraConfig,
        writer: SessionWriter,
        process_fps: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.cam_id = cfg.cam_id
        # How often the PIPELINE runs, which is not how often the sensor is
        # read. See CameraConfig.process_fps: the per-camera setting wins, then
        # the server's preview rate (the only rate anything consumes); 0 or
        # None anywhere in that chain means "every frame", the old behaviour.
        rate = cfg.process_fps if cfg.process_fps is not None else process_fps
        self.process_interval = 1.0 / rate if rate and rate > 0 else 0.0
        self.skipped = 0
        self.label = cfg.label or cfg.cam_id.replace("_", " ").title()
        self.source: CameraSource = build_camera(cfg)
        self.pipeline = Pipeline.from_config(cfg.pipeline)
        self.bind_pipeline()
        self.preview = LatestFrame()
        self.writer = writer
        # Two meters, because two different things are being asked about.
        # `rate` counts frames the PIPELINE produced -- what the browser sees.
        # `acquired` counts frames taken from the SENSOR, including the ones
        # the rate cap released without decoding. Reporting the configured fps
        # as the sensor rate, which is what status did before, answers "what
        # did you ask for" while looking like an answer to "what is happening".
        self.rate = RateMeter()
        self.acquired = RateMeter()
        self.errors = 0
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # The single owner of `self.source`. Every call that reaches the SDK
        # goes through it; see acquisition.py for why serialising with a mutex
        # was not the same thing. Replaces the old `_capture_lock`, which made
        # two threads take turns rather than making one responsible.
        self.owner = CameraOwner(self.cam_id)
        # Snapshotted at open, on the one thread that exists at that moment, so
        # the UI can ask for it without a round-trip through the queue. It is
        # a property of the configuration and does not change while running.
        self._control_spec: dict[str, dict[str, Any]] = {}
        # 'running' | 'stopped' | 'failed-stop'. The third is the one that
        # matters: a camera whose thread would not join is NOT stopped, and
        # closing it anyway races a thread that may be inside capture_request.
        self.lifecycle = "stopped"

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        # Opened here, before the capture thread exists, so there is still
        # exactly one thread in the process touching this camera. That is why
        # open does not need to go through the queue -- the invariant holds by
        # construction rather than by enforcement.
        self.source.open()
        self._control_spec = self.source.control_spec()
        # The MLA parameters are in SENSOR pixels, so the sensor frame has to be
        # declared before any of them mean anything -- here, once, from the
        # camera itself, rather than inferred from whatever frame arrives first.
        # A stored alignment expressed against some other frame (an old config
        # in preview pixels, or a changed sensor mode) is rebased now, once,
        # with a warning.
        mla = self.mla_stage()
        if mla is not None:
            w, h = self.source.describe().full_resolution
            mla.bind_sensor(int(w), int(h), Orientation.of(self.cfg))
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"capture-{self.cam_id}", daemon=True
        )
        self._thread.start()
        self.lifecycle = "running"
        log.info("%s: capture thread started", self.cam_id)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop, then close -- and only ever in that order.

        The old version joined with a timeout and closed the source regardless
        of whether the join succeeded. That is a `close()` racing a thread that
        may be inside `capture_request()`, which is a segfault rather than an
        error message.

        A join that times out is **failed-stop**, not stopped. The camera stays
        held and says so. A device an operator can see is still held beats a
        crash they cannot diagnose, and it is also the honest report: the
        thread really is still running.
        """
        self._stop.set()
        # Refuse queued work first, so a caller blocked on a still is told the
        # camera is going away instead of waiting out its deadline.
        self.owner.retire()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self.lifecycle = "failed-stop"
                self.last_error = (
                    f"capture thread did not stop within {timeout:.0f} s; the "
                    f"camera is still held and has NOT been closed")
                log.error("%s: %s", self.cam_id, self.last_error)
                return
        self.source.close()
        self.lifecycle = "stopped"
        log.info("%s: stopped", self.cam_id)

    def _run(self) -> None:
        """Read, process, publish -- with the middle step rate-limited.

        The sensor must be drained at its own rate: a picamera2 request left
        unreleased starves a four-deep pool and stalls capture. The *pipeline*
        need only run as often as something reads the result, which is the
        browser at `server.preview_fps`. Running it on every frame instead --
        stats, levels, the grid overlay and a ~3 ms presence map, twice over
        for two cameras -- is 60 passes a second on four cores that also have
        to encode JPEG and answer the web API, which is why editing a parameter
        felt slow while the preview itself looked fine.

        So a frame arriving early is released without being decoded, and the
        count of those is reported in status(): if `skipped` is not roughly
        (sensor fps - process fps) x uptime, the cap is not doing what it says.
        """
        self.owner.adopt()
        try:
            self._loop()
        finally:
            # Whatever ended the loop -- stop, or an exception that escaped the
            # per-iteration handler -- nothing else may now reach the camera
            # through this owner, and anything queued must be told.
            self.owner.retire()

    def _loop(self) -> None:
        backoff = 0.1
        due = 0.0
        while not self._stop.is_set():
            try:
                # Serviced before the preview read, so a still takes the next
                # request rather than waiting a further frame period. Bounded,
                # because the sensor has to be drained at its own rate whatever
                # else is pending.
                self.owner.service()
                if self.process_interval > 0:
                    now = time.monotonic()
                    if now < due:
                        self.source.skip_preview()
                        self.skipped += 1
                        # A skipped frame is still an exposure that arrived, so
                        # it counts towards the measured sensor cadence. Only
                        # counting processed frames would make the cap look
                        # like a stalling sensor.
                        self.acquired.tick()
                        continue
                    # Advance the deadline by exactly one interval rather than
                    # restarting it from now. 12 Hz does not divide 30 Hz, so a
                    # "now + interval" deadline always lands just after a frame
                    # and quantises down to 10 Hz; accumulating instead makes
                    # the gaps alternate 100/67 ms and the mean come out at the
                    # rate that was asked for. `max` drops the arrears after a
                    # stall, so recovery is not a burst.
                    due = max(now, due + self.process_interval)
                frame = self.source.read_preview()
                if frame is None:
                    time.sleep(0.05)
                    continue
                self.acquired.tick()
                frame = self.pipeline(frame)
                self.preview.publish(frame)
                self.rate.tick()
                backoff = 0.1
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("%s: capture loop error", self.cam_id)
                # Back off so a persistent hardware fault does not spin the CPU
                # while still recovering quickly from a transient one.
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)

    # -- actions --------------------------------------------------------

    def grab_still(self, raw: bool = True) -> Frame:
        """One full-resolution frame, taken by the owner thread.

        Split from the save deliberately. The camera work happens on the owner
        thread and the disk work does not: a write to a slow USB stick must
        never be holding an SDK request, and it must never be what a second
        capture is queued behind.
        """
        return self.owner.submit(
            "still", lambda: self.source.capture_full(raw=raw),
            STILL_DEADLINE_S,
        ).result

    def save_frame(self, frame: Frame, tag: str) -> dict[str, Any]:
        """Write a frame the owner already produced. Caller's thread."""
        return self.writer.save_still(
            frame,
            pipeline_settings=self.pipeline.settings_snapshot(),
            camera_info=self.source.describe().as_dict(),
            tag=tag,
            label=self.label,
        )

    def capture_still(self, raw: bool = True, tag: str = "still") -> dict[str, Any]:
        """Full-resolution capture, saved with full provenance.

        Two steps on two threads: the owner takes the frame, this thread writes
        it. `capture_all` uses the halves separately so both heads are asked
        before either is written -- the old version completed each camera's
        disk write before requesting the next, which put storage latency inside
        the pair skew.
        """
        return self.save_frame(self.grab_still(raw=raw), tag)

    def capture_preview(self, tag: str = "view") -> dict[str, Any]:
        """Save the preview frame exactly as displayed -- post-pipeline.

        Distinct from capture_still on purpose. This is low-resolution, gamma
        shaped, possibly with a grid drawn on it: a record of *what you were
        looking at*, useful for lab notes and for documenting an alignment
        state. It is not measurement data. Never fit anything to these.

        That last sentence is now enforced rather than written down: the frame
        is stamped `diagnostic` on its way out, so it is named
        `diagnostic_view_...` on disk and both offline readers refuse it. It
        used to rely on the reader noticing `space` and the pipeline block,
        which is the same shape of mistake as trusting a buffer because it has
        plausible structure -- correct information, in a place nobody checks.
        """
        frame = self.latest()
        if frame is None:
            raise RuntimeError(f"{self.cam_id}: no preview frame yet")
        return self.writer.save_still(
            replace(frame, validity=DIAGNOSTIC),
            pipeline_settings=self.pipeline.settings_snapshot(),
            camera_info=self.source.describe().as_dict(),
            tag=tag,
            label=self.label,
        )

    def set_controls(self, controls: dict[str, Any]) -> None:
        """Sensor controls, applied by the owner thread.

        Controls reach the SDK, so they go through the queue like everything
        else. They apply to FUTURE requests: a control submitted while a
        request is already in hand cannot have affected it, and the effective
        values for any given frame come from that frame's own metadata.
        """
        self.owner.submit(
            "control", lambda: self.source.set_controls(controls),
            CONTROL_DEADLINE_S,
        )

    def control_spec(self) -> dict[str, dict[str, Any]]:
        """Advertised control ranges, snapshotted at open.

        Served from the snapshot rather than the camera: it is a property of
        the configuration, the UI asks for it on every page load, and a
        round-trip through the command queue for a constant would be latency
        bought for nothing.
        """
        return self._control_spec

    def grab_full(self, timeout: float = 3.0) -> Frame | None:
        """A full-resolution mono frame, served by the capture thread.

        The caller never touches the camera. It raises a flag, the capture loop
        pulls `main` out of the request it is already holding, and the frame
        comes back here -- same exposure as the preview it arrived with. See
        cameras/base.py for why a second consumer is not allowed to exist.

        Returns None if no frame arrives in `timeout`, which is what a stalled
        camera looks like from here and is the caller's cue to say so rather
        than to retry.
        """
        return self.source.wait_full_frame(timeout)

    def presence_stage(self):
        """The checkerboard-presence stage, if this camera has one."""
        for st in self.pipeline.describe():
            if st["type"] == "checkerboard_presence":
                return self.pipeline.stage(st["name"])
        return None

    def bind_pipeline(self) -> None:
        """Wire stages that need to read another stage's parameters.

        Only one such link exists: the presence map counts saddle peaks into
        the MLA's micro-images, so it must read the same geometry the crops use
        rather than keeping a second copy that can drift. Re-run whenever the
        pipeline changes -- a stage added at runtime has no idea what else is
        in the pipeline with it.
        """
        presence, mla = self.presence_stage(), self.mla_stage()
        if presence is not None:
            presence.bind_geometry(mla)

    def mla_stage(self):
        """The MLA overlay stage, if this camera has one. None otherwise.

        The web layer needs it to derive sub-aperture crops from the same
        parameters the overlay draws with -- one source of truth, so the boxes
        you see and the crops you get cannot drift apart.
        """
        for st in self.pipeline.describe():
            if st["type"] == "mla_grid_overlay":
                return self.pipeline.stage(st["name"])
        return None

    def status(self) -> dict[str, Any]:
        version, frame = self.preview.get()
        return {
            "cam_id": self.cam_id,
            "label": self.label,
            "backend": self.cfg.backend,
            "open": self.source.is_open,
            # Four rate numbers, and the distinction between them is the whole
            # point. `fps` and `sensor_fps` are MEASURED and fall to zero when
            # the thing they measure stops; `process_fps` and `configured_fps`
            # are what was asked for and never change on their own. Reporting
            # the configured sensor rate under the name `sensor_fps`, which is
            # what this did before, answered the second question in the shape
            # of an answer to the first.
            "fps": round(self.rate.fps, 2),                 # pipeline, measured
            "sensor_fps": round(self.acquired.fps, 2),      # acquisition, measured
            "process_fps": (round(1.0 / self.process_interval, 2)
                            if self.process_interval > 0 else None),
            "configured_fps": float(self.cfg.fps),
            "skipped": self.skipped,
            "frames": version,
            # Seconds since the last acquisition and the last publish. These
            # are what a viewer needs to decide whether an image on screen is
            # current: a frame count that has stopped rising is only visible to
            # something that remembers the previous count.
            "acquired_age_s": _round_or_none(self.acquired.age),
            "published_age_s": _round_or_none(self.rate.age),
            "errors": self.errors,
            "last_error": self.last_error,
            # Stage failures, which the capture-loop error count never saw:
            # the pipeline catches them and passes the frame through.
            "stage_failures": self.pipeline.failures,
            "lifecycle": self.lifecycle,
            # The command queue. `max_depth` and `max_age_s` are the two a
            # bench run reads: if neither ever approached its bound, the
            # capacity is not what is limiting anything.
            "commands": self.owner.state(),
            "preview_shape": list(frame.shape) if frame is not None else None,
            "live": self.live_controls(),
            "info": self.source.describe().as_dict() if self.source.is_open else None,
        }

    def latest(self) -> Frame | None:
        return self.preview.get()[1]

    # -- live sensor readback --------------------------------------------

    def live_controls(self) -> dict[str, Any]:
        """What the sensor is *actually* doing right now, from frame metadata.

        Distinct from what was requested. Under auto-exposure the two differ by
        definition, and the whole point of showing this is that the AE-chosen
        exposure is a number you want to read off and then pin.
        """
        frame = self.latest()
        if frame is None:
            return {}
        out: dict[str, Any] = {}
        for key in ("ExposureTime", "AnalogueGain", "DigitalGain", "AeLocked"):
            if key in frame.meta:
                v = frame.meta[key]
                out[key] = round(float(v), 4) if isinstance(v, (int, float)) else v
        out["AeEnable"] = bool(self.source.auto_exposure)
        return out

    # -- state -----------------------------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline.settings_snapshot(),
            "controls": self.source.requested_controls(),
            # Orientation belongs here as much as the controls do. It describes
            # how the camera is bolted down, which does not change when the
            # process restarts -- and leaving it out is worse than merely
            # forgetting a setting: the saved pipeline carries the MLA
            # alignment's `reference_rotate_deg`, so a restart would find a
            # portrait-referenced grid on a camera the config says is
            # landscape, rebase it back, and undo the turn silently.
            "orientation": self.source.orientation,
        }

    def apply_state(self, state: dict[str, Any]) -> list[str]:
        """Restore a saved snapshot. Returns human-readable notes about
        anything that could not be applied, rather than raising -- a state file
        written before a config change must not stop the rig from starting."""
        notes: list[str] = []
        # Orientation before the pipeline, for reading rather than for
        # correctness: what actually matters is that both are in place before
        # bind_sensor runs, and Application._restore_state calls that
        # afterwards. What is NOT optional is that orientation is restored at
        # all -- the saved pipeline carries the alignment's
        # `reference_rotate_deg`, so without it a restart finds a
        # portrait-referenced grid on a camera the config calls landscape,
        # rebases it back, and undoes the turn with a warning that reads like
        # the 728->1456 migration it is not.
        orientation = state.get("orientation") or {}
        if orientation:
            try:
                deg = int(orientation.get("rotate_deg", self.cfg.rotate_deg) or 0)
                if deg in (0, 90, 180, 270):
                    self.cfg.rotate_deg = deg
                else:
                    notes.append(f"{self.cam_id}: saved rotate_deg {deg} is not a "
                                 f"quarter turn, ignored")
                for key in ("flip_horizontal", "flip_vertical"):
                    if key in orientation:
                        setattr(self.cfg, key, bool(orientation[key]))
            except (TypeError, ValueError) as exc:
                notes.append(f"{self.cam_id}: orientation not restored ({exc})")

        for stage_name, values in (state.get("pipeline") or {}).items():
            values = {k: v for k, v in values.items() if k != "type"}
            try:
                self.pipeline.update_params(stage_name, values)
            except KeyError:
                notes.append(f"{self.cam_id}: no stage {stage_name!r} any more, skipped")
            except Exception as exc:
                notes.append(f"{self.cam_id}/{stage_name}: {exc}")
        controls = state.get("controls") or {}
        if controls:
            try:
                self.set_controls(controls)
            except Exception as exc:
                notes.append(f"{self.cam_id}: controls not restored ({exc})")
        return notes


class Application:
    def __init__(
        self,
        cfg: AppConfig,
        state_path: Path | str | None = None,
        restore: bool = True,
    ) -> None:
        self.cfg = cfg
        self.writer = SessionWriter(cfg.storage, cfg.storage_root)
        self.cameras: dict[str, CameraRuntime] = {
            c.cam_id: CameraRuntime(c, self.writer, process_fps=cfg.server.preview_fps)
            for c in cfg.cameras
        }
        self.started_at = time.time()
        # Declared before a session, frozen once one starts. Persisted with
        # everything else so the board and acceptance settings survive a
        # restart mid-way through a calibration afternoon.
        self.calibration = CalibrationSettings()
        # The open calibration session, if any. Owned here rather than by the
        # web layer so it survives a browser reload: the operator has both
        # hands on the board and cannot be expected to notice a dropped tab.
        self.session: Any = None
        self.session_settings: CalibrationSettings | None = None
        # What the server was told to listen on. Set by __main__ before uvicorn
        # starts; the default is what an embedded or test Application reports.
        self.bound: tuple[str, int] = (cfg.server.host, cfg.server.port)
        self.restore = restore
        self.state = StateStore(Path(state_path), self._state_snapshot) if state_path else None
        self.restore_notes: list[str] = []
        self._storage_stop = threading.Event()
        self._storage_thread: threading.Thread | None = None

    # -- state -----------------------------------------------------------

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "config": str(getattr(self.cfg, "source_path", "") or ""),
            "cameras": {cid: cam.state_snapshot() for cid, cam in self.cameras.items()},
            "calibration": self.calibration.model_dump(),
        }

    # -- calibration ------------------------------------------------------

    def calibration_readiness(self) -> dict[str, Any]:
        return readiness_report(list(self.cameras.values()), self.calibration)

    def calibration_derived(self) -> dict[str, Any]:
        """Nominal optics -> the numbers that decide whether the board is right.

        Uses the first camera's grid pitch, since the two heads share a design
        and the figure is an aid to choosing a target, not a measurement.
        """
        pitch = 100.0
        for cam in self.cameras.values():
            stage = cam.mla_stage()
            if stage is not None:
                pitch = float(stage.params.pitch_px)
                break
        return DerivedOptics.compute(
            self.calibration.optics, self.calibration.board, pitch
        ).model_dump()

    # -- the calibration session -------------------------------------------
    #
    # One session at a time, owned here so it survives a browser reload and so
    # the console is a view of it rather than the thing that holds it.
    #
    # What runs while a session is open: the presence map, which is a pipeline
    # stage on preview frames the cameras already produce, and a decision loop
    # that reads it. Nothing else. Full-resolution frames are pulled through
    # the capture thread once per pose. See calibration/session.py.

    @property
    def session_running(self) -> bool:
        return bool(self.session and self.session.running)

    def session_start(self) -> dict[str, Any]:
        """Open a capture session. Raises on a blocked precondition."""
        readiness = self.calibration_readiness()
        if not readiness["ready"]:
            raise RuntimeError("; ".join(readiness["blocking_failures"]))
        self.session_stop()
        from .calibration.session import CaptureSession  # noqa: PLC0415 - needs cv2

        # Settings are frozen for the life of the session. Changing the board
        # halfway through would make the poses already recorded describe a
        # different object, and nothing in the files would say so.
        self.session_settings = self.calibration.model_copy(deep=True)
        self.session = CaptureSession(
            self.cameras,
            self.session_settings,
            root=self.writer.session_dir,
            storage_free_bytes=lambda: self.writer.state()["free_bytes"],
        )
        self.session.start()
        return self.session_state()

    def session_stop(self) -> dict[str, Any]:
        if self.session is not None:
            self.session.stop()
        return self.session_state()

    def session_state(self) -> dict[str, Any]:
        if self.session is None:
            return {
                "running": False, "phase": "idle", "title": "READY",
                "hint": "press start", "poses": 0, "recorded": 0,
                "events": [], "cameras": {}, "depth_px": [],
            }
        return self.session.state()

    def session_force(self) -> dict[str, Any]:
        if self.session is not None:
            self.session.force()
        return self.session_state()

    def session_discard(self) -> dict[str, Any]:
        if self.session is not None:
            self.session.discard_last()
        return self.session_state()

    def presence_overlay(self, cam_id: str):
        """The current preview with every saddle peak and tile count drawn.

        The answer to "no board is being noticed". A number cannot distinguish
        a misplaced grid from a board whose squares are too large from a lens
        cap; a picture with the peaks marked and the counts written in each
        micro-image does it at a glance.
        """
        from .calibration.presence import peaks_overlay  # noqa: PLC0415 - needs cv2

        cam = self.camera(cam_id)
        stage, mla = cam.presence_stage(), cam.mla_stage()
        frame = cam.latest()
        if stage is None or mla is None or frame is None or stage._detector is None:
            return None
        h, w = frame.data.shape[:2]
        return peaks_overlay(
            frame.data, stage._detector, mla.geometry_for(w, h),
            float(mla.params.crop_scale), int(stage.params.min_corners),
        )

    def session_shot(self, cam_id: str):
        """The annotated review image for the last pose, or None."""
        if self.session is None:
            return None
        return self.session.last_shot.get(cam_id)

    # -- storage -----------------------------------------------------------

    def storage_state(self) -> dict[str, Any]:
        """Devices on offer, and where output is currently going."""
        from .storage.devices import DATA_SUBDIR, list_devices  # noqa: PLC0415

        state = self.writer.state()
        active_mount = state["mount"]
        return {
            "active": state,
            "subdir": DATA_SUBDIR,
            "devices": [
                {**d.as_dict(), "active": d.mount == active_mount}
                for d in list_devices([self.cfg.storage_root])
            ],
        }

    def _storage_watch(self) -> None:
        """Notice a pulled disk within a couple of seconds, not at the next
        capture. Polling, because there is no portable mount-change signal and
        inotify does not fire on /proc/mounts the way you would hope."""
        while not self._storage_stop.wait(2.0):
            try:
                self.writer.check_and_recover()
            except Exception:
                log.exception("storage watch failed")

    def mark_dirty(self) -> None:
        """Call after any parameter or control change so autosave picks it up."""
        if self.state:
            self.state.mark_dirty()

    def _restore_state(self) -> None:
        if not (self.state and self.restore):
            return
        data = self.state.load()
        if "calibration" in data:
            try:
                self.calibration = CalibrationSettings.model_validate(data["calibration"])
            except Exception as exc:
                self.restore_notes.append(f"calibration settings not restored ({exc})")
        for cam_id, cam_state in (data.get("cameras") or {}).items():
            cam = self.cameras.get(cam_id)
            if cam is None:
                self.restore_notes.append(f"saved state names camera {cam_id!r}, not in config")
                continue
            self.restore_notes.extend(cam.apply_state(cam_state))
        for note in self.restore_notes:
            log.warning("restore: %s", note)

    def start(self) -> None:
        failures: list[str] = []
        for cam in self.cameras.values():
            try:
                cam.start()
            except Exception as exc:
                # One dead camera must not prevent the other from running --
                # half a rig is still useful for alignment work.
                failures.append(f"{cam.cam_id}: {exc}")
                log.exception("%s: failed to start", cam.cam_id)
        if failures and len(failures) == len(self.cameras):
            raise RuntimeError("no cameras started:\n  " + "\n  ".join(failures))
        # Restore only after the cameras are open: sensor controls need a live
        # device, and a pipeline parameter is meaningless before its stage
        # exists.
        self._restore_state()
        # ...and re-bind the MLA units afterwards, because a state file written
        # before the parameters were sensor-native carries its own
        # reference_width and has just overwritten what cam.start() bound. The
        # rebase is idempotent, so doing it twice costs nothing and skipping it
        # here would silently restore a preview-referenced grid over a
        # sensor-referenced one -- a factor of two, from a file, after
        # everything upstream was made correct.
        for cam in self.cameras.values():
            mla = cam.mla_stage()
            if mla is not None and cam.source.is_open:
                w, h = cam.source.describe().full_resolution
                mla.bind_sensor(int(w), int(h), Orientation.of(cam.cfg))
        if self.state:
            self.state.start_autosave()

        self._storage_stop.clear()
        self._storage_thread = threading.Thread(
            target=self._storage_watch, name="storage-watch", daemon=True
        )
        self._storage_thread.start()

        self.writer.write_session_manifest(
            {
                "started": self.started_at,
                "restore_notes": self.restore_notes,
                "config": self.cfg.model_dump(),
                "cameras": {cid: c.status() for cid, c in self.cameras.items()},
                "start_failures": failures,
            }
        )

    def stop(self) -> None:
        # The session first: its loop pulls frames from the cameras, so
        # stopping it after the sources close would raise on the way out.
        self.session_stop()
        self._storage_stop.set()
        if self._storage_thread:
            self._storage_thread.join(timeout=3.0)
            self._storage_thread = None
        # Save before closing the cameras: a snapshot taken after teardown
        # would read controls off a closed device.
        if self.state:
            self.state.stop(final_save=True)
        for cam in self.cameras.values():
            try:
                cam.stop()
            except Exception:
                log.exception("%s: error stopping", cam.cam_id)

    def camera(self, cam_id: str) -> CameraRuntime:
        try:
            return self.cameras[cam_id]
        except KeyError:
            raise KeyError(f"no camera {cam_id!r}; have {sorted(self.cameras)}") from None

    def status(self) -> dict[str, Any]:
        # `network` is here so a rig whose DHCP lease moved can be asked where
        # it went -- curl http://<whatever-still-works>/api/status | jq .network
        # -- rather than scanned for. Recomputed per call, cheaply, because an
        # address obtained at start-up is exactly the one that goes stale.
        host, port = self.bound
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "session_dir": str(self.writer.session_dir),
            "storage": self.writer.state(),
            "health": host_health(),
            "network": net.describe(port, host),
            "session_running": self.session_running,
            "state_file": str(self.state.path) if self.state else None,
            "cameras": [c.status() for c in self.cameras.values()],
        }
