"""The portability contracts, against a fake SDK rather than a fake camera.

`tests/fakesdk.py` puts a fake `picamera2` in `sys.modules`, so the REAL
`Picamera2Source` runs here: `_configure_and_start`, the negotiated read-back,
`classify`, `admit`, the control intersection, the sensor-mode depth. A
synthetic `CameraSource` bypasses every one of those, which is why the first
version of this work could not have caught anything in this file.

Each test names the observation from `docs/sensor-portability-review.md` that
it pins down. They are capability fixtures, **not claims that any physical
module behaves this way**: "if a driver does this, the adapter does that".

What these tests do NOT establish, and the review is explicit about it: driver
support for any particular module, optical correctness, or sustained
dual-camera performance. Those need the Pi and the hardware.
"""

from __future__ import annotations

import fakesdk
import numpy as np
import pytest

from trilobite.cameras.rawformat import (
    MIN_STRIDE_PAD_ALLOWANCE,
    RawFormatError,
    admit,
    classify,
    stride_pad_allowance,
)
from trilobite.config import CameraConfig


def source(cfg: CameraConfig | None = None, **kw) -> object:
    from trilobite.cameras.picam import Picamera2Source

    cfg = cfg or CameraConfig(cam_id="left", backend="picamera2",
                              preview_resolution=(320, 240), **kw)
    src = Picamera2Source(cfg)
    src.open()
    # No owner thread in a unit test; the ownership check is exercised in
    # tests/test_acquisition.py.
    src.bind_owner(None)
    return src


@pytest.fixture
def opened(fake_sdk):
    """Open the real adapter against a fake sensor, and close it after."""
    made = []

    def _open(sensor, **cfgkw):
        fake_sdk(sensor)
        cfgkw.setdefault("preview_resolution", (320, 240))
        src = source(CameraConfig(cam_id="left", backend="picamera2", **cfgkw))
        made.append(src)
        return src

    try:
        yield _open
    finally:
        for src in made:
            with pytest.raises(Exception) if False else _nullcontext():
                src.close()


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True        # closing a fake camera must never fail a test


# -- the baseline: the current rig, unchanged -------------------------------


def test_the_current_sensor_reads_exactly_as_the_fake_wrote_it(opened):
    """Case A. The control for everything else in this file.

    The expected array is built in `fakesdk.expected_image` from the fake's own
    parameters, with no use of the trimming or shifting helpers under test --
    so this compares the adapter against an independent construction rather
    than against itself.
    """
    sensor = fakesdk.imx296_mono()
    src = opened(sensor)
    read = src.read_raw()
    got = np.asarray(read.frame.data)

    assert got.shape == (1088, 1456)
    assert got.dtype == np.uint16
    assert np.array_equal(got, fakesdk.expected_image(sensor, (1456, 1088)))
    assert read.frame.validity == "science"
    # And the padding never reached the image: the fake fills it with 0xA5.
    assert int(got.max()) == (1 << sensor.sample_bits) - 1


def test_the_padding_sentinel_never_appears_in_the_image(opened):
    """A reader that leaks row padding produces a visibly wrong column rather
    than a slightly dark edge, which is the point of the sentinel."""
    src = opened(fakesdk.imx296_mono())
    got = np.asarray(src.read_raw().frame.data)
    # 0xA5A5 is what a leaked pair of padding bytes reads as.
    assert not (got == 0xA5A5).any()


# -- S3: the stream geometries are separate quantities ----------------------


def test_a_driver_adjusted_main_size_is_reported_not_the_request(opened):
    """S3. `describe()` reported what was ASKED FOR until 11 October.

    Every consumer of `full_resolution` -- the MLA reference frame, the
    readiness checks, the recording plan, the session manifest, the
    click-to-sensor mapping -- would then be working against a frame that does
    not exist. On this rig the request is honoured, which is exactly why it
    never showed.
    """
    sensor = fakesdk.imx296_mono()
    sensor.adjust_main_to = (1440, 1080)
    sensor.adjust_lores_to = (704, 528)
    src = opened(sensor, full_resolution=(1456, 1088),
                 preview_resolution=(728, 544))

    info = src.describe()
    assert tuple(info.full_resolution) == (1440, 1080)
    assert tuple(info.preview_resolution) == (704, 528)
    # ...and the arrays that actually arrive agree with what is advertised.
    assert src.capture_full(raw=False).data.shape == (1080, 1440)
    assert src.read_preview().data.shape == (528, 704)


def test_an_adjusted_size_is_recorded_as_a_note_rather_than_absorbed(opened):
    sensor = fakesdk.imx296_mono()
    sensor.adjust_main_to = (1440, 1080)
    src = opened(sensor, full_resolution=(1456, 1088))
    notes = " ".join(src.geometry()["notes"])
    assert "main was requested at 1456x1088" in notes
    assert "1440x1080" in notes


