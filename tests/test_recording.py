"""Stage 5b and 5c: the burst recorder, the continuous recorder, and the one
invariant both exist to hold.

    Every exposed frame is either in the output or counted in the manifest as
    a dropped frame inside a named interval. No frame is unaccounted for.

Almost everything below is a test of that sentence or of one of the two rules
that make it meaningful: that nothing may overwrite an unsaved burst, and that
a recording's pixel format, geometry and requested rate are fixed before Start
and do not change while it runs.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from trilobite.config import StorageConfig
from trilobite.recording import buffer as buf_mod
from trilobite.recording import continuous as cont_mod
from trilobite.recording.burst import CAPTURED, SAVED, BurstRecorder
from trilobite.recording.chunks import ChunkWriter, gaps_from, npy_header
from trilobite.recording.continuous import ContinuousRecorder
from trilobite.recording.ladder import RAW8, RAW16, Plan, enumerate_plans
from trilobite.recording.manager import RecordingManager
from trilobite.recording.preflight import available_format_keys, plan_from
from trilobite.storage.writer import SessionWriter, StorageRefused

W, H = 32, 24


@pytest.fixture
def writer(tmp_path):
    cfg = StorageConfig(
        root=str(tmp_path / "internal"),
        # Small so the tests exercise rollover and blocks rather than one file.
        chunk_frames=4, write_block_frames=2,
        reserve_mb=1, internal_reserve_mb=1,
    )
    w = SessionWriter(cfg, tmp_path / "internal")
    stick = tmp_path / "stick"
    stick.mkdir()
    w.retarget(stick)
    return w


def plan(fmt=RAW16, fps=30.0, heads=("left", "right"), duration=None):
    return Plan(fmt=fmt, fps=fps, heads=heads, width=W, height=H,
                duration_s=duration)


def meta(seq, validity="science"):
    from trilobite.recording.buffer import FrameMeta
    return FrameMeta(seq=seq, t_mono=seq / 30.0, t_wall=1e9 + seq / 30.0,
                     sensor_timestamp=seq * 33_333_333, validity=validity)


def pixels(value, dtype=np.uint16):
    return np.full((H, W), value, dtype=dtype)


# -- the arithmetic ---------------------------------------------------------


def test_the_plan_arithmetic_matches_the_numbers_in_the_plan_document():
    """1456 x 1088, two heads, 30 fps, uint16 -> 190 MB/s. If this number ever
    changes, every sizing decision in Stage 5 changes with it."""
    p = Plan(fmt=RAW16, fps=30.0, heads=("left", "right"),
             width=1456, height=1088)
    assert p.bytes_per_frame == 1456 * 1088 * 2
    assert round(p.mb_s) == 190
    assert round(Plan(fmt=RAW8, fps=30.0, heads=("left", "right"),
                      width=1456, height=1088).mb_s) == 95


def test_an_unmeasured_target_predicts_unknown_drops_not_zero():
    """The one answer this must never give by omission."""
    p = plan()
    assert p.predicted_drop_fraction(None) is None
    assert p.predicted_drop_fraction(0.0) is None
    assert p.predicted_drop_fraction(p.mb_s * 2) == 0.0


def test_the_predicted_drop_fraction_is_the_shortfall():
    p = Plan(fmt=RAW16, fps=30.0, heads=("a", "b"), width=1456, height=1088)
    half = p.mb_s / 2
    assert p.predicted_drop_fraction(half) == pytest.approx(0.5, abs=1e-6)


def test_a_format_that_was_not_established_is_not_offered():
    """The rule that keeps packed 10-bit out of the UI on a rig whose sensor
    does not provide it."""
    rows = enumerate_plans(heads=("left",), width=W, height=H, sensor_fps=30.0,
                           available_formats=None)
    assert {r["format"] for r in rows} == {"raw16"}

    caps_no_8bit = {"left": {"eight_bit_available": False,
                             "packed_available": False}}
    assert available_format_keys(caps_no_8bit) == {"raw16"}
    caps_8bit = {"left": {"eight_bit_available": True,
                          "packed_available": False}}
    assert available_format_keys(caps_8bit) == {"raw16", "raw8"}


def test_eight_bit_is_offered_only_when_every_head_has_it():
    """A plan spans the heads, so a rung one head cannot supply is not a rung."""
    mixed = {"left": {"eight_bit_available": True, "packed_available": False},
             "right": {"eight_bit_available": False, "packed_available": False}}
    assert available_format_keys(mixed) == {"raw16"}


def test_every_configuration_names_dropping_as_its_only_runtime_mechanism():
    rows = enumerate_plans(heads=("left",), width=W, height=H, sensor_fps=30.0,
                           available_formats={"raw16", "raw8"})
    for row in rows:
        runtime = [m for m in row["mechanisms"] if m["when"] == "runtime"]
        assert [m["mechanism"] for m in runtime] == ["drop"]


def test_a_lossy_format_can_never_claim_science():
    assert RAW8.validity == "diagnostic"
    assert RAW8.lossy
    assert plan_from("raw8", 30.0, ("left",), W, H).fmt.validity == "diagnostic"


def test_an_unknown_format_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="unknown recording format"):
        plan_from("raw12_wishful", 30.0, ("left",), W, H)


# -- the committed buffer ---------------------------------------------------


def test_a_buffer_is_not_armed_until_its_pages_are_committed():
    b = buf_mod.FrameBuffer(8, (H, W))
    assert not b.prefaulted
    report = b.prefault()
    assert b.prefaulted
    assert report["bytes"] == 8 * H * W * 2


def test_prefaulting_is_checked_against_rss_and_not_merely_attempted(monkeypatch):
    """`np.empty` succeeding proves nothing. If the pages did not become
    resident, arming must fail here rather than as an OOM kill at frame 400."""
    # Big enough that the RSS check applies: two samples of /proc/self/statm
    # milliseconds apart differ by whatever else the process did, which is the
    # same order as a few-megabyte buffer, so the check has a floor.
    b = buf_mod.FrameBuffer(32, (1088, 1456))
    assert b.nbytes > buf_mod.PREFAULT_CHECK_MIN_BYTES
    seq = iter([1_000_000, 1_000_000])          # RSS did not move
    monkeypatch.setattr(buf_mod, "rss_bytes", lambda: next(seq))
    with pytest.raises(buf_mod.PrefaultFailed, match="memory is not there"):
        b.prefault()


def test_a_full_buffer_refuses_rather_than_wrapping():
    b = buf_mod.FrameBuffer(2, (H, W))
    assert b.store(pixels(1), meta(1))
    assert b.store(pixels(2), meta(2))
    assert b.store(pixels(3), meta(3)) is False
    assert b.count == 2
    # And the first frame is still the first frame: a ring would have replaced
    # it, making the contents depend on when the recording stopped.
    assert int(b.view()[0, 0, 0]) == 1


def test_a_frame_of_the_wrong_shape_is_refused():
    b = buf_mod.FrameBuffer(2, (H, W))
    with pytest.raises(ValueError, match="breaks every reader"):
        b.store(np.zeros((H + 1, W), np.uint16), meta(1))


# -- chunks and the index ---------------------------------------------------


def test_the_npy_header_is_a_fixed_length_so_a_short_chunk_can_be_corrected():
    full = npy_header((128, H, W), np.dtype(np.uint16))
    short = npy_header((37, H, W), np.dtype(np.uint16))
    assert len(full) == len(short) == 128


def test_a_short_final_chunk_declares_its_true_frame_count(tmp_path):
    cw = ChunkWriter(tmp_path / "r", "left", (H, W), np.uint16, chunk_frames=4)
    for n in range(1, 7):
        cw.write_frame(pixels(n), meta(n))
    cw.close()
    files = sorted((tmp_path / "r").glob("*.npy"))
    assert [np.load(f, mmap_mode="r").shape[0] for f in files] == [4, 2]


def test_the_index_carries_sequence_numbers_as_exposed_not_as_stored(tmp_path):
    """The whole drop-accounting design rests on this. Renumbering on the way
    in would erase the only record of the loss."""
    cw = ChunkWriter(tmp_path / "r", "left", (H, W), np.uint16, chunk_frames=8)
    for n in (1, 2, 3, 7, 8):               # 4, 5, 6 dropped
        cw.write_frame(pixels(n), meta(n))
    cw.close()
    index = json.loads(
        next((tmp_path / "r").glob("*.index.json")).read_text())
    assert [f["seq"] for f in index["frames"]] == [1, 2, 3, 7, 8]
    assert index["gaps"] == [
        {"first_seq": 4, "last_seq": 6, "frames": 3,
         "seconds": round(4 / 30.0, 4)}]


def test_a_gap_is_measured_from_the_timestamps_either_side_of_it():
    """Not from the nominal frame interval -- which is exactly the assumption a
    dropped-frame recording invalidates."""
    metas = [meta(1), meta(10)]
    gaps = gaps_from(metas)
    assert len(gaps) == 1
    assert gaps[0].frames == 8
    assert gaps[0].seconds == pytest.approx(9 / 30.0, abs=1e-9)


def test_geometry_and_depth_cannot_change_mid_recording(tmp_path):
    """The invariant from ladder.py, checked where it can be violated."""
    cw = ChunkWriter(tmp_path / "r", "left", (H, W), np.uint16, chunk_frames=8)
    cw.write_frame(pixels(1), meta(1))
    with pytest.raises(ValueError, match="Geometry does not change"):
        cw.write_frame(np.zeros((H, W + 2), np.uint16), meta(2))
    with pytest.raises(ValueError, match="Sample depth does not change"):
        cw.write_frame(pixels(3, dtype=np.uint8), meta(3))


def test_the_chunk_summary_accounts_for_every_exposed_frame(tmp_path):
    cw = ChunkWriter(tmp_path / "r", "left", (H, W), np.uint16, chunk_frames=4)
    for n in (1, 2, 5, 6, 7):
        cw.write_frame(pixels(n), meta(n))
    cw.close()
    s = cw.summary()
    assert s["frames_stored"] == 5
    assert s["frames_exposed"] == 7
    assert s["frames_dropped"] == 2
    assert s["longest_gap_frames"] == 2


def test_a_non_science_frame_is_named_rather_than_merely_counted(tmp_path):
    cw = ChunkWriter(tmp_path / "r", "left", (H, W), np.uint16, chunk_frames=8)
    cw.write_frame(pixels(1), meta(1))
    cw.write_frame(pixels(2), meta(2, validity="unvalidated"))
    cw.close()
    s = cw.summary()
    assert s["all_science"] is False
    assert s["first_non_science_seq"] == 2


# -- the burst recorder -----------------------------------------------------


@pytest.fixture
def burst(writer):
    return BurstRecorder(writer.cfg, writer)


def test_a_burst_is_sized_from_measured_memory_not_a_constant(burst, monkeypatch):
    monkeypatch.setattr("trilobite.health.memory_available_mb", lambda: 100.0)
    small = burst.capacity_for(plan())["frames_per_head"]
    monkeypatch.setattr("trilobite.health.memory_available_mb", lambda: 800.0)
    large = burst.capacity_for(plan())["frames_per_head"]
    assert large > small * 4


def test_the_armed_duration_is_reported_before_start(burst):
    report = burst.arm(plan(), frames=60)
    assert report["frames_per_head"] == 60
    assert report["duration_s"] == pytest.approx(2.0)
    assert all(b["prefaulted"] for b in report["buffers"].values())


def test_a_full_buffer_stops_the_burst_and_names_the_reason(burst):
    burst.arm(plan(heads=("left",)), frames=3)
    burst.start()
    for n in range(1, 5):
        burst.offer("left", pixels(n), meta(n))
    assert burst.state == CAPTURED
    assert burst.stop_reason == "buffer full"
    assert burst.status()["frames"]["left"] == 3


def test_captured_says_in_words_that_ram_is_not_storage(burst):
    burst.arm(plan(heads=("left",)), frames=4)
    burst.start()
    burst.offer("left", pixels(1), meta(1))
    burst.stop()
    status = burst.status()
    assert status["unsaved"] is True
    assert "NOT SAVED" in status["headline"]
    assert "lost on a process restart" in status["headline"]


def test_nothing_may_overwrite_an_unsaved_burst(burst):
    burst.arm(plan(heads=("left",)), frames=4)
    burst.start()
    burst.offer("left", pixels(1), meta(1))
    burst.stop()
    with pytest.raises(RuntimeError, match="has not been saved"):
        burst.arm(plan(heads=("left",)), frames=4)
    with pytest.raises(RuntimeError, match="unsaved burst"):
        burst.disarm()


def test_discarding_an_unsaved_burst_is_an_explicit_act(burst):
    burst.arm(plan(heads=("left",)), frames=4)
    burst.start()
    burst.offer("left", pixels(1), meta(1))
    burst.stop()
    burst.discard()
    assert burst.unsaved is False
    burst.arm(plan(heads=("left",)), frames=4)        # now allowed


def test_a_flush_that_would_breach_the_reserve_is_refused_before_it_writes(
    burst, monkeypatch
):
    """The one thing a burst can do that a continuous recording cannot: the
    exact size is known, so the refusal happens while the data is still in RAM
    and can go somewhere else."""
    burst.arm(plan(heads=("left",)), frames=4)
    burst.start()
    for n in range(1, 4):
        burst.offer("left", pixels(n), meta(n))
    burst.stop()

    from trilobite.storage import devices
    monkeypatch.setattr(devices, "_usage", lambda p: (1 << 30, 1 << 10))
    with pytest.raises(StorageRefused) as exc:
        burst.flush()
    assert exc.value.state == "full"
    # Still in RAM, still unsaved, and the frames are still there.
    assert burst.state == CAPTURED
    assert burst.unsaved
    assert burst.status()["frames"]["left"] == 3


def test_a_flush_to_internal_storage_needs_a_per_save_override(burst, writer):
    writer.release(timeout=2.0)                   # back to the internal disk
    burst.arm(plan(heads=("left",)), frames=4)
    burst.start()
    for n in range(1, 4):
        burst.offer("left", pixels(n), meta(n))
    burst.stop()

    with pytest.raises(StorageRefused) as exc:
        burst.flush()
    assert exc.value.state == "internal-refused"
    assert burst.unsaved
    out = burst.flush(internal_ok=True)
    assert burst.state == SAVED
    assert out["heads"]["left"]["frames_stored"] == 3


def test_a_saved_burst_writes_chunks_an_index_and_a_journal(burst):
    burst.arm(plan(heads=("left", "right")), frames=6)
    burst.start()
    for n in range(1, 6):
        burst.offer("left", pixels(n), meta(n))
        burst.offer("right", pixels(n + 100), meta(n))
    burst.stop()
    out = burst.flush()

    root = Path(out["directory"])
    assert (root / "recording.json").exists()
    for head in ("left", "right"):
        chunks = sorted((root / head).glob("*.npy"))
        assert chunks
        assert sum(np.load(c, mmap_mode="r").shape[0] for c in chunks) == 5
        assert sorted((root / head).glob("*.index.json"))
    journal = json.loads((root / "recording.json").read_text())
    assert journal["kind"] == "burst"
    assert journal["synchronised"] is False
    assert journal["storage_generation"] == burst.writer.generation


def test_the_burst_journal_reports_measured_inter_head_timing_not_a_claim(burst):
    burst.arm(plan(heads=("left", "right")), frames=8)
    burst.start()
    for n in range(1, 6):
        burst.offer("left", pixels(n), meta(n))
        burst.offer("right", pixels(n), meta(n))
    burst.stop()
    out = burst.flush()
    offsets = out["inter_head_offset"]
    assert offsets["available"] is True
    assert "Measured, not enforced" in offsets["note"]
    assert offsets["n"] == 5
    assert "none is claimed" in out["synchronisation_note"]


def test_an_empty_burst_is_not_written(burst):
    burst.arm(plan(heads=("left",)), frames=4)
    burst.start()
    burst.stop()
    with pytest.raises(RuntimeError, match="empty"):
        burst.flush()


# -- the continuous recorder ------------------------------------------------


@pytest.fixture
def cont(writer):
    return ContinuousRecorder(writer.cfg, writer)


def _drain(rec, timeout=5.0):
    """Wait for the writer threads to catch up with what was offered."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(h.stored + h.dropped >= h.exposed for h in rec.heads.values()):
            return True
        time.sleep(0.01)
    return False


