"""Mirroring, and the raw format that must never be a compressed one.

Two unrelated properties, tested together because both are about a frame being
what it claims to be before anything downstream looks at it.

The raw-format tests exist because of a recorded session that came back as
structured noise: libcamera's default raw format on a Pi 5 for the mono IMX296
is MONO_PISP_COMP1, which is the imaging pipeline's *compressed* transport, one
byte per pixel. `make_array` hands those bytes over as a plain uint8 image, so
the result has the right shape and obvious structure and every value wrong --
the worst possible failure mode, because it looks like a picture that has gone
a bit wrong rather than like a decode failure.

What is asserted here is the CHOICE made at open time. What a buffer must
satisfy before it can call itself science data is a separate concern with no
camera dependency at all, and lives in test_rawformat.py.
"""

from __future__ import annotations

import numpy as np
import pytest

from trilobite.cameras.offline import SyntheticSource
from trilobite.cameras.rawformat import RawFormatError, classify
from trilobite.config import CameraConfig


def source(**kw):
    s = SyntheticSource(CameraConfig(
        cam_id="left", backend="synthetic", fps=1000,
        full_resolution=(160, 120), preview_resolution=(80, 60),
        synthetic_drift_px=0.0, **kw,
    ))
    s.open()
    return s


# -- the flip ---------------------------------------------------------------


def test_no_flip_is_the_identity():
    plain = source().read_preview().data
    again = source().read_preview().data
    assert np.array_equal(plain, again)


def test_horizontal_flip_mirrors_the_preview():
    plain = source().read_preview().data
    flipped = source(flip_horizontal=True).read_preview().data
    assert np.array_equal(flipped, np.flip(plain, axis=1))


def test_vertical_flip_mirrors_the_preview():
    plain = source().read_preview().data
    flipped = source(flip_vertical=True).read_preview().data
    assert np.array_equal(flipped, np.flip(plain, axis=0))


def test_both_flips_compose():
    plain = source().read_preview().data
    flipped = source(flip_horizontal=True, flip_vertical=True).read_preview().data
    assert np.array_equal(flipped, np.flip(np.flip(plain, axis=0), axis=1))


def test_the_flip_reaches_the_SAVED_frame_not_only_the_preview():
    """The whole point of the request, and the trap a display-only flip sets:
    you would mirror what you look at and not what you measure, and find out
    when the calibration comes back mirrored."""
    plain = source().capture_full(raw=True).data
    flipped = source(flip_horizontal=True).capture_full(raw=True).data
    assert flipped.shape == plain.shape
    assert np.array_equal(flipped, np.flip(plain, axis=1))


def test_the_full_frame_served_to_calibration_is_flipped_too():
    """Three paths produce pixels -- preview, capture_full, and the full frame
    the capture loop serves for pose detection. All three must agree, or the
    corners are recorded in a different frame from the one saved beside them."""
    s = source(flip_vertical=True)
    s.request_full_frame()
    s.read_preview()
    served = s.take_full_frame()
    assert served is not None

    # Compared with a tolerance, not exactly: two synthetic sources draw
    # independent sensor noise (sigma 1.5), so byte equality would be testing
    # the random number generator. The margin below is what makes it a real
    # test -- the correct orientation must be much closer than the wrong one.
    plain = source().capture_full(raw=False).data
    right = np.abs(served.data.astype(int) - np.flip(plain, axis=0).astype(int)).mean()
    wrong = np.abs(served.data.astype(int) - plain.astype(int)).mean()
    assert right < 5, right
    assert wrong > 4 * right, (right, wrong)


def test_a_flipped_frame_says_so_in_its_metadata():
    """A mirrored file that does not record the mirroring is unusable: nothing
    downstream can tell it from an un-mirrored one."""
    f = source(flip_horizontal=True, flip_vertical=False).capture_full()
    assert f.meta["flip_horizontal"] is True
    assert f.meta["flip_vertical"] is False