def test_the_five_geometries_are_reported_separately(opened):
    """S3. Physical array, raw stream, main, lores and what a consumer holds
    are five quantities. Collapsing them is what hid the adjustment above."""
    src = opened(fakesdk.imx296_mono(), preview_resolution=(728, 544))
    g = src.geometry()
    assert g["sensor"] == [1456, 1088]
    assert g["raw"]["size"] == [1456, 1088]
    assert g["raw"]["format"] == "R10"
    assert g["raw"]["stride_bytes"] == 2944
    assert g["main"]["size"] == (1456, 1088)
    assert g["lores"]["size"] == (728, 544)
    assert g["oriented_main"] == [1456, 1088]
    assert g["raw_matches_main"] is True


def test_a_quarter_turn_separates_the_oriented_sizes_from_the_streams(opened):
    src = opened(fakesdk.imx296_mono(), rotate_deg=90,
                 preview_resolution=(728, 544))
    g = src.geometry()
    assert g["main"]["size"] == (1456, 1088), "the stream is not rotated"
    assert g["oriented_main"] == [1088, 1456], "what a consumer holds is"
    # 3, not 1: `rotate_deg` is clockwise on screen and np.rot90's positive
    # direction on an image whose rows run downward is counter-clockwise, so
    # the sign flips in `quarter_turns` and nowhere else. Asserted against the
    # convention rather than against the number that reads naturally.
    assert g["quarter_turns"] == 3


# -- S4/S5: the sample depth is not the container width ---------------------


def test_a_twelve_bit_sensor_in_a_sixteen_bit_container_is_read_as_twelve(
    opened
):
    """S4 and S5, and the sharpest finding of the review.

    Before 11 October this frame was admitted as `science` with
    `raw_bits_nominal: 16` and a value ceiling of 65535 -- a ceiling nothing
    can exceed, which is not a check. The samples are 12-bit, left-shifted by
    four, and taking 16 as the depth also makes the msb shift zero when it
    should be four and any normalisation by full scale wrong by sixteen.
    """
    sensor = fakesdk.deep_mono_16()
    src = opened(sensor)
    assert src._raw_negotiated["format"] == "R16"

    read = src.read_raw()
    meta = read.frame.meta
    assert meta["raw_bits_nominal"] == 12
    assert meta["raw_bits_source"] == "sensor-mode"
    assert meta["raw_bits_from_format_name"] == 16
    assert meta["raw_container_bits"] == 16
    assert meta["raw_value_ceiling"] == 4095

    # And with the alignment left at the default, the frame is NOT science:
    # the values do not fit a 12-bit lsb-aligned sample, and the reservation
    # names the configuration change that would make it fit.
    assert read.frame.validity == "unvalidated"
    assert any("raw_alignment: msb" in r for r in meta["raw_reservations"])


def test_the_same_sensor_reconciles_once_the_alignment_is_declared(opened):
    """The other half: the reservation above is actionable, and acting on it
    resolves it. A grading that cannot be satisfied is a dead end."""
    src = opened(fakesdk.deep_mono_16(), raw_alignment="msb")
    read = src.read_raw()
    meta = read.frame.meta
    assert meta["raw_bits_nominal"] == 12
    assert meta["raw_sample_shift"] == 4, "12 bits in a 16-bit word, msb"
    assert meta["raw_value_ceiling"] == 4095 << 4
    assert read.frame.validity == "science", meta.get("raw_reservations")


def test_a_sample_depth_wider_than_its_container_is_refused(opened):
    """Not a reservation. If the mode says 12 bits and the format delivers one
    byte, one of the two is wrong and guessing which decides the scale of every
    value in the recording."""
    sensor = fakesdk.native_eight_bit()
    sensor.modes = (fakesdk.FakeMode((640, 480), "R8", "R8", 12),)
    src = opened(sensor)
    with pytest.raises(RawFormatError, match="cannot be wider than the word"):
        src.read_raw()


def test_the_capabilities_report_the_mode_depth_not_the_format_name(opened):
    """The recording ladder gates its 8-bit rung on this number. From the
    format name it would be 16 for the sensor above, which would offer a
    reduction from a depth the sensor never had."""
    src = opened(fakesdk.deep_mono_16(), raw_alignment="msb")
    caps = src.raw_capabilities()
    assert caps["delivered_bits"] == 12
    assert caps["delivered_bits_source"] == "sensor-mode"
    assert caps["reducible_to_eight"] is True


def test_a_native_eight_bit_sensor_is_not_reducible(opened):
    src = opened(fakesdk.native_eight_bit())
    caps = src.raw_capabilities()
    assert caps["delivered_bits"] == 8
    assert caps["reducible_to_eight"] is False
    assert src.read_raw().frame.validity == "science"


def test_without_mode_information_the_format_name_is_used_and_says_so(opened):
    """A driver that publishes no modes is not a reason to refuse; it is a
    reason to label the depth as nominal."""
    sensor = fakesdk.imx296_mono()
    sensor.modes = ()
    src = opened(sensor, raw_format="R10")
    meta = src.read_raw().frame.meta
    assert meta["raw_bits_nominal"] == 10
    assert meta["raw_bits_source"] == "format-name"