def test_a_continuous_recording_accounts_for_every_exposed_frame(cont):
    cont.arm(plan(heads=("left",), duration=None), slots=8)
    cont.start()
    for n in range(1, 41):
        cont.offer("left", pixels(n % 50), meta(n))
    _drain(cont)
    out = cont.stop()

    head = out["heads"]["left"]
    assert head["frames_exposed"] == 40
    assert head["frames_stored_counted"] + head["frames_dropped"] == 40
    assert head["accounted"] is True
    assert out["every_frame_accounted"] is True
    assert out["totals"]["frames_exposed"] == 40


def test_frames_on_disk_match_the_frames_the_index_claims(cont):
    cont.arm(plan(heads=("left",)), slots=16)
    cont.start()
    for n in range(1, 21):
        cont.offer("left", pixels(n), meta(n))
    _drain(cont)
    out = cont.stop()

    root = Path(out["directory"]) / "left"
    stored = 0
    seqs: list[int] = []
    for chunk in sorted(root.glob("*.npy")):
        arr = np.load(chunk, mmap_mode="r")
        index = json.loads(chunk.with_suffix(".index.json").read_text())
        assert arr.shape[0] == len(index["frames"]) == index["shape"][0]
        stored += arr.shape[0]
        seqs.extend(f["seq"] for f in index["frames"])
        # The pixel value was the sequence number, so this checks that frame i
        # in the file really is the frame the index says it is.
        for row, frame in zip(arr, index["frames"], strict=True):
            assert int(row[0, 0]) == frame["seq"]
    assert stored == out["heads"]["left"]["frames_stored"]
    assert seqs == sorted(seqs)


