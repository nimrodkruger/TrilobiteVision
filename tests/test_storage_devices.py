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
