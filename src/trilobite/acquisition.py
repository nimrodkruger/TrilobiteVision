"""One thread owns the camera. Everything else asks it for things.

Review finding **F1**, gate **G2**, and the smallest thing that closes them.

## The problem, precisely

`CameraRuntime.capture_still` called `source.capture_full()` on whichever
thread the web framework dispatched the request on, while the capture loop was
calling `read_preview()` on its own thread. Two mutexes serialised the calls,
which is why the rig usually worked.

**Serialisation is not ownership.** A mutex makes two callers take turns; it
does not make one of them responsible. It does not say who releases a request
when the thread holding it raises, which thread may call `close()` while
another is inside `capture_request()`, or what a caller's timeout means when
the SDK call it is waiting on cannot be interrupted.

An earlier design had a second thread pulling full frames for corner detection.
Two threads on a four-deep request pool, one at 30 Hz and one at 1 Hz, took the
Pi down repeatedly -- while a four-core CPU stress test did not, which is how
the camera path rather than the load was identified as the cause.

## What this is, and what it deliberately is not

A bounded queue serviced by the capture thread. A caller submits work, blocks
on a deadline, and gets a result or a refusal. The capture thread is the only
thread that ever calls into the source.

`docs/stage-3-contract.md` specifies seven command outcomes, control
coalescing, per-kind fairness and generation isolation across restarts. This
implements **five outcomes and no coalescing**, because the other machinery
guards against failure modes this rig has not exhibited and each one is another
thing to get wrong. What is here is what the finding actually requires:

  * one owner, provable by thread id;
  * a bound on the queue, so a burst is refused rather than swallowed;
  * a deadline, so an API call cannot hang on a stalled sensor;
  * release exactly once, on every path;
  * a stop that does not close the device out from under a running thread.

The rest is written down in the contract and stays unbuilt until something
needs it. Adding it later is a day; debugging six interacting states that
nobody exercises is not.

## Outcomes

    done        executed, result attached
    failed      executed, raised; the exception is attached
    rejected    never queued -- full, or the owner is not running
    expired     queued, deadline passed before execution started. NEVER runs
    abandoned   execution started, the caller's wait expired

`abandoned` exists because the alternative is a lie. A caller timing out does
not interrupt a blocking SDK call, and reporting `failed` would assert that
nothing happened when the capture may well have been taken.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Provisional, per docs/stage-3-contract.md section 4.1, and recorded in status
# so a bench run can say whether they were ever approached.
QUEUE_CAPACITY = 8          # commands in flight per head
WORK_PER_LOOP = 4           # commands serviced between two preview frames
STILL_DEADLINE_S = 3.0      # a still that has not run by now is not coming
CONTROL_DEADLINE_S = 1.0

DONE = "done"
FAILED = "failed"
REJECTED = "rejected"
EXPIRED = "expired"
ABANDONED = "abandoned"


class CommandRejected(RuntimeError):
    """The owner would not take the work. Not a failure of the work itself.

    Its own type because the caller's response differs: a rejection means
    nothing was attempted and retrying later is reasonable, where a failure
    means the camera tried and could not.
    """


class CommandExpired(TimeoutError):
    """The deadline passed. Whether anything happened is stated on the command."""


@dataclass
class Command:
    """One piece of work for the owner thread, and its single outcome."""

    kind: str
    fn: Callable[[], Any]
    deadline: float                     # absolute, time.monotonic()
    submitted: float
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    outcome: str = "queued"
    # Set when the caller stopped waiting. The owner still runs the command if
    # it is inside its deadline -- see the note on `abandoned` above -- but it
    # will not be waited on, so the result is dropped and counted.
    forsaken: bool = False

    @property
    def age(self) -> float:
        return time.monotonic() - self.submitted

    def _resolve(self, outcome: str, result: Any = None,
                 error: BaseException | None = None) -> bool:
        """Set the outcome once. Returns False if something got there first.

        The compare-and-set that makes a completion/timeout race resolve
        exactly once. Without it a command can be reported twice, in two
        different ways, to two different callers.
        """
        if self.outcome != "queued":
            return False
        self.outcome, self.result, self.error = outcome, result, error
        self.done.set()
        return True


class CameraOwner:
    """The single thread permitted to touch one camera source.

    Not a thread of its own: it wraps the capture loop that already exists, so
    there is no new thread to reason about and no question of which of two
    loops is authoritative. `service()` is called from inside that loop.
    """

    def __init__(self, cam_id: str, capacity: int = QUEUE_CAPACITY) -> None:
        self.cam_id = cam_id
        self._q: queue.Queue[Command] = queue.Queue(maxsize=capacity)
        self.capacity = capacity
        self._running = False
        self._owner_thread: int | None = None
        # Counters. Cheap, and they are how a bench run answers "did the queue
        # ever come near its bound" without guessing from latency.
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.rejected = 0
        self.expired = 0
        self.abandoned = 0
        self.max_depth = 0
        self.max_age_s = 0.0

    # -- lifecycle -------------------------------------------------------

    def adopt(self) -> None:
        """Called by the capture thread as it starts. Records who owns this."""
        self._owner_thread = threading.get_ident()
        self._running = True

    def retire(self) -> None:
        """Stop accepting work and refuse everything still queued.

        A queued command whose camera is going away must be told so, not left
        to time out: the caller is holding an HTTP request open on it.
        """
        self._running = False
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                return
            if cmd._resolve(REJECTED, error=CommandRejected(
                    f"{self.cam_id}: camera stopped before this ran")):
                self.rejected += 1

    @property
    def running(self) -> bool:
        return self._running

    @property
    def owns_current_thread(self) -> bool:
        return self._owner_thread == threading.get_ident()

    def assert_owner(self, what: str) -> None:
        """Fail loudly if something reaches the camera off the owner thread.

        The whole point of the stage, as one runtime check. It is cheap and it
        turns the class of bug that took the rig down into a traceback naming
        the caller, which is the difference between an afternoon and a minute.
        """
        if self._owner_thread is None or self.owns_current_thread:
            return
        raise RuntimeError(
            f"{self.cam_id}: {what} was called from thread "
            f"{threading.current_thread().name!r}, which does not own this "
            f"camera. Every SDK call goes through CameraOwner.submit(); two "
            f"threads on a four-deep request pool is what took the rig down.")

    # -- submitting ------------------------------------------------------

    def submit(self, kind: str, fn: Callable[[], Any], timeout: float) -> Command:
        """Queue work for the owner thread and wait for its outcome.

        Raises `CommandRejected` if the queue is full or the camera is not
        running, and `CommandExpired` if the deadline passes. Returns the
        `Command` on success so the caller can see how long it waited.
        """
        now = time.monotonic()
        cmd = Command(kind=kind, fn=fn, deadline=now + timeout, submitted=now)

        if self.owns_current_thread:
            # The owner asking itself. Queueing would put the command behind a
            # loop that is currently inside this call and never gets back to
            # service it -- a deadlock, and one that would only show up the
            # first time somebody submitted from a stage or from another
            # command. Run it inline instead: the invariant this class exists
            # to hold is "one thread touches the camera", and that is satisfied.
            try:
                cmd._resolve(DONE, result=fn())
                self.completed += 1
            except Exception as exc:                 # noqa: BLE001 - reported
                cmd._resolve(FAILED, error=exc)
                self.failed += 1
                raise
            self.submitted += 1
            return cmd

        if not self._running:
            self.rejected += 1
            raise CommandRejected(
                f"{self.cam_id}: camera is not running, so there is nothing to "
                f"ask. Start it before requesting a {kind}.")
        try:
            self._q.put_nowait(cmd)
        except queue.Full:
            self.rejected += 1
            raise CommandRejected(
                f"{self.cam_id}: {self.capacity} commands are already queued "
                f"for this camera and the oldest has been waiting "
                f"{self._oldest_age():.2f} s. Refusing rather than adding to a "
                f"backlog that is already the problem.") from None

        self.submitted += 1
        self.max_depth = max(self.max_depth, self._q.qsize())

        if not cmd.done.wait(timeout):
            # The command may be executing right now and cannot be interrupted.
            # Say so rather than claiming it failed.
            cmd.forsaken = True
            if cmd._resolve(ABANDONED):
                self.abandoned += 1
                raise CommandExpired(
                    f"{self.cam_id}: {kind} did not complete within "
                    f"{timeout:.1f} s. It may still be executing -- nothing "
                    f"here can interrupt an SDK call -- so do not assume "
                    f"nothing happened.")
            # It resolved between the wait expiring and the compare-and-set.
            # That is a success, not a timeout.

        if cmd.outcome == FAILED:
            raise cmd.error  # type: ignore[misc]
        if cmd.outcome == REJECTED:
            raise cmd.error or CommandRejected(f"{self.cam_id}: {kind} rejected")
        if cmd.outcome == EXPIRED:
            raise CommandExpired(
                f"{self.cam_id}: {kind} sat in the queue past its "
                f"{timeout:.1f} s deadline and was dropped without running.")
        return cmd

    def _oldest_age(self) -> float:
        try:
            return self._q.queue[0].age          # deque peek; no pop
        except (IndexError, AttributeError):
            return 0.0

    # -- servicing, on the owner thread ----------------------------------

    def service(self, budget: int = WORK_PER_LOOP) -> int:
        """Run up to `budget` queued commands. Returns how many ran.

        Bounded so a backlog cannot stall the preview indefinitely: the sensor
        still has to be drained at its own rate whatever else is pending.
        """
        ran = 0
        for _ in range(budget):
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                break

            self.max_age_s = max(self.max_age_s, cmd.age)

            # Checked HERE, not at submission: a command that has been sitting
            # in the queue past its deadline must never execute, or a still
            # requested ten seconds ago arrives now and is saved as current.
            if time.monotonic() > cmd.deadline:
                if cmd._resolve(EXPIRED):
                    self.expired += 1
                continue

            try:
                value = cmd.fn()
            except Exception as exc:                 # noqa: BLE001 - reported
                if cmd._resolve(FAILED, error=exc):
                    self.failed += 1
                    log.warning("%s: %s command failed: %s",
                                self.cam_id, cmd.kind, exc)
            else:
                if cmd._resolve(DONE, result=value):
                    self.completed += 1
                elif cmd.forsaken:
                    # Ran to completion after the caller gave up. Worth a line:
                    # it means the deadline is too tight for this workload.
                    log.info("%s: %s completed after its caller stopped "
                             "waiting (%.2f s)", self.cam_id, cmd.kind, cmd.age)
            ran += 1
        return ran

    # -- reporting -------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "depth": self._q.qsize(),
            "capacity": self.capacity,
            "oldest_age_s": round(self._oldest_age(), 3),
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "rejected": self.rejected,
            "expired": self.expired,
            "abandoned": self.abandoned,
            "max_depth": self.max_depth,
            "max_age_s": round(self.max_age_s, 3),
        }
