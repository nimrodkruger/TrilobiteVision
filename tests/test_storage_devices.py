"""Hot-pluggable output storage.

The interesting case is not "can it write to a chosen directory" -- it is what
happens when that directory goes away mid-session. Pulling a USB stick leaves
the mount point behind as an ordinary empty directory on the parent
filesystem, so writes keep *succeeding*, quietly, onto the SD card, under a
path that says otherwise. Two of the tests below are that scenario.
"""

from __future__ import annotations

import numpy as np
import pytest

from trilobite.config import StorageConfig
from trilobite.storage import devices
from trilobite.storage.writer import SessionWriter
from trilobite.types import Frame


@pytest.fixture
def writer(tmp_path):
    return SessionWriter(StorageConfig(root=str(tmp_path / "internal")), tmp_path / "internal")


def frame():
    return Frame.now(np.zeros((8, 8), dtype=np.uint8), "left", 1)


# -- enumeration ------------------------------------------------------------


def test_list_devices_always_offers_the_configured_root(tmp_path):
    found = devices.list_devices([tmp_path])
    assert found, "the configured root must always be offered, even off Linux"
    assert any(str(tmp_path).startswith(d.mount) for d in found)


def test_devices_report_capacity_and_a_target_subdirectory(tmp_path):
    d = next(d for d in devices.list_devices([tmp_path]) if str(tmp_path).startswith(d.mount))
    assert d.total_bytes > 0
    assert d.writable
    assert d.target.endswith(devices.DATA_SUBDIR)


def test_the_configured_root_does_not_appear_as_a_second_copy_of_its_disk(tmp_path):
    """The configured root lives on some filesystem that is already in the
    mount table. Adding the path itself would list one disk twice, as "/" and
    as "trilobite-data", and offering the user a choice between a thing and
    itself is worse than offering nothing."""
    found = devices.list_devices([tmp_path / "data"])
    mounts = [d.mount for d in found]
    assert len(mounts) == len(set(mounts))
    assert devices.mount_of(tmp_path / "data") in mounts


def test_listing_writes_nothing_to_any_filesystem(tmp_path, monkeypatch):
    """Listing happens every few seconds. Dropping a probe file into the root
    of every mounted filesystem that often is not acceptable, so the listing
    path must never call the probe."""
    monkeypatch.setattr(
        devices, "_writable", lambda p: pytest.fail("listing must not write a probe file")
    )
    devices.list_devices([tmp_path])


def test_a_read_only_mount_is_reported_unwritable_without_probing():
    assert devices._writable_hint("/", "ro,relatime") is False


def test_pseudo_filesystems_are_never_offered():
    # /dev/shm is a tmpfs: writable, roomy-looking, and gone on reboot.
    assert not any(d.mount == "/dev/shm" for d in devices.list_devices())


# -- retargeting ------------------------------------------------------------


def test_retarget_creates_a_new_session_directory_on_the_target(writer, tmp_path):
    stick = tmp_path / "stick"
    stick.mkdir()
    before = writer.session_dir
    state = writer.retarget(stick)
    assert writer.session_dir != before
    assert str(writer.session_dir).startswith(str(stick))
    assert state["root"] == str(stick)
    # Nothing is moved or deleted -- the earlier directory is still there.
    assert before.exists()


def test_retarget_refuses_an_unwritable_target(writer, tmp_path, monkeypatch):
    """A read-only USB stick must be refused at selection, not at the first
    capture. The probe is patched rather than chmod-ing a directory: root
    ignores the permission bits, so the real check would pass in CI and the
    test would assert nothing."""
    target = tmp_path / "ro"
    target.mkdir()
    monkeypatch.setattr(devices, "_writable", lambda p: False)
    with pytest.raises(ValueError, match="not writable"):
        writer.retarget(target)
    assert writer.root == writer.default_root


def test_release_returns_to_the_configured_root(writer, tmp_path):
    stick = tmp_path / "stick"
    stick.mkdir()
    writer.retarget(stick)
    writer.release()
    assert writer.root == writer.default_root
    assert str(writer.session_dir).startswith(str(writer.default_root))


def test_the_manifest_follows_the_session_to_a_new_device(writer, tmp_path):
    writer.write_session_manifest({"started": 1})
    stick = tmp_path / "stick"
    stick.mkdir()
    writer.retarget(stick)
    assert (writer.session_dir / "session.json").exists()


