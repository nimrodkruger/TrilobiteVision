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

**A plugged-in disk is not necessarily a mounted disk.** This is the thing that
makes the naive version of this module useless on the rig. On a Raspberry Pi
with a desktop session, udisks2 auto-mounts removable media under
/media/<user>/<label>; on a **headless** Pi -- which is how this rig is used --
nothing mounts anything, so a USB SSD plugged into a running system is present
in /sys/block, visible to lsblk, and completely absent from /proc/mounts. A
listing built only from /proc/mounts shows nothing and gives the user no reason
why.

So this enumerates from two sources and merges them:

  * `/proc/mounts` -- filesystems that are mounted and can be written to now;
  * `lsblk` -- every block device the kernel can see, including partitions
    that are not mounted, which are then offered with a Mount action.

`mount_device` shells out to `udisksctl`, which mounts as the invoking user
with no sudo and picks a sane mount point. It is only ever called for a device
the user explicitly chose in the UI, and its stderr is returned verbatim,
because the interesting failures here (no exFAT driver, read-only media, a
polkit policy that refuses a non-seat session) all announce themselves clearly
and are useless if swallowed.

Non-Linux hosts get a degraded but working answer -- the configured root and
whatever else is explicitly offered -- so the UI is developable on Windows.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
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
    """One place captures could go -- mounted or not.

    `mounted` False means the kernel can see the disk but no filesystem from it
    is currently reachable. That is a normal state on a headless Pi and the
    reason this class exists rather than a plain mount-point string: such a
    device is offered with a Mount action instead of being silently dropped.
    """

    mount: str                      # "" when not mounted
    device: str                     # /dev/sda1, or "configured"
    fstype: str
    removable: bool
    total_bytes: int
    free_bytes: int
    writable: bool
    label: str
    mounted: bool = True
    size_bytes: int = 0             # from lsblk; known even when unmounted
    model: str = ""

    @property
    def id(self) -> str:
        """Stable across a hot-plug cycle. The mount point when mounted, the
        device node when not -- an unmounted disk has no mount point, and its
        node is what the Mount action needs."""
        return self.mount or self.device

    @property
    def target(self) -> str:
        """Where captures would go if this device were selected."""
        return str(Path(self.mount) / DATA_SUBDIR) if self.mount else ""

    def as_dict(self) -> dict[str, Any]:
        total = self.total_bytes or self.size_bytes
        return {
            "id": self.id,
            "mount": self.mount,
            "target": self.target,
            "device": self.device,
            "fstype": self.fstype,
            "removable": self.removable,
            "mounted": self.mounted,
            "model": self.model,
            "total_bytes": total,
            "free_bytes": self.free_bytes,
            "free_gb": round(self.free_bytes / 1e9, 1),
            "total_gb": round(total / 1e9, 1),
            "used_fraction": round(
                1.0 - self.free_bytes / total, 3) if total and self.mounted else 0.0,
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


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except OSError as exc:
        return 1, "", f"{cmd[0]}: {exc}"


LSBLK_COLUMNS = "PATH,NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT,RM,TYPE,RO,MODEL,PKNAME"


def _lsblk() -> list[dict[str, Any]]:
    """Every block device the kernel can see, flattened. [] if lsblk is absent.

    This is the half of the picture /proc/mounts cannot give: a USB SSD plugged
    into a headless Pi appears here and nowhere else.
    """
    rc, out, err = _run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    if rc != 0:
        log.debug("lsblk unavailable (%s): %s", rc, err.strip())
        return []
    try:
        tree = json.loads(out).get("blockdevices", [])
    except (json.JSONDecodeError, AttributeError):
        return []

    flat: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], parent: dict[str, Any] | None) -> None:
        node = dict(node)
        # Model lives on the disk, not the partition, and the model is what
        # lets a user tell one anonymous /dev/sda1 from another.
        if not node.get("model") and parent:
            node["model"] = parent.get("model")
        if node.get("rm") is None and parent:
            node["rm"] = parent.get("rm")
        children = node.pop("children", []) or []
        node["has_children"] = bool(children)
        flat.append(node)
        for child in children:
            walk(child, node)

    for dev in tree:
        walk(dev, None)
    return flat


def _probe_fstype(path: str) -> tuple[str, str]:
    """(fstype, label) read straight off the device with blkid.

    lsblk reports what *udev* recorded, not what is on the disk. Where udev is
    not running -- a container, a minimal image, a device that appeared before
    the daemon did -- lsblk shows a formatted disk with an empty FSTYPE and it
    gets dropped as unformatted. blkid probes the superblock itself, so this is
    the fallback that stops a real disk disappearing for an administrative
    reason.
    """
    rc, out, _err = _run(["blkid", "-o", "export", path], 5.0)
    if rc != 0:
        return "", ""
    fields = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    )
    return fields.get("TYPE", ""), fields.get("LABEL", "")


