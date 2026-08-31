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
from typing import Any

from .bus import LatestFrame
from .cameras.base import CameraSource
from .cameras.registry import build_camera
from .config import AppConfig, CameraConfig
from .processing.pipeline import Pipeline
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
        )

    def status(self) -> dict[str, Any]:
        version, frame = self.preview.get()
        return {
            "cam_id": self.cam_id,
            "backend": self.cfg.backend,
            "open": self.source.is_open,
            "fps": round(self.rate.fps, 2),
            "frames": version,
            "errors": self.errors,
            "last_error": self.last_error,
            "preview_shape": list(frame.shape) if frame is not None else None,
            "info": self.source.describe().as_dict() if self.source.is_open else None,
        }

    def latest(self) -> Frame | None:
        return self.preview.get()[1]


class Application:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.writer = SessionWriter(cfg.storage, cfg.storage_root)
        self.cameras: dict[str, CameraRuntime] = {
            c.cam_id: CameraRuntime(c, self.writer) for c in cfg.cameras
        }
        self.started_at = time.time()

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
        self.writer.write_session_manifest(
            {
                "started": self.started_at,
                "config": self.cfg.model_dump(),
                "cameras": {cid: c.status() for cid, c in self.cameras.items()},
                "start_failures": failures,
            }
        )

    def stop(self) -> None:
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
            "cameras": [c.status() for c in self.cameras.values()],
        }