def test_the_flipped_array_is_contiguous():
    """np.flip returns a negative-stride view. Saving one works, but anything
    handing the buffer to a C library gets a surprise, so it is copied back."""
    f = source(flip_horizontal=True).capture_full()
    assert f.data.flags["C_CONTIGUOUS"]


# -- the raw format: a refusal, not a warning --------------------------------
#
# The old behaviour here was "log an error and carry on", in both of the cases
# that matter. A log line is not a control: nobody reads the journal of a rig
# that appears to be working, which is how 1,400 files of compressed transport
# came to be recorded. Both cases are now refusals at open time, and the only
# way past either is an explicit allow_unvalidated_raw in the config, which
# marks everything the camera produces as diagnostic for the rest of its life.


class _FakePicam:
    """Only what _choose_raw_format touches."""

    def __init__(self, modes):
        self.sensor_modes = modes


def _source(cfg_format=None, hatch=False):
    from trilobite.cameras.picam import Picamera2Source

    return Picamera2Source(CameraConfig(
        cam_id="left", backend="picamera2", raw_format=cfg_format,
        allow_unvalidated_raw=hatch))


def _choose(cfg_format, modes, hatch=False):
    src = _source(cfg_format, hatch)
    return src._choose_raw_format(_FakePicam(modes))


IMX296_MODES = [
    {"format": "MONO_PISP_COMP1", "unpacked": "MONO_PISP_COMP1", "size": (1456, 1088)},
    {"format": "R10_CSI2P", "unpacked": "R10", "size": (1456, 1088)},
    {"format": "R8", "unpacked": "R8", "size": (1456, 1088)},
]


def test_a_compressed_format_is_never_chosen_automatically():
    """The bug, in one assertion."""
    assert classify(_choose(None, IMX296_MODES)).admissible


def test_the_widest_unpacked_format_is_preferred():
    """R10 over R10_CSI2P (no bit-unpacking left to do) and over R8 (a 10-bit
    sensor should not be quietly recorded at 8)."""
    assert _choose(None, IMX296_MODES) == "R10"


def test_an_explicit_admissible_config_format_wins():
    assert _choose("R12", IMX296_MODES) == "R12"


def test_an_explicit_compressed_format_refuses_to_open():
    """Overriding used to be allowed with a log line. It is now a refusal:
    the operator asked for something that cannot be measured, and the camera
    declining to start is the only response that reaches them in time."""
    with pytest.raises(RawFormatError, match="COMPRESSED"):
        _choose("MONO_PISP_COMP1", IMX296_MODES)


def test_an_explicit_packed_format_refuses_to_open():
    with pytest.raises(RawFormatError, match="PACKED"):
        _choose("R10_CSI2P", IMX296_MODES)


def test_an_unknown_configured_format_refuses_to_open():
    with pytest.raises(RawFormatError, match="not one this code knows"):
        _choose("R11", IMX296_MODES)


def test_no_admissible_option_refuses_rather_than_falling_back():
    only_compressed = [{"format": "MONO_PISP_COMP1", "unpacked": "MONO_PISP_COMP1"}]
    with pytest.raises(RawFormatError, match="no admissible raw format"):
        _choose(None, only_compressed)


def test_the_hatch_turns_each_refusal_back_into_a_diagnostic_open(caplog):
    """Deliberately awkward but reachable: bringing a new sensor up needs to be
    possible. What it must not do is produce anything that calls itself
    science."""
    import logging

    src = _source("MONO_PISP_COMP1", hatch=True)
    with caplog.at_level(logging.ERROR):
        assert src._choose_raw_format(_FakePicam(IMX296_MODES)) == "MONO_PISP_COMP1"
    assert "UNVALIDATED" in src._raw_choice
    assert any("DIAGNOSTIC" in r.message for r in caplog.records)


# -- what the driver actually negotiated (R5) --------------------------------
#
# _choose_raw_format decides what to ASK FOR. Nothing it decides is evidence,
# because the driver answers for itself: it may substitute a format, and the
# raw stream's geometry is not the main stream's. The verdict is taken from
# camera_configuration() after configure, and these are the cases that
# separates.