def test_a_full_queue_drops_the_newest_frame_and_counts_it(cont):
    """Drop-newest, not a wrapping ring: the retained set must stay a
    prefix-consistent subsample rather than depending on when the stall ended."""
    cont.arm(plan(heads=("left",)), slots=4)
    cont.start()
    # Stop the writer thread so nothing drains, then offer more than the ring
    # holds. The frames that get in are the FIRST ones.
    head = cont.heads["left"]
    head._stop.set()
    head._thread.join(timeout=2.0)

    kept = [cont.offer("left", pixels(n), meta(n)) for n in range(1, 11)]
    assert kept[:4] == [True] * 4
    assert kept[4:] == [False] * 6
    assert head.dropped == 6
    assert head.exposed == 10
    assert head.drop_fraction == pytest.approx(0.6)


def test_the_heads_drop_independently(cont):
    """Coupling the drops would throw away good data to impose a pairing the
    rig does not guarantee."""
    cont.arm(plan(heads=("left", "right")), slots=4)
    cont.start()
    left = cont.heads["left"]
    left._stop.set()
    left._thread.join(timeout=2.0)

    right = cont.heads["right"]
    for n in range(1, 11):
        cont.offer("left", pixels(n), meta(n))
        cont.offer("right", pixels(n), meta(n))
        # Let the right head's writer keep its ring clear, so its drop count
        # is a statement about coupling rather than about scheduling luck.
        deadline = time.monotonic() + 5.0
        while right.ring.in_use and time.monotonic() < deadline:
            time.sleep(0.002)
    assert left.dropped == 6
    assert right.dropped == 0
    out = cont.stop()
    assert out["heads"]["right"]["frames_dropped"] == 0
    assert out["heads"]["left"]["frames_dropped"] == 6