# -- survival ---------------------------------------------------------------


def test_a_vanished_device_falls_back_to_the_internal_root(writer, tmp_path, monkeypatch):
    stick = tmp_path / "stick"
    stick.mkdir()
    writer.retarget(stick)
    # The mount point survives the unplug as an empty directory; only the
    # device behind it is gone. That is exactly the silent case.
    monkeypatch.setattr(devices, "is_mounted", lambda p: False)

    assert writer.check_and_recover() is True
    assert writer.root == writer.default_root
    assert writer.notes and "disappeared" in writer.notes[-1]


def test_recovery_is_a_no_op_while_the_device_is_healthy(writer, tmp_path):
    stick = tmp_path / "stick"
    stick.mkdir()
    writer.retarget(stick)
    assert writer.check_and_recover() is False
    assert writer.root == stick


def test_a_capture_is_never_lost_when_the_device_goes_away_mid_write(
    writer, tmp_path, monkeypatch
):
    stick = tmp_path / "stick"
    stick.mkdir()
    writer.retarget(stick)

    calls = {"n": 0}
    real = writer._write_image

    def flaky(stem, f):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "Input/output error")
        return real(stem, f)

    monkeypatch.setattr(writer, "_write_image", flaky)
    monkeypatch.setattr(devices, "is_mounted", lambda p: False)

    out = writer.save_still(frame())
    assert calls["n"] == 2
    assert out["image"].startswith(str(writer.default_root))


def test_state_reports_where_output_is_going(writer):
    s = writer.state()
    assert s["root"] == str(writer.default_root)
    assert s["session_dir"].startswith(s["root"])
    assert s["removable"] is False
    assert s["total_bytes"] > 0


# -- devices that are plugged in but not mounted ----------------------------
#
# The failure this covers: a USB SSD plugged into a headless Pi is visible to
# the kernel and mounted by nothing, because no desktop session exists to
# auto-mount it. A listing built only from /proc/mounts shows nothing, which is
# what happened on the rig.

LSBLK_HEADLESS_PI = {
    "blockdevices": [
        {"path": "/dev/mmcblk0", "name": "mmcblk0", "size": 31914983424, "fstype": None,
         "label": None, "mountpoint": None, "rm": False, "type": "disk", "ro": False,
         "model": None, "pkname": None,
         "children": [
             {"path": "/dev/mmcblk0p1", "name": "mmcblk0p1", "size": 536870912,
              "fstype": "vfat", "label": "bootfs", "mountpoint": "/boot/firmware",
              "rm": False, "type": "part", "ro": False, "model": None, "pkname": "mmcblk0"},
             {"path": "/dev/mmcblk0p2", "name": "mmcblk0p2", "size": 31377096704,
              "fstype": "ext4", "label": "rootfs", "mountpoint": "/", "rm": False,
              "type": "part", "ro": False, "model": None, "pkname": "mmcblk0"},
         ]},
        {"path": "/dev/sda", "name": "sda", "size": 1000204886016, "fstype": None,
         "label": None, "mountpoint": None, "rm": True, "type": "disk", "ro": False,
         "model": "Samsung PSSD T7", "pkname": None,
         "children": [
             {"path": "/dev/sda1", "name": "sda1", "size": 1000202788352,
              "fstype": "exfat", "label": "TRILOBITE", "mountpoint": None, "rm": None,
              "type": "part", "ro": False, "model": None, "pkname": "sda"},
         ]},
    ]
}


@pytest.fixture
def headless_pi(monkeypatch):
    monkeypatch.setattr(devices, "_lsblk", lambda: _flatten(LSBLK_HEADLESS_PI))
    monkeypatch.setattr(devices, "_read_proc_mounts", lambda: [
        ("/dev/mmcblk0p2", "/", "ext4", "rw,relatime"),
        ("/dev/mmcblk0p1", "/boot/firmware", "vfat", "rw,relatime"),
        ("proc", "/proc", "proc", "rw"),
    ])


def _flatten(tree):
    out = []

    def walk(node, parent):
        node = dict(node)
        if not node.get("model") and parent:
            node["model"] = parent.get("model")
        if node.get("rm") is None and parent:
            node["rm"] = parent.get("rm")
        kids = node.pop("children", []) or []
        out.append(node)
        for k in kids:
            walk(k, node)

    for d in tree["blockdevices"]:
        walk(d, None)
    return out


