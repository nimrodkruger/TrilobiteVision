"""The raw admission boundary, exercised from byte arrays with no camera.

That is the whole point of the module under test: every rejection path here is
reachable without a Pi, without libcamera and without a sensor in a particular
mode, so the code that decides whether a buffer is measurement data can be
tested as thoroughly as the code that fits models to it.

The fixtures are built the way `make_array` delivers buffers -- uint8, row
length in BYTES, stride padding on the right -- because the failure being
guarded against is precisely the difference between a row length in bytes and
a row length in pixels.
"""

from __future__ import annotations

import numpy as np
import pytest

from trilobite.cameras.rawformat import (
    MAX_STRIDE_PAD_BYTES,
    Admitted,
    RawFormatError,
    admit,
    best_format,
    classify,
    describe_refusal,
)
from trilobite.types import DIAGNOSTIC, SCIENCE, UNVALIDATED, VALIDITIES

W, H = 1456, 1088                      # IMX296


# -- golden fixtures ---------------------------------------------------------


def r8(width=W, height=H, stride_bytes=1472, fill=0, pad=200):
    """An R8 buffer: one byte per pixel, rows padded to `stride_bytes`."""
    buf = np.full((height, stride_bytes), pad, np.uint8)
    buf[:, :width] = fill
    return buf


def r10_unpacked(image16=None, pad_px=0):
    """An R10 buffer as picamera2 delivers it: uint16 values, uint8 array.

    A C-contiguous uint16 row of N pixels is 2N bytes, and `make_array` shapes
    the array by that byte count. So a 1456 px row arrives 2912 bytes wide, or
    2944 once the stride pad is on -- which is what made the reader report a
    2.022 : 1.000 aspect ratio when the width was read as a pixel count.
    """
    if image16 is None:
        image16 = (np.arange(H * W, dtype=np.uint32) % 1024).astype(np.uint16).reshape(H, W)
    if pad_px:
        image16 = np.concatenate(
            [image16, np.full((image16.shape[0], pad_px), 0x03FF, np.uint16)], axis=1)
    return np.ascontiguousarray(image16).view(np.uint8)


def pisp_comp1(width=W, height=H):
    """MONO_PISP_COMP1 as it arrives: one byte per pixel, plausible structure.

    Deliberately not noise. The reason this cost a session is that a compressed
    buffer LOOKS like an image, so a fixture of random bytes would be testing an
    easier problem than the real one.
    """
    x = np.linspace(0, 255, width, dtype=np.float32)
    y = np.linspace(0, 255, height, dtype=np.float32)[:, None]
    return ((x + y) / 2).astype(np.uint8)


# -- classification ----------------------------------------------------------


@pytest.mark.parametrize("name,bits,bpp", [
    ("R8", 8, 1), ("R10", 10, 2), ("R12", 12, 2), ("R16", 16, 2),
    ("SRGGB10", 10, 2), ("SBGGR12", 12, 2),
])
def test_the_known_formats_state_their_own_pixel_size(name, bits, bpp):
    fmt = classify(name)
    assert fmt is not None and fmt.admissible
    assert (fmt.bits, fmt.bytes_per_pixel) == (bits, bpp)
    assert fmt.max_value == (1 << bits) - 1


def test_compressed_and_packed_each_disqualify_on_their_own():
    """Tested as an invariant, not through the table, and the difference
    matters. Every compressed entry in KNOWN_FORMATS happens to carry
    `bytes_per_pixel = 0`, so `admissible` would give the right answer for all
    of them even if it stopped looking at `compressed` at all -- a mutation
    that removes the term survives the table-driven tests entirely. It is only
    caught by asking the question directly: a compressed format with a
    perfectly ordinary pixel size is still not admissible, because the
    objection is to what the bytes MEAN, not to how many there are.
    """
    from trilobite.cameras.rawformat import RawFormat

    plausible = dict(name="X", bits=8, bytes_per_pixel=1)
    assert RawFormat(**plausible).admissible
    assert not RawFormat(**plausible, compressed=True).admissible
    assert not RawFormat(**plausible, packed=True).admissible
    assert not RawFormat(name="X", bits=8, bytes_per_pixel=0).admissible


@pytest.mark.parametrize("name", [
    "MONO_PISP_COMP1", "PISP_COMP1_BGGR", "mono_pisp_comp1", "SOMETHING_COMP2",
])
def test_a_compressed_transport_is_recognised_and_refused(name):
    fmt = classify(name)
    assert fmt is not None, "recognised, so the refusal can say WHY"
    assert fmt.compressed and not fmt.admissible
    assert "COMPRESSED" in describe_refusal(name)


