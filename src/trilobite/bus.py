"""Decoupling between producers and consumers.

The capture thread must never be blocked by a consumer. A browser on a slow
link, a disk flush, an expensive processing stage -- none of these are allowed
to change the timing of the sensor read, because in optics work the capture
cadence is part of the measurement.

One primitive covers every consumer so far:

  LatestFrame  - single slot, newest wins. For anything where a stale frame is
                 worthless: the live preview, the telemetry readout. A reader
                 that falls behind silently skips ahead.

Recording will need the other shape: a bounded FIFO with an explicit drop
counter, so that an overflow gives you a number you can report rather than a
mystery. It is deliberately not here yet. An untested queue that nothing calls
is worse than no queue, because it reads as a decision already made.
"""

from __future__ import annotations

import threading

from .types import Frame


class LatestFrame:
    """One slot. Writers overwrite, readers wait for something newer."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._version = 0

    def publish(self, frame: Frame) -> None:
        with self._cond:
            self._frame = frame
            self._version += 1
            self._cond.notify_all()

    def get(self) -> tuple[int, Frame | None]:
        with self._cond:
            return self._version, self._frame

    def wait_newer(self, since: int, timeout: float = 5.0) -> tuple[int, Frame | None]:
        """Block until version > since, or timeout. Returns (version, frame).

        On timeout the caller gets whatever is currently there, which lets a
        stream emit a keepalive rather than dying when a camera goes quiet.
        """
        with self._cond:
            if self._version <= since:
                self._cond.wait_for(lambda: self._version > since, timeout=timeout)
            return self._version, self._frame