def _read_back(negotiated, requested="R10", hatch=False, full=(1456, 1088)):
    from trilobite.cameras.picam import Picamera2Source

    src = Picamera2Source(CameraConfig(
        cam_id="left", backend="picamera2", full_resolution=full,
        allow_unvalidated_raw=hatch))
    src._full_res = full
    src._requested_raw = requested
    src._raw_choice = f"{requested} (test)"

    class _P:
        def camera_configuration(self):
            return {"raw": dict(negotiated)}

    return src, src._read_back_raw(_P())


def test_the_negotiated_format_is_what_decides_admissibility():
    src, neg = _read_back({"format": "R10", "size": (1456, 1088), "stride": 2944})
    assert src._raw_admissible is True
    assert neg == {"format": "R10", "size": (1456, 1088), "stride": 2944}


def test_a_driver_substituting_a_compressed_format_refuses_to_open():
    """The case the open-time check cannot see. R10 was requested and accepted;
    the driver handed back the compressed transport, and from that point every
    buffer is self-consistent with the substitution and looks correct."""
    with pytest.raises(RawFormatError, match="COMPRESSED"):
        _read_back({"format": "MONO_PISP_COMP1", "size": (1456, 1088)})


def test_a_driver_substituting_an_admissible_format_is_allowed_but_named(caplog):
    """R10 requested, R8 delivered. Readable, so not a refusal -- but two bits
    per pixel have gone and nothing downstream would otherwise say so."""
    import logging

    with caplog.at_level(logging.WARNING):
        src, _ = _read_back({"format": "R8", "size": (1456, 1088)})
    assert src._raw_admissible is True
    assert "R10 was requested" in src._raw_choice
    assert any("negotiated R8" in r.message for r in caplog.records)


def test_a_camera_with_no_raw_stream_after_configure_refuses():
    from trilobite.cameras.picam import Picamera2Source

    src = Picamera2Source(CameraConfig(cam_id="left", backend="picamera2"))

    class _P:
        def camera_configuration(self):
            return {}

    with pytest.raises(RawFormatError, match="no raw stream"):
        src._read_back_raw(_P())


def test_the_raw_geometry_is_recorded_independently_of_main():
    """`full_resolution` sizes the MAIN stream; raw is configured at the
    sensor's native size. Validating a raw buffer against the main resolution
    happens to work only while the two are equal."""
    src, neg = _read_back({"format": "R10", "size": (1456, 1088)},
                          full=(728, 544))
    assert neg["size"] == (1456, 1088)
    assert src._full_res == (728, 544)


# -- the wiring: capture_full through the admission boundary -----------------
#
# The boundary itself is tested exhaustively in test_rawformat.py with no
# camera at all. What is asserted here is that capture_full actually consults
# it, tags the frame accordingly, and records the evidence -- the seam where a
# correct check and a correct capture path can still fail to meet.


class _FakeRequest:
    def __init__(self, arrays, meta):
        self._arrays = arrays
        self._meta = meta
        self.released = False

    def make_array(self, stream):
        return self._arrays[stream]

    def get_metadata(self):
        return dict(self._meta)

    def release(self):
        self.released = True


class _FakePicam2:
    def __init__(self, arrays, raw_format, meta=None, raw_size=(1456, 1088),
                 stride=None):
        self._arrays = arrays
        self._raw_format = raw_format
        self._raw_size = raw_size
        self._stride = stride
        self._meta = meta or {"ExposureTime": 5000}
        self.requests: list[_FakeRequest] = []

    def capture_request(self):
        r = _FakeRequest(self._arrays, self._meta)
        self.requests.append(r)
        return r

    def camera_configuration(self):
        raw = {"format": self._raw_format, "size": self._raw_size}
        if self._stride is not None:
            raw["stride"] = self._stride
        return {"raw": raw}