@pytest.mark.parametrize("name", ["R10_CSI2P", "SRGGB10_CSI2P"])
def test_a_packed_format_is_refused_and_names_its_unpacked_variant(name):
    fmt = classify(name)
    assert fmt is not None and fmt.packed and not fmt.admissible
    assert name.replace("_CSI2P", "") in describe_refusal(name)


@pytest.mark.parametrize("name", ["", None, "R11", "YUV420", "BGR888"])
def test_an_unknown_name_is_not_guessed_at(name):
    """An allowlist, not a denylist. The failure guarded against is an
    unfamiliar format arriving and being treated as ordinary pixels, so
    'not recognised' must mean 'refused', never 'probably fine'."""
    assert classify(name) is None
    assert "not one this code knows" in describe_refusal(name)


def test_best_format_takes_the_widest_unpacked_depth():
    """A 10-bit sensor recorded at 8 has thrown away two bits that cannot be
    recovered, and a packed name is not a fallback -- it is a format this code
    would mis-read."""
    assert best_format(["R8", "R10", "R10_CSI2P"]) == "R10"
    assert best_format(["R8", "R12_CSI2P"]) == "R8"
    assert best_format(["MONO_PISP_COMP1", "R10_CSI2P"]) is None
    assert best_format([]) is None


# -- admission, the happy paths ---------------------------------------------


def test_an_8_bit_buffer_loses_its_padding_and_nothing_else():
    got = admit(r8(), "R8", (W, H))
    assert isinstance(got, Admitted)
    assert got.array.shape == (H, W)
    assert got.array.dtype == np.uint8
    assert got.array.max() == 0, "only the padding may be removed"
    assert got.meta["raw_bytes_per_pixel"] == 1
    assert got.meta["raw_stride_bytes"] == 1472
    assert got.meta["raw_padding_px"] == 16


def test_a_10_bit_buffer_is_re_viewed_at_the_right_pixel_size():
    """The bug in one assertion. 2944 bytes is 1472 uint16 pixels, not 2944
    of anything. Cropping the width to 1456 would keep the first 728 pixels
    and half of the 729th: structure, at the wrong scale."""
    truth = (np.arange(H * W, dtype=np.uint32) % 1024).astype(np.uint16).reshape(H, W)
    delivered = r10_unpacked(truth, pad_px=16)
    assert delivered.shape == (H, 2944)

    got = admit(delivered, "R10", (W, H))
    assert got.array.dtype == np.uint16, "the pixels are 16-bit, not pairs of bytes"
    assert got.array.shape == (H, W)
    assert np.array_equal(got.array, truth), "a re-view: values must survive exactly"
    assert got.meta["raw_bytes_per_pixel"] == 2
    assert got.meta["raw_stride_bytes"] == 2944
    assert got.meta["raw_padding_px"] == 16


def test_an_unpadded_buffer_needs_no_padding_removed():
    got = admit(r10_unpacked(pad_px=0), "R10", (W, H))
    assert got.array.shape == (H, W)
    assert got.meta["raw_padding_px"] == 0


def test_an_already_uint16_buffer_gives_the_same_answer():
    """If picamera2 ever hands over a real uint16 array the row length in
    BYTES is unchanged, so the verdict must be too."""
    truth = np.full((H, W), 513, np.uint16)
    padded = np.concatenate([truth, np.full((H, 16), 7, np.uint16)], axis=1)
    got = admit(padded, "R10", (W, H))
    assert np.array_equal(got.array, truth)
    assert got.meta["raw_bytes_per_pixel"] == 2


# -- admission, the rejections ----------------------------------------------


def test_a_compressed_buffer_is_refused_however_plausible_it_looks():
    """The session-losing case. The buffer is exactly the sensor's shape, one
    byte per pixel, with visible structure -- everything a shape check would
    accept. It is refused on the FORMAT, which is the only evidence that
    distinguishes it."""
    buf = pisp_comp1()
    assert buf.shape == (H, W), "shape alone says this is a perfectly good frame"
    with pytest.raises(RawFormatError, match="COMPRESSED"):
        admit(buf, "MONO_PISP_COMP1", (W, H))