def test_an_unmounted_usb_disk_is_offered(headless_pi):
    found = devices.list_devices()
    stick = next((d for d in found if d.device == "/dev/sda1"), None)
    assert stick is not None, "a plugged-in but unmounted disk must still be listed"
    assert stick.mounted is False
    assert stick.removable is True
    assert stick.label == "TRILOBITE"
    assert stick.model == "Samsung PSSD T7"
    assert stick.size_bytes > 0


def test_an_unmounted_device_is_identified_by_its_node_not_a_mount_point(headless_pi):
    stick = next(d for d in devices.list_devices() if d.device == "/dev/sda1")
    assert stick.id == "/dev/sda1"
    assert stick.target == ""          # nowhere to write until it is mounted


def test_system_partitions_are_never_offered_for_mounting(headless_pi):
    ids = {d.id for d in devices.list_devices()}
    assert "/dev/mmcblk0p1" not in ids, "the boot partition is not a capture target"


def test_mounted_filesystems_still_come_first(headless_pi):
    found = devices.list_devices()
    assert found[0].mounted is True


def test_mount_device_returns_where_it_landed(monkeypatch):
    monkeypatch.setattr(
        devices, "_run",
        lambda cmd, timeout=10.0: (0, "Mounted /dev/sda1 at /media/flyeye/TRILOBITE.\n", ""),
    )
    assert devices.mount_device("/dev/sda1") == "/media/flyeye/TRILOBITE"


def test_mount_failure_surfaces_the_tools_own_words(monkeypatch):
    """"unknown filesystem type 'exfat'" tells you to install exfatprogs.
    Anything vaguer costs an afternoon."""
    monkeypatch.setattr(
        devices, "_run",
        lambda cmd, timeout=10.0: (
            1, "", "Error mounting /dev/sda1: unknown filesystem type 'exfat'"),
    )
    with pytest.raises(RuntimeError, match="exfat"):
        devices.mount_device("/dev/sda1")


def test_a_missing_udisks_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(
        devices, "_run", lambda cmd, timeout=10.0: (127, "", "udisksctl: not found"))
    with pytest.raises(RuntimeError, match="apt install"):
        devices.mount_device("/dev/sda1")


def test_mount_refuses_anything_that_is_not_a_block_device():
    with pytest.raises(ValueError):
        devices.mount_device("/media/stick")


def test_diagnostics_reports_all_three_sources(monkeypatch):
    monkeypatch.setattr(devices, "_run", lambda cmd, timeout=10.0: (0, "out", ""))
    monkeypatch.setattr(devices, "_lsblk", lambda: [])
    d = devices.diagnostics()
    assert {"lsblk", "udisks2", "proc_mounts", "block_devices", "enumerated"} <= set(d)
    assert d["udisks2"]["available"] is True


# -- durability -------------------------------------------------------------
#
# The field failure these exist for: a session directory on an external drive
# holding correctly named, correctly placed, ZERO-BYTE .npy and .json files,
# while session.json was intact. Nothing raised; every capture reported
# "saved". The cause is that close() does not write to a disk -- it hands the
# bytes to the page cache and returns. Metadata takes a different route and is
# journalled promptly, so the names survive a pulled disk and the data does
# not. session.json survived because it is written at startup and writeback had
# had minutes to flush it.


