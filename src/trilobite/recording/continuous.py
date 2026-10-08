"""Stage 5c: continuous recording to disk, with declared degradation.

The requirement, as stated: record continuously to disk, and handle a target
that cannot keep up by dropping frames or other declared lossy means. That one
permission is what makes this tractable. The previous design -- when every
exposed frame had to be retained -- had exactly one legal response to an
under-rate disk, which was to stop, so continuous recording was only possible
on a disk fast enough that it never happened. With permission to lose frames
the disk no longer has to be fast enough; **it has to be honest about what it
kept.**

## Shape

    capture thread -> slot ring (bounded, prefaulted) -> writer thread -> chunks
                                     |
                                     +-> counted drop

One ring and one writer thread per head. The capture thread never blocks on
the disk: it copies the frame out of the libcamera request into a free slot and
returns. If there is no free slot, the frame is dropped and counted, and the
request is released immediately -- which is the whole point, because an
unreleased request starves a four-deep pool and stalls the sensor itself.

## Drop-newest, and why not a ring that overwrites

A queue full at the moment a frame arrives drops **that** frame. It does not
overwrite the oldest, which is what a wrapping ring would do.

Overwriting the oldest makes the retained set depend on future events: a frame
already counted, already timestamped, already destined for the index would be
replaced by a later one, so what the file contains depends on when the stall
happened to end. Drop-newest keeps the retained set a prefix-consistent
subsample -- every frame the writer has seen is a frame that will be in the
file -- and makes the loss local to the instant it occurred.

It also falls out of the slot-pool design rather than needing a policy branch:
there is no free slot, so there is nowhere to put the frame.

## Heads drop independently

Two free-running sensors, two rings, two writers, two sets of drop counters.
Coupling the drops -- discarding head R's frame because head L's was dropped --
throws away good data to impose a pairing the rig does not guarantee. Nothing
triggers these sensors together and no synchronisation is claimed, so a
recording is **two independently sampled sequences sharing a recorded clock
domain**, reconciled offline from per-frame `SensorTimestamp`.

## Why a queue cannot save a slow disk, and is still worth having

A few hundred megabytes is two or three seconds of the pair stream. That is
enough to ride out a *stall* -- a garbage-collection pause in the drive's
controller, a metadata flush -- and no finite queue survives a *sustained*
deficit. The most likely cause of one is an SSD leaving its SLC cache part-way
through a multi-minute recording, which is why the pre-flight measures the
tail rate rather than the burst rate, and why dropping has to be a first-class
outcome rather than an error.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .. import health
from ..config import StorageConfig
from ..storage.writer import SessionWriter
from .buffer import FrameMeta, rss_bytes
from .chunks import RECORDING_SCHEMA, ChunkWriter, Journal
from .ladder import Plan

log = logging.getLogger(__name__)

IDLE = "idle"
ARMED = "armed"
RECORDING = "recording"
STOPPING = "stopping"
COMPLETE = "complete"
INCOMPLETE = "incomplete"      # stopped early, and the manifest says why
FAILED = "failed"

STOP_REQUESTED = "stopped by operator"
STOP_DURATION = "requested duration reached"
STOP_DROP_CEILING = "drop ceiling exceeded"
STOP_SPACE = "free-space reserve reached"
STOP_WRITE_ERROR = "write error"
STOP_TARGET_LOST = "target volume lost"

# The drop ceiling is a fraction, so it needs a denominator before it means
# anything: two dropped frames out of three is 67% and is also nothing at all.
# Two seconds of a 30 fps stream is enough to tell a stall from a deficit.
DROP_CEILING_MIN_FRAMES = 60


class SlotRing:
    """A fixed pool of prefaulted frame slots, handed out and returned.

    Not a queue of arrays: a queue of *indices* into one preallocated block.
    At 60 frames a second and 1.5 MB a frame, allocating per frame would be
    90 MB/s of allocation and the garbage collector would be doing it on the
    capture thread. Here the memory is committed once at arm time and the
    per-frame cost is one copy, which is unavoidable because the source is a
    libcamera request that must be released.
    """

    def __init__(self, frames: int, shape: tuple[int, int], dtype: Any) -> None:
        self.frames = int(frames)
        self.shape = (int(shape[0]), int(shape[1]))
        self.dtype = np.dtype(dtype)
        self.frame_bytes = int(np.prod(self.shape)) * self.dtype.itemsize
        self.nbytes = self.frame_bytes * self.frames
        self.store = np.empty((self.frames, *self.shape), dtype=self.dtype)
        self._free: collections.deque[int] = collections.deque(range(self.frames))
        self._lock = threading.Lock()
        self.prefault_ms = 0.0
        self.rss_delta: int | None = None

    def prefault(self) -> None:
        """Commit every page now. See recording/buffer.py for why."""
        before = rss_bytes()
        t0 = time.perf_counter()
        flat = self.store.reshape(-1).view(np.uint8)
        flat[::4096] = 0
        flat[-1] = 0
        self.prefault_ms = (time.perf_counter() - t0) * 1000.0
        after = rss_bytes()
        if before is not None and after is not None:
            self.rss_delta = after - before

    def acquire(self) -> int | None:
        """A free slot index, or None -- which IS the drop decision."""
        with self._lock:
            try:
                return self._free.popleft()
            except IndexError:
                return None

    def release(self, index: int) -> None:
        with self._lock:
            self._free.append(index)

    @property
    def in_use(self) -> int:
        with self._lock:
            return self.frames - len(self._free)

    def report(self) -> dict[str, Any]:
        return {
            "slots": self.frames,
            "bytes": self.nbytes,
            "gb": round(self.nbytes / 1e9, 3),
            "prefault_ms": round(self.prefault_ms, 1),
            "rss_delta_bytes": self.rss_delta,
        }


class HeadRecorder:
    """One head: its ring, its writer thread, its counters.

    The counters are the deliverable. A recording that lost frames is
    acceptable; one that lost frames without saying how many, where, or for how
    long is not, and these are what make the manifest's gap intervals
    reconcilable with what the operator watched on the screen.
    """

    def __init__(
        self,
        cam_id: str,
        directory: Path,
        plan: Plan,
        slots: int,
        dtype: Any,
        chunk_frames: int,
        block_frames: int,
        generation: int,
    ) -> None:
        self.cam_id = cam_id
        self.plan = plan
        self.ring = SlotRing(slots, (plan.height, plan.width), dtype)
        self.writer = ChunkWriter(
            directory / cam_id, cam_id, (plan.height, plan.width), dtype,
            chunk_frames=chunk_frames, prefix="rec", generation=generation)
        self.block_frames = max(1, int(block_frames))

        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # Counters. `exposed` counts every frame the SDK delivered during the
        # armed window, which is the denominator the acceptance criterion is
        # written against: every exposed frame is either in the output or
        # counted here.
        self.exposed = 0
        self.stored = 0
        self.dropped = 0
        self.first_seq: int | None = None
        self.last_seq: int | None = None
        self.bytes_written = 0
        self.write_error: BaseException | None = None
        # Rolling throughput, for the live display. Measured at the writer, so
        # it is the rate reaching the disk rather than the rate leaving the
        # sensor.
        self._marks: collections.deque[tuple[float, int]] = collections.deque(maxlen=64)
        # Recent drop decisions, for the instantaneous fraction.
        self._recent: collections.deque[bool] = collections.deque(maxlen=256)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"rec-write-{self.cam_id}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        pending = 0
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                if pending:
                    self.writer.sync()
                    pending = 0
                continue
            if item is None:
                break
            index, meta = item
            try:
                self.bytes_written += self.writer.write_frame(
                    self.ring.store[index], meta)
                self.stored += 1
                pending += 1
                if pending >= self.block_frames:
                    # The durability boundary. One fsync per block, never per
                    # frame: sixty a second collapses sustained throughput on a
                    # USB bridge and the completion journal gives the same
                    # guarantee more cheaply.
                    self.writer.sync()
                    self._marks.append((time.monotonic(), self.bytes_written))
                    pending = 0
            except BaseException as exc:                  # noqa: BLE001
                self.write_error = exc
                log.exception("%s: recording write failed", self.cam_id)
                self._stop.set()
                self.ring.release(index)
                break
            else:
                self.ring.release(index)
        if pending:
            try:
                self.writer.sync()
            except OSError as exc:
                self.write_error = self.write_error or exc

    def offer(self, pixels: np.ndarray, meta: FrameMeta) -> bool:
        """Take one frame from the capture thread. False means it was dropped.

        Never blocks. The copy into the slot is the only work done on the
        capture thread, and the request the pixels came from can be released
        the moment this returns.
        """
        self.exposed += 1
        if self.first_seq is None:
            self.first_seq = meta.seq
        self.last_seq = meta.seq

        index = self.ring.acquire()
        if index is None:
            self.dropped += 1
            self._recent.append(False)
            return False
        np.copyto(self.ring.store[index], pixels, casting="unsafe")
        self._q.put((index, meta))
        self._recent.append(True)
        return True

    def close(self, timeout: float = 30.0) -> dict[str, Any]:
        """Drain the queue, finish the chunks, return the summary."""
        self._stop.set()
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        summary = {}
        try:
            self.writer.close()
        except OSError as exc:
            self.write_error = self.write_error or exc
        summary = self.writer.summary()
        # The writer's own view is derived from the frames it got. These are
        # the capture side's counters, and the two must agree -- a mismatch
        # means a frame went missing between the ring and the file, which is
        # the one failure neither side can see alone.
        summary.update(
            frames_exposed=self.exposed,
            frames_dropped=self.dropped,
            frames_stored_counted=self.stored,
            drop_fraction=(round(self.dropped / self.exposed, 4)
                           if self.exposed else 0.0),
            accounted=(self.stored + self.dropped == self.exposed),
            unaccounted=self.exposed - self.stored - self.dropped,
            write_error=(None if self.write_error is None
                         else f"{type(self.write_error).__name__}: {self.write_error}"),
            ring=self.ring.report(),
        )
        return summary

    # -- live figures ----------------------------------------------------

    @property
    def drop_fraction(self) -> float:
        return self.dropped / self.exposed if self.exposed else 0.0

    @property
    def recent_drop_fraction(self) -> float:
        if not self._recent:
            return 0.0
        kept = sum(1 for ok in self._recent if ok)
        return 1.0 - kept / len(self._recent)

    @property
    def write_mb_s(self) -> float | None:
        if len(self._marks) < 2:
            return None
        (t0, b0), (t1, b1) = self._marks[0], self._marks[-1]
        if t1 <= t0:
            return None
        return (b1 - b0) / 1e6 / (t1 - t0)

    def live(self) -> dict[str, Any]:
        return {
            "cam_id": self.cam_id,
            "exposed": self.exposed,
            "stored": self.stored,
            "dropped": self.dropped,
            "drop_fraction": round(self.drop_fraction, 4),
            "drop_percent": round(self.drop_fraction * 100, 1),
            "recent_drop_percent": round(self.recent_drop_fraction * 100, 1),
            "queue_depth": self.ring.in_use,
            "queue_slots": self.ring.frames,
            "write_mb_s": (None if self.write_mb_s is None
                           else round(self.write_mb_s, 1)),
            "bytes": self.bytes_written,
            "chunks": len(self.writer.chunks),
            "write_error": (None if self.write_error is None
                            else str(self.write_error)),
            "preview_suppressed": self.preview_suppressed,
        }

    # -- the preview's standing ------------------------------------------
    #
    # A preview frame is never worth a recorded frame. While recording, the
    # preview already runs at ~2 Hz instead of the browser's 12, published out
    # of the recording request so it costs no extra camera access. That is
    # enough while the write path is keeping up.
    #
    # Once frames are being dropped, it is not: the pipeline pass, the JPEG
    # encode and the publish all run on the four cores that also hold two
    # capture loops and two writer threads, and spending them on a picture
    # while the recording is losing data has the priority backwards. So past
    # half the operator's own drop ceiling the preview stops entirely and says
    # so, and resumes when the recent drop rate falls back under the line.
    #
    # Half, and not the ceiling itself: at the ceiling the recording stops, so
    # a threshold there would never fire in time to help.

    preview_suppress_at: float = 0.0   # set by the recorder on arm

    @property
    def preview_suppressed(self) -> bool:
        limit = self.preview_suppress_at
        return bool(limit > 0 and self.recent_drop_fraction > limit)


class ContinuousRecorder:
    """A recording bounded by the disk rather than by RAM.

    One instance per rig. `offer` is called from each head's capture thread;
    everything else from the web layer or the application's own watchdog.
    """

    def __init__(self, cfg: StorageConfig, writer: SessionWriter) -> None:
        self.cfg = cfg
        self.writer = writer
        self._lock = threading.RLock()
        self.state = IDLE
        self.plan: Plan | None = None
        self.heads: dict[str, HeadRecorder] = {}
        self.directory: Path | None = None
        self.generation = 0
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.stop_reason: str | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.arm_report: dict[str, Any] = {}
        self.max_drop_fraction = float(cfg.max_drop_fraction)
        self._space_checked_at = 0.0
        self._usable_at_start = 0

    # -- sizing ----------------------------------------------------------

    def queue_capacity(self, plan: Plan) -> dict[str, Any]:
        """Slots per head, from measured MemAvailable.

        Deliberately a smaller fraction than the burst buffer's. The queue is
        only there to ride out a stall; making it bigger buys a second or two
        and takes the headroom the writer and the page cache need.
        """
        available_mb = health.memory_available_mb()
        cap_mb = (available_mb or 1024.0) * float(self.cfg.queue_memory_fraction)
        per_head_mb = cap_mb / max(1, plan.heads_count)
        slots = int((per_head_mb * (1 << 20)) // max(plan.bytes_per_frame, 1))
        slots = max(4, min(slots, 2048))
        return {
            "mem_available_mb": available_mb,
            "fraction": self.cfg.queue_memory_fraction,
            "slots_per_head": slots,
            "seconds_per_head": round(slots / plan.fps, 2) if plan.fps else 0.0,
            "bytes_total": slots * plan.bytes_per_frame * plan.heads_count,
        }

    def feasibility(self, plan: Plan,
                    sustained_mb_s: float | None) -> dict[str, Any]:
        """What this plan will do on this target, before Start is pressed."""
        state = self.writer.state()
        usable = int(state.get("usable_bytes", 0))
        drop = plan.predicted_drop_fraction(sustained_mb_s)
        max_duration = plan.max_duration_s(usable)
        return {
            "plan": plan.as_dict(sustained_mb_s=sustained_mb_s,
                                 usable_bytes=usable),
            "target": state.get("root"),
            "target_state": state.get("state"),
            "usable_bytes": usable,
            "usable_gb": round(usable / 1e9, 2),
            # The number the plan asks to be displayed before Start: a
            # recording that will hit the reserve in 90 s says so now rather
            # than failing at 90 s.
            "max_duration_s": round(max_duration, 1),
            "requested_duration_s": plan.duration_s,
            "fits": (plan.duration_s is None
                     or plan.duration_s <= max_duration),
            "predicted_drop_fraction": (
                None if drop is None else round(drop, 4)),
            "predicted_drop_percent": (
                None if drop is None else round(drop * 100, 1)),
            "sustained_mb_s": sustained_mb_s,
            "required_mb_s": round(plan.mb_s, 1),
            "drop_ceiling": self.max_drop_fraction,
            "queue": self.queue_capacity(plan),
            "measured": sustained_mb_s is not None,
            "note": (
                "No sustained measurement is on file for this target, so the "
                "drop prediction is unknown rather than zero. Run Measure."
                if sustained_mb_s is None else
                "The prediction assumes the shortfall is spread evenly. A real "
                "drive stalls in bursts, so expect the same total loss in "
                "fewer, longer gaps."),
        }

    # -- arming ----------------------------------------------------------

    def arm(self, plan: Plan, slots: int | None = None,
            internal_ok: bool = False,
            max_drop_fraction: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self.state in (RECORDING, STOPPING):
                raise RuntimeError(f"cannot arm while {self.state!r}.")

        capacity = self.queue_capacity(plan)
        want = capacity["slots_per_head"] if slots is None else int(slots)
        dtype = np.uint8 if plan.fmt.bytes_per_pixel <= 1.0 else np.uint16

        # Admitted here, for the whole planned recording where a duration was
        # given, so the reserve is checked against the plan rather than against
        # the moment. An open-ended recording is admitted for one second and
        # re-checked as it runs, because there is no total to check.
        need = plan.total_bytes() or int(plan.bytes_per_second)
        ceiling = (self.max_drop_fraction if max_drop_fraction is None
                   else float(max_drop_fraction))
        generation = self.writer.admit(need, internal_ok=internal_ok)
        try:
            root = Path(self.writer.session_dir) / _dirname(plan)
            root.mkdir(parents=True, exist_ok=True)
            heads: dict[str, HeadRecorder] = {}
            for cam_id in plan.heads:
                head = HeadRecorder(
                    cam_id, root, plan, want, dtype,
                    chunk_frames=int(self.cfg.chunk_frames),
                    block_frames=int(self.cfg.write_block_frames),
                    generation=generation)
                head.preview_suppress_at = ceiling / 2.0
                head.ring.prefault()
                heads[cam_id] = head
        except BaseException:
            self.writer.done()
            raise

        with self._lock:
            self.plan = plan
            self.heads = heads
            self.directory = root
            self.generation = generation
            self.state = ARMED
            self.stop_reason = None
            self.last_error = None
            if max_drop_fraction is not None:
                self.max_drop_fraction = float(max_drop_fraction)
            self._usable_at_start = int(
                self.writer.state().get("usable_bytes", 0))
            self.arm_report = {
                "plan": plan.as_dict(),
                "queue": capacity,
                "slots_per_head": want,
                "directory": str(root),
                "storage_generation": generation,
                "drop_ceiling": self.max_drop_fraction,
                "rings": {c: h.ring.report() for c, h in heads.items()},
            }
            return dict(self.arm_report)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.state != ARMED:
                raise RuntimeError(
                    f"cannot start from {self.state!r}; arm it first.")
            for head in self.heads.values():
                head.start()
            self.state = RECORDING
            self.started_at = time.monotonic()
            self.stopped_at = None
            self._space_checked_at = 0.0
            return self.status()

    # -- the capture path ------------------------------------------------

    def wants(self, cam_id: str) -> bool:
        return self.state == RECORDING and cam_id in self.heads

    def offer(self, cam_id: str, pixels: np.ndarray,
              meta: FrameMeta) -> bool | None:
        """Store one frame, or say why not. **Three outcomes, not two.**

        `None` means this recorder was not running when the frame arrived, so
        the frame was never part of the recording. It is **not a drop**, and
        returning `False` here -- which this did until 8 October -- put one
        phantom drop at the end of every recording: the frame in flight when
        the state left `RECORDING` was counted as lost by the capture loop
        while the recorder never counted it as exposed. That makes the two
        counts disagree by one, and their agreement is the only mechanism that
        would detect a frame genuinely lost between the capture thread and the
        ring. A detector that always fires by one is not a detector.
        """
        head = self.heads.get(cam_id)
        if head is None or self.state != RECORDING:
            return None
        kept = head.offer(pixels, meta)
        if head.write_error is not None:
            self._stop(STOP_WRITE_ERROR)
        return kept

    def watch(self) -> None:
        """Called on a timer. Enforces the ceilings a frame cannot see.

        Three things are checked here rather than in `offer`: the drop ceiling
        needs a denominator large enough to mean something, the free-space
        reserve is a `statvfs` call that must not happen per frame, and the
        duration limit is about wall time rather than about any one frame.
        """
        if self.state != RECORDING:
            return
        plan = self.plan
        if plan is None:
            return

        if (plan.duration_s and self.started_at is not None
                and time.monotonic() - self.started_at >= plan.duration_s):
            self._stop(STOP_DURATION)
            return

        # Checked PER HEAD, against that head's own denominator. Taking the
        # minimum exposure across heads would let a camera delivering nothing
        # suppress the ceiling for the one that is delivering badly -- and a
        # dead head is exactly the situation in which the other one's losses
        # matter most. Any head past the ceiling stops the recording, because
        # the recording is the pair.
        for head in self.heads.values():
            if head.exposed >= DROP_CEILING_MIN_FRAMES and (
                    head.drop_fraction > self.max_drop_fraction):
                # Past this point the honest answer is that this target cannot
                # do this job, and continuing to shed frames produces a
                # recording nobody will trust.
                self._stop(STOP_DROP_CEILING)
                return

        now = time.monotonic()
        if now - self._space_checked_at >= 2.0:
            self._space_checked_at = now
            state, _reason = self.writer.target_state()
            if state in ("absent", "wrong-volume", "quarantined", "read-only"):
                self._stop(STOP_TARGET_LOST)
            elif state == "full":
                self._stop(STOP_SPACE)

    def _stop(self, reason: str) -> None:
        with self._lock:
            if self.state != RECORDING:
                return
            self.state = STOPPING
            self.stopped_at = time.monotonic()
            self.stop_reason = reason
        log.info("recording stopping: %s", reason)

    def stop(self, reason: str = STOP_REQUESTED,
             timeout: float = 60.0) -> dict[str, Any]:
        """Stop, drain, close the chunks and write the journal."""
        with self._lock:
            if self.state in (IDLE, COMPLETE, INCOMPLETE):
                return self.status()
            if self.state == RECORDING:
                self.state = STOPPING
                self.stopped_at = time.monotonic()
                self.stop_reason = reason
            plan = self.plan
            heads = dict(self.heads)
            root = self.directory

        summaries = {}
        for cam_id, head in heads.items():
            summaries[cam_id] = head.close(timeout=timeout)

        clean = (self.stop_reason in (STOP_REQUESTED, STOP_DURATION)
                 and all(s.get("write_error") is None
                         for s in summaries.values())
                 and all(s.get("accounted") for s in summaries.values()))
        payload = self._journal(plan, root, summaries, clean)
        try:
            if root is not None:
                Journal(root, payload).write()
        except OSError as exc:
            payload["journal_error"] = f"{type(exc).__name__}: {exc}"
            log.exception("could not write the recording journal")

        self.writer.done()
        with self._lock:
            self.state = COMPLETE if clean else INCOMPLETE
            self.last_result = payload
            self.heads = {}
        return payload

    def _journal(self, plan: Plan | None, root: Path | None,
                 summaries: dict[str, Any], clean: bool) -> dict[str, Any]:
        exposed = sum(s.get("frames_exposed", 0) for s in summaries.values())
        stored = sum(s.get("frames_stored", 0) for s in summaries.values())
        dropped = sum(s.get("frames_dropped", 0) for s in summaries.values())
        unaccounted = sum(s.get("unaccounted", 0) for s in summaries.values())
        return {
            "schema": RECORDING_SCHEMA,
            "kind": "continuous",
            "complete": clean,
            "stop_reason": self.stop_reason,
            "plan": plan.as_dict() if plan else None,
            "directory": str(root) if root else None,
            "session_dir": str(self.writer.session_dir),
            "storage_generation": self.generation,
            "storage_identity": self.writer.identity_dict(),
            "started_t_wall": (
                None if self.started_at is None else
                time.time() - (time.monotonic() - self.started_at)),
            "duration_s": (
                round(self.stopped_at - self.started_at, 3)
                if self.started_at and self.stopped_at else None),
            "arm": self.arm_report,
            "drop_ceiling": self.max_drop_fraction,
            "heads": summaries,
            "totals": {
                "frames_exposed": exposed,
                "frames_stored": stored,
                "frames_dropped": dropped,
                "frames_unaccounted": unaccounted,
                "drop_fraction": (round(dropped / exposed, 4) if exposed else 0.0),
                "bytes": sum(s.get("bytes", 0) for s in summaries.values()),
            },
            # The acceptance criterion, evaluated and written down rather than
            # left to be checked by hand: every exposed frame is either in the
            # output or counted in a named gap.
            "every_frame_accounted": unaccounted == 0,
            "synchronised": False,
            "synchronisation_note": (
                "The heads are free-running and drop independently. No trigger "
                "couples them and none is claimed. Reconcile the pair offline "
                "from the per-frame SensorTimestamp in each head's index."),
            "degradation_note": (
                "Frame dropping is the ONLY runtime degradation. Pixel format, "
                "geometry and requested frame rate were fixed before Start and "
                "did not change: every frame in this recording has the same "
                "shape and depth."),
        }

    # -- reporting -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        heads = {c: h.live() for c, h in self.heads.items()}
        plan = self.plan
        elapsed = None
        if self.started_at is not None:
            end = self.stopped_at or time.monotonic()
            elapsed = round(end - self.started_at, 2)
        exposed = sum(h["exposed"] for h in heads.values())
        dropped = sum(h["dropped"] for h in heads.values())
        return {
            "state": self.state,
            "plan": plan.as_dict() if plan else None,
            "directory": str(self.directory) if self.directory else None,
            "elapsed_s": elapsed,
            "heads": heads,
            "frames_exposed": exposed,
            "frames_dropped": dropped,
            "drop_fraction": round(dropped / exposed, 4) if exposed else 0.0,
            "drop_percent": round(dropped / exposed * 100, 1) if exposed else 0.0,
            "drop_ceiling": self.max_drop_fraction,
            "write_mb_s": _sum_opt(h["write_mb_s"] for h in heads.values()),
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
            "storage_generation": self.generation,
            "last_result_directory": (self.last_result or {}).get("directory"),
            "every_frame_accounted": (
                (self.last_result or {}).get("every_frame_accounted")),
        }


def _sum_opt(values) -> float | None:
    got = [v for v in values if v is not None]
    return round(sum(got), 1) if got else None


def _dirname(plan: Plan) -> str:
    return (f"rec_{time.strftime('%Y%m%d_%H%M%S')}_"
            f"{plan.fmt.key}_{plan.fps:.0f}fps")