def test_exceeding_the_drop_ceiling_stops_the_recording(cont):
    cont.arm(plan(heads=("left",)), slots=4, max_drop_fraction=0.1)
    cont.start()
    head = cont.heads["left"]
    head._stop.set()
    head._thread.join(timeout=2.0)
    for n in range(1, 101):
        cont.offer("left", pixels(n), meta(n))

    cont.watch()
    assert cont.state == cont_mod.STOPPING
    out = cont.stop()
    assert out["stop_reason"] == cont_mod.STOP_DROP_CEILING
    assert out["complete"] is False


def test_the_ceiling_is_not_applied_before_the_denominator_means_anything(cont):
    cont.arm(plan(heads=("left",)), slots=2, max_drop_fraction=0.1)
    cont.start()
    head = cont.heads["left"]
    head._stop.set()
    head._thread.join(timeout=2.0)
    for n in range(1, 11):
        cont.offer("left", pixels(n), meta(n))
    cont.watch()
    assert cont.state == cont_mod.RECORDING      # 10 frames is not evidence
    cont.stop()


def test_a_lost_target_stops_the_recording_rather_than_diverting_it(
    cont, writer, tmp_path, monkeypatch
):
    cont.arm(plan(heads=("left",)), slots=8)
    cont.start()
    cont.offer("left", pixels(1), meta(1))

    from trilobite.storage import identity
    real = identity.st_dev_of
    monkeypatch.setattr(
        identity, "st_dev_of",
        lambda p: (real(p) or 0) + 1000 if str(p).startswith(str(writer.root))
        else real(p))
    cont._space_checked_at = 0.0
    cont.watch()
    out = cont.stop()
    assert out["stop_reason"] == cont_mod.STOP_TARGET_LOST
    assert out["complete"] is False


