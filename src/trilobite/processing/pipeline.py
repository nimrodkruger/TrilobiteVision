"""Ordered stage runner.

Thread-safety note: parameters are mutated from the web thread while the
capture thread is reading them. The lock is held only for parameter reads and
writes, never across the numpy work, so a slow stage cannot block the UI.
Individual stages therefore see a consistent parameter set for the duration of
one frame, which is the guarantee that matters.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..config import StageConfig
from ..types import Frame
from .base import Stage
from .registry import build_stage

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, stages: list[Stage] | None = None) -> None:
        self._stages: list[Stage] = list(stages or [])
        self._lock = threading.RLock()
        self._timings: dict[str, float] = {}
        # Per-stage failure counters and the most recent message. A stage that
        # throws on every frame used to be invisible to everything but the log:
        # the frame passed through unchanged, `CameraRuntime.errors` never saw
        # it, and the preview kept flowing at full rate. Degradation that looks
        # like success is the failure mode this subsystem exists to prevent.
        self._failures: dict[str, int] = {}
        self._last_error: dict[str, str] = {}

    @classmethod
    def from_config(cls, configs: list[StageConfig]) -> Pipeline:
        stages = [build_stage(c.type, name=c.name, **c.params) for c in configs]
        return cls(stages)

    # -- execution ------------------------------------------------------

    def __call__(self, frame: Frame) -> Frame:
        with self._lock:
            stages = list(self._stages)
        failed: list[str] = []
        for stage in stages:
            t0 = time.perf_counter()
            try:
                frame = stage(frame)
            except Exception as exc:
                # A broken stage must not kill the capture thread: pass the
                # frame through, because a degraded preview beats a dead rig
                # mid-experiment. But say so. Passing it through *silently* is
                # what made a permanently throwing stage indistinguishable from
                # a working one at every level above this line.
                self._failures[stage.name] = self._failures.get(stage.name, 0) + 1
                self._last_error[stage.name] = f"{type(exc).__name__}: {exc}"
                failed.append(stage.name)
                # Logged at exception level on the first failure and every
                # hundredth after, so a stage failing at 12 Hz does not bury
                # the journal while the counter still records every one.
                if self._failures[stage.name] == 1 or self._failures[stage.name] % 100 == 0:
                    log.exception(
                        "stage %s failed (%d times); frame passed through",
                        stage.name, self._failures[stage.name],
                    )
            self._timings[stage.name] = (time.perf_counter() - t0) * 1000.0
        if failed:
            # On the frame, so a consumer holding only the frame -- a sink, a
            # sidecar writer -- can tell it is not the frame that was asked for.
            frame = frame.derive(frame.data, pipeline_failed_stages=failed)
        return frame

    @property
    def failures(self) -> dict[str, dict[str, Any]]:
        """Per-stage failure count and last message, for `/api/status`."""
        with self._lock:
            return {
                name: {"count": n, "last_error": self._last_error.get(name, "")}
                for name, n in self._failures.items()
                if n
            }

    @property
    def failure_count(self) -> int:
        return sum(self._failures.values())

    # -- introspection --------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**s.describe(), "last_ms": round(self._timings.get(s.name, 0.0), 3)}
                for s in self._stages
            ]

    def settings_snapshot(self) -> dict[str, Any]:
        """Flat record of every parameter, for saving alongside captured data.

        This is what makes a captured image reproducible six months later.
        """
        with self._lock:
            return {s.name: {"type": s.type_name, **s.params.model_dump()} for s in self._stages}

    def stage(self, name: str) -> Stage:
        with self._lock:
            for s in self._stages:
                if s.name == name:
                    return s
        raise KeyError(f"no stage named {name!r}")

    # -- live modification ----------------------------------------------

    def update_params(self, stage_name: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self.stage(stage_name).update(values)

    def add(self, cfg: StageConfig, index: int | None = None) -> Stage:
        stage = build_stage(cfg.type, name=cfg.name, **cfg.params)
        with self._lock:
            names = {s.name for s in self._stages}
            if stage.name in names:
                raise ValueError(f"stage name {stage.name!r} already in pipeline")
            self._stages.insert(len(self._stages) if index is None else index, stage)
        return stage

    def remove(self, stage_name: str) -> None:
        with self._lock:
            self._stages = [s for s in self._stages if s.name != stage_name]

    def reorder(self, names: list[str]) -> None:
        with self._lock:
            by_name = {s.name: s for s in self._stages}
            if set(names) != set(by_name):
                raise ValueError("reorder must list every existing stage exactly once")
            self._stages = [by_name[n] for n in names]

    def reset(self) -> None:
        with self._lock:
            for s in self._stages:
                s.reset()
