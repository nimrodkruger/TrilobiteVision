"""One object the application and the web layer talk to about recording.

Both recorders, the pre-flight and the plan selection behind a single surface,
because the capture loop needs exactly one question answered per frame -- "do
you want this?" -- and asking two recorders separately in the hot path is how
the two end up both wanting it.

**Only one recorder may be active at a time**, and that is a rule rather than
an implementation convenience: a burst and a continuous recording would
compete for the same memory, the same disk bandwidth and the same frames, and
the sum of two conservative memory limits is not a conservative memory limit.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from ..config import StorageConfig
from ..storage.writer import SessionWriter
from . import burst as burst_mod
from . import continuous as cont_mod
from . import preflight as preflight_mod
from .buffer import FrameMeta
from .ladder import Plan, enumerate_plans

log = logging.getLogger(__name__)

BURST = "burst"
CONTINUOUS = "continuous"

# How long Start waits for every head's capture loop to reach the recording
# branch. One frame period at 30 fps is 33 ms, so this is ~150 of them: ample
# for a working head and short enough that a dead one is reported promptly.
START_WAIT_S = 5.0


def _missing_heads(missing: tuple[str, ...], wait_s: float) -> str:
    return (
        f"{', '.join(missing)} delivered no frame within {wait_s:.0f} s, so "
        f"the recording was stopped before it began. A recording that "
        f"silently contains some of the heads looks complete and is not; "
        f"check that head's status.")


def meta_from_frame(frame: Any) -> FrameMeta:
    """Build the index record for one acquired frame.

    `SensorTimestamp` is taken from the frame's own metadata and **not
    substituted** when absent. It is the only clock with a defined relation to
    exposure, so a None here is a real statement about what the driver
    delivered, and filling it with `t_mono` would produce an index that looks
    usable for timing and is not.
    """
    meta = frame.meta or {}
    stamp = meta.get("SensorTimestamp")
    reservations = tuple(meta.get("raw_reservations") or ())
    observed = meta.get("raw_observed_max")
    return FrameMeta(
        seq=int(frame.seq),
        t_mono=float(frame.t_mono),
        t_wall=float(frame.t_wall),
        sensor_timestamp=int(stamp) if isinstance(stamp, (int, float)) else None,
        validity=str(frame.validity),
        reservations=reservations,
        observed_max=int(observed) if isinstance(observed, (int, float)) else None,
    )


class RecordingManager:
    """Arming, starting, stopping and reporting, for both recorders."""

    def __init__(self, cfg: StorageConfig, writer: SessionWriter) -> None:
        self.cfg = cfg
        self.writer = writer
        self.burst = burst_mod.BurstRecorder(cfg, writer)
        self.continuous = cont_mod.ContinuousRecorder(cfg, writer)
        self.preflight = preflight_mod.Preflight()
        self._lock = threading.RLock()
        # Snapshotted at camera open, on the owner thread: cam_id -> that
        # camera's raw_capabilities(). The pre-flight needs it and must not
        # reach into a camera from the web thread to get it.
        self.camera_caps: dict[str, Any] = {}
        self.geometry: dict[str, Any] = {}
        self.active: str | None = None
        self.last_error: str | None = None
        # The start barrier. See `_begin`.
        self._entered: set[str] = set()
        self._loops: set[str] = set()
        self._entered_cv = threading.Condition()
        self._awaiting_entry = False

    # -- registration ----------------------------------------------------

    def register(self, cam_id: str, caps: dict[str, Any],
                 width: int, height: int, fps: float,
                 orientation: dict[str, Any] | None = None,
                 oriented: bool = False) -> None:
        """Called by a camera runtime as it opens. Off the hot path.

        The geometry recorded here must be **the frame this backend's
        `read_raw` will actually deliver**, which is the sensor frame for
        picamera2 (it does not orient; see cameras/base.RawRead) and the
        post-rotation frame for the offline backends (they go through
        `capture_full`, which does). `oriented` says which, and the two have to
        be set together: a plan built for the sensor frame against a backend
        that delivers a turned one is a shape mismatch on every frame.
        """
        with self._lock:
            self.camera_caps[cam_id] = caps
            self.geometry[cam_id] = {
                "width": int(width), "height": int(height), "fps": float(fps),
                # Carried on every plan, because the stored pixels are in
                # sensor orientation and the reader has to be told what to
                # apply. See ladder.Plan.orientation.
                "orientation": dict(orientation or {}),
                "oriented": bool(oriented)}

    def unregister(self, cam_id: str) -> None:
        with self._lock:
            self.camera_caps.pop(cam_id, None)
            self.geometry.pop(cam_id, None)

    def heads(self) -> tuple[str, ...]:
        return tuple(sorted(self.geometry))

    def common_geometry(self) -> tuple[int, int, float]:
        """The frame the heads agree on, or a refusal.

        Two heads of different sizes cannot go into one plan: the chunk files
        are per head so it would be *storable*, but the plan's arithmetic, the
        buffer sizing and the predicted drop fraction are all per frame, and a
        single number for two different frames is wrong in a way nobody would
        notice until the recording did not fit.
        """
        if not self.geometry:
            raise RuntimeError("no cameras are open, so there is nothing to record")
        sizes = {(g["width"], g["height"]) for g in self.geometry.values()}
        if len(sizes) > 1:
            raise RuntimeError(
                f"the heads report different sensor frames ({sizes}). A single "
                f"recording plan cannot describe both; record them separately.")
        oriented = {bool(g.get("oriented")) for g in self.geometry.values()}
        if len(oriented) > 1:
            # Mixing backends, one of which turns its frames and one of which
            # does not. The sizes happen to match, so nothing would fail -- and
            # the journal would carry one orientation flag for two different
            # conventions, which the reader would apply to both.
            raise RuntimeError(
                "the heads disagree about whether their frames arrive "
                "oriented, so one journal cannot describe both. Record them "
                "separately.")
        w, h = next(iter(sizes))
        fps = min(g["fps"] for g in self.geometry.values())
        return w, h, fps

    @property
    def oriented(self) -> bool:
        return any(bool(g.get("oriented")) for g in self.geometry.values())

    # -- the hot path ----------------------------------------------------

    def wants(self, cam_id: str) -> bool:
        """Does anything want raw frames from this head right now?

        One boolean read per frame, and deliberately not a lock: it is called
        at sensor rate from the capture thread, and a lock here would serialise
        the two capture loops against each other for no benefit. The worst a
        race can do is offer one frame to a recorder that has just stopped,
        which `offer` refuses.
        """
        return (self.burst.wants(cam_id)
                or self.continuous.wants(cam_id))

    def wants_preview(self, cam_id: str) -> bool:
        """Is a preview frame worth taking out of the same request?

        A preview frame is never worth a recorded frame, so while recording the
        preview is published from the recording request at a much lower rate --
        the rate is the caller's own clock, not this. What this answers is the
        stronger question: whether the preview should run at all.

        It should not, once the recording is losing frames. Past half the
        operator's drop ceiling the pipeline pass and the JPEG encode are
        competing for the cores the write path needs, and spending them on a
        picture while data is being lost has the priority backwards. See
        `ContinuousRecorder.preview_suppressed`.
        """
        head = self.continuous.heads.get(cam_id)
        if head is not None and self.continuous.wants(cam_id):
            return not head.preview_suppressed
        return True

    def offer(self, cam_id: str, pixels: np.ndarray, frame: Any) -> bool | None:
        """Hand one acquired raw frame to whichever recorder is running.

        **Three outcomes, not two**, and the third is the one that matters for
        the accounting:

            True   stored
            False  DROPPED -- the recorder was running and had no room
            None   nobody wanted it, because the recording ended between the
                   capture thread's `wants` check and this call

        `None` is not a drop. The recorder did not count that frame as exposed
        either, so counting it as dropped on the capture side would invent a
        discrepancy between two counts that are supposed to agree -- and that
        agreement is how a frame genuinely lost between the capture thread and
        the recorder would be detected. Collapsing the two into one boolean
        made every recording end with one phantom drop.
        """
        meta = meta_from_frame(frame)
        kept: bool | None = None
        if self.burst.wants(cam_id):
            kept = self.burst.offer(cam_id, pixels, meta)
        elif self.continuous.wants(cam_id):
            kept = self.continuous.offer(cam_id, pixels, meta)
        if kept is not None and self._awaiting_entry:
            # This head's capture loop has reached the recording branch, so it
            # can no longer release a frame to the preview cap. One plain bool
            # read per frame once the barrier is satisfied; see `_begin`.
            self._note_entry(cam_id)
        return kept

    # -- the start barrier -----------------------------------------------

    def attach_loop(self, cam_id: str) -> None:
        """A capture loop for this head is now turning.

        The start barrier waits on these and not on `register`: a head can be
        registered with no loop behind it (a unit test driving `offer` by hand,
        or a thread that has died), and waiting on such a head would hang a
        start that is otherwise fine. Waiting on a loop that exists is the
        actual question -- see `_begin`.
        """
        with self._entered_cv:
            self._loops.add(cam_id)

    def detach_loop(self, cam_id: str) -> None:
        with self._entered_cv:
            self._loops.discard(cam_id)

    def _note_entry(self, cam_id: str) -> None:
        with self._entered_cv:
            if cam_id not in self._entered:
                self._entered.add(cam_id)
                self._entered_cv.notify_all()

    def _begin(self, heads: tuple[str, ...], begin: Any, wait_s: float
               ) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
        """Start a recorder and **do not return until every head is in it.**

        Without this, `start()` returning meant only that a flag had been set.
        Each capture loop decides once per frame whether to record or to
        release the frame to the preview cap, and a loop that evaluated that
        check microseconds before the flag flipped releases one more frame
        undecoded. That frame is never offered to the recorder, so **nothing
        counts it**: it is not a drop, it is simply absent, which is the one
        kind of loss this stage is built to make impossible.

        The window is a single frame period and would almost never be noticed,
        which is exactly why it has to be closed here rather than tolerated.
        So the contract becomes the stronger one worth having: when Start
        returns, every head is recording.

        A head that has not delivered a frame within `wait_s` is a failure to
        start, not a recording to continue with -- a recording that silently
        contains one of two heads is worse than no recording, because it looks
        complete. The caller stops it and names the heads that did not arrive.
        """
        with self._entered_cv:
            self._entered.clear()
            # Only heads with a live capture loop. Snapshotted before the
            # recorder starts, so a loop that stops during the wait still
            # counts as missing rather than quietly dropping out of the set.
            expected = set(heads) & set(self._loops)
            self._awaiting_entry = True
        try:
            out = begin()
        except BaseException:
            self._awaiting_entry = False
            raise
        deadline = time.monotonic() + max(0.0, wait_s)
        try:
            with self._entered_cv:
                while not expected.issubset(self._entered):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._entered_cv.wait(remaining)
                missing = tuple(sorted(expected - self._entered))
                confirmed = tuple(sorted(expected & self._entered))
        finally:
            self._awaiting_entry = False
        return out, missing, confirmed

    def watch(self) -> None:
        """Called on the application's storage timer. Enforces the ceilings."""
        self.continuous.watch()
        if (self.continuous.state == cont_mod.STOPPING
                and self.active == CONTINUOUS):
            # The recorder decided to stop itself -- drop ceiling, reserve, a
            # lost volume. Finishing the write is this thread's job, not the
            # capture thread's: closing chunks and writing a journal is disk
            # work and must not happen inside a frame callback.
            try:
                self.continuous.stop(reason=self.continuous.stop_reason
                                     or "stopped")
            except Exception as exc:                      # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("could not finish the recording")
            finally:
                self.active = None

    # -- pre-flight ------------------------------------------------------

    def measure(self, budget_bytes: int = 8 << 30,
                budget_seconds: float = 30.0) -> dict[str, Any]:
        with self._lock:
            if self.active is not None:
                raise RuntimeError(
                    f"a {self.active} recording is active; measuring would "
                    f"compete with it for the disk it is writing to.")
        w, h, fps = self.common_geometry()
        pf = preflight_mod.run(
            self.writer, dict(self.camera_caps), width=w, height=h,
            sensor_fps=fps, budget_bytes=budget_bytes,
            budget_seconds=budget_seconds)
        with self._lock:
            self.preflight = pf
        return pf.as_dict(self.writer)

    def options(self) -> dict[str, Any]:
        """Everything the UI needs to offer a configuration.

        Plans come from the pre-flight when it is current, and from the
        conservative default when it is not -- in which case every predicted
        drop fraction is **unknown rather than zero**, which is the one answer
        this must never give by omission.
        """
        try:
            w, h, fps = self.common_geometry()
        except RuntimeError as exc:
            return {"available": False, "reason": str(exc)}

        pf = self.preflight
        current = pf.valid_for(self.writer) and bool(pf.t_wall)
        sustained = pf.sustained_mb_s if current else None
        keys = (set(pf.formats) if current
                else preflight_mod.available_format_keys(self.camera_caps))
        plans = enumerate_plans(
            heads=self.heads(), width=w, height=h, sensor_fps=fps,
            available_formats=keys, sustained_mb_s=sustained)
        state = self.writer.state()
        return {
            "available": True,
            "heads": list(self.heads()),
            "sensor": {"width": w, "height": h, "fps": fps},
            "preflight": pf.as_dict(self.writer),
            "preflight_current": current,
            "plans": plans,
            "storage": {
                "root": state.get("root"),
                "state": state.get("state"),
                "state_human": state.get("state_human"),
                "removable": state.get("removable"),
                "generation": state.get("generation"),
                "usable_bytes": state.get("usable_bytes"),
                "usable_gb": round(state.get("usable_bytes", 0) / 1e9, 2),
                "reserve_bytes": state.get("reserve_bytes"),
            },
            "defaults": {
                "drop_ceiling": self.cfg.max_drop_fraction,
                "chunk_frames": self.cfg.chunk_frames,
                "write_block_frames": self.cfg.write_block_frames,
                # Always False. The override is per save, and the UI must
                # present it unticked every time -- see burst.py.
                "internal_ok": False,
            },
        }

    def plan(self, key: str, fps: float,
             duration_s: float | None = None) -> Plan:
        w, h, sensor_fps = self.common_geometry()
        if fps > sensor_fps * 1.001:
            raise ValueError(
                f"{fps:.1f} fps was asked for and the sensor is configured for "
                f"{sensor_fps:.1f}. A recording cannot run faster than the "
                f"frames arrive.")
        return preflight_mod.plan_from(
            key, fps, self.heads(), w, h, duration_s=duration_s,
            orientation={c: dict(g.get("orientation") or {})
                         for c, g in self.geometry.items()},
            oriented=self.oriented)

    # -- burst -----------------------------------------------------------

    def arm_burst(self, key: str, fps: float,
                  duration_s: float | None = None,
                  frames: int | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_free(BURST)
            plan = self.plan(key, fps, duration_s)
            out = self.burst.arm(plan, frames=frames)
            self.active = BURST
            return out

    def start_burst(self, wait_s: float = START_WAIT_S) -> dict[str, Any]:
        heads = tuple(sorted(self.burst.buffers))
        out, missing, confirmed = self._begin(heads, self.burst.start, wait_s)
        if missing:
            self.burst.stop(reason=f"no frames from {', '.join(missing)}")
            raise RuntimeError(_missing_heads(missing, wait_s))
        out["heads_confirmed"] = list(confirmed)
        return out

    def stop_burst(self) -> dict[str, Any]:
        return self.burst.stop()

    def save_burst(self, internal_ok: bool = False,
                   label: str | None = None) -> dict[str, Any]:
        out = self.burst.flush(internal_ok=internal_ok, label=label)
        return out

    def discard_burst(self) -> dict[str, Any]:
        return self.burst.discard()

    def release_burst(self) -> dict[str, Any]:
        out = self.burst.disarm()
        with self._lock:
            if self.active == BURST:
                self.active = None
        return out

    # -- continuous ------------------------------------------------------

    def feasibility(self, key: str, fps: float,
                    duration_s: float | None) -> dict[str, Any]:
        plan = self.plan(key, fps, duration_s)
        sustained = (self.preflight.sustained_mb_s
                     if self.preflight.valid_for(self.writer) else None)
        return self.continuous.feasibility(plan, sustained)

    def arm_continuous(self, key: str, fps: float,
                       duration_s: float | None = None,
                       internal_ok: bool = False,
                       max_drop_fraction: float | None = None,
                       slots: int | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_free(CONTINUOUS)
            plan = self.plan(key, fps, duration_s)
            out = self.continuous.arm(
                plan, slots=slots, internal_ok=internal_ok,
                max_drop_fraction=max_drop_fraction)
            self.active = CONTINUOUS
            return out

    def start_continuous(self, wait_s: float = START_WAIT_S) -> dict[str, Any]:
        heads = tuple(sorted(self.continuous.heads))
        out, missing, confirmed = self._begin(
            heads, self.continuous.start, wait_s)
        if missing:
            try:
                self.continuous.stop(reason=f"no frames from {', '.join(missing)}")
            finally:
                with self._lock:
                    self.active = None
            raise RuntimeError(_missing_heads(missing, wait_s))
        # The heads OBSERVED in the recording branch, not the plan's heads.
        # They differ only when a head has no capture loop behind it, which in
        # production does not happen and in a test means the test is driving
        # `offer` by hand -- and reporting the plan here would state as fact
        # something that was never checked.
        out["heads_confirmed"] = list(confirmed)
        return out

    def stop_continuous(self) -> dict[str, Any]:
        out = self.continuous.stop()
        with self._lock:
            self.active = None
        return out

    # -- state -----------------------------------------------------------

    def _require_free(self, wanted: str) -> None:
        if self.burst.unsaved:
            raise RuntimeError(
                "there is a burst in RAM that has not been saved. It exists "
                "nowhere else and would be lost. Save it, or discard it "
                "explicitly.")
        if self.active is not None and self.active != wanted:
            raise RuntimeError(
                f"a {self.active} recording is already armed or running. Only "
                f"one recorder runs at a time: two would compete for the same "
                f"memory, the same disk bandwidth and the same frames.")

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "burst": self.burst.status(),
            "continuous": self.continuous.status(),
            "preflight": self.preflight.as_dict(self.writer),
            "heads": list(self.heads()),
            "last_error": self.last_error,
        }