# -- S4: Bayer -------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["RGGB", "BGGR", "GRBG", "GBRG"])
def test_every_bayer_pattern_opens_and_is_marked_colour(opened, pattern):
    """S4. The adapter must not be mono-only, and the mosaic order has to
    survive -- an ISP luma preview can look right while raw Bayer is
    mishandled."""
    src = opened(fakesdk.bayer_sensor(pattern, 12))
    info = src.describe()
    assert info.mono is False
    read = src.read_raw()
    assert read.frame.meta["raw_format"] == f"S{pattern}12"
    assert read.frame.validity == "science"
    assert np.asarray(read.frame.data).dtype == np.uint16


def test_a_sixteen_bit_bayer_container_is_known(opened):
    """S4 named this directly: mono `R16` was in the allowlist and the Bayer
    16-bit names were not, so a driver negotiating `SBGGR16` could not open at
    all. The refusal was safe and it was also wrong -- those bytes are
    ordinary left-shifted samples this code can read."""
    for pattern in ("RGGB", "BGGR", "GRBG", "GBRG"):
        fmt = classify(f"S{pattern}16")
        assert fmt is not None and fmt.admissible
        assert fmt.mono is False
        assert fmt.container_bits == 16

    src = opened(fakesdk.bayer_sensor("BGGR", 16))
    assert src._raw_negotiated["format"] == "SBGGR16"
    assert np.asarray(src.read_raw().frame.data).dtype == np.uint16


# -- 4.2: stride and padding ------------------------------------------------


def test_the_padding_allowance_follows_the_row_rather_than_a_constant():
    """A flat 256-byte bound was chosen against this sensor's 2912-byte row.
    The allowance is the driver's to choose and a wider sensor's row can carry
    more; what is actually inconsistent is padding of a whole row or more,
    because that is the shape a pixel-size disagreement takes."""
    assert stride_pad_allowance(64) == MIN_STRIDE_PAD_ALLOWANCE
    assert stride_pad_allowance(8112) == 8111


def test_a_large_but_legitimate_stride_pad_still_reconciles(opened):
    """The behaviour change, end to end. 512 bytes of padding on a 2912-byte
    row was graded `unvalidated` under the flat bound, for doing nothing
    wrong."""
    sensor = fakesdk.imx296_mono()
    sensor.stride_pad = 512
    src = opened(sensor)
    read = src.read_raw()
    assert read.frame.meta["raw_stride_bytes"] == 3424
    assert read.frame.validity == "science", read.frame.meta["raw_reservations"]
    assert np.asarray(read.frame.data).shape == (1088, 1456)


def test_padding_of_a_whole_row_is_still_reserved(opened):
    """The bound did not simply go away. Padding as wide as the image is the
    factor-of-two shape of a format disagreement."""
    sensor = fakesdk.imx296_mono()
    sensor.stride_pad = 2912
    src = opened(sensor)
    read = src.read_raw()
    assert read.frame.validity == "unvalidated"
    assert any("internally inconsistent" in r
               for r in read.frame.meta["raw_reservations"])


def test_a_driver_that_reports_no_stride_still_admits(opened):
    sensor = fakesdk.imx296_mono()
    sensor.report_stride = False
    src = opened(sensor)
    read = src.read_raw()
    assert read.frame.meta["raw_stride_source"] == "inferred"
    assert read.frame.validity == "science"


def test_a_non_contiguous_buffer_is_handled_rather_than_misread():
    """4.2 lists byte views, uint16 views and non-contiguous arrays. The
    re-view to uint16 requires contiguity, and a view that is not contiguous
    must be copied rather than reinterpreted."""
    base = np.zeros((1088, 2944 * 2), np.uint8)
    base[:, : 2912 * 2 : 2] = 7
    sliced = base[:, ::2]                       # a non-contiguous uint8 view
    assert not sliced.flags["C_CONTIGUOUS"]
    got = admit(sliced, "R10", (1456, 1088), stride_bytes=2944)
    assert got.array.dtype == np.uint16
    assert got.array.shape == (1088, 1456)


# -- 4.1: substitution, controls, failure ----------------------------------


def test_a_substituted_compressed_format_refuses_to_open(fake_sdk):
    """R5, re-asserted through the whole adapter rather than through
    `_choose_raw_format` alone. The request was for an uncompressed format and
    the driver handed back a compressed one; from that point the buffers are
    self-consistent and look like a slightly damaged photograph."""
    sensor = fakesdk.imx296_mono()
    sensor.substitute_format = "MONO_PISP_COMP1"
    fake_sdk(sensor)
    with pytest.raises(RawFormatError, match="COMPRESSED"):
        source()
    # And the device was given back: a held camera makes the next open report
    # no cameras at all, which reads as a cable fault.
    assert fakesdk.FakeCamera.opened[-1].closed is True


