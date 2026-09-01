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
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..config import StorageConfig
from ..types import Frame
from . import devices

log = logging.getLogger(__name__)


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
        path.write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")
        return path

    def _write_image(self, stem: str, frame: Frame) -> Path:
        cam_dir = self.session_dir / frame.cam_id
        cam_dir.mkdir(parents=True, exist_ok=True)
        fmt = self.cfg.still_format
        img_path = cam_dir / f"{stem}.{'npy' if fmt == 'npy' else fmt}"
        if fmt == "npy":
            np.save(img_path, frame.data)
        else:
            try:
                import cv2  # noqa: PLC0415

                if not cv2.imwrite(str(img_path), frame.data):
                    raise RuntimeError(f"cv2.imwrite failed for {img_path}")
            except ImportError:
                from PIL import Image  # noqa: PLC0415

                Image.fromarray(frame.data).save(img_path)
        return img_path

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
            img_path = self._write_image(stem, frame)
        except OSError as exc:
            # The device was pulled between the check and the write. Recover to
            # the internal disk and write there rather than losing the frame --
            # a capture you asked for is worth more than the directory it was
            # meant to land in, and the note records what happened.
            self._note(f"write to {self.session_dir} failed ({exc}); recovering")
            if not self.check_and_recover():
                raise
            img_path = self._write_image(stem, frame)

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
        }
        meta_path = img_path.with_suffix(".json")
        meta_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

        log.info("saved %s (%s %s)", img_path.name, frame.data.shape, frame.data.dtype)
        return {"image": str(img_path), "metadata": str(meta_path), **sidecar}