def test_a_packed_buffer_is_refused_rather_than_cropped():
    """10-bit packed is 5 bytes per 4 pixels: 1820 bytes for 1456 px. Its width
    is not a pixel count at any scale, so cropping it would be nonsense."""
    with pytest.raises(RawFormatError, match="PACKED"):
        admit(np.zeros((H, 1820), np.uint8), "R10_CSI2P", (W, H))


def test_an_unknown_format_is_refused_even_when_the_shape_fits_perfectly():
    with pytest.raises(RawFormatError, match="not one this code knows"):
        admit(np.zeros((H, W), np.uint8), "R11", (W, H))


def test_a_row_count_that_disagrees_is_the_finding_not_a_thing_to_fix():
    """Padding is a per-row phenomenon. A row COUNT that disagrees with the
    sensor means this is not the frame it claims to be, and no reshaping of it
    is legitimate."""
    with pytest.raises(RawFormatError, match="rows"):
        admit(r8(height=1080), "R8", (W, H))


def test_a_buffer_short_of_the_sensor_width_is_refused():
    with pytest.raises(RawFormatError, match="Off by -"):
        admit(np.zeros((H, W - 8), np.uint8), "R8", (W, H))


def test_padding_beyond_the_bound_is_refused():
    """The allowance is generous because the alignment is not ours to promise,
    and bounded because an unbounded one would accept a buffer of an entirely
    different format whose row happens to be longer."""
    admit(r8(stride_bytes=W + MAX_STRIDE_PAD_BYTES), "R8", (W, H))
    with pytest.raises(RawFormatError, match="stride padding"):
        admit(r8(stride_bytes=W + MAX_STRIDE_PAD_BYTES + 1), "R8", (W, H))


def test_the_two_readings_of_a_2944_byte_row_are_distinguished_by_the_format():
    """The direction of the check, asserted. The same bytes are a valid R10
    frame and an invalid R8 one, and only the negotiated format decides which.
    Inferring the pixel size from the row length -- which is what this
    replaced -- cannot tell these apart."""
    buf = r10_unpacked(pad_px=16)
    assert admit(buf, "R10", (W, H)).array.shape == (H, W)
    with pytest.raises(RawFormatError, match="Off by 1488"):
        admit(buf, "R8", (W, H))


def test_an_odd_row_length_cannot_be_whole_16_bit_pixels():
    with pytest.raises(RawFormatError, match="odd number"):
        admit(np.zeros((H, 2913), np.uint8), "R10", (W, H))


def test_a_value_above_the_declared_bit_depth_indicts_the_driver():
    """The check with teeth against a driver that hands back something other
    than what it negotiated: R10 that is really R12 has the right stride, the
    right shape and the right dtype, and differs only in its values."""
    truth = np.zeros((H, W), np.uint16)
    truth[10, 10] = 1024                     # one count past 10-bit
    with pytest.raises(RawFormatError, match="above the 1023 maximum"):
        admit(r10_unpacked(truth), "R10", (W, H))


def test_the_value_check_can_be_skipped_and_says_so_in_the_evidence():
    """About 1 ms on a 1.6 Mpx frame, which is why it is on the still path and
    off the preview path. Whether it ran is recorded, so a sidecar never
    implies a check that did not happen."""
    truth = np.zeros((H, W), np.uint16)
    truth[0, 0] = 4095
    got = admit(r10_unpacked(truth), "R10", (W, H), check_values=False)
    assert got.meta["raw_values_checked"] is False
    assert admit(r8(), "R8", (W, H)).meta["raw_values_checked"] is True


def test_a_one_dimensional_buffer_is_refused():
    with pytest.raises(RawFormatError, match="one plane"):
        admit(np.zeros(H * W, np.uint8), "R8", (W, H))


# -- what the evidence is for -----------------------------------------------


def test_the_admitted_frame_rescales_isotropically():
    """The reported symptom, closed. 2944 x 1088 against a 1456-wide reference
    is 2.0220 across and 2.0000 down, so no single pitch exists for it."""
    from trilobite.optics.mla import MLAGeometry

    sensor = MLAGeometry(W, H, 100.0)
    with pytest.raises(ValueError, match="anisotropic"):
        sensor.rescaled(2944, H)

    got = admit(r10_unpacked(np.zeros((H, W), np.uint16), pad_px=16), "R10", (W, H))
    assert sensor.rescaled(got.array.shape[1], got.array.shape[0]).pitch == pytest.approx(100.0)