def test_controls_the_sensor_does_not_advertise_are_dropped_with_a_warning(
    opened
):
    """4.4. One config file has to serve a mono rig with no colour controls
    and a colour rig with them."""
    src = opened(fakesdk.imx296_mono(),
                 controls={"ExposureTime": 5000, "AwbEnable": True})
    assert "AwbEnable" in src._dropped_controls
    assert "AwbEnable" in src.describe().detail["dropped_controls"]
    assert fakesdk.FakeCamera.opened[-1].applied_controls["ExposureTime"] == 5000


def test_a_colour_sensor_keeps_the_colour_controls(opened):
    src = opened(fakesdk.bayer_sensor("RGGB", 12),
                 controls={"AwbEnable": True})
    assert src._dropped_controls == []


def test_a_frame_rate_outside_the_sensors_range_is_refused(opened):
    """4.4. Refused, not clamped: a clamped rate delivers a cadence other than
    the one the recording plan was built from."""
    src = opened(fakesdk.native_eight_bit())          # 8000-100000 us
    assert src.request_frame_rate(60.0) == 60.0
    with pytest.raises(ValueError, match="accepts 8000-100000 us"):
        src.request_frame_rate(500.0)


def test_a_driver_without_frame_duration_limits_cannot_set_a_rate(opened):
    """And says so by returning None, which the recorder treats as a refusal
    rather than routing around."""
    sensor = fakesdk.imx296_mono()
    sensor.controls = {"ExposureTime": (60, 1_000_000, 10_000)}
    src = opened(sensor)
    assert src.request_frame_rate(10.0) is None


# -- S1: the physical pixel pitch ------------------------------------------


def test_the_pitch_comes_from_the_driver_property(opened):
    """S1. `UnitCellSize` is documented as an x/y size in nanometres."""
    src = opened(fakesdk.imx296_mono())
    pitch, origin = src.pixel_pitch()
    assert pitch == pytest.approx(3.45e-3)
    assert origin == "driver:UnitCellSize"
    assert src.describe().pixel_pitch_mm == pytest.approx(3.45e-3)


def test_a_configured_pitch_beats_the_driver(opened):
    """An explicit operator statement wins, so a wrong tuning file can be
    worked around without patching the application."""
    src = opened(fakesdk.imx296_mono(), pixel_pitch_mm=0.002)
    assert src.pixel_pitch() == (0.002, "config:pixel_pitch_mm")


def test_a_known_model_without_the_property_falls_back_to_the_table(opened):
    """The table is keyed on the MODEL, which is the whole point: an unlisted
    sensor gets no pitch rather than inheriting the IMX296's."""
    sensor = fakesdk.imx296_mono()
    sensor.properties = {}                  # no UnitCellSize
    src = opened(sensor)
    assert src.pixel_pitch() == (3.45e-3, "table:imx296")


def test_an_unknown_sensor_with_no_property_has_no_pitch(opened):
    """S1, stated as an assertion: a new sensor must not inherit 3.45 um.

    `None` is the correct answer and the calibration code treats it as one --
    the metric figures become unavailable rather than wrong.
    """
    sensor = fakesdk.no_unit_cell()
    sensor.model = "fake-unknown-module"
    src = opened(sensor)
    assert src.pixel_pitch() == (None, "unknown")
    assert src.describe().pixel_pitch_mm is None


def test_a_non_square_pixel_is_reported_with_a_warning(opened, caplog):
    sensor = fakesdk.imx296_mono()
    sensor.properties = {"UnitCellSize": (3450, 2000)}
    with caplog.at_level("WARNING"):
        src = opened(sensor)
        pitch, origin = src.pixel_pitch()
    assert pitch == pytest.approx(3.45e-3), "the x pitch, as documented"
    assert origin == "driver:UnitCellSize"
    assert "not square" in caplog.text


# -- Layer C: one geometry matrix through the whole stack -------------------
#
# Small equivalents of the review's cases, each run end to end: open, preview,
# pipeline, still with its sidecar, a continuous recording, and both the
# accounting and the desktop reader over the result. The point is not that a
# physical module offers these combinations -- it is that nothing in the stack
# assumes one geometry.

CASES = [
    # key              width height bits align  mono  note
    ("A-current",       1456, 1088, 10, "lsb",  True,  "the current rig"),
    ("B-wide",          1920, 1080, 10, "lsb",  True,  "16:9, wider"),
    ("C-deep",          2028, 1520, 12, "msb",  True,  "larger and deeper"),
    ("D-native8",        640,  480,  8, "lsb",  True,  "exact 8-bit samples"),
    ("E-odd",           1457, 1089, 10, "lsb",  True,  "odd dimensions"),
    ("F-bayer",         2028, 1520, 12, "lsb",  False, "colour mosaic"),
]


