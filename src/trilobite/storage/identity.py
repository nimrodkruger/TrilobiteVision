"""Which volume is this, really, and is it still the one that was chosen?

Stage 5a. The failure this closes, stated once:

    A USB disk is mounted at /media/pi/SSD. The operator selects it. Captures
    go to /media/pi/SSD/trilobite-data/session_.../ and land. The disk is then
    pulled -- or the cable twitches and the kernel re-enumerates it elsewhere.
    **The mount point does not disappear.** It is an ordinary directory on the
    root filesystem, it is writable, and `Path.exists()` says yes. Every
    subsequent write succeeds, onto the SD card, under a path that names the
    SSD. At 190 MB/s that is a dead card inside a few minutes, and a session
    that reports success throughout.

`devices.is_mounted` carried a docstring claiming it compared device ids. It did
not -- it walked up to the first extant parent and asked `os.access`, which is
true of the leftover directory as well. That gap is what this module fills.

The mechanism is `st_dev`: the kernel's identifier for the filesystem backing
an inode. Every file on one mounted filesystem shares it, and no two
simultaneously mounted filesystems share it. So recording it at selection time
and comparing it before each admission answers three different questions that
a boolean cannot:

  * the volume is gone, and the path now resolves to some other filesystem
    (almost always the root one) -- **wrong volume**, the dangerous case;
  * the path does not resolve at all -- **absent**;
  * it resolves to the right volume -- and then free space, writability and
    measured throughput are separate questions with separate answers.

`st_dev` is not stable across a reboot or a replug, which is exactly the
property wanted: an identity that survived a replug would not detect one. It is
recorded per selection and discarded on release.

A deliberate limitation: two different sticks mounted at the same path in
sequence will usually differ in `st_dev`, and the bench test for that case
(pull one stick, plug a different one in at the same path) is in the plan
because "usually" is not "always". `fsid` from statvfs is added to the record as
a second discriminator where the filesystem supplies one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# What a target can be, as one word. These replace a boolean that collapsed
# five distinguishable conditions, each with a different correct response.
OK = "ok"
ABSENT = "absent"              # the path does not resolve
WRONG_VOLUME = "wrong-volume"  # it resolves, to a different filesystem
READ_ONLY = "read-only"
FULL = "full"                  # below the reserve, which is not the same as 0
QUARANTINED = "quarantined"    # it discarded data once; it does not get another go
UNKNOWN = "unknown"

# Ordered worst-first for reporting: a quarantined device that is also full
# should report the quarantine, because that is the one that will not clear.
SEVERITY = (QUARANTINED, WRONG_VOLUME, ABSENT, READ_ONLY, FULL, UNKNOWN, OK)

HUMAN = {
    OK: "ready",
    ABSENT: "not present",
    WRONG_VOLUME: "a DIFFERENT filesystem is at this path",
    READ_ONLY: "mounted read-only",
    FULL: "below the free-space reserve",
    QUARANTINED: "quarantined after discarding data",
    UNKNOWN: "cannot be determined",
}


@dataclass(frozen=True)
class DeviceIdentity:
    """Who a volume was, at the moment it was chosen.

    `st_dev` is the discriminator. The rest is for the record and for the
    operator: a refusal that says "st_dev 0x821 != 0xfe01" is correct and
    useless, where one that names the model and the label is actionable.
    """

    path: str                       # the directory that was selected
    mount: str                      # its mount point at selection time
    st_dev: int
    fstype: str = ""
    device: str = ""                # /dev/sda1
    label: str = ""
    model: str = ""
    total_bytes: int = 0
    fsid: str = ""                  # statvfs f_fsid, where the fs supplies one
    # Measured at selection, carried so a later refusal can quote it. See
    # writer.verify_device: the sustained figure, not the burst one.
    sustained_mb_s: float | None = None
    mean_mb_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        bits = [self.label or self.device or self.mount or self.path]
        if self.model:
            bits.append(self.model)
        if self.fstype:
            bits.append(self.fstype)
        if self.total_bytes:
            bits.append(f"{self.total_bytes / 1e9:.0f} GB")
        return " / ".join(b for b in bits if b)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mount": self.mount,
            "st_dev": self.st_dev,
            "st_dev_hex": hex(self.st_dev),
            "fstype": self.fstype,
            "device": self.device,
            "label": self.label,
            "model": self.model,
            "total_bytes": self.total_bytes,
            "fsid": self.fsid,
            "sustained_mb_s": self.sustained_mb_s,
            "mean_mb_s": self.mean_mb_s,
            "description": self.describe(),
            **self.extra,
        }


def _fsid(path: str) -> str:
    """statvfs f_fsid, as hex, or "" when the filesystem does not supply one.

    A second discriminator for the replug case, and nothing more: Linux returns
    0 for many filesystem types, exFAT among them, so it is recorded when
    present and never relied upon.
    """
    try:
        value = int(getattr(os.statvfs(path), "f_fsid", 0) or 0)
    except (OSError, AttributeError, ValueError):
        return ""
    return hex(value) if value else ""


def st_dev_of(path: str | Path) -> int | None:
    """The filesystem id backing `path`, or None if it does not resolve.

    Deliberately does NOT walk up to a parent. Walking up is what made the old
    check useless: the mount point's parent is the root filesystem, which always
    exists, so every answer came back positive.
    """
    try:
        return int(os.stat(str(path)).st_dev)
    except OSError:
        return None


def identify(path: str | Path, lookup: Any = None) -> DeviceIdentity | None:
    """Record who this volume is. None when the path does not exist.

    `lookup` is an optional callable taking the mount point and returning extra
    descriptive fields (device node, label, model) -- `devices.describe_mount`
    in practice, injected so this module does not depend on lsblk being present
    or on the device enumerator at all.
    """
    p = Path(os.path.expanduser(str(path)))
    dev = st_dev_of(p)
    if dev is None:
        return None

    from . import devices as _devices  # noqa: PLC0415 - avoids a cycle at import

    mount = _devices.mount_of(p)
    total, _free = _devices._usage(str(p))
    info: dict[str, Any] = {}
    if lookup is not None:
        try:
            info = dict(lookup(mount) or {})
        except Exception as exc:                      # noqa: BLE001 - advisory
            log.debug("identity lookup for %s failed: %s", mount, exc)

    return DeviceIdentity(
        path=str(p), mount=mount, st_dev=dev,
        fstype=str(info.get("fstype", "")),
        device=str(info.get("device", "")),
        label=str(info.get("label", "")),
        model=str(info.get("model", "")),
        total_bytes=int(total),
        fsid=_fsid(str(p)),
    )


def verify(identity: DeviceIdentity | None, path: str | Path) -> tuple[str, str]:
    """Is `path` still the volume `identity` recorded? Returns (state, reason).

    `identity is None` means nothing was recorded -- the internal default root,
    which is not removable and needs no identity -- so the answer is UNKNOWN
    rather than a failure. Callers treat UNKNOWN as "do not block", because
    refusing to write to the fallback disk because it has no identity on file
    would turn a safety check into an outage.
    """
    if identity is None:
        return UNKNOWN, "no identity was recorded for this target"

    dev = st_dev_of(path)
    if dev is None:
        return ABSENT, f"{path} does not exist; {identity.describe()} is gone"
    if dev != identity.st_dev:
        return WRONG_VOLUME, (
            f"{path} is now on filesystem {hex(dev)}, but {identity.describe()} "
            f"was {hex(identity.st_dev)}. The selected volume has been removed "
            f"and this path is a directory on a DIFFERENT disk -- writing here "
            f"would put data on that disk under a name that says otherwise."
        )
    if identity.fsid:
        current = _fsid(str(path))
        if current and current != identity.fsid:
            return WRONG_VOLUME, (
                f"{path} reports filesystem id {current}, recorded as "
                f"{identity.fsid}. Same st_dev, different filesystem: a "
                f"replaced volume that landed on the same device number."
            )
    return OK, f"{identity.describe()} is present"


def worst(states: list[str]) -> str:
    """The state that should be reported when several apply at once."""
    for candidate in SEVERITY:
        if candidate in states:
            return candidate
    return UNKNOWN
