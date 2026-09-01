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
from pathlib import Path
from typing import Any

from .bus import LatestFrame
from .calibration import CalibrationSettings, DerivedOptics, readiness_report
from .cameras.base import CameraSource
from .cameras.registry import build_camera
from .config import AppConfig, CameraConfig
from .health import host_health
from .processing.pipeline import Pipeline
from .state import StateStore
from .storage.writer import SessionWriter
from .types import Frame

log = logging.getLogger(__name__)


class RateMeter:
    """Rolling frame-rate estimate over a short window."""

    def __init__(self, window: int = 60) -> None:
        self._t = deque(maxlen=window)

    def tick(self) -> None:
        self._t.append(time.monotonic())

    @property
    def fps(self) -> float:
        if len(self._t) < 2:
            return 0.0
        span = self._t[-1] - self._t[0]
        return (len(self._t) - 1) / span if span > 0 else 0.0


class CameraRuntime:
    """A camera, its preview pipeline, its capture thread and its output slot."""

    def __init__(self, cfg: CameraConfig, writer: SessionWriter) -> None:
        self.cfg = cfg
        self.cam_id = cfg.cam_id
        self.label = cfg.label or cfg.cam_id.replace("_", " ").title()
        self.source: CameraSource = build_camera(cfg)
        self.pipeline = Pipeline.from_config(cfg.pipeline)
        self.preview = LatestFrame()
        self.writer = writer
        self.rate = RateMeter()
        self.errors = 0
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._capture_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self.source.open()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"capture-{self.cam_id}", daemon=True
        )
        self._thread.start()
        log.info("%s: capture thread started", self.cam_id)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self.source.close()
        log.info("%s: stopped", self.cam_id)

    def _run(self) -> None:
        backoff = 0.1
        while not self._stop.is_set():
            try:
                frame = self.source.read_preview()
                if frame is None:
                    time.sleep(0.05)
                    continue
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

    def capture_still(self, raw: bool = True, tag: str = "still") -> dict[str, Any]:
        """Full-resolution capture, saved with full provenance.

        Serialised per camera: two simultaneous still requests on one sensor
        will fight over the request pool.
        """
        with self._capture_lock:
            frame = self.source.capture_full(raw=raw)
        return self.writer.save_still(
            frame,
            pipeline_settings=self.pipeline.settings_snapshot(),
            camera_info=self.source.describe().as_dict(),
            tag=tag,
            label=self.label,
        )

    def capture_preview(self, tag: str = "view") -> dict[str, Any]:
        """Save the preview frame exactly as displayed -- post-pipeline.

        Distinct from capture_still on purpose. This is low-resolution, gamma
        shaped, possibly with a grid drawn on it: a record of *what you were
        looking at*, useful for lab notes and for documenting an alignment
        state. It is not measurement data, and the sidecar says so via
        `space` and the pipeline block. Never fit anything to these.
        """
        frame = self.latest()
        if frame is None:
            raise RuntimeError(f"{self.cam_id}: no preview frame yet")
        return self.writer.save_still(
            frame,
            pipeline_settings=self.pipeline.settings_snapshot(),
            camera_info=self.source.describe().as_dict(),
            tag=tag,
            label=self.label,
        )

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
            "fps": round(self.rate.fps, 2),
            "frames": version,
            "errors": self.errors,
            "last_error": self.last_error,
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
        }

    def apply_state(self, state: dict[str, Any]) -> list[str]:
        """Restore a saved snapshot. Returns human-readable notes about
        anything that could not be applied, rather than raising -- a state file
        written before a config change must not stop the rig from starting."""
        notes: list[str] = []
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
                self.source.set_controls(controls)
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
            c.cam_id: CameraRuntime(c, self.writer) for c in cfg.cameras
        }
        self.started_at = time.time()
        # Declared before a session, frozen once one starts. Persisted with
        # everything else so the board and acceptance settings survive a
        # restart mid-way through a calibration afternoon.
        self.calibration = CalibrationSettings()
        # Live detection: one worker per camera, started on demand, holding
        # only the latest pass. Never writes anything.
        self.detection: dict[str, Any] = {}
        self.detection_settings: CalibrationSettings | None = None
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

    # -- live detection ----------------------------------------------------
    #
    # No recording. The workers hold the latest pass per camera and nothing
    # else: this stage answers "does the detector see the board in the
    # micro-images", which has to be true before recording poses is worth
    # building. See calibration/detect.py.

    @property
    def detection_running(self) -> bool:
        return any(w.running for w in self.detection.values())

    def detection_start(self) -> dict[str, Any]:
        """Start a detection worker per camera. Raises on a blocked precondition."""
        readiness = self.calibration_readiness()
        if not readiness["ready"]:
            raise RuntimeError("; ".join(readiness["blocking_failures"]))
        self.detection_stop()
        # The settings are frozen at start: the board a pass was measured
        # against must not change under it mid-pass, and a detector rebuilt on
        # every settings edit would produce results that cannot be compared.
        self.detection_settings = self.calibration.model_copy(deep=True)
        spec = self.detection_settings.detection
        from .calibration.detect import DetectionWorker  # noqa: PLC0415 - needs cv2

        # Shared across the cameras, so `concurrent_cameras` limits how many
        # full-frame passes run at once rather than each camera limiting only
        # itself. Two at a time doubles the peak current draw, and that is the
        # load a marginal Pi 5 supply fails on first.
        gate = threading.Semaphore(max(1, int(spec.concurrent_cameras)))
        for cam_id, cam in self.cameras.items():
            worker = DetectionWorker(
                cam,
                self.detection_settings.board,
                self.detection_settings.acceptance,
                min_interval=float(spec.interval_s),
                annotate_overlay=bool(spec.overlay),
                normalize=bool(spec.normalize_illumination),
                accuracy=bool(spec.high_accuracy),
                max_tiles=int(spec.max_tiles),
                max_duty=float(spec.max_duty),
                gate=gate,
            )
            worker.start()
            self.detection[cam_id] = worker
        log.info("detection started on %s", ", ".join(self.detection))
        return self.detection_status()

    def detection_stop(self) -> dict[str, Any]:
        for worker in self.detection.values():
            worker.stop()
        self.detection.clear()
        return self.detection_status()

    def detection_status(self) -> dict[str, Any]:
        cams: dict[str, Any] = {}
        for cam_id, worker in self.detection.items():
            latest = worker.latest()
            cams[cam_id] = {
                "running": worker.running,
                "passes": worker.passes,
                "result": latest.as_dict() if latest else None,
            }
        return {
            "running": self.detection_running,
            "recording": False,   # stated explicitly: this stage never writes
            "settings": (
                self.detection_settings.model_dump() if self.detection_settings else None
            ),
            "cameras": cams,
        }

    def detection_overlay(self, cam_id: str):
        """The annotated frame from the latest pass, or None."""
        worker = self.detection.get(cam_id)
        if worker is None:
            return None
        latest = worker.latest()
        return None if latest is None else latest.overlay

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
        # Detection first: its workers pull frames from the cameras, so
        # stopping them after the sources close would raise on the way out.
        self.detection_stop()
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
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "session_dir": str(self.writer.session_dir),
            "storage": self.writer.state(),
            "health": host_health(),
            "detection_running": self.detection_running,
            "state_file": str(self.state.path) if self.state else None,
            "cameras": [c.status() for c in self.cameras.values()],
        }