def _matrix_sensor(width, height, bits, mono, alignment="lsb"):
    """A fake sensor for one matrix row, with a realistic format name.

    `alignment` decides where the sample sits in its 16-bit word, and the
    fixture has to agree with what the camera config declares -- the first
    version of this set `sample_shift` on every deep sensor regardless, and
    admission caught the disagreement on four of the six rows. That is the
    value check doing its job on a fixture that lied, which is the right place
    to find out.
    """
    if mono:
        unpacked = "R8" if bits <= 8 else f"R{bits}"
        packed = unpacked if bits <= 8 else f"R{bits}_CSI2P"
    else:
        unpacked = f"SRGGB{bits}"
        packed = f"SRGGB{bits}_CSI2P"
    return fakesdk.FakeSensor(
        model=f"fake-{width}x{height}-{bits}",
        sensor_resolution=(width, height),
        modes=(fakesdk.FakeMode((width, height), packed, unpacked, bits),),
        sample_bits=bits,
        sample_shift=((16 - bits) if (bits > 8 and alignment == "msb") else 0),
        mono=mono,
        stride_pad=16,
        controls={"FrameDurationLimits": (1_000, 1_000_000, 33_333),
                  "ExposureTime": (60, 500_000, 10_000)},
        properties={"UnitCellSize": (3450, 3450)},
    )


@pytest.mark.parametrize("key,width,height,bits,alignment,mono,note", CASES,
                         ids=[c[0] for c in CASES])
