"""A recording made by the application, read back by the desktop reader.

The unit tests in `test_recording.py` drive the recorders with numpy arrays,
which is the right level for the drop accounting and the state machines. This
file closes the loop the operator actually uses: cameras open, a recording is
armed and run through the real capture loop, and then `scripts/read_capture.py`
opens the directory from the outside and recomputes the accounting from the
files rather than reading the rig's claims.

That recomputation IS the acceptance criterion for Stage 5c. The rig saying
"every frame accounted for" is a claim; the reader agreeing, from the chunks
and indexes alone, is evidence.
"""

from __future__ import annotations

import importlib.util
import json
import re
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from trilobite.app import Application
from trilobite.config import AppConfig, CameraConfig, StageConfig, StorageConfig

READER = Path(__file__).resolve().parent.parent / "scripts" / "read_capture.py"


@pytest.fixture(scope="module")
def reader():
    """Import the reader script as a module, the way a desktop user runs it."""
    spec = importlib.util.spec_from_file_location("read_capture", READER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["read_capture"] = mod
    spec.loader.exec_module(mod)
    return mod


def _cam(cam_id: str, rotate: int = 0) -> CameraConfig:
    return CameraConfig(
        cam_id=cam_id, backend="synthetic", fps=20.0,
        full_resolution=(128, 96), preview_resolution=(64, 48),
        rotate_deg=rotate, synthetic_drift_px=0.0,
        pipeline=[StageConfig(type="stats", name="stats")],
    )


@pytest.fixture
def rig(tmp_path):
    external = tmp_path / "stick"
    external.mkdir()
    app = Application(
        AppConfig(
            storage=StorageConfig(
                root=str(tmp_path / "internal"),
                chunk_frames=8, write_block_frames=2,
                reserve_mb=1, internal_reserve_mb=1,
                recording_preview_fps=2.0),
            cameras=[_cam("left"), _cam("right")],
            server={"preview_fps": 10.0},
        ),
        state_path=None, restore=False,
    )
    app.start()
    app.writer.retarget(external)
    yield app
    app.stop()


def _record(app, seconds: float = 1.5, **kw) -> dict:
    app.recording.arm_continuous("raw16", 20.0, **kw)
    app.recording.start_continuous()
    time.sleep(seconds)
    return app.recording.stop_continuous()


# -- the loop end to end ----------------------------------------------------


def test_the_capture_loop_feeds_the_recorder(rig):
    out = _record(rig, 1.5)
    assert out["totals"]["frames_exposed"] > 5
    assert out["every_frame_accounted"] is True
    for cam_id in ("left", "right"):
        assert out["heads"][cam_id]["frames_stored"] > 0


def test_the_capture_side_counters_agree_with_the_recorder(rig):
    """Two independent counts of the same frames. A divergence means one went
    missing between the capture thread and the recorder, which is the only loss
    neither side can see alone."""
    out = _record(rig, 1.5)
    for cam in rig.cameras.values():
        head = out["heads"][cam.cam_id]
        assert cam.recorded == head["frames_stored_counted"]
        assert cam.record_dropped == head["frames_dropped"]


def test_the_preview_keeps_running_while_recording_but_much_slower(rig):
    """A preview frame is never worth a recorded frame, and the browser cap
    competes for the same cores as the write path."""
    rig.recording.arm_continuous("raw16", 20.0)
    rig.recording.start_continuous()
    cam = rig.cameras["left"]
    before = cam.preview.get()[0]
    time.sleep(2.0)
    during = cam.preview.get()[0]
    out = rig.recording.stop_continuous()

    published = during - before
    recorded = out["heads"]["left"]["frames_stored_counted"]
    # Still alive -- an operator cannot frame a shot on a frozen image.
    assert published >= 1
    # ...and well below the recording rate. 2 Hz against 20 Hz, with slack for
    # scheduling on a loaded test host.
    assert published < recorded


def test_no_frame_is_skipped_undecoded_while_armed(rig):
    """`skip_preview` releases a frame without decoding it, which is exactly
    wrong while recording: a released frame is a lost frame."""
    cam = rig.cameras["left"]
    rig.recording.arm_continuous("raw16", 20.0)
    rig.recording.start_continuous()
    before = cam.skipped
    time.sleep(1.5)
    after = cam.skipped
    rig.recording.stop_continuous()
    assert after == before


# -- what the reader makes of it --------------------------------------------


def test_the_reader_recomputes_the_accounting_and_agrees(rig, reader):
    out = _record(rig, 1.5)
    rec = reader.open_recording(Path(out["directory"]))
    acc = rec.accounting()
    assert acc["agrees"] is True
    assert acc["unaccounted"] == 0
    for cam_id in ("left", "right"):
        assert (acc["heads"][cam_id]["stored_on_disk"]
                == out["heads"][cam_id]["frames_stored"])


def test_the_reader_reads_frames_in_exposed_order_with_their_sequence(rig, reader):
    out = _record(rig, 1.5)
    rec = reader.open_recording(Path(out["directory"]))
    seqs = [int(r["seq"]) for r, _ in rec.frames("left")]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert len(seqs) == out["heads"]["left"]["frames_stored"]


def test_the_reader_refuses_a_newer_schema_rather_than_half_reading_it(
    rig, reader, tmp_path
):
    out = _record(rig, 1.0)
    journal = Path(out["directory"]) / "recording.json"
    payload = json.loads(journal.read_text())
    payload["schema"] = reader.SUPPORTED_RECORDING_SCHEMA + 1
    journal.write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match="understands up to"):
        reader.open_recording(Path(out["directory"]))


