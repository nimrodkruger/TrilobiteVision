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
from ..types import Frame
from . import devices

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


def fsync_dir(path: Path) -> None:
    """Make a directory entry durable, so the *name* survives a power cut too.

    Fsyncing a file guarantees its contents; it says nothing about the entry
    that points at it. Both are needed, and the directory one is not available
    on Windows -- opening a directory raises there -- so a failure is logged at
    debug and ignored rather than failing a capture on the dev machine.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:                       # Windows, or an odd FUSE mount
        log.debug("cannot open %s to fsync: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:                       # some FUSE backends refuse
        log.debug("cannot fsync directory %s: %s", path, exc)
    finally:
        os.close(fd)


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
            message=(f"{size_bytes / 1e6:.0f} MB written, flushed and read back "
                     f"identically in {write_ms:.0f} ms "
                     f"({size_bytes / 1e6 / max(write_ms / 1000, 1e-6):.0f} MB/s)"),
        )
    except OSError as exc:
        result["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return result


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


class SessionWriter:
    def __init__(self, cfg: StorageConfig, root: Path) -> None:
        self.cfg = cfg
        # The configured root is the fallback, always. It lives on the internal
        # disk, it is always mounted, and it is where captures go when the
        # chosen device is absent -- which is better than losing them.
        self.default_root = Path(root)
        self.root = Path(root)
        self._lock = threading.Lock()
        self._counter = 0
        self._manifest: dict[str, Any] | None = None
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

    def retarget(self, root: Path | str) -> dict[str, Any]:
        """Point subsequent captures at a different filesystem.

        A **new session directory** is created there rather than the current
        one being moved or mirrored. Moving would mean copying files while
        another thread appends to them; mirroring would leave two divergent
        copies of a session. A new directory is unambiguous, and the note left
        behind says where the earlier part of the afternoon went.

        Files already written stay where they are. Nothing is deleted, ever.
        """
        target = Path(os.path.expanduser(str(root)))
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"cannot create {target}: {exc}") from None
        if not devices._writable(str(target)):
            raise ValueError(f"{target} is not writable (read-only mount, or permissions)")

        with self._lock:
            previous = self.session_dir
            self.root = target
            self.session_dir = self._new_session_dir(target)
        self._note(f"output moved to {self.session_dir} (was {previous})")
        if self._manifest is not None:
            self.write_session_manifest(self._manifest)
        return self.state()

    def release(self) -> dict[str, Any]:
        """Go back to the internal default so a device can be unplugged safely.

        The honest counterpart to a hot-plug story: there is no eject here, so
        the sequence is release, confirm the UI says the output is internal
        again, then pull the disk.
        """
        if Path(self.root) == Path(self.default_root):
            return self.state()
        return self.retarget(self.default_root)

    # -- survival ----------------------------------------------------------

    def check_and_recover(self) -> bool:
        """Fall back to the default root if the active one has gone away.

        Returns True when a recovery happened. Called on a timer and again
        after any write failure -- pulling a USB stick leaves the mount point
        behind as an ordinary empty directory, so writes keep *succeeding*, to
        the SD card, under a path that says otherwise. That silent case is the
        one this exists for.
        """
        if Path(self.root) == Path(self.default_root):
            return False
        if devices.is_mounted(self.root) and devices._writable(str(self.root)):
            return False
        lost = self.root
        with self._lock:
            self.root = Path(self.default_root)
            self.session_dir = self._new_session_dir(self.root)
        self._note(
            f"{lost} disappeared or went read-only; captures now go to {self.session_dir}"
        )
        if self._manifest is not None:
            self.write_session_manifest(self._manifest)
        return True

    def state(self) -> dict[str, Any]:
        """Where output is going, and how much room is left there."""
        root = str(self.root)
        total, free = devices._usage(root)
        return {
            "root": root,
            "default_root": str(self.default_root),
            "session_dir": str(self.session_dir),
            "mount": devices.mount_of(root),
            "removable": Path(root) != Path(self.default_root),
            "present": devices.is_mounted(root),
            "free_bytes": free,
            "total_bytes": total,
            "free_gb": round(free / 1e9, 1),
            "total_gb": round(total / 1e9, 1),
            "notes": list(self.notes),
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
        pipeline_settings: dict[str, Any] | None = None,
        camera_info: dict[str, Any] | None = None,
        tag: str = "still",
        label: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._counter += 1
            n = self._counter

        # cam_id leads the filename after the tag, so a directory listing sorts
        # by what the file *is* and names which physical camera produced it
        # without opening the sidecar.
        stem = f"{tag}_{frame.cam_id}_{n:06d}_{datetime.now().strftime('%H%M%S_%f')}"

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
            # rest of the session must not be written to either.
            self._note(f"write to {self.session_dir} failed ({exc}); recovering")
            if not self.check_and_recover():
                raise
            img_path, img_bytes = self._write_image(stem, frame)

        sidecar = {
            "file": img_path.name,
            "cam_id": frame.cam_id,
            "camera_label": label or frame.cam_id,
            "tag": tag,
            "seq": frame.seq,
            "t_monotonic": frame.t_mono,
            "t_wall": frame.t_wall,
            "t_iso": datetime.fromtimestamp(frame.t_wall).isoformat(),
            "space": frame.space,
            "dtype": str(frame.data.dtype),
            "shape": list(frame.data.shape),
            "sensor_metadata": _jsonable(frame.meta),
            "pipeline": _jsonable(pipeline_settings or {}),
            "camera": _jsonable(camera_info or {}),
            "bytes": img_bytes,
        }
        meta_path = img_path.with_suffix(".json")
        payload = json.dumps(sidecar, indent=2).encode("utf-8")
        write_durably(meta_path, payload)
        verify_size(meta_path, len(payload))

        log.info("saved %s (%s %s, %d bytes on disk)",
                 img_path.name, frame.data.shape, frame.data.dtype, img_bytes)
        return {"image": str(img_path), "metadata": str(meta_path), **sidecar}