def test_the_reserve_stops_a_recording_and_says_so(cont, monkeypatch):
    cont.arm(plan(heads=("left",)), slots=8)
    cont.start()
    cont.offer("left", pixels(1), meta(1))
    from trilobite.storage import devices
    monkeypatch.setattr(devices, "_usage", lambda p: (1 << 30, 1 << 10))
    cont._space_checked_at = 0.0
    cont.watch()
    out = cont.stop()
    assert out["stop_reason"] == cont_mod.STOP_SPACE


def test_the_max_recordable_duration_is_known_before_start(cont, monkeypatch):
    from trilobite.storage import devices
    # 1 MB usable above the 1 MB reserve.
    monkeypatch.setattr(devices, "_usage", lambda p: (4 << 30, 2 << 20))
    p = plan(heads=("left",), duration=3600.0)
    report = cont.feasibility(p, sustained_mb_s=None)
    expected = (2 << 20) - (1 << 20)
    assert report["max_duration_s"] == pytest.approx(
        expected / p.bytes_per_second, abs=0.05)
    assert report["fits"] is False
    assert report["predicted_drop_fraction"] is None
    assert "unknown rather than zero" in report["note"]


def test_a_clean_recording_is_complete_and_a_stopped_one_is_not(cont):
    cont.arm(plan(heads=("left",)), slots=16)
    cont.start()
    for n in range(1, 9):
        cont.offer("left", pixels(n), meta(n))
    _drain(cont)
    out = cont.stop()
    assert out["complete"] is True
    assert out["stop_reason"] == cont_mod.STOP_REQUESTED
    assert out["degradation_note"].startswith("Frame dropping is the ONLY")