def test_padding_would_move_the_grid_origin_off_centre():
    """Why it could not simply be ignored. The grid hangs off the frame centre,
    and the centre of a 1472-wide array is 8 px right of the image's -- a
    quarter of a checkerboard square on every micro-image, which reads as a rig
    that will not detect rather than as a units bug."""
    from trilobite.optics.mla import MLAGeometry

    assert (MLAGeometry(1472, H, 100.0).origin[0]
            - MLAGeometry(W, H, 100.0).origin[0]) == pytest.approx(8.0)


def test_a_frame_asserts_nothing_about_itself_by_default():
    """Supervisory review R2, as one assertion.

    `validity` used to default to `science`, so every construction that never
    considered the question made the strongest claim in the system by
    omission. The default now says nothing, and `science` has to be put there
    by something that established it.
    """
    from trilobite.types import Frame

    f = Frame.now(np.zeros((4, 4), np.uint8), "left", 1)
    assert f.validity == UNVALIDATED
    assert not f.is_science
    assert f.source_kind == "unknown"


def test_unvalidated_is_not_a_weaker_diagnostic():
    """Three values, three meanings. `diagnostic` is a positive statement that
    the values are wrong; `unvalidated` is the absence of any statement. Both
    are inadmissible, and collapsing them would make an ISP frame and a
    compressed buffer indistinguishable in the record."""
    assert len({SCIENCE, DIAGNOSTIC, UNVALIDATED}) == 3
    assert VALIDITIES == (SCIENCE, DIAGNOSTIC, UNVALIDATED)


def test_validity_and_source_survive_a_pipeline_stage():
    """`derive` carries both automatically, which is the reason they are fields
    and not metadata keys: a claim about admissibility must be impossible to
    lose by forgetting to copy a dictionary entry."""
    from trilobite.types import SRC_RAW, Frame

    f = Frame.now(np.zeros((4, 4), np.uint8), "left", 1,
                  validity=DIAGNOSTIC, source_kind=SRC_RAW)
    out = f.derive(np.ones((4, 4), np.uint8))
    assert out.validity == DIAGNOSTIC
    assert out.source_kind == SRC_RAW


# -- R3: the sample representation, not merely its size ----------------------
#
# `dtype.itemsize` is not a check on the dtype. int16, float16 and uint16 are
# all two bytes wide, and probes admitted int16(-1) and float16(0.5) as 10-bit
# sensor counts. A negative count and a fractional count are both impossible,
# so the invariant the module advertised was simply false.


@pytest.mark.parametrize("dtype,why", [
    (np.int16, "signed integer"),
    (np.float16, "floating point"),
])
def test_a_two_byte_buffer_that_is_not_unsigned_is_refused(dtype, why):
    buf = np.zeros((H, W + 16), dtype)
    with pytest.raises(RawFormatError, match=why):
        admit(buf, "R10", (W, H))


def test_a_negative_sample_is_refused_before_any_value_check():
    """The exact probe from the review. It has the right item size, the right
    shape and the right stride, and -1 is not a photon count."""
    buf = np.full((H, W + 16), -1, np.int16)
    with pytest.raises(RawFormatError, match="cannot be negative or fractional"):
        admit(buf, "R10", (W, H))


def test_a_fractional_sample_is_refused():
    buf = np.full((H, W + 16), 0.5, np.float16)
    with pytest.raises(RawFormatError, match="cannot be negative or fractional"):
        admit(buf, "R10", (W, H))


def test_a_byte_swapped_buffer_is_refused():
    """Right item size, right shape, every value wrong by a byte swap."""
    buf = np.zeros((H, W + 16), np.dtype(">u2"))
    with pytest.raises(RawFormatError, match="byte order"):
        admit(buf, "R10", (W, H))


@pytest.mark.parametrize("size", [(0, H), (W, 0), (-1, H)])
def test_a_non_positive_negotiated_geometry_is_refused(size):
    with pytest.raises(RawFormatError, match="non-positive"):
        admit(r8(), "R8", size)


# -- R3: sample depth, container width and alignment are three things --------
#
# The Picamera2 manual describes Pi 5 uncompressed samples as left-shifted in
# their 16-bit word, and warns against deriving the sensor depth from the
# format name. `R10` says the sample is ten bits; it does not say whether those
# ten bits are 0-9 or 6-15, and the two readings differ by a factor of 64 in
# every pixel.


