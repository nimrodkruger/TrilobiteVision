"""On-device storage.

Two rules encoded here, both learned the expensive way:

1. **Never save pixels without their metadata.** Every image gets a JSON
   sidecar carrying the sensor settings, the full pipeline parameter set, the
   camera description and the timestamps. An uncalibrated image with no record
   of how it was taken is not data.

2. **Default to lossless and unprocessed.** .npy holds the native dtype with
   no compression artefacts and loads in one line on the desktop. PNG and TIFF
   are offered for interchange. JPEG is deliberately not an option for the
   science path.

Sessions group captures. A session directory is created once per run and
everything from that run lands in it, so a day's work is one folder you can
copy off the Pi in a single scp.

3. **The output device is movable while the rig runs.** The SD card is the
   wrong place for a session and the right USB SSD is often not plugged in when
   the application starts. So the writer's root is not fixed at construction:
   `retarget()` moves it, `release()` puts it back, and `check_and_recover()`
   notices when the device it is writing to has been pulled and falls back
   rather than letting every subsequent capture raise. See storage/devices.py.

4. **A capture is not saved until it is on the device, and that is checked.**
   `close()` does not write anything to a disk. It returns as soon as the bytes
   are in the kernel's page cache, and writeback flushes them at its leisure --
   thirty seconds later by default, or never if the power goes or the disk is
   pulled. File *metadata* takes a different route: on a journalling filesystem
   the directory entry is durable long before the data is. The two together
   produce a failure that looks like nothing else: a session directory full of
   correctly named, correctly placed, **zero-byte** files.

   That is not hypothetical. It cost a field session: every capture reported
   "saved", `session.json` was intact -- written at startup, so writeback had
   had minutes to flush it -- and every .npy and .json from the run itself was
   empty. So each file is now fsync'd, its directory is fsync'd so the name is
   durable too, and the size on disk is read back and checked against what was
   written before the capture is reported as saved. A few milliseconds per
   frame, against losing an afternoon.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import StorageConfig
from ..types import SCIENCE, Frame
from . import devices, identity

log = logging.getLogger(__name__)


class EmptyWriteError(OSError):
    """A file was created but its bytes did not reach the device.

    Its own type because the caller's response differs from an ordinary write
    failure: an OSError from `write()` means nothing was written and the frame
    can be retried elsewhere, whereas this means the filesystem accepted every
    byte and then produced a file of the wrong size, which indicts the device
    or the mount rather than the code.
    """


def fsync_file(fh) -> None:
    """Flush one open file all the way to the device."""
    fh.flush()
    os.fsync(fh.fileno())


def fsync_dir(path: Path) -> bool:
    """Make a directory entry durable, so the *name* survives a power cut too.

    Fsyncing a file guarantees its contents; it says nothing about the entry
    that points at it. Both are needed, and the directory one is not available
    on Windows -- opening a directory raises there -- so a failure is logged at
    debug and ignored rather than failing a capture on the dev machine.

    **Returns whether it actually happened**, which is the part that used to be
    swallowed entirely. A caller reporting a capture as durable needs to know
    which kind of durable it got, and a platform difference belongs in the
    record as a measured capability rather than as an assumption baked into a
    test.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:                       # Windows, or an odd FUSE mount
        log.debug("cannot open %s to fsync: %s", path, exc)
        return False
    try:
        os.fsync(fd)
        return True
    except OSError as exc:                       # some FUSE backends refuse
        log.debug("cannot fsync directory %s: %s", path, exc)
        return False
    finally:
        os.close(fd)


# Durability answers, keyed by directory. It is a property of the mount, and
# this is asked once per capture. Cleared on retarget: a new target is a new
# filesystem and may answer differently.
_DURABILITY_CACHE: dict[str, str] = {}

STRICT = "strict"            # contents AND the directory entry are fsync'd
FILE_ONLY = "file-only"      # contents only; the name may not survive a cut


def durability_of(path: Path) -> str:
    """What this filesystem can actually promise, as a word for the record.

    `strict` means a power cut after a reported save leaves the file, named,
    with its bytes. `file-only` means the contents are down but the directory
    entry may not be, so the file can come back nameless.

    **Measured, not inferred from `sys.platform`.** A Linux host on an exotic
    mount can be `file-only` too, and that is precisely the case where guessing
    from the platform gives the wrong answer with confidence -- which is the
    failure mode this whole module exists to avoid.
    """
    key = str(path)
    cached = _DURABILITY_CACHE.get(key)
    if cached is None:
        cached = STRICT if fsync_dir(path) else FILE_ONLY
        _DURABILITY_CACHE[key] = cached
    return cached


def write_durably(path: Path, payload: bytes) -> int:
    """Write bytes and do not return until they are on the device.

    Returns the size the filesystem reports afterwards, which is the number the
    caller must check -- `write()` returning the full length only means the
    page cache accepted it.
    """
    with open(path, "wb") as fh:
        fh.write(payload)
        fsync_file(fh)
    fsync_dir(path.parent)
    return path.stat().st_size