def _wired(raw_format, buffer, *, hatch=False, full=(1456, 1088),
           raw_size=None, stride=None, rotate_deg=0, alignment="lsb"):
    """A source wired past open(), with the raw stream already negotiated.

    `hatch` is `allow_unvalidated_raw`, and it is the ONLY thing that decides
    whether an inadmissible buffer yields a diagnostic frame or a refusal --
    which is supervisory review R4. Admissibility itself comes from the
    negotiated format, exactly as `_read_back_raw` would set it.
    """
    from trilobite.cameras.picam import Picamera2Source
    from trilobite.cameras.rawformat import classify

    raw_size = raw_size or full
    src = Picamera2Source(CameraConfig(
        cam_id="left", backend="picamera2", full_resolution=full,
        rotate_deg=rotate_deg, allow_unvalidated_raw=hatch,
        raw_alignment=alignment))
    src._picam = _FakePicam2({"raw": buffer}, raw_format, raw_size=raw_size,
                             stride=stride)
    src._full_res = full
    src._open = True
    fmt = classify(raw_format)
    src._raw_admissible = bool(fmt and fmt.admissible)
    src._raw_choice = f"{raw_format} (test)"
    src._raw_negotiated = {"format": raw_format, "size": raw_size,
                           "stride": stride}
    return src


def _r10_bytes(h, w, pad_px=16):
    truth = (np.arange(h * w, dtype=np.uint32) % 1024).astype(np.uint16).reshape(h, w)
    padded = np.concatenate([truth, np.full((h, pad_px), 0x03FF, np.uint16)], axis=1)
    return truth, np.ascontiguousarray(padded).view(np.uint8)


def test_an_admitted_raw_capture_is_science_and_carries_its_evidence():
    truth, delivered = _r10_bytes(1088, 1456)
    f = _wired("R10", delivered, stride=2944).capture_full(raw=True)

    assert f.is_science and f.validity == "science"
    assert f.source_kind == "raw"
    assert f.data.shape == (1088, 1456)
    assert np.array_equal(f.data, truth)
    assert f.meta["raw_admitted"] is True
    assert f.meta["raw_format"] == "R10"
    assert f.meta["raw_bytes_per_pixel"] == 2
    assert f.meta["raw_padding_px"] == 16
    assert f.meta["raw_stride_source"] == "negotiated"
    assert f.meta["allow_unvalidated_raw"] is False


def test_a_science_request_fails_rather_than_degrading_when_the_hatch_is_shut():
    """Supervisory review R4, and the one that mattered most in this batch.

    The runtime path caught every admission failure and returned diagnostic
    pixels regardless, so a science capture request was answered -- and
    reported as a success -- with data the code had just established were not
    sensor counts. `allow_unvalidated_raw` was advertised as the way that
    happens and was never consulted.
    """
    buf = np.full((1088, 1456), 128, np.uint8)
    src = _wired("MONO_PISP_COMP1", buf)
    with pytest.raises(RawFormatError, match="no science capture to return"):
        src.capture_full(raw=True)
    assert all(r.released for r in src._picam.requests), (
        "a refusal must not cost a request from the pool")


def test_the_refusal_names_the_setting_that_would_permit_it():
    src = _wired("MONO_PISP_COMP1", np.zeros((1088, 1456), np.uint8))
    with pytest.raises(RawFormatError, match="allow_unvalidated_raw"):
        src.capture_full(raw=True)


def test_a_compressed_capture_comes_back_diagnostic_only_with_the_hatch_open():
    """With the opt-in set, a frame is still produced -- you have to be able to
    look at something while bringing a sensor up. What must not happen is that
    it calls itself science."""
    buf = np.full((1088, 1456), 128, np.uint8)
    f = _wired("MONO_PISP_COMP1", buf, hatch=True).capture_full(raw=True)

    assert not f.is_science and f.validity == "diagnostic"
    assert f.meta["raw_admitted"] is False
    assert "COMPRESSED" in f.meta["raw_refusal"]
    assert f.meta["raw_buffer_shape"] == [1088, 1456]
    assert f.meta["raw_negotiated_format"] == "MONO_PISP_COMP1"
    # Untouched, deliberately: there is no correct interpretation to apply, so
    # applying none is the honest answer.
    assert f.data.shape == (1088, 1456)