def test_the_three_quantities_are_recorded_separately():
    got = admit(r10_unpacked(pad_px=16), "R10", (W, H))
    assert got.meta["raw_bits_nominal"] == 10
    assert got.meta["raw_bits_source"] == "format-name"
    assert got.meta["raw_container_bits"] == 16
    assert got.meta["raw_alignment"] == "lsb"
    assert got.meta["raw_sample_shift"] == 0


def test_left_aligned_samples_are_admitted_when_declared():
    """A 10-bit sample shifted into bits 6-15 reaches 65472, which the
    right-aligned reading refuses outright."""
    samples = np.full((H, W), 1000, np.uint16)
    buf = r10_unpacked(samples << 6, pad_px=16)

    with pytest.raises(RawFormatError, match="above the 1023 maximum"):
        admit(buf, "R10", (W, H), alignment="lsb")

    got = admit(buf, "R10", (W, H), alignment="msb")
    assert got.meta["raw_sample_shift"] == 6
    assert got.meta["raw_value_ceiling"] == 1023 << 6
    assert got.meta["raw_observed_max"] == 64000
    assert got.meta["raw_low_bits_zero_filled"] is True
    # The pixels are NOT shifted on the way out. Rescaling every value
    # silently on the way to disk is the act this boundary exists to prevent.
    assert int(got.array.max()) == 64000


def test_a_left_aligned_refusal_names_the_setting_that_would_fix_it():
    buf = r10_unpacked(np.full((H, W), 1000, np.uint16) << 6, pad_px=16)
    with pytest.raises(RawFormatError, match="raw_alignment"):
        admit(buf, "R10", (W, H), alignment="lsb")


def test_left_alignment_still_catches_a_wider_sample():
    """The ceiling is weaker under msb but not absent: 12-bit data delivered as
    R10 reaches 65520, past the 65472 a left-aligned 10-bit sample can hold."""
    buf = r10_unpacked(np.full((H, W), 4095, np.uint16) << 4, pad_px=16)
    with pytest.raises(RawFormatError, match="above the 65472 maximum"):
        admit(buf, "R10", (W, H), alignment="msb")


def test_non_zero_low_bits_are_recorded_rather_than_refused():
    """Some pipelines replicate the high bits downward instead of zero-filling.
    That is not proof of anything wrong, so it is evidence, not a refusal."""
    buf = r10_unpacked((np.full((H, W), 1000, np.uint16) << 6) | 0b101010, pad_px=16)
    got = admit(buf, "R10", (W, H), alignment="msb")
    assert got.meta["raw_low_bits_zero_filled"] is False
    assert got.meta["raw_low_bits_max"] == 0b101010


def test_an_unknown_alignment_is_refused_rather_than_guessed():
    """A dark left-aligned frame and a bright right-aligned one have
    indistinguishable histograms. A guess would enter the record with the same
    confidence as a measurement."""
    with pytest.raises(RawFormatError, match="cannot be guessed"):
        admit(r8(), "R8", (W, H), alignment="whatever")


def test_alignment_is_moot_when_the_sample_fills_its_container():
    """R8 in one byte and R16 in two have nowhere to shift to."""
    for name in ("R8",):
        for how in ("lsb", "msb"):
            assert admit(r8(), name, (W, H), alignment=how).meta["raw_sample_shift"] == 0


# -- R5: the negotiated stride is exact, not a guide -------------------------


def test_the_negotiated_stride_must_match_exactly():
    buf = r10_unpacked(pad_px=16)                       # 2944 bytes per row
    assert admit(buf, "R10", (W, H), stride_bytes=2944).array.shape == (H, W)
    with pytest.raises(RawFormatError, match="negotiated a stride"):
        admit(buf, "R10", (W, H), stride_bytes=2912)


def test_which_stride_rule_was_applied_is_recorded():
    """Two different strengths of evidence, and a sidecar must not present them
    as the same thing."""
    buf = r10_unpacked(pad_px=16)
    assert admit(buf, "R10", (W, H), stride_bytes=2944).meta["raw_stride_source"] == "negotiated"
    assert admit(buf, "R10", (W, H)).meta["raw_stride_source"] == "inferred"


def test_an_internally_inconsistent_negotiation_is_refused():
    """A driver reporting a stride narrower than one row of pixels is
    describing a configuration nothing here can reconcile."""
    with pytest.raises(RawFormatError, match="internally inconsistent"):
        admit(r10_unpacked(pad_px=16), "R10", (W, H), stride_bytes=100)