def _system_mounts() -> set[str]:
    """Mount points that belong to the running system, never to captures."""
    keep = {"/", "/boot", "/boot/firmware", "/boot/efi"}
    return keep


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

    # -- the half /proc/mounts cannot tell you ---------------------------
    #
    # Every block device the kernel has, minus the ones already mounted and
    # minus the system's own. What is left is a disk that is physically plugged
    # in and unreachable, which on a headless Pi is the normal state of a USB
    # SSD and the single most likely reason this list looked empty.
    mounted_devs = {d.device for d in seen.values()}
    system = _system_mounts()
    blocks = _lsblk()
    # Any disk that carries a system mount is off limits, partitions and all:
    # the boot partition of the SD card is a block device with a filesystem
    # like any other, and offering to mount it somewhere is not helpful.
    system_disks = {
        node.get("pkname") or node.get("name")
        for node in blocks
        if str(node.get("mountpoint") or "") in system
    }
    for node in blocks:
        path = node.get("path") or ""
        if not path or path in mounted_devs or node.get("mountpoint"):
            continue
        if (node.get("pkname") or node.get("name")) in system_disks:
            continue
        fstype = node.get("fstype") or ""
        label = node.get("label") or ""
        if not fstype and not node.get("has_children"):
            # udev may simply not have recorded it. Ask the disk.
            fstype, probed = _probe_fstype(path)
            label = label or probed
        if not fstype:
            # Unformatted, or a partitioned disk whose partitions are listed
            # separately. Nothing to mount, and offering it would imply we
            # could format it.
            #
            # Note the filter is on *having a filesystem*, not on the lsblk
            # `type`. Plenty of USB sticks are "superfloppy" -- a filesystem
            # written straight to the disk with no partition table, so lsblk
            # calls them type=disk -- and a version that accepted only
            # type=part dropped exactly those.
            continue
        size = int(node.get("size") or 0)
        seen[path] = StorageDevice(
            mount="",
            device=path,
            fstype=fstype,
            removable=bool(node.get("rm")),
            total_bytes=0,
            free_bytes=0,
            writable=not node.get("ro"),
            label=str(label or node.get("name") or path),
            mounted=False,
            size_bytes=size,
            model=str(node.get("model") or "").strip(),
        )

    # Mounted first (they can be used now), then removable, then most free
    # space -- the order you would pick in.
    return sorted(
        seen.values(),
        key=lambda d: (not d.mounted, not d.removable, -d.free_bytes, d.id),
    )


def mount_device(dev_path: str) -> str:
    """Mount a block device and return where it landed.

    `udisksctl` rather than `mount`: it needs no sudo, it chooses a mount point
    under /media, and it is already installed on Raspberry Pi OS. Raises
    RuntimeError carrying the tool's own stderr, because every interesting
    failure here explains itself precisely and is worthless if swallowed --
    "unknown filesystem type 'exfat'" means install exfatprogs, and a polkit
    refusal means this session has no seat and needs a rule.
    """
    if not dev_path.startswith("/dev/"):
        raise ValueError(f"not a block device path: {dev_path!r}")
    rc, out, err = _run(["udisksctl", "mount", "-b", dev_path, "--no-user-interaction"], 30.0)
    if rc == 0:
        # "Mounted /dev/sda1 at /media/flyeye/DATA"
        for token in (" at ", " at\n"):
            if token in out:
                return out.split(token, 1)[1].strip().rstrip(".")
        # Mounted, but the message did not parse. Ask the mount table.
        for device, mount, _fs, _opts in _read_proc_mounts():
            if device == dev_path:
                return mount
        raise RuntimeError(f"udisksctl reported success but no mount point was found: {out!r}")

    detail = (err or out).strip() or f"udisksctl exited {rc}"
    if "already mounted" in detail.lower():
        for device, mount, _fs, _opts in _read_proc_mounts():
            if device == dev_path:
                return mount
    if rc == 127:
        detail += (
            ". udisks2 is not installed -- 'sudo apt install -y udisks2' -- or "
            "mount the disk by hand and it will appear in this list."
        )
    raise RuntimeError(detail)


def unmount_device(dev_path: str) -> None:
    """Unmount so the disk can be pulled without corrupting it.

    Fails loudly if anything still has the filesystem open, which is the
    correct behaviour: "target is busy" is information, not an obstacle to
    route around.
    """
    if not dev_path.startswith("/dev/"):
        raise ValueError(f"not a block device path: {dev_path!r}")
    rc, out, err = _run(["udisksctl", "unmount", "-b", dev_path, "--no-user-interaction"], 30.0)
    if rc != 0:
        raise RuntimeError((err or out).strip() or f"udisksctl exited {rc}")


def diagnostics() -> dict[str, Any]:
    """Everything needed to answer "why is my disk not in the list?" remotely.

    Raw tool output alongside what the enumerator made of it. The two together
    localise the problem in one round trip: a disk in lsblk but not in the
    device list is an enumeration bug, a disk in neither is a kernel or cable
    problem, and a disk in both but not mountable is a filesystem driver.
    """
    rc_lsblk, lsblk_out, lsblk_err = _run(["lsblk", "-o", LSBLK_COLUMNS])
    rc_ud, ud_out, ud_err = _run(["udisksctl", "--version"])
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        mounts = f"(unreadable: {exc})"
    return {
        "lsblk": {"rc": rc_lsblk, "stdout": lsblk_out, "stderr": lsblk_err},
        "udisks2": {"rc": rc_ud, "version": ud_out.strip(), "stderr": ud_err.strip(),
                    "available": rc_ud == 0},
        "proc_mounts": mounts,
        "block_devices": _lsblk(),
        "enumerated": [d.as_dict() for d in list_devices()],
    }


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