def test_a_chunk_with_no_index_is_skipped_rather_than_invented(rig, reader, capsys):
    """Without the index there is no record of which exposures those frames
    were, so treating them as consecutive would invent a timeline."""
    out = _record(rig, 1.5)
    root = Path(out["directory"]) / "left"
    indexes = sorted(root.glob("*.index.json"))
    assert indexes
    kept = len(indexes) - 1
    indexes[-1].unlink()

    rec = reader.open_recording(Path(out["directory"]))
    assert len(rec.heads["left"]) == kept
    assert "interrupted write" in capsys.readouterr().err


def test_an_exported_frame_carries_the_recordings_validity_not_a_better_one(
    rig, reader, tmp_path
):
    out = _record(rig, 1.5)
    rec = reader.open_recording(Path(out["directory"]))
    first_seq = int(next(iter(rec.frames("left")))[0]["seq"])

    dest = tmp_path / "frames"
    args = type("A", (), {
        "path": Path(out["directory"]), "head": "left", "seq": first_seq,
        "every": 1, "export": dest, "show": False, "save": None,
        "no_stretch": True})()
    assert reader.read_recording(args) == 0

    sidecars = sorted(dest.glob("*.json"))
    assert len(sidecars) == 1
    side = json.loads(sidecars[0].read_text())
    assert side["acquisition"]["seq"] == first_seq
    # The synthetic backend produces unvalidated frames; the export must not
    # promote that to science because the format happened to be exact.
    assert side["validity"] == side["acquisition"]["validity"]
    assert side["validity"] != "science"
    assert side["exported_from"]["recording"] == str(out["directory"])
    # And the orientation is marked APPLIED, so a reader does not apply it
    # a second time.
    assert side["acquisition"]["sensor_metadata"]["raw_oriented"] is True


def test_exporting_a_dropped_frame_says_it_was_dropped(rig, reader, tmp_path, capsys):
    out = _record(rig, 1.0)
    dest = tmp_path / "frames"
    args = type("A", (), {
        "path": Path(out["directory"]), "head": "left", "seq": 10 ** 7,
        "every": 1, "export": dest, "show": False, "save": None,
        "no_stretch": True})()
    reader.read_recording(args)
    assert "one of the dropped ones" in capsys.readouterr().err


# -- orientation ------------------------------------------------------------


def test_a_recording_stores_unoriented_pixels_and_the_reader_turns_them(
    tmp_path, reader
):
    """The one thing that would be silently wrong if the journal did not carry
    the transform: a rotated camera whose recording is read flat."""
    external = tmp_path / "stick"
    external.mkdir()
    app = Application(
        AppConfig(
            storage=StorageConfig(root=str(tmp_path / "internal"),
                                  chunk_frames=8, write_block_frames=2,
                                  reserve_mb=1, internal_reserve_mb=1),
            cameras=[_cam("left", rotate=90)],
        ),
        state_path=None, restore=False,
    )
    app.start()
    app.writer.retarget(external)
    try:
        out = _record(app, 1.2)
    finally:
        app.stop()

    rec = reader.open_recording(Path(out["directory"]))
    assert rec.plan["pixels_oriented"] is False
    assert rec.orientation("left")["rotate_deg"] == 90

    chunk = rec.heads["left"][0]["npy"]
    stored = np.load(chunk, mmap_mode="r")
    # Stored in the SENSOR frame: 128 x 96 as configured, not the 96 x 128 a
    # still from this camera would be.
    assert stored.shape[1:] == (96, 128)
    # ...and the reader hands back the turned frame.
    _record_meta, pixels = next(iter(rec.frames("left")))
    assert pixels.shape == (128, 96)