def test_a_capture_is_fsynced_before_it_is_reported_saved(writer, monkeypatch):
    """The mechanism, asserted directly: fsync must be called on the file, and
    on the directory holding it, before save_still returns."""
    import os as _os

    synced = []
    real_fsync = _os.fsync
    monkeypatch.setattr(_os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    out = writer.save_still(frame())
    # image + image's directory + sidecar + sidecar's directory
    assert len(synced) >= 4, synced
    assert out["bytes"] > 0


def test_save_still_reports_the_size_on_disk(writer):
    out = writer.save_still(frame())
    from pathlib import Path as _P
    assert out["bytes"] == _P(out["image"]).stat().st_size
    assert out["bytes"] > 128            # a .npy header alone is 128 bytes


def test_an_empty_file_is_an_error_not_a_success(writer, monkeypatch):
    """The whole point. A filesystem that accepts every byte and stores none
    must produce a failure the operator sees at the rig, not a directory of
    empty files discovered at the desk a day later."""
    from trilobite.storage import writer as W

    monkeypatch.setattr(W, "write_durably", lambda path, payload: (path.write_bytes(b""), 0)[1])
    monkeypatch.setattr(devices, "is_mounted", lambda p: True)

    with pytest.raises(W.EmptyWriteError) as exc:
        writer.save_still(frame())
    assert "0 bytes" in str(exc.value)


def test_a_short_write_is_an_error_too(writer, monkeypatch):
    """Not only zero. A device that is full, or failing, or lying about its
    writes gives a file of the wrong length, and that is equally not a save."""
    from trilobite.storage import writer as W

    def truncating(path, payload):
        path.write_bytes(payload[: len(payload) // 2])
        return path.stat().st_size

    monkeypatch.setattr(W, "write_durably", truncating)
    monkeypatch.setattr(devices, "is_mounted", lambda p: True)

    with pytest.raises(W.EmptyWriteError) as exc:
        writer.save_still(frame())
    assert "bytes on disk but" in str(exc.value)


def test_an_empty_write_falls_back_to_the_internal_disk(writer, tmp_path, monkeypatch):
    """EmptyWriteError is an OSError deliberately, so it takes the existing
    recovery path: a device that just silently discarded a frame is one the
    rest of the session must not be written to either."""
    from trilobite.storage import writer as W

    stick = tmp_path / "stick"
    stick.mkdir()
    writer.retarget(stick)

    calls = {"n": 0}
    real = W.write_durably

    def once_empty(path, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            path.write_bytes(b"")
            return 0
        return real(path, payload)

    monkeypatch.setattr(W, "write_durably", once_empty)
    monkeypatch.setattr(devices, "is_mounted", lambda p: False)

    out = writer.save_still(frame())
    assert out["image"].startswith(str(writer.default_root))
    assert out["bytes"] > 128
    assert any("recovering" in n for n in writer.notes)


def test_the_written_file_actually_loads_back(writer):
    """End to end, because a size check is not a content check."""
    import numpy as _np

    f = Frame.now(_np.arange(64, dtype=_np.uint8).reshape(8, 8), "left", 1)
    out = writer.save_still(f)
    assert _np.array_equal(_np.load(out["image"]), f.data)


def test_the_session_manifest_is_durable_too(writer):
    """It survived the field failure by luck -- it is written at startup, so
    writeback had flushed it. A manifest written after a retarget has no such
    head start."""
    import os as _os

    synced = []
    real_fsync = _os.fsync
    import trilobite.storage.writer as W
    monkeypatch_target = W.os
    orig = monkeypatch_target.fsync
    monkeypatch_target.fsync = lambda fd: (synced.append(fd), real_fsync(fd))[1]
    try:
        path = writer.write_session_manifest({"hello": "world"})
    finally:
        monkeypatch_target.fsync = orig
    assert path.stat().st_size > 0
    assert len(synced) >= 2


def test_verify_device_passes_on_a_working_filesystem(tmp_path):
    from trilobite.storage.writer import verify_device

    r = verify_device(tmp_path, size_bytes=1 << 20)
    assert r["ok"] is True
    assert r["on_disk"] == 1 << 20
    assert r["write_mb_s"] > 0
    assert not list(tmp_path.glob(".trilobite-verify-*")), "the probe must be cleaned up"


def test_verify_device_fails_on_a_filesystem_that_stores_nothing(tmp_path, monkeypatch):
    """The exact field failure, simulated: writes are accepted and discarded."""
    from trilobite.storage import writer as W

    monkeypatch.setattr(W, "write_durably", lambda path, payload: (path.write_bytes(b""), 0)[1])
    r = W.verify_device(tmp_path, size_bytes=1 << 20)
    assert r["ok"] is False
    assert "0 bytes" in r["message"]


def test_verify_device_fails_when_the_bytes_come_back_wrong(tmp_path, monkeypatch):
    from trilobite.storage import writer as W

    def corrupting(path, payload):
        path.write_bytes(bytes(len(payload)))      # right length, wrong content
        return path.stat().st_size

    monkeypatch.setattr(W, "write_durably", corrupting)
    r = W.verify_device(tmp_path, size_bytes=1 << 16)
    assert r["ok"] is False
    assert "corrupting" in r["message"]
