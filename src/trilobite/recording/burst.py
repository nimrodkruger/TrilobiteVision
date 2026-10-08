"""Stage 5b: the bounded burst recorder.

Frames go into prefaulted RAM at full sensor rate and are written to disk
afterwards, in one pass, when encoding or write speed no longer determines the
capture rate. That decoupling is the whole value of the design, and it is also
why burst came before continuous: **a burst's size is arithmetic before a
single byte is written.** `frames x bytes_per_frame x heads` is not a forecast,
so "will this fit on this target with the reserve intact" can be answered
before the flush starts, where a continuous recording can only be monitored
while it runs.

## The state machine, and why "captured" is an exception state

    idle -> armed -> recording -> captured (NOT SAVED) -> saved
                                      |
                                      +-> discarded (explicit act only)

`captured` means the frames exist **only in RAM**. They do not survive a
process restart, a crash, or a power cut, and the UI says so in those words.
It is a state to be cleared, not a resting place: the normal path flushes
promptly and automatically to the verified external target, and `captured`
exists for when that fails.

Two rules follow from RAM being temporary, and both are about not destroying
something that cannot be recovered:

  * **arming is refused while an unsaved burst exists.** Nothing may overwrite
    a recording that is not on a disk yet.
  * **discarding is an explicit act.** There is no implicit discard anywhere --
    not on re-arm, not on stop, not on a failed flush.

## Storage protection applies in full

An earlier draft of this stage claimed a burst needed neither storage identity
nor a free-space reserve, and that a failed flush could "retry against the
internal disk". That was wrong, and the second part was precisely the failure
Stage 5a exists to prevent: a 2-4 GB write diverted onto the SD card. So:

  * external target only, by default;
  * the internal-storage override is **per save and starts unticked** every
    time, because the point of it is that it is a deliberate act and a
    remembered preference is the opposite of one;
  * the reserve is enforced against the known exact size **before the flush
    begins**, while the data is still in RAM and can go somewhere else;
  * the target's identity is verified, so the volume being written to is the
    one that was selected.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .. import health
from ..config import StorageConfig
from ..storage.writer import SessionWriter, StorageRefused
from .buffer import FrameBuffer, FrameMeta, PrefaultFailed
from .chunks import RECORDING_SCHEMA, ChunkWriter, Journal
from .ladder import Plan

log = logging.getLogger(__name__)

IDLE = "idle"
ARMING = "arming"
ARMED = "armed"
RECORDING = "recording"
CAPTURED = "captured"          # in RAM, NOT saved
SAVING = "saving"
SAVED = "saved"
FAILED = "failed"

# Why a recording stopped. Reported, not inferred from the frame count.
STOP_REQUESTED = "stopped by operator"
STOP_FULL = "buffer full"
STOP_DURATION = "requested duration reached"


class BurstRecorder:
    """One burst across all heads. Driven from the capture loops.

    Thread model: `offer` is called from each head's own capture thread and
    touches only that head's buffer, which is the one place per-head
    independence buys something. Every state transition takes `_lock`.
    """

    def __init__(self, cfg: StorageConfig, writer: SessionWriter) -> None:
        self.cfg = cfg
        self.writer = writer
        self._lock = threading.RLock()
        self.state = IDLE
        self.plan: Plan | None = None
        self.buffers: dict[str, FrameBuffer] = {}
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.stop_reason: str | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.arm_report: dict[str, Any] = {}

    # -- sizing ----------------------------------------------------------

    def capacity_for(self, plan: Plan) -> dict[str, Any]:
        """How many frames per head will fit, and for how long.

        From measured `MemAvailable`, never from a config constant: the right
        number on an 8 GB board is the wrong one on a 4 GB board, and the
        failure from guessing high is an OOM kill mid-recording.

        The fraction is conservative on purpose. What has to fit alongside the
        buffer: the application itself, libcamera's own buffer pools, and the
        flush working set -- and dirty page cache counts against available
        memory until writeback completes, so a multi-gigabyte write shrinks
        free RAM while the buffer is still being held.
        """
        available_mb = health.memory_available_mb()
        cap_mb = min(
            (available_mb or 1024.0) * float(self.cfg.burst_memory_fraction),
            float(self.cfg.burst_max_mb),
        )
        per_frame_all_heads = plan.bytes_per_frame * plan.heads_count
        frames = int((cap_mb * (1 << 20)) // max(per_frame_all_heads, 1))
        frames = max(0, frames)
        seconds = frames / plan.fps if plan.fps > 0 else 0.0
        return {
            "mem_available_mb": available_mb,
            "cap_mb": round(cap_mb, 1),
            "fraction": self.cfg.burst_memory_fraction,
            "max_mb": self.cfg.burst_max_mb,
            "frames_per_head": frames,
            "bytes_total": frames * per_frame_all_heads,
            "gb_total": round(frames * per_frame_all_heads / 1e9, 2),
            # What the operator sees before pressing Start. Computed from the
            # buffer that will actually be prefaulted, not from the size that
            # was requested.
            "duration_s": round(seconds, 2),
        }

    # -- arming ----------------------------------------------------------

    def arm(self, plan: Plan, frames: int | None = None) -> dict[str, Any]:
        """Allocate and prefault the buffers. Refuses over an unsaved burst."""
        with self._lock:
            if self.state in (RECORDING, ARMING, SAVING):
                raise RuntimeError(
                    f"cannot arm while {self.state}.")
            if self.state == CAPTURED:
                raise RuntimeError(
                    "there is a burst in RAM that has not been saved. Arming "
                    "now would overwrite it and it exists nowhere else. Save "
                    "it, or discard it explicitly.")
            self.state = ARMING

        try:
            capacity = self.capacity_for(plan)
            want = capacity["frames_per_head"] if frames is None else int(frames)
            if want < 1:
                raise RuntimeError(
                    f"only {capacity['mem_available_mb']} MB of memory is "
                    f"available, which is not enough for one "
                    f"{plan.bytes_per_frame / 1e6:.1f} MB frame per head.")
            want = min(want, capacity["frames_per_head"])

            buffers: dict[str, FrameBuffer] = {}
            reports = {}
            for cam_id in plan.heads:
                buf = FrameBuffer(want, (plan.height, plan.width),
                                  _dtype_for(plan))
                reports[cam_id] = buf.prefault()
                buffers[cam_id] = buf
        except (MemoryError, PrefaultFailed, RuntimeError) as exc:
            with self._lock:
                self.state = IDLE
                self.last_error = f"{type(exc).__name__}: {exc}"
            raise

        with self._lock:
            self.plan = plan
            self.buffers = buffers
            self.state = ARMED
            self.started_at = None
            self.stopped_at = None
            self.stop_reason = None
            self.last_error = None
            self.arm_report = {
                "capacity": capacity,
                "frames_per_head": want,
                "duration_s": round(want / plan.fps, 2) if plan.fps else 0.0,
                "buffers": reports,
                "plan": plan.as_dict(),
            }
            return dict(self.arm_report)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.state != ARMED:
                raise RuntimeError(
                    f"cannot start a burst from {self.state!r}; arm it first.")
            self.state = RECORDING
            self.started_at = time.monotonic()
            return self.status()

    # -- the capture path ------------------------------------------------

    @property
    def recording(self) -> bool:
        return self.state == RECORDING

    def wants(self, cam_id: str) -> bool:
        return self.state == RECORDING and cam_id in self.buffers

    def offer(self, cam_id: str, pixels: np.ndarray,
              meta: FrameMeta) -> bool | None:
        """Store one frame. `False` dropped, `None` never part of the burst.

        Called from the head's capture thread, with a view into a libcamera
        request that is about to be released -- so the copy inside `store`
        is not an optimisation choice, it is the reason this is called at all.

        A full buffer **stops the recording** rather than wrapping. A ring
        would make the contents depend on when it was stopped, and a silent
        drop would make a burst that lost its tail indistinguishable from one
        that did not. That case returns `False`: the frame arrived and was
        genuinely lost.

        `None` is the different case -- the burst was already over when the
        frame arrived, so it was never part of it. Returning `False` here, as
        this did until 8 October, charged every burst one phantom drop for the
        frame in flight when it stopped. See ContinuousRecorder.offer.
        """
        buf = self.buffers.get(cam_id)
        if buf is None or self.state != RECORDING:
            return None
        if buf.store(pixels, meta):
            # The duration limit is checked here rather than on a timer: the
            # frame count is what the limit is about, and a timer would stop a
            # recording that had not yet taken the frames it promised.
            plan = self.plan
            if (plan is not None and plan.duration_s
                    and buf.count >= plan.frames_per_head()):
                self._stop(STOP_DURATION)
            return True
        self._stop(STOP_FULL)
        return False

    def _stop(self, reason: str) -> None:
        with self._lock:
            if self.state != RECORDING:
                return
            self.state = CAPTURED
            self.stopped_at = time.monotonic()
            self.stop_reason = reason
            log.info("burst stopped (%s): %s", reason,
                     {c: b.count for c, b in self.buffers.items()})

    def stop(self, reason: str = STOP_REQUESTED) -> dict[str, Any]:
        self._stop(reason)
        return self.status()

    # -- the flush -------------------------------------------------------

    def planned_bytes(self) -> int:
        """Exactly how much the flush will write. Known before it starts."""
        total = 0
        for buf in self.buffers.values():
            total += buf.count * buf.frame_bytes
        # The indexes and the journal. Small, and still counted: a reserve
        # check that ignores them can pass and then fail on the last file.
        return total + (1 << 20) * max(1, len(self.buffers))

    def flush(self, internal_ok: bool = False,
              label: str | None = None) -> dict[str, Any]:
        """Write the buffered frames to the verified target.

        `internal_ok` is per call and never remembered. See the module
        docstring: the whole value of the override is that it is a deliberate
        act, and a remembered preference is not one.
        """
        with self._lock:
            if self.state not in (CAPTURED, FAILED):
                raise RuntimeError(
                    f"nothing to flush: the recorder is {self.state!r}.")
            if not any(b.count for b in self.buffers.values()):
                raise RuntimeError("the burst is empty; nothing to write.")
            self.state = SAVING
            plan = self.plan
            assert plan is not None

        need = self.planned_bytes()
        try:
            # Admitted BEFORE the first byte, against the exact size. A flush
            # that would breach the reserve is refused while the data is still
            # in RAM and can go somewhere else -- which is the one thing a
            # burst can do that a continuous recording cannot.
            with self.writer.writing(need, internal_ok=internal_ok) as generation:
                result = self._write_all(plan, generation, label)
        except StorageRefused as exc:
            with self._lock:
                self.state = CAPTURED          # still in RAM, still unsaved
                self.last_error = str(exc)
            log.warning("burst flush refused (%s): %s", exc.state, exc)
            raise
        except OSError as exc:
            with self._lock:
                self.state = CAPTURED
                self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("burst flush failed")
            raise

        with self._lock:
            self.state = SAVED
            self.last_result = result
            self.last_error = None
        return result

    def _write_all(self, plan: Plan, generation: int,
                   label: str | None) -> dict[str, Any]:
        root = Path(self.writer.session_dir) / _burst_dirname(label)
        root.mkdir(parents=True, exist_ok=True)

        heads: dict[str, Any] = {}
        total_bytes = 0
        t0 = time.perf_counter()
        for cam_id, buf in self.buffers.items():
            if not buf.count:
                continue
            cw = ChunkWriter(root / cam_id, cam_id, buf.shape, buf.dtype,
                             chunk_frames=int(self.cfg.chunk_frames),
                             prefix="burst", generation=generation)
            cw.write_block(buf.view(), buf.meta)
            cw.close()
            heads[cam_id] = cw.summary()
            total_bytes += cw.bytes_written
        elapsed = time.perf_counter() - t0

        payload = {
            "schema": RECORDING_SCHEMA,
            "kind": "burst",
            "plan": plan.as_dict(),
            "storage_generation": generation,
            "storage_identity": self.writer.identity_dict(),
            "session_dir": str(self.writer.session_dir),
            "directory": str(root),
            "stop_reason": self.stop_reason,
            "duration_s": (
                round(self.stopped_at - self.started_at, 3)
                if self.started_at and self.stopped_at else None),
            "arm": self.arm_report,
            "heads": heads,
            "bytes": total_bytes,
            "gb": round(total_bytes / 1e9, 3),
            "flush_seconds": round(elapsed, 2),
            "flush_mb_s": round(total_bytes / 1e6 / max(elapsed, 1e-6), 1),
            # No synchronisation is claimed. The heads are free-running and the
            # manifest says so rather than leaving a reader to assume the pair
            # was triggered together. The measured offset distribution is the
            # honest statement, and it is computed from the per-frame
            # SensorTimestamps in the indexes rather than asserted here.
            "synchronised": False,
            "synchronisation_note": (
                "The heads are free-running. No trigger couples them and none "
                "is claimed. Per-frame SensorTimestamp is recorded per head in "
                "its own clock domain; reconcile the pair offline from those."),
        }
        payload["inter_head_offset"] = _offsets(self.buffers)
        Journal(root, payload).write()
        return payload

    def discard(self) -> dict[str, Any]:
        """Throw the RAM contents away. Only ever by explicit request."""
        with self._lock:
            if self.state in (RECORDING, SAVING, ARMING):
                raise RuntimeError(f"cannot discard while {self.state!r}.")
            for buf in self.buffers.values():
                buf.reset()
            self.state = ARMED if self.buffers else IDLE
            self.stop_reason = None
            self.last_error = None
            return self.status()

    def disarm(self) -> dict[str, Any]:
        """Release the buffers. Refuses over an unsaved burst."""
        with self._lock:
            if self.state == CAPTURED and any(
                    b.count for b in self.buffers.values()):
                raise RuntimeError(
                    "there is an unsaved burst in RAM. Save or discard it "
                    "before releasing the buffers.")
            if self.state in (RECORDING, SAVING, ARMING):
                raise RuntimeError(f"cannot disarm while {self.state!r}.")
            self.buffers = {}
            self.plan = None
            self.arm_report = {}
            self.state = IDLE
            return self.status()

    # -- reporting -------------------------------------------------------

    @property
    def unsaved(self) -> bool:
        return (self.state in (CAPTURED, FAILED)
                and any(b.count for b in self.buffers.values()))

    def status(self) -> dict[str, Any]:
        stored = {c: b.count for c, b in self.buffers.items()}
        capacity = min((b.frames for b in self.buffers.values()), default=0)
        plan = self.plan
        elapsed = None
        if self.started_at is not None:
            end = self.stopped_at or time.monotonic()
            elapsed = round(end - self.started_at, 2)
        return {
            "state": self.state,
            "unsaved": self.unsaved,
            # The words the plan asks for, and they say what they mean.
            "headline": _headline(self.state, self.unsaved),
            "frames": stored,
            "capacity_frames": capacity,
            "fill_fraction": (
                round(max(stored.values(), default=0) / capacity, 3)
                if capacity else 0.0),
            "elapsed_s": elapsed,
            "armed_duration_s": self.arm_report.get("duration_s"),
            "stop_reason": self.stop_reason,
            "plan": plan.as_dict() if plan else None,
            "bytes_in_ram": sum(b.count * b.frame_bytes
                                for b in self.buffers.values()),
            "planned_write_bytes": self.planned_bytes() if stored else 0,
            "last_error": self.last_error,
            "last_result_directory": (
                (self.last_result or {}).get("directory")),
        }


def _headline(state: str, unsaved: bool) -> str:
    if state == CAPTURED and unsaved:
        return ("Captured -- NOT SAVED. The frames are in RAM only and are "
                "lost on a process restart or a power cut.")
    return {
        IDLE: "Idle.",
        ARMING: "Arming: committing memory.",
        ARMED: "Armed.",
        RECORDING: "Recording into RAM.",
        SAVING: "Writing to the verified target.",
        SAVED: "Saved.",
        FAILED: "Failed.",
    }.get(state, state)


def _dtype_for(plan: Plan) -> Any:
    """The in-RAM dtype for a plan's format.

    8-bit comes off the ISP as uint8 and is stored as uint8: widening it to
    uint16 would double the memory for no information and make the file claim a
    depth the data does not have.
    """
    return np.uint8 if plan.fmt.bytes_per_pixel <= 1.0 else np.uint16


def _burst_dirname(label: str | None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in (label or "") if c.isalnum() or c in "-_")
    return f"burst_{stamp}" + (f"_{safe}" if safe else "")


def _offsets(buffers: dict[str, FrameBuffer]) -> dict[str, Any]:
    """The measured inter-head timing, for exactly two heads.

    Reported as a distribution rather than a single number because that is what
    it is: two free-running sensors drift against each other and the spread is
    the quantity of interest. Nearest-neighbour matching on SensorTimestamp,
    which is the only clock with a defined relation to exposure.
    """
    names = [c for c, b in buffers.items() if b.count]
    if len(names) != 2:
        return {"available": False,
                "reason": f"{len(names)} head(s) with frames"}
    a, b = (buffers[n] for n in names)
    ta = np.array([m.sensor_timestamp for m in a.meta
                   if m.sensor_timestamp is not None], dtype=np.int64)
    tb = np.array([m.sensor_timestamp for m in b.meta
                   if m.sensor_timestamp is not None], dtype=np.int64)
    if ta.size == 0 or tb.size == 0:
        return {"available": False,
                "reason": "no SensorTimestamp in the delivered metadata"}
    idx = np.searchsorted(tb, ta).clip(1, tb.size - 1) if tb.size > 1 else None
    if idx is None:
        deltas = ta - tb[0]
    else:
        left, right = tb[idx - 1], tb[idx]
        pick = np.where(np.abs(ta - left) <= np.abs(ta - right), left, right)
        deltas = ta - pick
    ms = deltas / 1e6
    return {
        "available": True,
        "heads": names,
        "n": int(ms.size),
        "mean_ms": round(float(np.mean(ms)), 4),
        "median_ms": round(float(np.median(ms)), 4),
        "std_ms": round(float(np.std(ms)), 4),
        "min_ms": round(float(np.min(ms)), 4),
        "max_ms": round(float(np.max(ms)), 4),
        "note": ("Measured, not enforced. Nearest-neighbour match on "
                 f"SensorTimestamp, {names[0]} minus {names[1]}."),
    }