# -- the MATLAB path --------------------------------------------------------
#
# `matlab/tv_recording_frame.m` does not load a chunk; it parses the .npy
# header, seeks past the preceding rows and reads one frame's bytes. That
# arithmetic is where a reader goes wrong silently -- an off-by-one in the
# header length returns a frame shifted by two pixels, and reshaping in the
# wrong order returns the transpose -- and neither shows up as an error.
#
# No MATLAB or Octave runs here, so this does NOT execute the .m file and is
# not a check on its syntax. What it does check is the layout the .m file
# depends on: the same steps, in Python, against a real recording, compared
# with `np.load`. If a future change to the chunk writer moves a byte, this
# fails and the .m file needs the same edit.


def _matlab_style_frame(npy: Path, row: int) -> np.ndarray:
    """Re-implement tv_recording_frame's reader, step for step."""
    with open(npy, "rb") as fh:
        assert fh.read(6) == b"\x93NUMPY"
        major, _minor = fh.read(2)
        if major == 1:
            (hlen,) = struct.unpack("<H", fh.read(2))
            preamble = 10
        else:
            (hlen,) = struct.unpack("<I", fh.read(4))
            preamble = 12
        header = fh.read(hlen).decode("latin-1")
        data_start = preamble + hlen

        descr = re.search(r"'descr'\s*:\s*'([^']+)'", header).group(1)
        shape_txt = re.search(r"'shape'\s*:\s*\(([^)]*)\)", header).group(1)
        assert "'fortran_order': False" in header
        shape = [int(v) for v in re.findall(r"\d+", shape_txt)]
        assert len(shape) == 3
        _n, height, width = shape

        order, code = descr[0], descr[1:]
        nbytes = {"u1": 1, "u2": 2, "i2": 2, "f4": 4}[code]
        frame_bytes = height * width * nbytes

        fh.seek(data_start + (row - 1) * frame_bytes)
        raw = fh.read(frame_bytes)
        assert len(raw) == frame_bytes

    dtype = np.dtype("u1" if nbytes == 1 else code)
    flat = np.frombuffer(raw, dtype=dtype)
    if order == ">" and nbytes > 1:
        flat = flat.byteswap()
    # `reshape(v, [W H]).'` in MATLAB. MATLAB's reshape fills the FIRST
    # dimension fastest, so `order="F"` is what makes this an emulation of the
    # .m file rather than a convenient numpy reshape that happens to work:
    # written the numpy way it would pass whatever the .m file said.
    return flat.reshape((width, height), order="F").T


def test_the_matlab_frame_arithmetic_lands_on_the_right_bytes(rig):
    out = _record(rig, 1.5)
    directory = Path(out["directory"])

    for cam_id in ("left", "right"):
        indexes = sorted((directory / cam_id).glob("*.index.json"))
        assert indexes, f"no chunk index for {cam_id}"
        for ipath in indexes:
            index = json.loads(ipath.read_text(encoding="utf-8"))
            npy = directory / cam_id / index["file"]
            whole = np.load(npy, mmap_mode="r")
            # Every row, not just the first: an error in the frame stride is
            # invisible at row 1 and compounds after it.
            for row in range(1, len(index["frames"]) + 1):
                got = _matlab_style_frame(npy, row)
                assert got.shape == whole.shape[1:]
                assert np.array_equal(got, np.asarray(whole[row - 1])), (
                    f"{npy.name} row {row} read at the wrong offset")

            # The two numbers tv_recording_frame cross-checks before reading.
            assert index["header_bytes"] == 128
            assert (index["frame_bytes"]
                    == whole.shape[1] * whole.shape[2] * whole.dtype.itemsize)


def test_the_matlab_accounting_recomputes_the_same_losses(rig):
    """tv_read_recording derives stored/exposed/dropped from the indexes alone,
    the way `Recording.accounting` does, and the journal has to agree with
    both."""
    out = _record(rig, 1.5)
    directory = Path(out["directory"])

    for cam_id in ("left", "right"):
        seqs: list[int] = []
        for ipath in sorted((directory / cam_id).glob("*.index.json")):
            index = json.loads(ipath.read_text(encoding="utf-8"))
            seqs.extend(int(f["seq"]) for f in index["frames"])
        seqs.sort()
        stored = len(seqs)
        exposed = seqs[-1] - seqs[0] + 1
        gaps = [(a + 1, b - 1) for a, b in zip(seqs, seqs[1:], strict=False)
                if b > a + 1]
        dropped = exposed - stored

        assert stored + dropped - exposed == 0
        assert sum(b - a + 1 for a, b in gaps) == dropped

        claimed = out["heads"][cam_id]
        assert claimed["frames_stored"] == stored
        assert claimed["frames_exposed"] == exposed
        assert claimed["frames_dropped"] == dropped