def verify_size(path: Path, expected: int | None = None) -> int:
    """Read the size back off the filesystem and insist it is plausible.

    `expected is None` means the writer was a third-party encoder (cv2, PIL)
    whose output length is not known in advance, so the only check available is
    that the file is not empty. That still catches the failure this exists for.
    """
    try:
        got = path.stat().st_size
    except OSError as exc:
        raise EmptyWriteError(f"{path} vanished immediately after writing: {exc}") from None

    if got == 0:
        raise EmptyWriteError(
            f"{path} is 0 bytes after writing. The filesystem accepted the data "
            f"and did not store it -- typically a device that was pulled or lost "
            f"power before writeback, or a mount that is silently discarding "
            f"writes. Nothing was saved."
        )
    if expected is not None and got != expected:
        raise EmptyWriteError(
            f"{path} is {got} bytes on disk but {expected} were written. "
            f"The device is full, failing, or lying about its writes."
        )
    return got


def measure_sustained(
    directory: Path,
    budget_bytes: int = 8 << 30,
    budget_seconds: float = 30.0,
    block_bytes: int = 8 << 20,
) -> dict[str, Any]:
    """Sustained write throughput, reported as the **last quarter's** rate.

    Stage 5a, and the reason it exists is a specific property of consumer SSDs
    rather than a general preference for longer benchmarks.

    A modern consumer drive absorbs the first few gigabytes into an SLC cache
    at its headline rate and then falls to the native rate of its flash --
    which on a DRAM-less QLC drive can be a factor of four or five lower. A
    4 MiB probe measures the cache. A five-minute pair recording is 11-57 GB
    and spends almost all of itself past the cache, so a configuration chosen
    from the probe figure is chosen from a number that describes the first
    twenty seconds of a five-minute job.

    So this writes until it has moved `budget_bytes` or spent
    `budget_seconds`, whichever comes first, and reports both the mean and the
    rate over the final quarter of the run. **The last quarter is the figure
    that predicts a recording**; the mean is reported beside it because the
    ratio between them is itself the diagnostic -- a drive where they agree has
    no cache cliff to fall off, and one where the mean is three times the tail
    will stall part-way through a recording that the mean said would fit.

    The data is `os.urandom` once and reused per block. Writing the same block
    repeatedly is fine here and deliberate: these are block devices through a
    filesystem, not a deduplicating array, and generating 8 GB of randomness
    would measure the CPU.

    fsync happens once per block, not once at the end. At the end it would
    return after the page cache had absorbed several gigabytes and report a
    number that is memory bandwidth. Per block is also what the recorder
    itself does, so this measures the path the recording will take.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".trilobite-sustained-{os.getpid()}"
    block = os.urandom(block_bytes)

    result: dict[str, Any] = {
        "path": str(probe), "ok": False,
        "budget_bytes": budget_bytes, "budget_seconds": budget_seconds,
        "block_bytes": block_bytes,
    }
    # (elapsed_at_completion, cumulative_bytes) per block, for the tail split.
    marks: list[tuple[float, int]] = []
    written = 0
    try:
        start = time.perf_counter()
        with open(probe, "wb") as fh:
            while written < budget_bytes:
                fh.write(block)
                fsync_file(fh)
                written += block_bytes
                elapsed = time.perf_counter() - start
                marks.append((elapsed, written))
                if elapsed >= budget_seconds:
                    break
        fsync_dir(directory)
        total_s = marks[-1][0] if marks else 0.0
        verify_size(probe, written)

        mean = written / 1e6 / max(total_s, 1e-6)
        # The last quarter BY BYTES, not by blocks: if the drive slowed down,
        # the final quarter of the data took much longer than a quarter of the
        # blocks, and splitting by block count would dilute the very effect
        # this is measuring.
        cut = written * 0.75
        tail = [m for m in marks if m[1] > cut]
        if len(tail) >= 2:
            t0, b0 = tail[0]
            t1, b1 = tail[-1]
            sustained = (b1 - b0) / 1e6 / max(t1 - t0, 1e-6)
        else:
            sustained = mean
        result.update(
            ok=True, bytes=written, seconds=round(total_s, 2),
            mean_mb_s=round(mean, 1), sustained_mb_s=round(sustained, 1),
            blocks=len(marks),
            cliff_ratio=round(mean / max(sustained, 1e-6), 2),
            message=(
                f"{written / 1e9:.1f} GB in {total_s:.1f} s: mean "
                f"{mean:.0f} MB/s, last quarter {sustained:.0f} MB/s"
                + ("" if mean < sustained * 1.5 else
                   "  -- the mean is well above the tail, so this drive has a "
                   "write cache and the tail figure is the one to plan with")
            ),
        )
    except OSError as exc:
        result.update(
            message=f"{type(exc).__name__}: {exc}",
            bytes=written, seconds=round(marks[-1][0], 2) if marks else 0.0,
        )
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return result


def verify_device(directory: Path, size_bytes: int = 4 << 20) -> dict[str, Any]:
    """Write, flush, read back and compare. Answers "will this disk keep data?"

    Worth having as a deliberate action because the alternative is finding out
    at the end of a session. It exercises the same path a capture takes -- a
    few megabytes, fsync'd, size checked -- and then does the one thing a
    capture cannot afford to: reads every byte back and compares it.

    It does NOT prove the device survives being unplugged; nothing short of
    unplugging it does. What it catches is the class of mount that accepts
    writes and stores nothing, a full or read-only filesystem, and a device
    slow enough that the capture rate will not hold.

    **`write_mb_s` from this function is a burst figure and must not be used to
    size a recording.** It writes four megabytes, which on any SSD lands
    entirely in the write cache. `measure_sustained` above is the one a
    recording configuration is chosen from.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".trilobite-verify-{os.getpid()}"
    payload = os.urandom(size_bytes)

    result: dict[str, Any] = {"path": str(probe), "bytes": size_bytes, "ok": False}
    try:
        t0 = time.perf_counter()
        write_durably(probe, payload)
        write_ms = (time.perf_counter() - t0) * 1000.0

        on_disk = verify_size(probe, size_bytes)

        t0 = time.perf_counter()
        got = probe.read_bytes()
        read_ms = (time.perf_counter() - t0) * 1000.0

        if got != payload:
            raise EmptyWriteError(
                f"{probe} read back {len(got)} bytes that do not match what was "
                f"written. The device is corrupting data."
            )
        result.update(
            ok=True, on_disk=on_disk,
            write_ms=round(write_ms, 1), read_ms=round(read_ms, 1),
            write_mb_s=round(size_bytes / 1e6 / max(write_ms / 1000, 1e-6), 1),
            burst_only=True,
            message=(f"{size_bytes / 1e6:.0f} MB written, flushed and read back "
                     f"identically in {write_ms:.0f} ms "
                     f"({size_bytes / 1e6 / max(write_ms / 1000, 1e-6):.0f} MB/s "
                     f"-- a burst figure, not a recording rate)"),
        )
    except OSError as exc:
        result["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return result


# Bumped when the shape of a sidecar changes in a way a reader must notice.
# Both readers refuse a version they do not know rather than guessing at it.
SIDECAR_SCHEMA = 1


def _controls_block(
    requested: dict[str, Any] | None, meta: dict[str, Any],
) -> dict[str, Any]:
    """Requested and effective, kept apart, with the gap named.

    `requested` is what somebody asked the sensor for. `effective` is what this
    frame's own driver metadata reports. They are different things and the
    difference is the whole point: under auto-exposure the request is a
    preference and the metadata is the fact, and a control submitted while a
    request was already in flight cannot have affected it at all.

    Anything requested that the metadata does not report comes back under
    `unknown`. **It is never backfilled from the request** -- that would be
    presenting a preference as a measurement, which is the same move the raw
    admission boundary refuses for pixel values.
    """
    requested = dict(requested or {})
    effective_keys = ("ExposureTime", "AnalogueGain", "DigitalGain", "AeLocked",
                      "ColourGains", "SensorTimestamp")
    effective = {k: _jsonable(meta[k]) for k in effective_keys if k in meta}
    unknown = sorted(k for k in requested if k not in effective)
    return {
        "requested": _jsonable(requested),
        "effective": effective,
        "unknown": unknown,
    }


def _pipeline_block(processing: dict[str, Any] | None) -> dict[str, Any]:
    """Stage parameters in the flat `{name: {type, ...}}` shape readers expect.

    Built from the frozen execution record, never from the live pipeline.
    """
    if not processing:
        return {}
    if processing.get("ran"):
        return {
            s["name"]: {"type": s["type"], **_jsonable(s.get("params") or {})}
            for s in processing.get("stages") or []
        }
    return _jsonable(processing.get("params_at_capture") or {})


def _jsonable(value: Any) -> Any:
    """libcamera metadata contains tuples, numpy scalars and enums."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class StorageRefused(OSError):
    """This target may not be written to, and nothing was attempted.

    Distinct from `EmptyWriteError` (the device took the bytes and lost them)
    and from a plain `OSError` (the write was attempted and failed). A refusal
    means the data is still wherever the caller has it, which for a burst or a
    recording chunk is the whole point: a flush refused before it starts can be
    sent somewhere else, where one refused half way through cannot.
    """

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


class SessionWriter:
    def __init__(self, cfg: StorageConfig, root: Path) -> None:
        self.cfg = cfg
        # The configured root is the fallback, always. It lives on the internal
        # disk, it is always mounted, and it is where captures go when the
        # chosen device is absent -- which is better than losing them.
        self.default_root = Path(root)
        self.root = Path(root)
        self._lock = threading.Lock()
        # Wraps the same lock, so `with self._drained:` IS `with self._lock:`.
        # Release waits on it for in-flight writes to finish; every write
        # notifies it on the way out. See `release`.
        self._drained = threading.Condition(self._lock)
        self._counter = 0
        self._manifest: dict[str, Any] | None = None

        # -- Stage 5a ----------------------------------------------------
        # Which volume the current root IS, recorded when it was selected.
        # None for the internal default, which is not removable.
        self._identity: identity.DeviceIdentity | None = None
        # Incremented by every retarget and every release. Every write carries
        # the generation it was admitted under, so a chunk written to the old
        # target cannot be mistaken for part of the new one, and a completion
        # journal can say which volume each member actually reached.
        self.generation = 1
        # False while a release is draining. New writes are refused rather than
        # queued: the operator has said they want to unplug the disk, and
        # admitting one more write is the opposite of what that means.
        self._accepting = True
        self._inflight = 0
        # Mount point -> why it was quarantined. A device that accepted bytes
        # and produced an empty file has told us something about itself that
        # does not expire within a session. `check_and_recover` used to be able
        # to return False and leave every subsequent capture pointed at it.
        self._quarantine: dict[str, str] = {}
        # (filename, monotonic time, bytes, durability) of the last file that
        # actually landed. Reported in state() so "is this device still taking
        # data" has an answer that is evidence rather than inference from a
        # mount point looking healthy.
        self._last_write: tuple[str, float, int, str] | None = None
        # Human-readable record of every retarget and every recovery. Surfaced
        # in the UI, because a session that silently moved to a different disk
        # halfway through is a session you will spend an hour looking for.
        self.notes: list[str] = []
        self.session_dir = self._new_session_dir(self.root)
        log.info("session directory: %s", self.session_dir)

    # -- session directories ---------------------------------------------

    @staticmethod
    def _new_session_dir(root: Path) -> Path:
        d = Path(root) / datetime.now().strftime("session_%Y%m%d_%H%M%S")
        # Two retargets inside one second would otherwise collide onto the same
        # directory and interleave two sessions' files.
        n, base = 1, d
        while d.exists():
            n += 1
            d = base.with_name(f"{base.name}_{n}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _note(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.notes.append(f"{stamp}  {msg}")
        del self.notes[:-20]
        log.warning("storage: %s", msg)

    # -- retargeting -------------------------------------------------------

    def retarget(self, root: Path | str, force: bool = False) -> dict[str, Any]:
        """Point subsequent captures at a different filesystem.

        A **new session directory** is created there rather than the current
        one being moved or mirrored. Moving would mean copying files while
        another thread appends to them; mirroring would leave two divergent
        copies of a session. A new directory is unambiguous, and the note left
        behind says where the earlier part of the afternoon went.

        Files already written stay where they are. Nothing is deleted, ever.

        Stage 5a additions: the volume's identity is recorded here, which is
        the only moment it can be, and the generation counter is bumped. A
        quarantined device is refused unless `force`, because the whole value
        of a quarantine is that it is not trivially re-entered.
        """
        target = Path(os.path.expanduser(str(root)))
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"cannot create {target}: {exc}") from None
        if not devices._writable(str(target)):
            raise ValueError(f"{target} is not writable (read-only mount, or permissions)")

        key = str(target)
        if not force and key in self._quarantine:
            raise ValueError(
                f"{target} was quarantined: {self._quarantine[key]}. Selecting "
                f"it again needs an explicit override -- a device that lost "
                f"data once is not one to put a session on by accident.")

        # A different filesystem may make a different durability promise, and
        # the cached answer is per directory. Drop it rather than carry the old
        # mount's answer onto the new one.
        _DURABILITY_CACHE.clear()

        is_default = Path(target) == Path(self.default_root)
        # Identity BEFORE the lock: it stats the filesystem and may call lsblk.
        new_identity = None if is_default else identity.identify(
            target, lookup=devices.describe_mount)

        with self._lock:
            previous = self.session_dir
            self.root = target
            self._identity = new_identity
            self.generation += 1
            self._accepting = True
            self.session_dir = self._new_session_dir(target)
            gen = self.generation
        self._note(f"output moved to {self.session_dir} (was {previous}), "
                   f"generation {gen}"
                   + (f", volume {new_identity.describe()}" if new_identity else ""))
        if self._manifest is not None:
            self.write_session_manifest(self._manifest)
        return self.state()

    def identity_dict(self) -> dict[str, Any] | None:
        """The selected volume's recorded identity, or None for the default.

        Public because the recording journals record it: a manifest that names
        a directory but not the volume it was written to cannot answer "was
        this the disk I selected" after the fact, which is the whole point of
        recording an identity in the first place.
        """
        ident = self._identity
        return ident.as_dict() if ident else None

    def release(self, timeout: float = 10.0) -> dict[str, Any]:
        """Go back to the internal default so a device can be unplugged safely.

        **This now drains, and that is the Stage 5a correction.** The old
        version called `retarget` immediately. `save_still` held `_lock` only
        long enough to allocate a counter, so a write to the old target could
        still be inside `fsync` when release returned -- and the UI's advice is
        "release, then remove the disk", which made the sequence unsafe exactly
        when a capture was in flight. There is no eject here, so release is the
        only handoff there is and it has to mean something.

        So: stop accepting new writes, wait for the in-flight ones, and only
        then move. A wait that times out does NOT release; it reports what is
        still outstanding and leaves the target where it is, because telling an
        operator a disk is safe to pull while a write is inside the kernel is
        the failure this is meant to remove.
        """
        if Path(self.root) == Path(self.default_root):
            return self.state()

        with self._drained:
            self._accepting = False
            deadline = time.monotonic() + timeout
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outstanding = self._inflight
                    self._accepting = True
                    self._note(
                        f"release refused: {outstanding} write(s) still in "
                        f"flight after {timeout:.0f} s. The disk has NOT been "
                        f"released and must not be removed.")
                    state = self.state()
                    state["released"] = False
                    state["inflight"] = outstanding
                    return state
                self._drained.wait(min(remaining, 0.25))

        state = self.retarget(self.default_root)
        state["released"] = True
        state["inflight"] = 0
        return state

    # -- write admission ---------------------------------------------------

    def target_state(self, need_bytes: int = 0) -> tuple[str, str]:
        """Can the current target take `need_bytes` right now? (state, reason).

        The four-way distinction the review asked for, plus the two the stage
        added. Absent, wrong-volume, read-only, full, quarantined, ok -- each
        with a different correct response, where before they collapsed into one
        boolean that could not tell "the disk is gone" from "the disk is full"
        from "this is a different disk at the same path".
        """
        root = str(self.root)
        if root in self._quarantine:
            return identity.QUARANTINED, self._quarantine[root]

        state, reason = identity.verify(self._identity, root)
        if state in (identity.ABSENT, identity.WRONG_VOLUME):
            return state, reason

        if not os.path.isdir(root):
            return identity.ABSENT, f"{root} is not a directory"
        if not devices._writable_hint(root):
            return identity.READ_ONLY, f"{root} is not writable"

        _total, free = devices._usage(root)
        reserve = self.reserve_for(root)
        if free - need_bytes < reserve:
            return identity.FULL, (
                f"{root} has {free / 1e9:.2f} GB free and a "
                f"{reserve / 1e9:.2f} GB reserve; "
                f"{need_bytes / 1e9:.2f} GB was requested, which would leave "
                f"{(free - need_bytes) / 1e9:.2f} GB.")
        return identity.OK, reason

    def reserve_for(self, root: str | Path) -> int:
        """Free space that must survive a write, in bytes.

        Two numbers, because the consequences differ. On a removable target the
        reserve stops a session filling the disk it is writing to. On the
        internal disk it protects the SD card the operating system is running
        from -- a full root filesystem is not a lost session, it is a Pi that
        will not boot, so that reserve is the larger of the two.
        """
        if Path(root) == Path(self.default_root):
            return int(self.cfg.internal_reserve_mb) << 20
        return int(self.cfg.reserve_mb) << 20

    def admit(self, need_bytes: int = 0, *, internal_ok: bool = False) -> int:
        """Claim the right to write `need_bytes`. Returns the generation.

        Raises `StorageRefused` and writes nothing. The returned generation is
        recorded with whatever gets written, so a member can later be matched
        to the volume it actually reached.

        `internal_ok` is the per-write override for the internal disk, and it
        defaults to False here as well as in the UI. A recording or a burst
        flush that lands on the SD card unasked is the failure Stage 5a exists
        to prevent, and defaulting the parameter the other way would mean every
        new caller opted into it by omission.
        """
        with self._drained:
            if not self._accepting:
                raise StorageRefused(
                    "releasing",
                    f"{self.root} is being released so a disk can be removed; "
                    f"no new writes are being accepted.")
            is_internal = Path(self.root) == Path(self.default_root)
            if is_internal and not internal_ok and need_bytes > 0:
                raise StorageRefused(
                    "internal-refused",
                    f"the active target is the internal disk ({self.root}) and "
                    f"this write is {need_bytes / 1e9:.2f} GB. Select an "
                    f"external volume, or tick the internal-storage override "
                    f"for this save.")
            state, reason = self.target_state(need_bytes)
            if state != identity.OK and state != identity.UNKNOWN:
                raise StorageRefused(state, f"{identity.HUMAN[state]}: {reason}")
            self._inflight += 1
            return self.generation

    def done(self) -> None:
        """Release one admission. Must be called on every path."""
        with self._drained:
            self._inflight = max(0, self._inflight - 1)
            self._drained.notify_all()

    @contextlib.contextmanager
    def writing(self, need_bytes: int = 0, *, internal_ok: bool = False):
        """`with writer.writing(n) as generation:` -- admit, then always release."""
        gen = self.admit(need_bytes, internal_ok=internal_ok)
        try:
            yield gen
        finally:
            self.done()

    def quarantine(self, reason: str, target: str | None = None) -> None:
        """Mark a target as one that must not be selected again this session.

        Called when a device accepts bytes and produces a file of the wrong
        size, when a write to it fails, or when its identity stops matching.
        None of those is transient and none is the code's fault: either the
        volume went away mid-write or the mount is discarding data, and in both
        cases the rest of the session goes somewhere else.

        **Keyed on the selected target path, not on the mount point.** Keyed on
        the mount point it would quarantine `/` whenever the chosen directory
        happens to sit on the root filesystem -- true on a dev machine, and
        true of any misconfiguration that points the rig at its own disk -- and
        that would take the fallback down with it. The operator chose a path;
        the path is what is refused.

        **The default root is never quarantined.** It is the fallback, and a
        fallback that can be disabled by the failure it exists to absorb is not
        one. If the internal disk is also failing, the writes fail and say so,
        which is the honest outcome.
        """
        key = str(Path(os.path.expanduser(target or str(self.root))))
        if Path(key) == Path(self.default_root):
            self._note(f"NOT quarantining the fallback root {key}: {reason}")
            return
        if key in self._quarantine:
            return
        self._quarantine[key] = reason
        self._note(f"{key} QUARANTINED: {reason}")

    # -- survival ----------------------------------------------------------

    def check_and_recover(self) -> bool:
        """Fall back to the default root if the active one is no longer usable.

        Returns True when a recovery happened. Called on a timer and again
        after any write failure.

        **Now driven by `target_state` rather than by `is_mounted`.** The old
        test was `is_mounted(root) and _writable(root)`, and `is_mounted`
        walked up to the first extant parent -- so for the case this function
        exists for, a pulled stick whose mount point remains as an ordinary
        directory on the SD card, it returned True and no recovery happened.
        The identity comparison is what detects it.
        """
        if Path(self.root) == Path(self.default_root):
            return False
        state, reason = self.target_state()
        if state in (identity.OK, identity.UNKNOWN, identity.FULL):
            # FULL is deliberately not a recovery. Diverting a session onto the
            # internal disk because the external one filled up is how the SD
            # card gets filled next, and the operator can see "full" and decide.
            return False
        lost = self.root
        if state in (identity.WRONG_VOLUME, identity.ABSENT):
            self.quarantine(f"disappeared mid-session ({state})", target=str(lost))
        # retarget bumps the generation, which is what stops anything still
        # holding the old one from writing into the new session directory.
        self.retarget(self.default_root, force=True)
        self._note(f"{lost} is {identity.HUMAN.get(state, state)}: {reason}; "
                   f"captures now go to {self.session_dir}")
        return True

    def state(self) -> dict[str, Any]:
        """Where output is going, and how much room is left there."""
        root = str(self.root)
        total, free = devices._usage(root)
        target_state, target_reason = self.target_state()
        return {
            "root": root,
            "default_root": str(self.default_root),
            "session_dir": str(self.session_dir),
            "mount": devices.mount_of(root),
            "removable": Path(root) != Path(self.default_root),
            # Kept for existing callers and the dashboard, and it is the WEAK
            # check -- see devices.is_mounted. `target_state` below is the one
            # that decides whether a write is admitted.
            "present": devices.is_mounted(root),
            "state": target_state,
            "state_reason": target_reason,
            "state_human": identity.HUMAN.get(target_state, target_state),
            "generation": self.generation,
            "accepting": self._accepting,
            "inflight": self._inflight,
            "identity": self._identity.as_dict() if self._identity else None,
            "quarantined": dict(self._quarantine),
            "reserve_bytes": self.reserve_for(root),
            "free_bytes": free,
            "total_bytes": total,
            "free_gb": round(free / 1e9, 1),
            "total_gb": round(total / 1e9, 1),
            "usable_bytes": max(0, free - self.reserve_for(root)),
            "notes": list(self.notes),
            # When something last actually landed, and what. A mounted,
            # writable, roomy device tells you nothing about whether the last
            # capture reached it -- and the moment that matters is exactly the
            # moment those three all still look fine. None means nothing has
            # been written in this run, which is different from "long ago".
            "last_write": (None if self._last_write is None else {
                "file": self._last_write[0],
                "age_s": round(time.monotonic() - self._last_write[1], 2),
                "bytes": self._last_write[2],
                "durability": self._last_write[3],
            }),
        }

    def write_session_manifest(self, payload: dict[str, Any]) -> Path:
        """Record the rig configuration once, at startup -- and again in every
        session directory a retarget creates, so no folder is ever orphaned
        from the configuration that produced it."""
        self._manifest = payload
        path = self.session_dir / "session.json"
        body = json.dumps(_jsonable(payload), indent=2).encode("utf-8")
        write_durably(path, body)
        verify_size(path, len(body))
        return path

    def _write_image(self, stem: str, frame: Frame) -> tuple[Path, int]:
        """Write one image and return its path and its verified size on disk.

        The .npy path serialises to memory first and writes the buffer itself,
        rather than handing the path to `np.save`. That costs one copy of the
        frame -- about 3 MB, irrelevant here -- and buys the two things that
        matter: the bytes can be fsync'd, and the expected length is known
        exactly, so the size read back afterwards is checked against a number
        rather than merely against zero.
        """
        cam_dir = self.session_dir / frame.cam_id
        cam_dir.mkdir(parents=True, exist_ok=True)
        fmt = self.cfg.still_format
        img_path = cam_dir / f"{stem}.{'npy' if fmt == 'npy' else fmt}"

        if fmt == "npy":
            buf = io.BytesIO()
            np.save(buf, frame.data, allow_pickle=False)
            payload = buf.getvalue()
            write_durably(img_path, payload)
            return img_path, verify_size(img_path, len(payload))

        # Third-party encoders own the file handle, so the best available
        # sequence is: let them write, then fsync the result by path.
        try:
            import cv2  # noqa: PLC0415

            if not cv2.imwrite(str(img_path), frame.data):
                raise RuntimeError(f"cv2.imwrite failed for {img_path}")
        except ImportError:
            from PIL import Image  # noqa: PLC0415

            Image.fromarray(frame.data).save(img_path)

        with open(img_path, "rb+") as fh:
            fsync_file(fh)
        fsync_dir(cam_dir)
        return img_path, verify_size(img_path)

    def save_still(
        self,
        frame: Frame,
        camera_info: dict[str, Any] | None = None,
        tag: str = "still",
        label: str | None = None,
        controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a frame and the record of where it came from.

        **`pipeline_settings` is gone.** It was read from the live pipeline at
        save time, which is the Stage 4 bug: edit a gain between capture and
        save and the sidecar described a value that never touched the pixels,
        while a raw capture -- which never enters the pipeline at all -- got a
        full parameter block describing processing that did not happen. What
        the frame was processed under now travels on the frame, frozen at
        execution by `Pipeline.__call__`, and this method serialises it.

        `controls` is the REQUESTED set, kept separate from what the frame's
        own metadata says was effective. See `_controls_block`.
        """
        # The still path is admitted with `internal_ok=True` and that is
        # deliberate, not an oversight in the override policy. A still is a few
        # megabytes; losing one the operator asked for in order to keep it off
        # the internal disk is the wrong trade, and the reserve still applies,
        # so it cannot fill the card. The override exists for the multi-gigabyte
        # writes -- a burst flush and a recording -- where landing on the SD
        # card unasked is the failure the stage exists to prevent.
        need = int(getattr(frame.data, "nbytes", 0)) + (1 << 16)
        try:
            generation = self.admit(need, internal_ok=True)
        except StorageRefused as refused:
            # The target is gone, swapped, or quarantined, and admission caught
            # it BEFORE anything was written -- which is new, and strictly
            # better than the old order: the old code attempted the write, let
            # it fail, and recovered from the failure. Falling back here keeps
            # the promise that a still the operator asked for is not lost to
            # the directory it was meant to land in, and the reserve still
            # applies on the internal disk so this cannot fill the card.
            if refused.state not in (identity.ABSENT, identity.WRONG_VOLUME,
                                     identity.QUARANTINED):
                raise
            if not self.check_and_recover():
                raise
            generation = self.admit(need, internal_ok=True)
        try:
            with self._lock:
                self._counter += 1
                n = self._counter
            return self._save_still_admitted(
                frame, generation, n, camera_info, tag, label, controls)
        finally:
            self.done()

    def _save_still_admitted(
        self,
        frame: Frame,
        generation: int,
        n: int,
        camera_info: dict[str, Any] | None,
        tag: str,
        label: str | None,
        controls: dict[str, Any] | None,
    ) -> dict[str, Any]:

        # cam_id leads the filename after the tag, so a directory listing sorts
        # by what the file *is* and names which physical camera produced it
        # without opening the sidecar.
        #
        # A frame whose pixels were not established as sensor counts says so in
        # its NAME, ahead of the tag. In the filename rather than only in the
        # sidecar because the failure this guards against is somebody loading a
        # directory of .npy files by glob and fitting a model to them -- which
        # is exactly what happened with the compressed session -- and a leading
        # prefix is the one piece of provenance that survives `ls`, a glob and
        # a drag into MATLAB.
        #
        # Three validities, three names, because `diagnostic` and
        # `unvalidated` are different claims: the first says the values are
        # known to be wrong, the second says nothing was established. Calling
        # an ISP frame `diagnostic_` would be as inaccurate in its own
        # direction as calling it `science`.
        prefix = "" if frame.validity == SCIENCE else f"{frame.validity}_"
        stem = (f"{prefix}{tag}_{frame.cam_id}_{n:06d}_"
                f"{datetime.now().strftime('%H%M%S_%f')}")

        try:
            img_path, img_bytes = self._write_image(stem, frame)
        except OSError as exc:
            # The device was pulled between the check and the write, or it
            # accepted the bytes and produced an empty file. Recover to the
            # internal disk and write there rather than losing the frame -- a
            # capture you asked for is worth more than the directory it was
            # meant to land in, and the note records what happened.
            #
            # EmptyWriteError is an OSError so it lands here too, deliberately:
            # a device that just silently discarded a frame is a device the
            # rest of the session must not be written to either. Stage 5a makes
            # that explicit -- it is quarantined, so `check_and_recover` cannot
            # later hand the session back to it.
            self._note(f"write to {self.session_dir} failed ({exc}); recovering")
            if isinstance(exc, EmptyWriteError):
                self.quarantine(f"accepted bytes and produced the wrong size: {exc}")
            elif Path(self.root) != Path(self.default_root):
                # A plain write failure on the selected external target is
                # enough to leave it, even when its identity still checks out.
                # `check_and_recover` deliberately treats a healthy-looking
                # target as no reason to move -- that is right for the timer
                # that calls it, and wrong here, where the evidence is a failed
                # write rather than a poll. So move explicitly.
                self.quarantine(f"write failed: {exc}")
            if Path(self.root) == Path(self.default_root):
                # Nowhere left to fall back to.
                raise
            self.retarget(self.default_root, force=True)
            # The recovery retargeted, which bumped the generation. Record the
            # one this file actually went out under rather than the one it was
            # admitted under, or the sidecar names a volume it never reached.
            generation = self.generation
            img_path, img_bytes = self._write_image(stem, frame)

        # THREE BLOCKS, ONE FILE, ONE VERSION.
        #
        # `acquisition` is where the pixels came from, `processing` is what was
        # done to them, `saved` is this particular write. They are separate
        # because they have different lifetimes: the same retained frame saved
        # twice must produce identical acquisition and processing blocks and
        # may legitimately differ in `saved`. Three blocks rather than three
        # files, because three files would have to be rejoined by every reader
        # for no benefit.
        #
        # The identity keys stay at the top level. They are what a reader
        # indexes on and what a directory listing is sorted by, and burying
        # them inside a block would mean opening two levels to find out which
        # camera a file came from.
        sidecar = {
            "schema": SIDECAR_SCHEMA,
            "file": img_path.name,
            "cam_id": frame.cam_id,
            "camera_label": label or frame.cam_id,
            "tag": tag,
            "t_iso": datetime.fromtimestamp(frame.t_wall).isoformat(),

            "acquisition": {
                "seq": frame.seq,
                "t_monotonic": frame.t_mono,
                "t_wall": frame.t_wall,
                "space": frame.space,
                # 'science', 'diagnostic' or 'unvalidated'. The field an
                # offline reader must check before fitting anything:
                # `space: raw` says the ISP was bypassed, which is a statement
                # about the PATH, not about whether the values that came down
                # it are sensor counts.
                "validity": frame.validity,
                # What produced the pixels. Recorded alongside validity because
                # measurement eligibility is not one predicate -- corner
                # geometry off an ISP mono frame is defensible where radiometry
                # off the same frame is not, and a reader cannot tell those
                # apart without this.
                "source_kind": frame.source_kind,
                "dtype": str(frame.data.dtype),
                "shape": list(frame.data.shape),
                "sensor_metadata": _jsonable(frame.meta),
                "controls": _controls_block(controls, frame.meta),
                "camera": _jsonable(camera_info or {}),
            },

            # Frozen at execution, or `ran: false` naming what was bypassed.
            # Never read from the live pipeline.
            "processing": _jsonable(frame.processing or {"ran": False}),

            # The stage parameters, in the flat shape every existing reader
            # already understands -- but now taken from the FROZEN record
            # rather than from the live pipeline, which is the whole point of
            # the stage. For a processed frame these are the values the pixels
            # went through; for a raw capture they are the alignment as it
            # stood at exposure, and `processing.ran` is false to say so. The
            # MLA geometry is needed either way: it describes the optics, not
            # a processing step, and `read_capture.py --grid` reads it here.
            "pipeline": _pipeline_block(frame.processing),

            # -- flattened duplicates, for readers that predate the blocks ----
            # Kept deliberately and marked, because a sidecar schema change
            # that breaks every existing reader and every archived analysis
            # script is a worse outcome than six duplicated scalars. The blocks
            # above are authoritative; these go when the readers no longer
            # look at them.
            "seq": frame.seq,
            "t_monotonic": frame.t_mono,
            "t_wall": frame.t_wall,
            "space": frame.space,
            "validity": frame.validity,
            "source_kind": frame.source_kind,
            "dtype": str(frame.data.dtype),
            "shape": list(frame.data.shape),
            "sensor_metadata": _jsonable(frame.meta),
            "camera": _jsonable(camera_info or {}),
            "bytes": img_bytes,
            # What durability this filesystem actually gave us, measured on the
            # directory the file went into. `file-only` means the bytes are on
            # the device but the directory entry may not be, so a power cut can
            # return the file without its name. Recorded rather than assumed,
            # because it is the difference between "saved" meaning two
            # different things on the Pi and on a Windows desktop.
            "durability": durability_of(img_path.parent),
        }
        # The one block that may legitimately differ between two saves of the
        # same retained frame: which write this was, when, and to where. Built
        # BEFORE serialisation, or it would not be in the file at all.
        sidecar["saved"] = {
            "save_id": n,
            "t_iso": datetime.now().isoformat(),
            "path": str(img_path),
            "bytes": img_bytes,
            "durability": sidecar["durability"],
            "session_dir": str(self.session_dir),
            # Which volume, and which selection of it. Two files with the same
            # generation went to the same filesystem under the same identity;
            # a change of generation between two files means the target moved
            # between them, which is the one thing a directory listing cannot
            # show. See storage/identity.py.
            "storage_generation": generation,
            "storage_identity": (
                self._identity.as_dict() if self._identity else None),
        }

        meta_path = img_path.with_suffix(".json")
        payload = json.dumps(sidecar, indent=2).encode("utf-8")
        write_durably(meta_path, payload)
        verify_size(meta_path, len(payload))

        # Recorded only here, after BOTH members are on the device and both
        # sizes verified. Setting it earlier would make "last write" mean "last
        # write attempted", which is the class of claim this module exists to
        # stop making.
        self._last_write = (img_path.name, time.monotonic(), img_bytes,
                            sidecar["durability"])

        log.info("saved %s (%s %s, %d bytes on disk)",
                 img_path.name, frame.data.shape, frame.data.dtype, img_bytes)
        return {"image": str(img_path), "metadata": str(meta_path), **sidecar}
