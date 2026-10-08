"""A block of RAM that is actually there.

The whole module exists for one sentence from the Stage 5 plan, and it is worth
stating precisely because it is counter-intuitive:

    `np.empty` on Linux does not commit pages. The kernel returns a mapping and
    faults physical pages in on first touch, so **a successful allocation
    proves nothing.** Asking for 3 GB on a board with 1 GB free succeeds
    instantly and fails at frame 400, as an OOM kill, mid-recording.

That failure mode is unacceptable for a recorder whose entire value is that it
knows its own limits before it starts. So arming prefaults: every page is
written to, which forces the kernel to find real memory for it now, at arm
time, where the cost is a few seconds of waiting and the failure is a message.

Prefaulting by writing one byte per page rather than zeroing the whole array is
deliberate and about an order of magnitude faster: a page fault is per page, so
touching one byte in each commits all of it. Writing zeros is what commits it,
not reading -- Linux serves reads of untouched anonymous pages from the shared
zero page and allocates nothing.

And because "I wrote to every page" is still an assumption about what the
kernel did, the result is **checked against RSS**: resident set size before and
after must have risen by approximately the buffer size. If it has not, the
prefault did not do what it claims and arming fails rather than reporting a
capacity that does not exist.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

PAGE = 4096

# Prefaulting is considered to have worked if RSS rose by at least this
# fraction of the buffer. Not 1.0: RSS is sampled from /proc and the process is
# doing other things, pages can be reclaimed under pressure between the write
# and the read, and on a host with transparent huge pages the accounting moves
# in 2 MiB steps. A buffer that produced less than this much resident memory
# did not get committed, whatever the allocation returned.
PREFAULT_MIN_FRACTION = 0.8

# Below this the RSS check is not evidence of anything. Two samples of
# /proc/self/statm taken milliseconds apart differ by whatever else the process
# did in between -- an allocation in another thread, a log line, a page of
# Python bytecode -- and that noise is the same order as a few-megabyte buffer.
# So the check applies only where the signal is unambiguous. This is not a
# relaxation of the guarantee: a buffer this small cannot be the thing that
# gets the process OOM-killed, which is the failure the check exists for.
PREFAULT_CHECK_MIN_BYTES = 64 << 20


def rss_bytes() -> int | None:
    """Resident set size, or None off Linux. Evidence, not an estimate."""
    try:
        fields = Path("/proc/self/statm").read_text().split()
        return int(fields[1]) * PAGE          # field 2 is resident pages
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True)
class FrameMeta:
    """What is known about one stored frame, beside its pixels.

    Deliberately small and flat: one of these exists per frame, a five-minute
    recording has eighteen thousand of them, and they are serialised into the
    index as-is.

    `seq` is the acquisition sequence number **as exposed**, not as stored. That
    is the single most important field in this class: a gap in the stored
    sequence numbers is the record of a dropped frame, and renumbering on the
    way in would erase exactly the information the whole drop-accounting design
    exists to preserve.
    """

    seq: int
    t_mono: float
    t_wall: float
    sensor_timestamp: int | None     # libcamera's SensorTimestamp, ns
    validity: str
    reservations: tuple[str, ...] = ()
    observed_max: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": self.seq,
            "t_mono": round(self.t_mono, 6),
            "t_wall": round(self.t_wall, 6),
            "sensor_timestamp": self.sensor_timestamp,
            "validity": self.validity,
        }
        if self.reservations:
            out["reservations"] = list(self.reservations)
        if self.observed_max is not None:
            out["observed_max"] = self.observed_max
        return out


class PrefaultFailed(RuntimeError):
    """The buffer was allocated and the memory is not there.

    Its own type because the response differs from an allocation failure: a
    MemoryError means ask for less, where this means the kernel handed back a
    mapping it cannot back, and arming must refuse rather than report a
    capacity the recording will not get.
    """


class FrameBuffer:
    """A fixed number of fixed-shape frames in committed RAM.

    One buffer per head. Not a ring: a full buffer **stops** the recording and
    says how long it got, because silently overwriting the start of a burst
    produces a recording whose contents depend on when it was stopped.
    """

    def __init__(self, frames: int, shape: tuple[int, int],
                 dtype: Any = np.uint16) -> None:
        if frames < 1:
            raise ValueError(f"a buffer of {frames} frames holds nothing")
        self.frames = int(frames)
        self.shape = (int(shape[0]), int(shape[1]))
        self.dtype = np.dtype(dtype)
        self.frame_bytes = int(np.prod(self.shape)) * self.dtype.itemsize
        self.nbytes = self.frame_bytes * self.frames
        self.data = np.empty((self.frames, *self.shape), dtype=self.dtype)
        self.meta: list[FrameMeta] = []
        self.prefaulted = False
        self.prefault_ms = 0.0
        self.rss_delta = None
        self._lock = threading.Lock()

    # -- arming ----------------------------------------------------------

    def prefault(self) -> dict[str, Any]:
        """Force every page resident. Raises `PrefaultFailed` if it did not.

        One byte per page, then RSS is checked. See the module docstring for
        why both halves are needed.
        """
        before = rss_bytes()
        t0 = time.perf_counter()
        flat = self.data.reshape(-1).view(np.uint8)
        flat[::PAGE] = 0
        # The final partial page is not reached by the stride above when the
        # buffer is not a whole multiple of PAGE.
        flat[-1] = 0
        self.prefault_ms = (time.perf_counter() - t0) * 1000.0
        after = rss_bytes()

        if before is not None and after is not None:
            self.rss_delta = after - before
            want = self.nbytes * PREFAULT_MIN_FRACTION
            if self.nbytes >= PREFAULT_CHECK_MIN_BYTES and self.rss_delta < want:
                raise PrefaultFailed(
                    f"wrote to every page of a {self.nbytes / 1e9:.2f} GB "
                    f"buffer and resident memory rose by only "
                    f"{self.rss_delta / 1e9:.2f} GB. The allocation succeeded "
                    f"and the memory is not there, which is the failure that "
                    f"arrives as an OOM kill part-way through a recording. "
                    f"Arm a shorter buffer.")
        self.prefaulted = True
        log.info("prefaulted %.2f GB in %.0f ms (RSS +%s)",
                 self.nbytes / 1e9, self.prefault_ms,
                 "unknown" if self.rss_delta is None
                 else f"{self.rss_delta / 1e9:.2f} GB")
        return self.report()

    def report(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "shape": list(self.shape),
            "dtype": str(self.dtype),
            "frame_bytes": self.frame_bytes,
            "bytes": self.nbytes,
            "gb": round(self.nbytes / 1e9, 2),
            "prefaulted": self.prefaulted,
            "prefault_ms": round(self.prefault_ms, 1),
            "rss_delta_bytes": self.rss_delta,
            "stored": self.count,
        }

    # -- filling ---------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self.meta)

    @property
    def full(self) -> bool:
        return len(self.meta) >= self.frames

    def store(self, pixels: np.ndarray, meta: FrameMeta) -> bool:
        """Copy one frame in. False when the buffer is full and nothing was stored.

        The copy is the point: the caller's array is a view into a libcamera
        request that has to be released immediately, so the data must be out of
        it before this returns. `np.copyto` into the preallocated slot rather
        than appending to a list, so no allocation happens per frame and the
        whole recording occupies exactly the memory that was proved available
        at arm time.
        """
        if pixels.shape != self.shape:
            raise ValueError(
                f"frame is {pixels.shape}, buffer holds {self.shape}. A "
                f"recording whose frames are not all the same shape breaks "
                f"every reader downstream, so this is refused rather than "
                f"reshaped.")
        with self._lock:
            i = len(self.meta)
            if i >= self.frames:
                return False
            np.copyto(self.data[i], pixels, casting="unsafe")
            self.meta.append(meta)
            return True

    def reset(self) -> None:
        """Forget the contents. Keeps the committed pages, which is the point:
        re-arming after a discard costs nothing."""
        with self._lock:
            self.meta.clear()

    def view(self) -> np.ndarray:
        """The stored frames, and only those. A view, never a copy."""
        return self.data[: self.count]