def test_the_journal_is_written_last_so_its_presence_means_finished(cont):
    cont.arm(plan(heads=("left",)), slots=8)
    cont.start()
    root = Path(cont.directory)
    cont.offer("left", pixels(1), meta(1))
    _drain(cont)
    assert not (root / "recording.json").exists()
    cont.stop()
    assert (root / "recording.json").exists()


def test_arming_releases_its_storage_admission_when_it_fails(writer):
    """A failed arm must not leave the writer holding an admission, or every
    later release would refuse to drain."""
    rec = ContinuousRecorder(writer.cfg, writer)
    before = writer._inflight
    with pytest.raises(ValueError):
        rec.arm(plan(heads=("left",)), slots=-5)
    assert writer._inflight == before


def test_a_recording_holds_its_admission_until_it_stops(cont, writer):
    before = writer._inflight
    cont.arm(plan(heads=("left",)), slots=8)
    assert writer._inflight == before + 1
    cont.start()
    cont.stop()
    assert writer._inflight == before


# -- the manager ------------------------------------------------------------


@pytest.fixture
def manager(writer):
    m = RecordingManager(writer.cfg, writer)
    caps = {"eight_bit_available": True, "packed_available": False,
            "measured": True}
    m.register("left", caps, W, H, 30.0)
    m.register("right", dict(caps), W, H, 30.0)
    return m


def test_only_one_recorder_runs_at_a_time(manager):
    manager.arm_continuous("raw16", 30.0)
    with pytest.raises(RuntimeError, match="only one recorder|Only one recorder"):
        manager.arm_burst("raw16", 30.0, frames=4)
    manager.stop_continuous()


def test_the_manager_refuses_to_arm_over_an_unsaved_burst(manager):
    manager.arm_burst("raw16", 30.0, frames=4)
    manager.start_burst()
    manager.burst.offer("left", pixels(1), meta(1))
    manager.stop_burst()
    with pytest.raises(RuntimeError, match="has not been saved"):
        manager.arm_continuous("raw16", 30.0)


def test_heads_of_different_sizes_cannot_share_one_plan(manager):
    manager.register("right", {"eight_bit_available": True,
                               "packed_available": False}, W + 8, H, 30.0)
    with pytest.raises(RuntimeError, match="different sensor frames"):
        manager.common_geometry()


def test_a_recording_cannot_be_asked_to_run_faster_than_the_sensor(manager):
    with pytest.raises(ValueError, match="cannot run faster"):
        manager.plan("raw16", 60.0)


def test_the_options_say_when_no_measurement_is_on_file(manager):
    opts = manager.options()
    assert opts["available"] is True
    assert opts["preflight_current"] is False
    assert all(p["predicted_drop_fraction"] is None for p in opts["plans"])
    # And the internal-storage override is never offered pre-ticked.
    assert opts["defaults"]["internal_ok"] is False


def test_a_measurement_is_invalidated_by_a_change_of_target(manager, writer, tmp_path):
    manager.measure(budget_bytes=1 << 20, budget_seconds=0.5)
    assert manager.options()["preflight_current"] is True
    other = tmp_path / "other"
    other.mkdir()
    writer.retarget(other)
    assert manager.options()["preflight_current"] is False
    assert "Measure again" in manager.options()["preflight"]["stale_reason"]


def test_the_measurement_reports_a_tail_rate_and_a_mean(manager):
    out = manager.measure(budget_bytes=4 << 20, budget_seconds=2.0)
    st = out["storage"]
    assert st["ok"] is True
    assert st["sustained_mb_s"] > 0
    assert st["mean_mb_s"] > 0
    assert "last quarter" in st["message"]


def test_the_manager_offers_only_measured_formats_after_a_measurement(manager):
    manager.measure(budget_bytes=1 << 20, budget_seconds=0.5)
    keys = {p["format"] for p in manager.options()["plans"]}
    assert keys == {"raw16", "raw8"}


def test_a_watch_finishes_a_self_stopped_recording(manager):
    manager.arm_continuous("raw16", 30.0, slots=4, max_drop_fraction=0.01)
    manager.start_continuous()
    head = manager.continuous.heads["left"]
    head._stop.set()
    head._thread.join(timeout=2.0)
    for n in range(1, 101):
        manager.offer("left", pixels(n), _frame(n))
        manager.offer("right", pixels(n), _frame(n))
    manager.watch()
    assert manager.active is None
    assert manager.continuous.state == cont_mod.INCOMPLETE


