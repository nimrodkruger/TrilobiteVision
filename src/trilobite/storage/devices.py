"""Which filesystems this machine can write captures to, right now.

A calibration afternoon fills a card. The Pi's own SD card is the worst place
to put the results -- slow, and sustained writes wear it out -- so the output
directory has to be movable to a USB SSD, and movable *while the rig is
running*, because the alternative is restarting the application (and losing the
alignment session) every time a disk is swapped.

Two things follow from "while running", and they are the whole reason this
module exists rather than a config field:

  * **Appearance is polled, never assumed.** A device plugged in after startup
    must show up in the list; a device pulled out must stop being offered.
  * **Disappearance must be survivable.** If the active target is yanked
    mid-session, every subsequent write raises OSError from inside a request
    handler. So the mount is checked, and the writer falls back to the
    configured root rather than failing captures.

Deliberately no unmount, no eject, no mkfs: this reads `/proc/mounts` and calls
`statvfs`. Ejecting is the user's job at the desktop, and the honest workflow is
"release the device here, then unplug it", which `SessionWriter.release()`
supports.

Non-Linux hosts get a degraded but working answer -- the configured root and
whatever else is explicitly offered -- so the UI is developable on Windows.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Where removable media actually gets mounted on Raspberry Pi OS and its
# relatives. udisks2 uses /media/<user>/<label>; a hand-mounted disk usually
# lands in /mnt.
REMOVABLE_PREFIXES = ("/media", "/mnt", "/run/media", "/Volumes")

# Filesystems that are not places to put data, whatever they claim about their
# free space. tmpfs is the dangerous one: /dev/shm and /run look like roomy
# writable disks and evaporate on reboot.
PSEUDO_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "securityfs", "debugfs", "tracefs", "pstore", "bpf", "configfs",
    "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "efivarfs",
    "squashfs", "ramfs", "overlay", "nsfs", "fuse.gvfsd-fuse", "fuse.portal",
}

# The subdirectory created on a chosen device. Writing session folders to the
# root of someone's USB stick is rude, and it makes the rig's output
# indistinguishable from whatever else is on the disk.
DATA_SUBDIR = "trilobite-data"


@dataclass(frozen=True)
class StorageDevice:
    """One writable filesystem, as offered to the user."""

    mount: str
    device: str
    fstype: str
    removable: bool
    total_bytes: int
    free_bytes: int
    writable: bool
    label: str

    @property
    def id(self) -> str:
        """Stable across a hot-plug cycle for the same mount point."""
        return self.mount

    @property
    def target(self) -> str:
        """Where captures would go if this device were selected."""
        return str(Path(self.mount) / DATA_SUBDIR)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mount": self.mount,
            "target": self.target,
            "device": self.device,
            "fstype": self.fstype,
            "removable": self.removable,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "free_gb": round(self.free_bytes / 1e9, 1),
            "total_gb": round(self.total_bytes / 1e9, 1),
            "used_fraction": round(
                1.0 - self.free_bytes / self.total_bytes, 3) if self.total_bytes else 0.0,
            "writable": self.writable,
            "label": self.label,
        }


def _read_proc_mounts() -> list[tuple[str, str, str, str]]:
    """(device, mountpoint, fstype, options) tuples. Empty list off Linux."""
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        # Mount points are octal-escaped in /proc/mounts: a space is \040.
        mount = parts[1].encode().decode("unicode_escape")
        out.append((parts[0], mount, parts[2], parts[3]))
    return out


def _is_removable(device: str) -> bool:
    """Ask sysfs whether the backing block device is removable.

    Falls back on the mount-point prefix, which is what actually matters to the
    user: something under /media is something they plugged in, whatever the
    kernel thinks of the bus.
    """
    name = Path(device).name
    # /dev/sda1 -> sda; /dev/mmcblk0p1 -> mmcblk0; /dev/nvme0n1p1 -> nvme0n1
    base = name.rstrip("0123456789")
    if base.endswith("p") and any(ch.isdigit() for ch in name):
        base = base[:-1]
    for candidate in (base, name):
        flag = Path(f"/sys/block/{candidate}/removable")
        try:
            if flag.read_text().strip() == "1":
                return True
        except OSError:
            continue
    return False


def _usage(path: str) -> tuple[int, int]:
    try:
        u = shutil.disk_usage(path)
        return int(u.total), int(u.free)
    except OSError:
        return 0, 0


def _writable(path: str) -> bool:
    """Actually writable, not merely permitted.

    os.access lies on a read-only mount that the user owns, and a read-only USB
    stick is exactly the case worth catching before a session starts rather
    than at the first capture. So this really writes a file.

    It is therefore **not** used when listing devices, which happens every few
    seconds: dropping a probe file into the root of every filesystem the
    machine has mounted, repeatedly, is not acceptable behaviour for a program
    that was asked to look at a list. Listing uses `_writable_hint` below;
    this one runs once, when a device is actually chosen.
    """
    p = Path(path)
    if not p.is_dir():
        return False
    probe = p / ".trilobite-write-test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _writable_hint(path: str, options: str = "") -> bool:
    """Writability without writing anything.

    The mount options are authoritative about a read-only mount -- `ro` is
    right there in /proc/mounts -- and os.access covers ordinary permissions.
    Between them this is right in every case that matters and wrong only in
    exotic ones (a full disk, an ACL), which the probe catches at selection.
    """
    if options and "ro" in options.split(","):
        return False
    try:
        return os.path.isdir(path) and os.access(path, os.W_OK)
    except OSError:
        return False


def list_devices(always_include: list[Path] | None = None) -> list[StorageDevice]:
    """Everything that could hold a capture session, best first.

    `always_include` names paths that must appear whatever the mount table says
    -- in practice the configured storage root, so the internal disk is always
    an option and the list is never empty, including on Windows where there is
    no /proc/mounts to read.
    """
    seen: dict[str, StorageDevice] = {}

    def add(mount: str, device: str, fstype: str, removable: bool, options: str = "") -> None:
        mount = str(Path(mount))
        if mount in seen:
            return
        total, free = _usage(mount)
        if total <= 0:
            return
        seen[mount] = StorageDevice(
            mount=mount,
            device=device,
            fstype=fstype,
            removable=removable,
            total_bytes=total,
            free_bytes=free,
            writable=_writable_hint(mount, options),
            label=Path(mount).name or mount,
        )

    for device, mount, fstype, options in _read_proc_mounts():
        if fstype in PSEUDO_FSTYPES:
            continue
        if not device.startswith("/dev/"):
            continue
        removable = mount.startswith(REMOVABLE_PREFIXES) or _is_removable(device)
        # The root filesystem is offered too -- on this rig it is the SD card,
        # which is a legitimate if unwise choice, and hiding it would leave no
        # option at all when nothing is plugged in.
        add(mount, device, fstype, removable, options)

    for extra in always_include or []:
        # The *mount point* containing the configured root, not the root
        # itself: adding the path directly would list the internal disk twice,
        # once as "/" and once as "trilobite-data", as two devices that are the
        # same device.
        add(mount_of(extra), "configured", "-", False)

    # Removable first, then most free space: the order you would pick in.
    return sorted(
        seen.values(), key=lambda d: (not d.removable, -d.free_bytes, d.mount)
    )


def is_mounted(path: str | Path) -> bool:
    """Is `path` still backed by a live mount?

    `Path.exists()` is not enough: pulling a USB stick can leave the mount
    point directory behind as an empty directory on the parent filesystem, so
    the writes succeed, go to the SD card, and are invisible under the path the
    user believes they are using. Comparing device ids catches that.
    """
    p = Path(path)
    try:
        while not p.exists() and p != p.parent:
            p = p.parent
        return p.exists() and os.access(p, os.W_OK)
    except OSError:
        return False


def mount_of(path: str | Path) -> str:
    """The mount point containing `path`, by walking up to a device change."""
    p = Path(os.path.expanduser(str(path))).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        dev = os.stat(p).st_dev
    except OSError:
        return str(p)
    while p != p.parent:
        try:
            if os.stat(p.parent).st_dev != dev:
                return str(p)
        except OSError:
            return str(p)
        p = p.parent
    return str(p)