def test_a_buffer_that_betrays_a_validated_format_is_loud(caplog):
    """Different from the hatch case and worse. The format was checked and
    accepted at open time and the driver still delivered something that does
    not reconcile with it -- that indicts the driver, not the config."""
    import logging

    src = _wired("R10", np.zeros((1080, 2944), np.uint8))
    with caplog.at_level(logging.ERROR), pytest.raises(RawFormatError):
        src.capture_full(raw=True)
    assert any("validated at open time" in r.message for r in caplog.records)


def test_admission_uses_the_raw_geometry_not_the_main_resolution():
    """R5 at the capture seam. `full_resolution` sizes main; the raw stream is
    at the sensor's native size. Checking one against the other works only
    while they happen to be equal."""
    truth, delivered = _r10_bytes(1088, 1456)
    f = _wired("R10", delivered, full=(728, 544), raw_size=(1456, 1088),
               stride=2944).capture_full(raw=True)
    assert f.data.shape == (1088, 1456)
    assert f.meta["raw_width"] == 1456
    assert (f.meta["image_width"], f.meta["image_height"]) == (1456, 1088)


def test_a_stride_that_disagrees_with_the_negotiated_one_is_refused():
    """Once the driver states its stride there is no bounded-pad guessing
    left to do: the row length either matches it or the buffer is not the one
    the configuration describes."""
    _, delivered = _r10_bytes(1088, 1456, pad_px=16)          # 2944 bytes
    src = _wired("R10", delivered, stride=2912)               # driver says 2912
    with pytest.raises(RawFormatError, match="negotiated a stride"):
        src.capture_full(raw=True)


def test_admission_runs_before_orientation():
    """Load-bearing order. The stride padding is on the RIGHT of the buffer as
    the sensor delivers it; turn the frame first and it is along the bottom,
    where cropping the right removes real image instead."""
    truth, delivered = _r10_bytes(1088, 1456)
    f = _wired("R10", delivered, stride=2944,
               rotate_deg=90).capture_full(raw=True)

    assert f.data.shape == (1456, 1088), "the frame is turned"
    assert np.array_equal(f.data, np.rot90(truth, k=-1)), "and nothing else changed"
    assert f.meta["raw_padding_px"] == 16, "padding is still counted in sensor terms"
    assert (f.meta["image_width"], f.meta["image_height"]) == (1088, 1456)


def test_the_request_is_released_even_on_the_diagnostic_path():
    """A request left unreleased starves a four-deep pool and stalls the
    sensor, so neither a refusal nor a diagnostic capture may cost one."""
    src = _wired("MONO_PISP_COMP1", np.zeros((1088, 1456), np.uint8), hatch=True)
    src.capture_full(raw=True)
    assert all(r.released for r in src._picam.requests)


def test_the_processed_stream_is_unvalidated_not_science():
    """`raw=False` is the ISP output. Nothing establishes it as sensor counts,
    and nothing establishes that it is not -- which is what `unvalidated` says,
    and what the old default of `science` did not (R2)."""
    src = _wired("R10", np.zeros((1088, 2944), np.uint8))
    src._picam._arrays["main"] = np.zeros((1088, 1456), np.uint8)
    f = src.capture_full(raw=False)
    assert f.space == "mono8"
    assert f.validity == "unvalidated"
    assert f.source_kind == "isp_main"
    assert "raw_admitted" not in f.meta


def test_the_preview_and_the_served_full_frame_are_both_unvalidated():
    """Three producers, one rule. Neither ISP stream may claim admission."""
    s = source()
    s.request_full_frame()
    preview = s.read_preview()
    served = s.take_full_frame()
    assert preview.validity == "unvalidated"
    assert served.validity == "unvalidated"