class _FakeFrame:
    """The attributes the manager reads off a Frame, and nothing else."""

    def __init__(self, seq):
        self.seq = seq
        self.t_mono = seq / 30.0
        self.t_wall = 1e9 + seq / 30.0
        self.validity = "science"
        self.meta = {"SensorTimestamp": seq * 33_333_333,
                     "raw_observed_max": 1000}


def _frame(seq):
    return _FakeFrame(seq)


def test_a_missing_sensor_timestamp_is_recorded_as_missing(manager):
    """It is the only clock with a defined relation to exposure. Substituting
    t_mono would produce an index that looks usable for timing and is not."""
    from trilobite.recording.manager import meta_from_frame
    f = _FakeFrame(7)
    f.meta = {}
    assert meta_from_frame(f).sensor_timestamp is None
    assert meta_from_frame(_FakeFrame(7)).sensor_timestamp == 7 * 33_333_333


def test_the_capture_side_and_recorder_counters_must_agree(manager):
    manager.arm_continuous("raw16", 30.0)
    manager.start_continuous()
    for n in range(1, 21):
        manager.offer("left", pixels(n), _frame(n))
        manager.offer("right", pixels(n), _frame(n))
    _drain(manager.continuous)
    out = manager.stop_continuous()
    for head in out["heads"].values():
        assert head["frames_exposed"] == 20
        assert head["unaccounted"] == 0


def test_threads_offering_concurrently_lose_no_frame_to_a_race(manager):
    """Two capture threads, one recorder. The acceptance criterion has to hold
    under the concurrency the rig actually has."""
    manager.arm_continuous("raw16", 30.0, slots=8)
    manager.start_continuous()

    def feed(cam_id):
        for n in range(1, 201):
            manager.offer(cam_id, pixels(n % 60), _frame(n))

    threads = [threading.Thread(target=feed, args=(c,))
               for c in ("left", "right")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20.0)
    _drain(manager.continuous, timeout=20.0)
    out = manager.stop_continuous()
    assert out["every_frame_accounted"] is True
    for head in out["heads"].values():
        assert head["frames_exposed"] == 200
        assert head["frames_stored_counted"] + head["frames_dropped"] == 200


# -- the three-outcome offer contract ---------------------------------------
#
# `offer` has to distinguish "dropped" from "never part of the recording", and
# the distinction is not cosmetic: the capture loop keeps its own count of
# frames it handed over, the recorder keeps its own count of frames it took,
# and comparing the two is the ONLY way a frame lost between the capture thread
# and the ring would ever be noticed. Charging the stopped-recorder case as a
# drop makes the two counts disagree by one on every recording, which turns
# that comparison from a detector into noise.


def test_the_recorder_itself_returns_none_once_it_has_stopped(cont):
    """Asserted on the recorder and not only through the manager. The manager
    asks `wants` first and returns None by itself, so a manager-level test
    passes whatever the recorder returns -- it never reaches the branch."""
    cont.arm(plan(heads=("left",)), slots=4)
    cont.start()
    assert cont.offer("left", pixels(1), meta(1)) is True
    head = cont.heads["left"]
    cont.stop()

    assert cont.offer("left", pixels(2), meta(2)) is None
    assert cont.offer("right", pixels(2), meta(2)) is None    # not in the plan
    assert head.dropped == 0
    assert head.exposed == 1


def test_the_burst_recorder_itself_returns_none_once_it_has_stopped(burst):
    burst.arm(plan(heads=("left",)), frames=8)
    burst.start()
    assert burst.offer("left", pixels(1), meta(1)) is True
    burst.stop()

    assert burst.offer("left", pixels(2), meta(2)) is None
    assert burst.offer("right", pixels(2), meta(2)) is None
    assert burst.buffers["left"].count == 1


def test_a_frame_arriving_after_the_recording_ended_is_not_a_drop(manager):
    manager.arm_continuous("raw16", 30.0)
    manager.start_continuous()
    manager.offer("left", pixels(1), _frame(1))
    _drain(manager.continuous)
    head = manager.continuous.heads["left"]
    manager.stop_continuous()

    dropped_before = head.dropped
    exposed_before = head.exposed
    # The frame the capture thread had already read when Stop landed.
    assert manager.offer("left", pixels(2), _frame(2)) is None
    assert head.dropped == dropped_before
    assert head.exposed == exposed_before


def test_a_frame_arriving_after_a_burst_ended_is_not_a_drop(manager):
    manager.arm_burst("raw16", 30.0, frames=8)
    manager.start_burst()
    manager.offer("left", pixels(1), _frame(1))
    manager.stop_burst()

    before = manager.burst.buffers["left"].count
    assert manager.offer("left", pixels(2), _frame(2)) is None
    assert manager.burst.buffers["left"].count == before
    manager.discard_burst()


