"""Decoupling between producers and consumers.

The capture thread must never be blocked by a consumer. A browser on a slow
link, a disk flush, an expensive processing stage -- none of these are allowed
to change the timing of the sensor read, because in optics work the capture
cadence is part of the measurement.

Two primitives cover every consumer so far:

  LatestFrame  - single slot, newest wins. For anything where a stale frame is
                 worthless: the live preview, the telemetry readout. A reader
                 that falls behind silently skips ahead.

  FrameQueue   - bounded FIFO with an explicit drop counter. For anything where
                 every frame matters: recording, burst capture. When it
                 overflows you get a number you can report, not a mystery.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

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


@dataclass
class QueueStats:
    accepted: int = 0
    dropped: int = 0

    @property
    def drop_rate(self) -> float:
        total = self.accepted + self.dropped
        return self.dropped / total if total else 0.0


class FrameQueue:
    """Bounded queue that counts what it had to throw away."""

    def __init__(self, maxsize: int = 64) -> None:
        self._q: queue.Queue[Frame] = queue.Queue(maxsize=maxsize)
        self.stats = QueueStats()

    def offer(self, frame: Frame) -> bool:
        """Non-blocking put. Returns False and counts a drop if full."""
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            self.stats.dropped += 1
            return False
        self.stats.accepted += 1
        return True

    def take(self, timeout: float = 1.0) -> Frame | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def __len__(self) -> int:
        return self._q.qsize()
