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
"""

from __future__ import annotations

import numpy as np

from trilobite.cameras.offline import SyntheticSource
from trilobite.cameras.picam import _is_compressed_raw
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


# -- the raw format ---------------------------------------------------------


def test_pisp_compressed_formats_are_recognised():
    """Name-matched, because that is what libcamera exposes. The naming is
    stable: everything compressed carries PISP and COMP."""
    for name in ("MONO_PISP_COMP1", "PISP_COMP1_MONO", "pisp_comp1",
                 "BGGR_PISP_COMP1"):
        assert _is_compressed_raw(name), name


def test_ordinary_raw_formats_are_not_rejected():
    for name in ("R8", "R10", "R12", "R10_CSI2P", "SRGGB10", "SBGGR12_CSI2P"):
        assert not _is_compressed_raw(name), name


class _FakePicam:
    """Only what _choose_raw_format touches."""

    def __init__(self, modes):
        self.sensor_modes = modes


def _choose(cfg_format, modes):
    from trilobite.cameras.picam import Picamera2Source

    src = Picamera2Source(CameraConfig(
        cam_id="left", backend="picamera2", raw_format=cfg_format))
    return src._choose_raw_format(_FakePicam(modes))


IMX296_MODES = [
    {"format": "MONO_PISP_COMP1", "unpacked": "MONO_PISP_COMP1", "size": (1456, 1088)},
    {"format": "R10_CSI2P", "unpacked": "R10", "size": (1456, 1088)},
    {"format": "R8", "unpacked": "R8", "size": (1456, 1088)},
]


def test_a_compressed_format_is_never_chosen_automatically():
    """The bug, in one assertion."""
    assert not _is_compressed_raw(_choose(None, IMX296_MODES))


def test_the_widest_unpacked_format_is_preferred():
    """R10 over R10_CSI2P (no bit-unpacking left to do) and over R8 (a 10-bit
    sensor should not be quietly recorded at 8)."""
    assert _choose(None, IMX296_MODES) == "R10"


def test_an_explicit_config_format_wins():
    assert _choose("R12", IMX296_MODES) == "R12"


def test_an_explicit_compressed_format_is_honoured_but_logged(caplog):
    """Overriding is allowed -- it is the operator's rig -- but it cannot be
    silent, because silence is exactly what cost the session."""
    import logging

    with caplog.at_level(logging.ERROR):
        assert _choose("MONO_PISP_COMP1", IMX296_MODES) == "MONO_PISP_COMP1"
    assert any("COMPRESSED" in r.message for r in caplog.records)


def test_no_uncompressed_option_falls_back_loudly(caplog):
    import logging

    only_compressed = [{"format": "MONO_PISP_COMP1", "unpacked": "MONO_PISP_COMP1"}]
    with caplog.at_level(logging.ERROR):
        assert _choose(None, only_compressed) is None
    assert any("no uncompressed raw format" in r.message for r in caplog.records)