def test_the_matrix_opens_and_reads_exactly(opened, key, width, height, bits,
                                            alignment, mono, note):
    """Every row: the array that arrives is bit-for-bit what the fake wrote,
    at the right shape and dtype, and graded science."""
    sensor = _matrix_sensor(width, height, bits, mono, alignment)
    # The alignment the fake writes with and the one the config declares must
    # agree, and admission is what notices when they do not -- which is the
    # contract: the alignment is configuration, not something to infer from
    # the data.
    src = opened(sensor, raw_alignment=alignment,
                 preview_resolution=(width // 4 or 1, height // 4 or 1))

    read = src.read_raw()
    got = np.asarray(read.frame.data)
    assert got.shape == (height, width), note
    assert got.dtype == (np.uint8 if bits <= 8 else np.uint16)
    assert np.array_equal(got, fakesdk.expected_image(sensor, (width, height)))
    assert read.frame.validity == "science", read.frame.meta["raw_reservations"]
    assert read.frame.meta["raw_bits_nominal"] == bits
    assert read.frame.meta["raw_bits_source"] == "sensor-mode"
    assert src.describe().mono is mono
    assert tuple(src.describe().full_resolution) == (width, height)


@pytest.mark.parametrize("key,width,height,bits,alignment,mono,note", CASES,
                         ids=[c[0] for c in CASES])
def test_the_matrix_records_and_reads_back(tmp_path, fake_sdk, key, width,
                                           height, bits, alignment, mono,
                                           note):
    """The same rows through a real recording and the desktop reader.

    This is the part a synthetic camera could never cover: the frames come
    from the real adapter, through the real admission, into the real chunk
    writer, and are recomputed from the files by `scripts/read_capture.py`.
    """
    import importlib.util
    import json
    import sys
    import time
    from pathlib import Path

    from trilobite.app import Application
    from trilobite.config import AppConfig, CameraConfig, StorageConfig

    fake_sdk(_matrix_sensor(width, height, bits, mono, alignment))
    external = tmp_path / "stick"
    external.mkdir()
    app = Application(
        AppConfig(
            storage=StorageConfig(root=str(tmp_path / "internal"),
                                  chunk_frames=4, write_block_frames=2,
                                  reserve_mb=1, internal_reserve_mb=1),
            cameras=[CameraConfig(
                cam_id="left", backend="picamera2", fps=20.0,
                raw_alignment=alignment,
                preview_resolution=(max(1, width // 4), max(1, height // 4)),
                pipeline=[])],
        ),
        state_path=None, restore=False,
    )
    app.start()
    app.writer.retarget(external)
    try:
        # Every offered configuration for this sensor, not just the default:
        # the 8-bit rung's fidelity depends on the delivered depth and that is
        # what case D is for.
        offered = {(p["format"], p["exact"], p["validity_ceiling"])
                   for p in app.recording.options()["plans"]}
        if bits <= 8:
            assert ("raw8" not in {f for f, _e, _v in offered}
                    or all(e for f, e, _v in offered if f == "raw8")), (
                "an 8-bit store of 8-bit samples is exact")
        else:
            assert any(f == "raw8" and not e for f, e, _v in offered), (
                "a reduction from deeper samples is lossy")

        app.recording.arm_continuous("raw16", 20.0)
        app.recording.start_continuous()
        time.sleep(0.8)
        out = app.recording.stop_continuous()
    finally:
        app.stop()

    assert out["every_frame_accounted"] is True, note
    assert out["totals"]["frames_stored"] > 0

    spec = importlib.util.spec_from_file_location(
        "read_capture",
        Path(__file__).resolve().parent.parent / "scripts" / "read_capture.py")
    reader = importlib.util.module_from_spec(spec)
    sys.modules["read_capture"] = reader
    spec.loader.exec_module(reader)

    rec = reader.open_recording(Path(out["directory"]))
    assert rec.accounting()["unaccounted"] == 0
    _meta, pixels = next(iter(rec.frames("left")))
    assert pixels.shape == (height, width), "the reader agrees on the geometry"

    # The index's frame_bytes must match the array, which is the number the
    # MATLAB reader seeks with.
    index = json.loads(
        next((Path(out["directory"]) / "left").glob("*.index.json"))
        .read_text(encoding="utf-8"))
    arr = np.load(next((Path(out["directory"]) / "left").glob("*.npy")),
                  mmap_mode="r")
    assert index["frame_bytes"] == arr.shape[1] * arr.shape[2] * arr.dtype.itemsize


# -- S6: unlike heads ------------------------------------------------------


def _register(manager, cam_id, **kw):
    caps = {"reducible_to_eight": True, "packed_available": False,
            "measured": True}
    defaults = dict(width=1456, height=1088, fps=30.0, sample_bits=10,
                    alignment="lsb", mono=True)
    defaults.update(kw)
    manager.register(cam_id, caps, **defaults)


def _manager(tmp_path):
    from trilobite.config import StorageConfig
    from trilobite.recording.manager import RecordingManager
    from trilobite.storage.writer import SessionWriter

    cfg = StorageConfig(root=str(tmp_path / "internal"), reserve_mb=1,
                        internal_reserve_mb=1)
    writer = SessionWriter(cfg, cfg.root)
    return RecordingManager(cfg, writer)


def test_heads_of_equal_size_but_different_depth_are_refused(tmp_path):
    """S6. Equal sizes are not equal frames.

    One recording plan carries one depth and one reduction shift. Two heads at
    10 and 12 bits would have produced frames of the right shape with half of
    them at the wrong scale, and the shift was locked by whichever head
    delivered first -- so which half was wrong depended on thread timing.
    """
    m = _manager(tmp_path)
    _register(m, "left", sample_bits=10)
    _register(m, "right", sample_bits=12)
    with pytest.raises(RuntimeError, match="different depths"):
        m.common_geometry()


def test_heads_with_different_alignment_are_refused(tmp_path):
    """The shift between the two differs by a factor of 64."""
    m = _manager(tmp_path)
    _register(m, "left", alignment="lsb")
    _register(m, "right", alignment="msb")
    with pytest.raises(RuntimeError, match="different sample alignments"):
        m.common_geometry()


def test_a_mono_and_a_colour_head_are_refused(tmp_path):
    """The stored arrays would be indistinguishable and the mosaic order is
    not recoverable afterwards."""
    m = _manager(tmp_path)
    _register(m, "left", mono=True)
    _register(m, "right", mono=False)
    with pytest.raises(RuntimeError, match="mono and the other is a colour"):
        m.common_geometry()


def test_matching_heads_are_accepted(tmp_path):
    """The refusals above must not fire on the ordinary pair, which is the
    whole rig as it stands."""
    m = _manager(tmp_path)
    _register(m, "left")
    _register(m, "right")
    assert m.common_geometry() == (1456, 1088, 30.0)
    assert m.delivered_bits() == 10


def test_an_unestablished_depth_does_not_block_a_recording(tmp_path):
    """A backend that cannot state its depth -- replay, or a driver with no
    sensor modes -- must still be recordable. `None` on both heads agrees."""
    m = _manager(tmp_path)
    _register(m, "left", sample_bits=None)
    _register(m, "right", sample_bits=None)
    assert m.common_geometry() == (1456, 1088, 30.0)
    assert m.delivered_bits() is None


# -- S7: the 8-bit rung's fidelity depends on the sensor -------------------


def test_an_eight_bit_store_of_eight_bit_samples_is_exact():
    """S7. `raw8` was unconditionally lossy and diagnostic. On a sensor whose
    samples are 8 bits there is nothing below them to lose."""
    from trilobite.recording.ladder import RAW8, RAW16, Plan

    native = Plan(fmt=RAW8, fps=30.0, heads=("left",), width=64, height=48,
                  delivered_bits=8)
    assert native.exact is True
    assert native.validity_ceiling == "science"

    reduced = Plan(fmt=RAW8, fps=30.0, heads=("left",), width=64, height=48,
                   delivered_bits=10)
    assert reduced.exact is False
    assert reduced.validity_ceiling == "diagnostic"

    # And the full-depth rung is exact either way.
    assert Plan(fmt=RAW16, fps=30.0, heads=("left",), width=64, height=48,
                delivered_bits=10).exact is True


def test_an_unknown_depth_treats_an_eight_bit_store_as_a_reduction():
    """The conservative direction. Calling a lossy store exact is the error
    that cannot be undone from the file afterwards."""
    from trilobite.recording.ladder import RAW8, Plan

    unknown = Plan(fmt=RAW8, fps=30.0, heads=("left",), width=64, height=48,
                   delivered_bits=None)
    assert unknown.exact is False
    assert unknown.validity_ceiling == "diagnostic"


def test_the_offered_options_state_the_right_fidelity(tmp_path):
    from trilobite.recording.ladder import enumerate_plans

    deep = {(p["format"], p["exact"]) for p in enumerate_plans(
        heads=("left",), width=64, height=48, sensor_fps=30.0,
        available_formats={"raw16", "raw8"}, delivered_bits=10)}
    assert ("raw8", False) in deep

    native = {(p["format"], p["exact"]) for p in enumerate_plans(
        heads=("left",), width=64, height=48, sensor_fps=30.0,
        available_formats={"raw16", "raw8"}, delivered_bits=8)}
    assert ("raw8", True) in native


# -- S1: the metric quantities are unavailable without a pitch -------------


def test_the_derived_optics_refuse_to_compute_without_a_pitch():
    """S1. `f_px` is a pixel count derived from millimetres, so the pitch is
    the only thing relating them. It was a module constant -- the IMX296's --
    read directly by this arithmetic, so any other sensor inherited it and
    produced confident millimetres wrong by the ratio of the pitches."""
    from trilobite.calibration.settings import (
        BoardSpec,
        DerivedOptics,
        NominalOptics,
        invert_derived,
    )

    optics = NominalOptics()
    with_pitch = DerivedOptics.compute(optics, BoardSpec(), 100.0,
                                       pitch_mm=3.45e-3,
                                       pitch_source="driver:UnitCellSize")
    assert with_pitch.available is True
    assert with_pitch.f_px > 0
    assert with_pitch.pitch_mm == pytest.approx(3.45e-3)
    assert with_pitch.pitch_source == "driver:UnitCellSize"

    without = DerivedOptics.compute(optics, BoardSpec(), 100.0,
                                    pitch_mm=None, pitch_source="unknown")
    assert without.available is False
    assert without.f_px == 0.0
    assert without.baseline_mm == 0.0
    assert "pixel pitch is not established" in without.note
    # ...and the inverse refuses too, rather than returning a focal length in
    # the wrong units.
    assert invert_derived(1000.0, -0.5, 500.0, pitch_mm=None) == {}


def test_a_different_pitch_gives_a_different_answer():
    """The assertion that the pitch is load-bearing. If these came out equal,
    threading it through would be decoration."""
    from trilobite.calibration.settings import (
        BoardSpec,
        DerivedOptics,
        NominalOptics,
    )

    optics = NominalOptics()
    a = DerivedOptics.compute(optics, BoardSpec(), 100.0, pitch_mm=3.45e-3)
    b = DerivedOptics.compute(optics, BoardSpec(), 100.0, pitch_mm=1.55e-3)
    assert a.f_px != b.f_px
    assert a.baseline_mm != b.baseline_mm
    # The pixel-domain quantities do NOT depend on the pitch, which is the
    # other half of the contract: detection and alignment work in pixels and
    # must be unaffected by a missing physical constant.
    assert a.kappa_px == b.kappa_px
    assert a.alpha == b.alpha


def test_the_camera_is_asked_for_its_pitch(opened):
    """And the answer travels with its source, so a figure from a model table
    is distinguishable from one the driver published."""
    from trilobite.calibration.settings import camera_pitch

    class _Holder:
        def __init__(self, source):
            self.source = source

    src = opened(fakesdk.imx296_mono())
    assert camera_pitch(_Holder(src)) == (3.45e-3, "driver:UnitCellSize")


# -- S2: a placeholder geometry authorises nothing -------------------------


def test_an_unreported_geometry_is_none_rather_than_the_old_sensors_size():
    """S2. It returned `(1456, 1088)` after any failure -- the geometry of one
    particular sensor, handed back as though it had been measured, so every
    check built on it computed against a frame that may not exist."""
    from trilobite.calibration.settings import _full_resolution

    class _Broken:
        def describe(self):
            raise RuntimeError("camera not open")

    class _Empty:
        def describe(self):
            from trilobite.types import CameraInfo
            return CameraInfo(cam_id="x", model="m", backend="b",
                              full_resolution=(0, 0),
                              preview_resolution=(0, 0), mono=True)

    class _Holder:
        def __init__(self, source):
            self.source = source

    assert _full_resolution(_Holder(_Broken())) is None
    assert _full_resolution(_Holder(_Empty())) is None


def test_readiness_blocks_when_the_geometry_is_unknown(tmp_path):
    """And the refusal is blocking, because every check after it is arithmetic
    on the frame size."""
    from trilobite.calibration.settings import CalibrationSettings, readiness_report

    class _Source:
        is_open = True
        orientation = {"rotate_deg": 0}

        def describe(self):
            raise RuntimeError("not open")

    class _Cam:
        cam_id = "left"
        errors = 0

        def __init__(self):
            self.source = _Source()

        def mla_stage(self):
            from trilobite.config import StageConfig
            from trilobite.processing.stages.plenoptic import MLAGridOverlay
            return MLAGridOverlay(StageConfig(type="mla_grid_overlay", name="mla"))

    report = readiness_report([_Cam()], CalibrationSettings())
    geometry = [c for c in report["checks"] if c["id"] == "left.geometry"]
    assert geometry, [c["id"] for c in report["checks"]]
    assert geometry[0]["ok"] is False
    assert geometry[0]["blocking"] is True
    assert report["ready"] is False


# -- S8: state does not transfer across a sensor change --------------------


def test_saved_state_records_which_sensor_it_was_written_for(fake_sdk, tmp_path):
    """S8. State is keyed on the logical head name, so a replacement module in
    the same socket is handed the old one's state."""
    from trilobite.app import Application
    from trilobite.config import AppConfig, CameraConfig, StorageConfig

    fake_sdk(fakesdk.imx296_mono())
    app = Application(
        AppConfig(storage=StorageConfig(root=str(tmp_path / "s")),
                  cameras=[CameraConfig(cam_id="left", backend="picamera2",
                                        preview_resolution=(320, 240))]),
        state_path=None, restore=False)
    app.start()
    try:
        saved = app.cameras["left"].state_snapshot()
    finally:
        app.stop()

    identity = saved["sensor_identity"]
    assert identity["model"] == "imx296"
    assert identity["frame_shape"] == [1088, 1456]
    assert identity["raw_format"] == "R10"
    assert identity["sample_bits"] == 10
    assert identity["pixel_pitch_mm"] == pytest.approx(3.45e-3)


def test_state_from_another_sensor_keeps_orientation_and_drops_the_alignment(
    fake_sdk, tmp_path
):
    """The correction. Orientation still applies -- the mount has not moved --
    and the MLA alignment is in the old sensor's pixels, so it must not."""
    from trilobite.app import Application
    from trilobite.config import AppConfig, CameraConfig, StageConfig, StorageConfig

    def build(sensor):
        fake_sdk(sensor)
        app = Application(
            AppConfig(storage=StorageConfig(root=str(tmp_path / "s")),
                      cameras=[CameraConfig(
                          cam_id="left", backend="picamera2",
                          preview_resolution=(320, 240),
                          pipeline=[StageConfig(type="mla_grid_overlay", name="mla")])]),
            state_path=None, restore=False)
        app.start()
        return app

    old = build(fakesdk.imx296_mono())
    try:
        # `pitch_px`, not an offset: the stage wraps an offset modulo the
        # pitch when it binds to a sensor, so an offset is a poor witness for
        # "was this applied". The pitch is carried through unchanged.
        old.cameras["left"].mla_stage().params.pitch_px = 123.0
        old.cameras["left"].cfg.rotate_deg = 90
        saved = old.cameras["left"].state_snapshot()
    finally:
        old.stop()
    assert saved["pipeline"]["mla"]["pitch_px"] == 123.0

    # A different module in the same socket, under the same name.
    new = build(fakesdk.deep_mono_16())
    try:
        notes = new.cameras["left"].apply_state(saved)
        assert any("different sensor" in n for n in notes), notes
        assert new.cameras["left"].sensor_changed
        # Orientation restored...
        assert new.cameras["left"].cfg.rotate_deg == 90
        # ...alignment not.
        assert new.cameras["left"].mla_stage().params.pitch_px != 123.0
        # And it is visible in status, not only in a start-up log line.
        assert new.cameras["left"].status()["sensor_changed"]
    finally:
        new.stop()


def test_state_from_the_same_sensor_applies_in_full(fake_sdk, tmp_path):
    """The refusal above must not fire on an ordinary restart, which is the
    case that happens every day."""
    from trilobite.app import Application
    from trilobite.config import AppConfig, CameraConfig, StageConfig, StorageConfig

    def build():
        fake_sdk(fakesdk.imx296_mono())
        app = Application(
            AppConfig(storage=StorageConfig(root=str(tmp_path / "s")),
                      cameras=[CameraConfig(
                          cam_id="left", backend="picamera2",
                          preview_resolution=(320, 240),
                          pipeline=[StageConfig(type="mla_grid_overlay", name="mla")])]),
            state_path=None, restore=False)
        app.start()
        return app

    first = build()
    try:
        first.cameras["left"].mla_stage().params.pitch_px = 123.0
        saved = first.cameras["left"].state_snapshot()
    finally:
        first.stop()

    second = build()
    try:
        notes = second.cameras["left"].apply_state(saved)
        assert not any("different sensor" in n for n in notes), notes
        assert second.cameras["left"].sensor_changed == []
        assert second.cameras["left"].mla_stage().params.pitch_px == 123.0
    finally:
        second.stop()


def test_an_old_state_file_without_an_identity_still_restores(fake_sdk, tmp_path):
    """Absence is not a difference. A state file written before this field
    existed has no identity, and refusing it would break every existing rig
    for nothing."""
    from trilobite.app import _identity_differences

    assert _identity_differences({}, {"model": "imx296"}) == []
    assert _identity_differences({"model": "imx296"}, {}) == []
    # An unknown field on either side is not evidence of a change either.
    assert _identity_differences({"model": "imx296", "sample_bits": None},
                                 {"model": "imx296", "sample_bits": 10}) == []
    assert _identity_differences({"model": "imx296"},
                                 {"model": "imx477"}) == [
        "model was 'imx296', now 'imx477'"]