def test_a_frame_for_a_head_that_is_not_in_the_plan_is_not_a_drop(manager):
    manager.arm_continuous("raw16", 30.0)
    manager.start_continuous()
    try:
        assert manager.offer("middle", pixels(1), _frame(1)) is None
    finally:
        manager.stop_continuous()


def test_a_full_ring_is_a_drop_and_says_so(manager):
    """The other side of the same contract: a frame that arrived while the
    recording was running and could not be stored IS a drop, and must not be
    quietly reported as 'not wanted'."""
    manager.arm_continuous("raw16", 30.0, slots=1)
    manager.start_continuous()
    head = manager.continuous.heads["left"]
    # Stop the writer draining so the ring cannot be refilled.
    head._stop.set()
    outcomes = [manager.offer("left", pixels(n), _frame(n)) for n in range(1, 6)]
    manager.stop_continuous()
    assert False in outcomes, outcomes
    assert None not in outcomes
    assert head.dropped == outcomes.count(False)


# -- the start barrier ------------------------------------------------------


def test_start_refuses_when_a_head_with_a_live_loop_delivers_nothing(manager):
    """Start must not return while a head could still release a frame to the
    preview cap -- a frame nothing counts, because `offer` is never called for
    it. A head that never arrives is a failed start, not a recording to carry
    on with: one that silently holds a single head looks complete."""
    manager.attach_loop("left")
    manager.attach_loop("right")
    manager.arm_continuous("raw16", 30.0)

    # Only `left` ever reaches the recording branch.
    def feed_left():
        for n in range(1, 4):
            manager.offer("left", pixels(n), _frame(n))

    threading.Thread(target=feed_left, daemon=True).start()
    with pytest.raises(RuntimeError, match=r"right delivered no frame"):
        manager.start_continuous(wait_s=1.0)
    assert manager.active is None
    assert manager.continuous.state != "recording"


def test_start_waits_only_for_heads_that_have_a_loop(manager):
    """A head registered with nothing driving it -- a unit test, or a thread
    that died -- must not hang a start that is otherwise fine."""
    manager.arm_continuous("raw16", 30.0)
    out = manager.start_continuous(wait_s=0.2)
    try:
        assert out["heads_confirmed"] == []
        assert manager.continuous.state == "recording"
    finally:
        manager.stop_continuous()


def test_start_returns_once_every_head_has_arrived(manager):
    manager.attach_loop("left")
    manager.arm_continuous("raw16", 30.0)

    def feed():
        time.sleep(0.05)
        manager.offer("left", pixels(1), _frame(1))

    threading.Thread(target=feed, daemon=True).start()
    out = manager.start_continuous(wait_s=2.0)
    try:
        assert out["heads_confirmed"] == ["left"]
    finally:
        _drain(manager.continuous)
        manager.stop_continuous()


def test_a_detached_loop_is_no_longer_waited_for(manager):
    manager.attach_loop("left")
    manager.detach_loop("left")
    manager.arm_continuous("raw16", 30.0)
    out = manager.start_continuous(wait_s=0.2)
    try:
        assert out["heads_confirmed"] == []
    finally:
        manager.stop_continuous()


def test_the_preview_stands_down_once_the_recording_is_losing_frames(cont):
    """A preview frame is never worth a recorded frame. The 2 Hz cap is enough
    while the disk keeps up; past half the drop ceiling the pipeline pass and
    the JPEG encode are competing with the write path for the same cores."""
    cont.arm(plan(heads=("left",)), slots=1, max_drop_fraction=0.20)
    cont.start()
    head = cont.heads["left"]
    assert head.preview_suppress_at == pytest.approx(0.10)
    assert head.preview_suppressed is False

    # Stall the writer so the single slot cannot be recycled, then offer
    # enough frames that the recent window is mostly drops.
    head._stop.set()
    for n in range(1, 40):
        cont.offer("left", pixels(n), meta(n))
    assert head.dropped > 0
    assert head.recent_drop_fraction > 0.10
    assert head.preview_suppressed is True
    assert head.live()["preview_suppressed"] is True
    cont.stop()


def test_the_manager_passes_the_suppression_on_to_the_capture_loop(manager):
    manager.arm_continuous("raw16", 30.0, slots=1, max_drop_fraction=0.20)
    manager.start_continuous(wait_s=0.0)
    head = manager.continuous.heads["left"]
    try:
        assert manager.wants_preview("left") is True
        head._stop.set()
        for n in range(1, 40):
            manager.offer("left", pixels(n), _frame(n))
        assert manager.wants_preview("left") is False
        # A head that is not recording is unaffected: the preview is only
        # subordinate to a recording that exists.
        assert manager.wants_preview("middle") is True
    finally:
        manager.stop_continuous()
    assert manager.wants_preview("left") is True
