"""Ordered stage runner, and the record of what it actually did.

**Thread-safety, corrected in Stage 4.** The lock used to be held only while
copying the stage list, not across the stage work -- on the reasoning that a
slow stage should not block the UI. The docstring then claimed that "individual
stages see a consistent parameter set for the duration of one frame", which did
not follow: a parameter update landing between stage two and stage three gave
that frame a mixture of two revisions, and nothing recorded which.

The lock is now held for the whole invocation. A pipeline pass is a few
milliseconds; a slider waiting that long is imperceptible, and in exchange one
execution means one revision. That is a property worth more than the
microseconds, because a frame processed under a mixture of settings cannot be
reproduced from the record afterwards.

**The record.** `__call__` attaches a frozen account of its own execution to
the frame it returns -- the revision, the ordered stages, each one's parameters
AS USED and its outcome. The writer serialises that and reads nothing live.
Before this, the sidecar was built from `settings_snapshot()` at SAVE time, so
editing a gain between capture and save wrote a value that never touched the
pixels; and a raw capture, which never enters the pipeline at all, still got a
full parameter block describing processing that did not happen.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
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
        # Bumped by every change to parameters or topology. Stamped into each
        # execution record so two frames can be compared, and so "this frame
        # was processed under revision 7" is a statement with a referent.
        self._revision = 0

    @classmethod
    def from_config(cls, configs: list[StageConfig]) -> Pipeline:
        stages = [build_stage(c.type, name=c.name, **c.params) for c in configs]
        return cls(stages)

    # -- execution ------------------------------------------------------

    def __call__(self, frame: Frame) -> Frame:
        """Run every stage, and attach what happened to the frame.

        The whole pass is under the lock. See the module docstring: one
        execution, one revision, or the record is a fiction.
        """
        with self._lock:
            revision = self._revision
            stages = list(self._stages)
            record: list[dict[str, Any]] = []
            failed: list[str] = []

            for stage in stages:
                # Captured BEFORE the stage runs, so the record holds the values
                # the pixels were actually processed under -- not whatever the
                # parameters are by the time anyone asks.
                entry: dict[str, Any] = {
                    "name": stage.name,
                    "type": stage.type_name,
                    "params": stage.params.model_dump(),
                }
                t0 = time.perf_counter()
                try:
                    frame = stage(frame)
                except Exception as exc:
                    # A broken stage must not kill the capture thread: pass the
                    # frame through, because a degraded preview beats a dead rig
                    # mid-experiment. But say so. Passing it through *silently*
                    # is what made a permanently throwing stage
                    # indistinguishable from a working one above this line.
                    self._failures[stage.name] = self._failures.get(stage.name, 0) + 1
                    self._last_error[stage.name] = f"{type(exc).__name__}: {exc}"
                    failed.append(stage.name)
                    entry["outcome"] = "failed"
                    entry["reason"] = f"{type(exc).__name__}: {exc}"
                    # Logged on the first failure and every hundredth after, so
                    # a stage failing at 12 Hz does not bury the journal while
                    # the counter still records every one.
                    if (self._failures[stage.name] == 1
                            or self._failures[stage.name] % 100 == 0):
                        log.exception(
                            "stage %s failed (%d times); frame passed through",
                            stage.name, self._failures[stage.name],
                        )
                else:
                    # Three outcomes, not two, and "skipped" carries its reason.
                    # A disabled stage is a legal no-op and must not read as
                    # having processed anything -- that distinction is how a
                    # presence stage that shipped switched off looked identical
                    # to one that was working. A stage declining the frame's
                    # SPACE is the same kind of silence: the lenslet extractor
                    # accepts only `raw` and quietly passes a mono8 preview
                    # straight through.
                    if not getattr(stage.params, "enabled", True):
                        entry["outcome"] = "skipped"
                        entry["reason"] = "disabled"
                    elif stage.accepts and frame.space not in stage.accepts:
                        entry["outcome"] = "skipped"
                        entry["reason"] = (
                            f"accepts {list(stage.accepts)}, frame is "
                            f"{frame.space!r}")
                    else:
                        entry["outcome"] = "ok"
                ms = (time.perf_counter() - t0) * 1000.0
                self._timings[stage.name] = ms
                entry["ms"] = round(ms, 3)
                record.append(entry)

        processed = {"ran": True, "revision": revision, "stages": record}
        if failed:
            # Kept as its own metadata key as well. A consumer holding only the
            # frame used it before this record existed, and removing it would
            # break them for no gain.
            frame = frame.derive(frame.data, pipeline_failed_stages=failed)
        return replace(frame, processing=processed)

    def bypass_record(self) -> dict[str, Any]:
        """The processing record for a frame that never entered the pipeline.

        A raw capture is taken straight off the sensor; the preview pipeline
        does not touch it. It used to be saved with a full copy of that
        pipeline's parameters anyway, which is a description of processing that
        did not happen -- and indistinguishable, in the file, from one that did.
        """
        with self._lock:
            return {
                "ran": False,
                "revision": self._revision,
                "bypassed": [s.name for s in self._stages],
                # The parameters as they stood AT CAPTURE, recorded as context
                # rather than as a description of processing. This matters for
                # one concrete reason: the MLA alignment is a measurement of
                # the OPTICS, not a processing step, and a raw capture needs it
                # in the sidecar so `read_capture.py --grid` can draw the grid
                # that was aligned at the moment of exposure.
                #
                # Snapshotted here, on the owner thread at capture time -- not
                # at save time, which is the bug this stage closes. What it is
                # NOT is a claim that any of it touched the pixels; `ran: false`
                # and `bypassed` say that plainly.
                "params_at_capture": {
                    s.name: {"type": s.type_name, **s.params.model_dump()}
                    for s in self._stages
                },
            }

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
        """Every parameter as it stands RIGHT NOW. For the UI, not for sidecars.

        It used to be what the writer saved beside a capture, which is the
        Stage 4 bug: a value read at save time is not the value the pixels were
        processed under. What a frame was processed under lives on the frame,
        in `Frame.processing`, put there by `__call__`.
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
            out = self.stage(stage_name).update(values)
            self._revision += 1
            return out

    def add(self, cfg: StageConfig, index: int | None = None) -> Stage:
        stage = build_stage(cfg.type, name=cfg.name, **cfg.params)
        with self._lock:
            names = {s.name for s in self._stages}
            if stage.name in names:
                raise ValueError(f"stage name {stage.name!r} already in pipeline")
            self._stages.insert(len(self._stages) if index is None else index, stage)
            self._revision += 1
        return stage

    def remove(self, stage_name: str) -> None:
        with self._lock:
            self._stages = [s for s in self._stages if s.name != stage_name]
            self._revision += 1

    def reorder(self, names: list[str]) -> None:
        with self._lock:
            by_name = {s.name: s for s in self._stages}
            if set(names) != set(by_name):
                raise ValueError("reorder must list every existing stage exactly once")
            self._stages = [by_name[n] for n in names]
            self._revision += 1

    def reset(self) -> None:
        with self._lock:
            for s in self._stages:
                s.reset()
            self._revision += 1

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision
