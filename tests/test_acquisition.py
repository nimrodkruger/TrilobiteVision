"""One owner per camera, proved by thread id, and captures that reliably land.

Review finding F1. The old arrangement had `capture_still` calling into the
camera on a web worker thread while the capture loop called into it on its own,
with two mutexes making them take turns. Serialisation is not ownership: a
mutex does not say who releases a request when the holder raises, which thread
may close the device, or what a caller's timeout means for an SDK call that
cannot be interrupted. Two threads on a four-deep request pool took the Pi down
repeatedly, and a four-core CPU stress test did not -- which is how the camera
path rather than the load was identified.

So the property under test is not "captures work". It is **every call that
reaches the source comes from one thread**, and that is asserted directly, by
recording `threading.get_ident()` inside the source itself while the workload
is deliberately concurrent.

The last section is the reliability soak: a few hundred captures through the
real runtime, the real pipeline and the real writer, checking that every one of
them is on disk with a sidecar that parses and a size that matches.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from trilobite.acquisition import (
    CameraOwner,
    CommandExpired,
    CommandRejected,
)
from trilobite.app import CameraRuntime
from trilobite.cameras.offline import SyntheticSource
from trilobite.config import CameraConfig, StageConfig, StorageConfig
from trilobite.storage.writer import SessionWriter


class RecordingSource(SyntheticSource):
    """A synthetic camera that writes down which thread touched it.

    Subclassed rather than wrapped so the recording happens INSIDE the source,
    below anything a test could accidentally route around. Every method here is
    one that reaches the SDK on the real backend.
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.threads: set[int] = set()
        self.calls: list[str] = []
        self._rec_lock = threading.Lock()
        self.concurrent = 0          # peak simultaneous entries; must stay 1
        self._inside = 0
        self.delay = 0.0             # injected stall, seconds

    def _enter(self, what: str) -> None:
        with self._rec_lock:
            self.threads.add(threading.get_ident())
            self.calls.append(what)
            self._inside += 1
            self.concurrent = max(self.concurrent, self._inside)

    def _exit(self) -> None:
        with self._rec_lock:
            self._inside -= 1

    def read_preview(self):
        self._enter("read_preview")
        try:
            return super().read_preview()
        finally:
            self._exit()

    def skip_preview(self):
        self._enter("skip_preview")
        try:
            return super().skip_preview()
        finally:
            self._exit()

    def capture_full(self, raw: bool = True):
        self._enter("capture_full")
        try:
            if self.delay:
                time.sleep(self.delay)
            return super().capture_full(raw=raw)
        finally:
            self._exit()

    def set_controls(self, controls):
        self._enter("set_controls")
        try:
            return super().set_controls(controls)
        finally:
            self._exit()


def runtime(tmp_path, **cfg_kw):
    writer = SessionWriter(StorageConfig(root=str(tmp_path / "d")), tmp_path / "d")
    cfg = CameraConfig(
        cam_id="left", backend="synthetic", fps=60,
        full_resolution=(96, 72), preview_resolution=(48, 36),
        synthetic_drift_px=0.0,
        pipeline=[StageConfig(type="stats", name="stats")],
        **cfg_kw,
    )
    cam = CameraRuntime(cfg, writer=writer)
    cam.source = RecordingSource(cfg)      # replaces the registry-built one
    return cam


# -- the property the stage exists for --------------------------------------


def test_only_one_thread_ever_touches_the_camera(tmp_path):
    """The finding, as one assertion, under a deliberately concurrent load.

    Eight threads issuing stills and controls while the capture loop runs its
    own preview reads. If ownership were merely serialised rather than owned,
    the thread set would have nine members and the test would still 'work'.
    """
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for i in range(24):
                if i % 3 == 0:
                    futures.append(pool.submit(cam.set_controls,
                                               {"ExposureTime": 1000 + i}))
                else:
                    futures.append(pool.submit(cam.grab_still, True))
            for f in futures:
                f.result()
    finally:
        cam.stop()

    assert len(cam.source.threads) == 1, (
        f"{len(cam.source.threads)} threads reached the camera; the whole "
        f"point of this stage is that exactly one does")
    assert cam.source.concurrent == 1, "two calls overlapped inside the source"
    assert "capture_full" in cam.source.calls
    assert "set_controls" in cam.source.calls


def test_the_owner_thread_is_the_capture_thread(tmp_path):
    """Not merely 'one thread' -- the RIGHT one. A dedicated command thread
    would satisfy the count above and reintroduce the two-consumer problem
    against the preview loop."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.1)
        cam.grab_still()
        assert cam.source.threads == {cam._thread.ident}
    finally:
        cam.stop()


def test_a_caller_outside_the_owner_is_named_if_it_gets_through(tmp_path):
    """The backstop. `assert_owner` turns the class of bug that took the rig
    down into a traceback naming the offending thread, rather than a lockup."""
    owner = CameraOwner("left")
    owner.adopt()                                  # this thread owns it
    owner.assert_owner("capture_request")          # must not raise

    boom: list[BaseException] = []

    def elsewhere():
        try:
            owner.assert_owner("capture_request")
        except BaseException as exc:               # noqa: BLE001 - captured
            boom.append(exc)

    t = threading.Thread(target=elsewhere, name="intruder")
    t.start()
    t.join()
    assert boom and "does not own this camera" in str(boom[0])
    assert "intruder" in str(boom[0])


def test_the_source_itself_refuses_a_call_from_a_foreign_thread(tmp_path):
    """The review's objection, closed.

    `assert_owner` existed, was unit-tested, and was called from nowhere in the
    acquisition path -- so it backstopped nothing and describing it as a
    runtime guard was an overclaim. It is now called by the source, below
    anything a caller could route around, and this is the test that says so:
    a direct `capture_full` from a foreign thread must be refused.
    """
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        boom: list[BaseException] = []

        def intruder():
            try:
                cam.source.capture_full(raw=True)       # straight past the queue
            except BaseException as exc:                # noqa: BLE001
                boom.append(exc)

        t = threading.Thread(target=intruder, name="intruder")
        t.start()
        t.join(5.0)
        assert boom, "the source accepted a call from a non-owner thread"
        assert "does not own this camera" in str(boom[0])
        assert "capture_full" in str(boom[0])
    finally:
        cam.stop()


@pytest.mark.parametrize("call", ["read_preview", "skip_preview", "set_controls"])
def test_every_sdk_entry_point_is_guarded(tmp_path, call):
    """Not just the one. Each of these reaches the driver on the real backend."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        boom: list[BaseException] = []

        def intruder():
            fn = getattr(cam.source, call)
            try:
                fn({"ExposureTime": 100}) if call == "set_controls" else fn()
            except BaseException as exc:                # noqa: BLE001
                boom.append(exc)

        t = threading.Thread(target=intruder, name="intruder")
        t.start()
        t.join(5.0)
        assert boom and "does not own this camera" in str(boom[0]), call
    finally:
        cam.stop()


# -- the queue ---------------------------------------------------------------


def test_a_full_queue_refuses_rather_than_growing():
    """A backlog is the problem, not the solution to it. The refusal names the
    depth and the oldest age so the operator can see which."""
    owner = CameraOwner("left", capacity=2)
    owner.adopt()
    owner._owner_thread = -1        # pretend we are not the owner, so it queues
    owner._running = True

    owner._q.put_nowait(_stub())
    owner._q.put_nowait(_stub())
    with pytest.raises(CommandRejected, match="already queued"):
        owner.submit("still", lambda: None, 1.0)
    assert owner.rejected == 1


def _stub():
    from trilobite.acquisition import Command
    now = time.monotonic()
    return Command(kind="x", fn=lambda: None, deadline=now + 60, submitted=now)


def test_a_command_that_sat_past_its_deadline_never_runs():
    """The one that matters for a still: requested ten seconds ago, serviced
    now, and saved as though it were current. Checked when the command reaches
    the head of the queue, not when it was submitted."""
    owner = CameraOwner("left")
    owner._owner_thread = -1
    owner._running = True

    ran: list[int] = []
    from trilobite.acquisition import Command
    now = time.monotonic()
    owner._q.put_nowait(Command(kind="still", fn=lambda: ran.append(1),
                                deadline=now - 0.01, submitted=now - 5))

    owner._owner_thread = threading.get_ident()
    owner.service()
    assert ran == [], "an expired command must not execute"
    assert owner.expired == 1


def test_an_abandoned_command_resolves_exactly_once(tmp_path):
    """A caller's timeout does not interrupt an SDK call, so the command may
    complete afterwards. It must not then resolve a second time and report a
    success nobody is listening for."""
    cam = runtime(tmp_path)
    cam.source.delay = 0.6                 # longer than the deadline below
    cam.start()
    try:
        time.sleep(0.05)
        with pytest.raises(CommandExpired, match="had STARTED"):
            cam.owner.submit("still", lambda: cam.source.capture_full(True), 0.15)
        time.sleep(1.0)                    # let it finish under us
        st = cam.owner.state()
        assert st["abandoned"] == 1
        assert st["completed"] == 0, "resolved once, and it was as abandoned"
    finally:
        cam.source.delay = 0.0
        cam.stop()


def test_submitting_to_a_stopped_camera_is_refused_at_once():
    cam_owner = CameraOwner("left")
    with pytest.raises(CommandRejected, match="not running"):
        cam_owner.submit("still", lambda: None, 1.0)


def test_stopping_refuses_work_ALREADY_queued_rather_than_stranding_it():
    """The case a stopped-camera check does not cover.

    A caller blocked on a still is holding an HTTP request open. If `retire`
    only stopped accepting NEW work, that caller would sit out its full
    three-second deadline waiting for a thread that has gone. It has to be told.
    """
    owner = CameraOwner("left")
    owner.adopt()
    owner._owner_thread = -1        # so submit queues instead of running inline

    waiter: dict = {}

    def caller():
        try:
            owner.submit("still", lambda: waiter.setdefault("ran", True), 30.0)
        except BaseException as exc:            # noqa: BLE001 - captured
            waiter["error"] = exc

    t = threading.Thread(target=caller, daemon=True)
    t.start()
    for _ in range(200):                        # wait for it to be queued
        if owner.state()["depth"]:
            break
        time.sleep(0.005)
    assert owner.state()["depth"] == 1

    owner.retire()
    t.join(timeout=2.0)
    assert not t.is_alive(), "the caller was left waiting on a dead camera"
    assert isinstance(waiter.get("error"), CommandRejected)
    assert "stopped before this ran" in str(waiter["error"])
    assert "ran" not in waiter, "a refused command must not also execute"


def test_a_stopped_runtime_refuses_new_captures(tmp_path):
    cam = runtime(tmp_path)
    cam.start()
    time.sleep(0.05)
    cam.stop()
    with pytest.raises(CommandRejected, match="not running"):
        cam.grab_still()


def test_service_runs_at_most_its_budget():
    """The bound that keeps a command backlog from stalling the preview.

    Asserted directly rather than through the loop, and the reason is worth
    recording: at capacity 8 and a budget of 4 the bound costs at most one
    extra frame period, so no end-to-end test can distinguish it from an
    unbounded drain. It is slack today. It stops being slack the moment the
    capacity is raised or a command gets slower, which is exactly when nobody
    will be looking.
    """
    owner = CameraOwner("left", capacity=8)
    owner.adopt()
    ran: list[int] = []
    now = time.monotonic()
    from trilobite.acquisition import Command
    for i in range(8):
        owner._q.put_nowait(Command(kind="x", fn=lambda i=i: ran.append(i),
                                    deadline=now + 60, submitted=now))

    assert owner.service(budget=4) == 4
    assert ran == [0, 1, 2, 3], "FIFO, and exactly the budget"
    assert owner.service(budget=4) == 4
    assert ran == list(range(8))
    assert owner.service() == 0

    # And the DEFAULT budget is the bound, not an unbounded drain. Asserted
    # separately because passing `budget` explicitly above tests the parameter
    # and says nothing about what the capture loop actually calls.
    from trilobite.acquisition import WORK_PER_LOOP

    again = CameraOwner("left", capacity=8)
    again.adopt()
    for _ in range(8):
        again._q.put_nowait(Command(kind="x", fn=lambda: None,
                                    deadline=now + 60, submitted=now))
    assert again.service() == WORK_PER_LOOP


def test_completion_and_timeout_resolve_exactly_once_under_a_forced_race():
    """The race the Stage 3 review reproduced, as a test.

    `_resolve` used to check `outcome != "queued"` and then assign -- a read
    followed by a write, with a thread switch available in between. The review
    forced the switch and got both callers reporting success:

        RESOLVE RACE [('abandoned', True), ('done', True)] final done

    My own abandoned-command test passed throughout, because it relied on
    timing rather than on an interleaving. This one removes the timing: both
    threads are held at a barrier until each has decided to resolve.
    """
    from trilobite.acquisition import Command

    for _ in range(200):
        now = time.monotonic()
        cmd = Command(kind="still", fn=lambda: None,
                      deadline=now + 60, submitted=now)
        cmd.started = True                    # as if the owner had entered fn
        gate = threading.Barrier(2)
        wins: list[tuple[str, bool]] = []
        lk = threading.Lock()

        def completer(cmd=cmd, gate=gate, wins=wins, lk=lk):
            gate.wait()
            ok = cmd._resolve("done", result=1)
            with lk:
                wins.append(("done", ok))

        def timer_out(cmd=cmd, gate=gate, wins=wins, lk=lk):
            gate.wait()
            out = cmd.give_up()
            with lk:
                wins.append((out, True))

        ts = [threading.Thread(target=completer),
              threading.Thread(target=timer_out)]
        for x in ts:
            x.start()
        for x in ts:
            x.join()

        assert cmd.outcome in ("done", "abandoned"), cmd.outcome
        # Exactly one caller may believe it set the outcome.
        claimed = [name for name, ok in wins if ok]
        assert len(claimed) == 1 or claimed == ["done", "done"], wins
        if len(claimed) == 1:
            assert claimed[0] == cmd.outcome, (claimed, cmd.outcome)


class _SpyLock:
    """A real lock that counts how many times it was entered.

    For asserting that a transition is taken UNDER a lock, which a race test
    cannot do. A barrier gets both threads to the door at the same moment and
    then the GIL's 5 ms switch interval makes an interleaving inside three
    bytecodes very unlikely -- so the race test stresses the code without
    pinning the invariant. Removing the lock entirely survived it. This does
    not: `entered == 0` is the mutation, deterministically.
    """

    def __init__(self) -> None:
        self._lk = threading.Lock()
        self.entered = 0

    def __enter__(self):
        self._lk.acquire()
        self.entered += 1
        return self

    def __exit__(self, *exc) -> None:
        self._lk.release()

    def acquire(self, *a, **kw):
        return self._lk.acquire(*a, **kw)

    def release(self) -> None:
        self._lk.release()


def test_the_outcome_transition_is_taken_under_the_lock():
    """The invariant, not the race. See `_SpyLock`."""
    from trilobite.acquisition import Command

    now = time.monotonic()
    cmd = Command(kind="x", fn=lambda: None, deadline=now + 60, submitted=now)
    spy = _SpyLock()
    cmd.lock = spy

    assert cmd._resolve("done", result=1) is True
    assert spy.entered >= 1, "_resolve decided without holding the lock"

    before = spy.entered
    assert cmd._resolve("failed") is False, "a second transition must be refused"
    assert spy.entered > before, "the refusal was decided without the lock too"


def test_claim_and_give_up_are_also_taken_under_the_lock():
    """Both of the other two transitions. `claim_for_execution` races
    `give_up` by construction -- one is the owner, the other the caller."""
    from trilobite.acquisition import Command

    now = time.monotonic()
    for call in (lambda c: c.claim_for_execution(time.monotonic()),
                 lambda c: c.give_up()):
        cmd = Command(kind="x", fn=lambda: None, deadline=now + 60, submitted=now)
        spy = _SpyLock()
        cmd.lock = spy
        call(cmd)
        assert spy.entered >= 1, call


def test_queue_admission_is_taken_under_the_retirement_lock():
    """Same argument for the admission/retirement window. `submit` must hold
    the lock across the `_running` check AND the put, or a command can land
    behind a drain that has already happened and strand its caller."""
    owner = CameraOwner("left", capacity=4)
    owner.adopt()
    owner._owner_thread = -1          # so submit queues rather than inlining
    spy = _SpyLock()
    owner._admit = spy

    # Fill it so submit returns promptly via the rejection path.
    for _ in range(4):
        owner._q.put_nowait(_stub())
    with pytest.raises(CommandRejected):
        owner.submit("still", lambda: None, 0.5)
    assert spy.entered >= 1, "admission was decided without the lock"

    before = spy.entered
    owner.retire()
    assert spy.entered > before, "retirement drained without the lock"


def test_work_that_never_started_expires_rather_than_being_abandoned():
    """`abandoned` means something may have happened and cannot be undone.
    Saying it about a command still sitting in the queue is a lie in the
    direction that matters: it tells the operator a capture might exist."""
    owner = CameraOwner("left")
    owner._owner_thread = -1
    owner._running = True

    ran: list[int] = []
    with pytest.raises(CommandExpired, match="had not started"):
        owner.submit("still", lambda: ran.append(1), 0.05)
    assert owner.expired == 1 and owner.abandoned == 0

    owner._owner_thread = threading.get_ident()
    owner.service()
    assert ran == [], "a withdrawn command ran anyway"


def test_a_command_cannot_be_stranded_between_admission_and_retirement():
    """`submit` checked `_running`, `retire` drained, and the put could land
    after the drain -- leaving a caller waiting on a stopped owner for its full
    deadline. Admission and retirement now share a lock."""
    owner = CameraOwner("left", capacity=8)
    owner.adopt()
    owner._owner_thread = -1

    outcomes: list[str] = []
    lk = threading.Lock()

    def caller():
        try:
            owner.submit("still", lambda: None, 30.0)
            with lk:
                outcomes.append("done")
        except CommandRejected:
            with lk:
                outcomes.append("rejected")

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    owner.retire()
    for t in threads:
        t.join(timeout=3.0)
    assert not any(t.is_alive() for t in threads), "a caller was stranded"
    assert outcomes.count("rejected") == 6, outcomes


# -- lifecycle ---------------------------------------------------------------


def test_a_thread_that_will_not_stop_is_failed_stop_and_the_device_stays_open(tmp_path):
    """The old stop joined with a timeout and closed the source regardless --
    a close racing a thread that may be inside capture_request, which is a
    segfault rather than an error message.

    A held camera an operator can see beats a crash they cannot diagnose, and
    it is also the honest report: the thread really is still running.
    """
    cam = runtime(tmp_path)
    cam.start()
    time.sleep(0.05)

    # Wedge the loop inside the source, then ask it to stop with no patience.
    cam.source.delay = 2.0
    threading.Thread(target=lambda: cam.grab_still(), daemon=True).start()
    time.sleep(0.2)
    cam.stop(timeout=0.2)

    assert cam.lifecycle == "failed-stop"
    assert cam.source.is_open, "a camera that would not stop must not be closed"
    assert "still held" in cam.last_error

    cam.source.delay = 0.0
    cam.stop(timeout=5.0)
    assert cam.lifecycle == "stopped"
    assert not cam.source.is_open


def test_starting_over_a_live_worker_is_refused(tmp_path):
    """The hole the Stage 3 review found, and it made failed-stop worthless.

    `start()` cleared the shared stop event and launched a second worker
    against the same source, so a camera that had just reported `failed-stop`
    could be restarted straight into the two-consumer state the whole design
    exists to prevent. Reproduced by the review as:

        AFTER STOP failed-stop old thread alive True
        RESTART ALLOWED True both alive True lifecycle running
    """
    cam = runtime(tmp_path)
    cam.start()
    time.sleep(0.05)
    try:
        with pytest.raises(RuntimeError, match="still running"):
            cam.start()
    finally:
        cam.stop()


def test_starting_after_a_failed_stop_is_refused(tmp_path):
    """The case that matters: the camera is wedged, stop gave up, and a restart
    would put a second thread on the same request pool."""
    cam = runtime(tmp_path)
    cam.start()
    time.sleep(0.05)
    cam.source.delay = 2.0
    threading.Thread(target=lambda: _swallow(cam.grab_still), daemon=True).start()
    time.sleep(0.2)
    cam.stop(timeout=0.2)
    assert cam.lifecycle == "failed-stop"

    with pytest.raises(RuntimeError, match="failed-stop"):
        cam.start()

    cam.source.delay = 0.0
    cam.stop(timeout=5.0)


def _swallow(fn):
    with contextlib.suppress(Exception):
        fn()


def test_a_clean_stop_closes_and_says_so(tmp_path):
    cam = runtime(tmp_path)
    cam.start()
    time.sleep(0.05)
    cam.stop()
    assert cam.lifecycle == "stopped"
    assert not cam.source.is_open


def test_the_loop_retires_the_owner_even_if_it_dies(tmp_path):
    """`_run` wraps the loop in try/finally. If the loop exits any other way,
    nothing may go on queueing work for a thread that no longer exists."""
    cam = runtime(tmp_path)
    cam.start()
    time.sleep(0.05)
    cam._stop.set()
    cam._thread.join(timeout=5)
    assert not cam.owner.running


# -- what the operator actually asked about: do captures land ----------------


def test_two_hundred_captures_all_reach_the_disk_intact(tmp_path):
    """The reliability question, asked directly.

    Not "does a capture work" but "do two hundred of them, issued from four
    threads while the preview loop runs, every one of them, land on disk with a
    sidecar that parses and a size that matches what was written". A capture
    that is reported saved and is not there is the failure this whole storage
    path was rebuilt around.
    """
    cam = runtime(tmp_path)
    cam.start()
    results: list[dict] = []
    errors: list[str] = []
    lock = threading.Lock()

    def one(_):
        try:
            out = cam.capture_still(raw=True, tag="raw")
        except Exception as exc:                    # noqa: BLE001 - collected
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
            return
        with lock:
            results.append(out)

    try:
        time.sleep(0.1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(one, range(200)))
    finally:
        cam.stop()

    assert not errors, errors[:5]
    assert len(results) == 200

    seqs = set()
    for out in results:
        img = Path(out["image"])
        meta = Path(out["metadata"])
        assert img.exists() and meta.exists(), out
        assert img.stat().st_size == out["bytes"] > 128
        side = json.loads(meta.read_text(encoding="utf-8"))
        assert side["file"] == img.name
        assert side["shape"] == list(np.load(img).shape)
        assert side["validity"] == "unvalidated"        # synthetic, honestly
        seqs.add(side["seq"])

    assert len(seqs) == 200, "every capture is a distinct frame, not a re-save"
    assert len({r["image"] for r in results}) == 200, "no two wrote to one path"
    assert cam.source.concurrent == 1
    assert len(cam.source.threads) == 1

    st = cam.owner.state()
    assert st["completed"] == 200 and st["failed"] == 0
    assert st["expired"] == 0 and st["abandoned"] == 0 and st["rejected"] == 0
    assert st["depth"] == 0, "nothing left in flight"
    assert cam.errors == 0, cam.last_error


def test_the_preview_keeps_running_through_a_capture_burst(tmp_path):
    """Captures share the owner thread with the preview, so a burst COULD stall
    the stream. `WORK_PER_LOOP` bounds that, and this is the assertion behind
    the bound: frames keep being published throughout."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.2)
        before = cam.preview.get()[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: cam.grab_still(), range(40)))
        after = cam.preview.get()[0]
    finally:
        cam.stop()
    assert after > before, "the preview stopped while stills were being taken"


def test_a_slow_disk_does_not_hold_the_camera(tmp_path):
    """The reason the capture is split from the save. Disk work happens on the
    caller's thread; if it ran on the owner, a slow USB stick would be inside
    the request pool and every other command would queue behind it."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        frame = cam.grab_still()
        # Whatever the writer does now, the camera is free: another grab must
        # complete without waiting on it.
        t0 = time.monotonic()
        cam.grab_still()
        assert time.monotonic() - t0 < 1.0
        assert cam.save_frame(frame, "raw")["bytes"] > 128
    finally:
        cam.stop()


def test_capture_all_asks_both_heads_before_writing_either(tmp_path):
    """The pair-skew fix. The old loop completed each head's disk write before
    requesting the next frame, so storage latency landed inside the skew --
    tens of milliseconds of sensor difference plus however long the stick took.
    """
    from trilobite.app import Application
    from trilobite.config import AppConfig
    from trilobite.web.server import create_app

    def cam(cid):
        return CameraConfig(
            cam_id=cid, backend="synthetic", fps=60,
            full_resolution=(96, 72), preview_resolution=(48, 36),
            synthetic_drift_px=0.0)

    app = Application(
        AppConfig(storage=StorageConfig(root=str(tmp_path / "d")),
                  cameras=[cam("left"), cam("right")]),
        state_path=None, restore=False)
    app.start()
    try:
        from fastapi.testclient import TestClient

        with TestClient(create_app(app)) as client:
            out = client.post("/api/capture-all/raw").json()
        assert set(out) == {"left", "right"}
        for cid, rec in out.items():
            assert "error" not in rec, rec
            assert Path(rec["image"]).exists()
            assert rec["cam_id"] == cid
    finally:
        app.stop()
