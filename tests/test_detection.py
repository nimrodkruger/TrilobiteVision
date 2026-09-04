"""Corner detection in a micro-image, end to end, with no camera and no Pi.

The synthetic `plenoptic_board` source exists for exactly this: a frame whose
every micro-image contains a complete checkerboard, so a correct detector finds
one pattern per whole tile and any of the three usual mistakes -- wrong scale,
wrong offset, wrong board size -- finds none. Without it the difference between
"the detector works" and "the detector runs" is invisible.

The scale test is the one worth reading. It is the preview-vs-sensor factor of
two written as an assertion: run the detector with the preview-pixel geometry
on the full-resolution frame and it must fail badly, which is what the bug
looked like before geometry_for existed.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from trilobite.calibration.detect import CornerDetector, DetectionResult, TileResult, annotate
from trilobite.calibration.settings import AcceptanceSpec, BoardSpec
from trilobite.cameras.offline import SyntheticSource
from trilobite.config import CameraConfig
from trilobite.optics.mla import MLAGeometry

cv2 = pytest.importorskip("cv2")

BOARD = (4, 3)
PITCH = 100.0


def source(rotation=0.0):
    cfg = CameraConfig(
        cam_id="left",
        backend="synthetic",
        full_resolution=(1456, 1088),
        preview_resolution=(728, 544),
        synthetic_pattern="plenoptic_board",
        synthetic_pitch_px=PITCH,
        synthetic_rotation_deg=rotation,
        synthetic_board=BOARD,
        # No drift. The board otherwise wanders up to 3 px with the WALL CLOCK,
        # and at some phases an edge micro-image loses a corner -- so
        # "every whole tile yields the full pattern" fails about one run in
        # thirty, on the clock rather than on anything in the code. The same
        # flake was fixed in test_presence.py and not propagated here.
        synthetic_drift_px=0.0,
    )
    s = SyntheticSource(cfg)
    s.open()
    return s


def full_frame(src):
    """A full-resolution frame through the capture-thread handshake.

    Deliberately not a direct call into the source. Nothing outside the capture
    loop is allowed to touch the camera -- a second consumer of the picamera2
    request pool is what took the rig down -- so the tests exercise the same
    path the application uses: raise a flag, let the capture call service it.
    """
    src.request_full_frame()
    src.read_preview()
    return src.take_full_frame()


def detector(**kw):
    board = BoardSpec(cols=BOARD[0], rows=BOARD[1], square_mm=5.0)
    acc = AcceptanceSpec(**{"min_corners_per_tile": 6, "min_cross_tiles": 5, **kw})
    return CornerDetector(board, acc)


def sensor_geometry(rotation=0.0):
    return MLAGeometry(width=1456, height=1088, pitch=PITCH, rotation_deg=rotation)


# -- the full-frame handshake ------------------------------------------------


def test_a_full_frame_arrives_only_through_the_capture_call():
    src = source()
    src.request_full_frame()
    assert src.take_full_frame() is None, "nothing until the capture loop runs"
    src.read_preview()
    frame = src.take_full_frame()
    assert frame is not None
    assert frame.data.shape == (1088, 1456)


def test_the_full_frame_and_its_preview_share_a_sequence_number():
    """They are one exposure. A pose's frame and the presence map that
    triggered it must describe the same instant, not two a frame apart."""
    src = source()
    src.request_full_frame()
    preview = src.read_preview()
    assert src.take_full_frame().seq == preview.seq


def test_wait_full_frame_times_out_rather_than_hanging():
    src = source()
    src.close()                                    # no capture loop will run
    assert src.wait_full_frame(timeout=0.2) is None


# -- the happy path ---------------------------------------------------------


def test_every_whole_tile_yields_the_full_pattern():
    frame = full_frame(source())
    g = sensor_geometry()
    r = detector().run(frame.data, g, "left", frame.seq)
    assert len(r.tiles) > 100
    assert len(r.found_tiles) == len(r.tiles)
    assert all(t.n_corners == BOARD[0] * BOARD[1] for t in r.found_tiles)
    assert r.accepted


def test_corners_come_back_in_frame_coordinates_inside_their_own_tile():
    """A corner reported outside the tile it was found in means the tile-to-frame
    map is wrong, which is silent: the fit would simply converge on nonsense."""
    frame = full_frame(source())
    g = sensor_geometry()
    r = detector().run(frame.data, g, "left", frame.seq)
    side = g.crop_side(1.0)
    for t in r.found_tiles:
        x0, y0 = g.crop_origin(t.i, t.j, 1.0)
        assert t.corners[:, 0].min() >= x0 - 1
        assert t.corners[:, 1].min() >= y0 - 1
        assert t.corners[:, 0].max() <= x0 + side + 1
        assert t.corners[:, 1].max() <= y0 + side + 1


def test_rotation_does_not_stop_detection():
    frame = full_frame(source(rotation=3.0))
    g = sensor_geometry(rotation=3.0)
    r = detector().run(frame.data, g, "left", frame.seq, scale=0.9)
    assert len(r.found_tiles) > 0.9 * len(r.tiles)


def test_derotated_and_plain_crops_agree_on_corner_positions():
    """The claim that de-rotation costs precision but not validity
    (calibration-spec §2.6), as a test: both routes must put the corners in the
    same place to well under a pixel."""
    SCALE = 0.9

    frame = full_frame(source(rotation=4.0))
    g = sensor_geometry(rotation=4.0)
    det = detector()
    plain = det.run(frame.data, g, "left", 1, scale=SCALE, derotate=False)
    derot = det.run(frame.data, g, "left", 1, scale=SCALE, derotate=True)

    # SCALE is 0.9, not 0.85, and the difference is not arbitrary. The
    # synthetic board leaves a one-square quiet margin inside each
    # micro-image; a crop at 0.85 of the pitch cuts into it, and
    # findChessboardCornersSB can then lock onto a 4x3 sub-grid shifted by a
    # square. Both routes are then "right" about different patterns and the
    # corners differ by multiples of the square size. Measured across scales on
    # this target: 1.0, 0.95 and 0.9 all give zero disagreements out of ~130
    # tiles; 0.85 gives eight. That is a property of the target's margin, not
    # of the extraction, and this test is about the extraction.

    # Compared only on tiles that are whole under BOTH predicates. The two
    # routes read different sampling windows -- one axis-aligned, one rotated
    # with the lattice -- so at 4 degrees the outermost ring is whole for one
    # and reaches off-sensor for the other, where the bilinear sampler clamps
    # at the border. A clamped edge distorts the pattern and moves its corners
    # by a few pixels, which is a real property of sampling past the sensor and
    # not the precision claim being tested here.
    #
    # Five tiles, all at extreme indices, and they were being compared until
    # the synthetic board's wall-clock drift was pinned to zero -- the drift
    # had been moving them in and out of the intersection.
    both = (set(g.whole_indices(SCALE, derotate=True))
            & set(g.whole_indices(SCALE, derotate=False)))
    assert len(both) > 20, len(both)

    by_key = {t.key: t for t in derot.found_tiles}
    compared = 0
    for t in plain.found_tiles:
        other = by_key.get(t.key)
        if other is None or other.n_corners != t.n_corners:
            continue
        if tuple(t.key) not in both:
            continue
        d = np.linalg.norm(t.corners - other.corners, axis=1)
        assert d.mean() < 0.5, (t.key, d.mean())
        compared += 1
    assert compared > 20, compared


# -- the failure the scale fix exists to prevent ----------------------------


def test_preview_scale_geometry_on_a_full_frame_finds_almost_nothing():
    frame = full_frame(source())
    wrong = MLAGeometry(width=1456, height=1088, pitch=PITCH / 2)   # preview pitch
    r = detector().run(frame.data, wrong, "left", 1)
    assert len(r.found_tiles) == 0


def test_the_sensor_frame_is_the_reference_not_whatever_arrives():
    """The units decision, asserted. Parameters are SENSOR pixels, bound once
    from the camera; a preview frame flowing through establishes nothing.

    The old behaviour took the reference from the first frame it saw, which was
    always the preview, so the stored pitch silently meant preview pixels and
    every consumer needed a conversion it could forget. That is a factor of two
    hiding between the stored value and the fit."""
    from trilobite.processing.stages.plenoptic import MLAGridOverlay

    stage = MLAGridOverlay("mla", enabled=True, pitch_px=PITCH)
    src = source()
    stage.apply(src.read_preview())
    assert stage.reference_shape is None, "a preview frame must not bind the units"

    stage.bind_sensor(1456, 1088)
    assert stage.reference_shape == (1456, 1088)
    assert stage.params.pitch_px == pytest.approx(PITCH), "binding must not rescale"

    # For the sensor frame the conversion is now the identity ...
    frame = full_frame(src)
    g = stage.geometry_for(frame.data.shape[1], frame.data.shape[0])
    assert g.pitch == pytest.approx(PITCH)
    r = detector().run(frame.data, g, "left", 1)
    assert len(r.found_tiles) == len(r.tiles) > 100

    # ... and the overlay is the only place left that scales, downwards.
    assert stage.geometry_for(728, 544).pitch == pytest.approx(PITCH / 2)


def test_an_old_preview_referenced_alignment_is_rebased_once():
    """The migration path. A config or state file written when the parameters
    meant preview pixels carries its 728-wide reference, so binding to the
    sensor rescales it to mean the same physical lenslets -- once, loudly."""
    from trilobite.processing.stages.plenoptic import MLAGridOverlay

    stage = MLAGridOverlay("mla", enabled=True, pitch_px=PITCH / 2,
                           offset_x=3.0, offset_y=-4.0,
                           reference_width=728, reference_height=544)
    stage.bind_sensor(1456, 1088)

    assert stage.params.pitch_px == pytest.approx(PITCH)
    assert stage.params.offset_x == pytest.approx(6.0)
    assert stage.params.offset_y == pytest.approx(-8.0)
    assert stage.reference_shape == (1456, 1088)

    # Idempotent: binding again changes nothing.
    stage.bind_sensor(1456, 1088)
    assert stage.params.pitch_px == pytest.approx(PITCH)


def test_wrong_board_size_finds_nothing():
    frame = full_frame(source())
    board = BoardSpec(cols=9, rows=6, square_mm=20.0)
    det = CornerDetector(board, AcceptanceSpec())
    r = det.run(frame.data, sensor_geometry(), "left", 1)
    assert len(r.found_tiles) == 0
    assert not r.accepted


def test_flat_tiles_are_skipped_not_reported_as_examined_failures():
    flat = np.full((1088, 1456), 128, dtype=np.uint8)
    r = detector().run(flat, sensor_geometry(), "left", 1)
    assert r.tiles and all(t.skipped for t in r.tiles)
    assert not r.accepted


# -- the five-tile cross, which is all that runs live -----------------------


def test_a_five_tile_cross_costs_a_fraction_of_the_full_field():
    """Not a timing assertion -- a scope one. The live check must look at five
    tiles, not at all hundred and seventeen, because that ratio is the whole
    reason the design fits on a Pi."""
    frame = full_frame(source())
    g = sensor_geometry()
    det = detector()
    cross = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]
    found = sum(det.detect_tile(g.crop(frame.data, i, j, 1.0))[0] for i, j in cross)
    assert found == 5
    assert len(g.whole_indices(1.0, derotate=False)) > 100


# -- acceptance -------------------------------------------------------------


def result_with(found_keys):
    r = DetectionResult(cam_id="left", t_wall=0.0, seq=1, frame_shape=(1088, 1456), board=BOARD)
    for i in range(-2, 3):
        for j in range(-2, 3):
            hit = (i, j) in found_keys
            r.tiles.append(TileResult(i, j, hit, 12 if hit else 0))
    return r


def test_a_full_cross_is_accepted():
    assert detector().crosses(result_with({(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)})) == [(0, 0)]


def test_four_in_a_row_is_not_a_cross():
    assert detector().crosses(result_with({(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)})) == []


def test_a_partial_cross_is_accepted_when_the_rule_is_relaxed():
    det = detector(min_cross_tiles=3)
    assert (0, 0) in det.crosses(result_with({(0, 0), (1, 0), (0, 1)}))


def test_a_tile_below_the_corner_threshold_does_not_count():
    det = detector(min_corners_per_tile=20)
    assert det.crosses(result_with({(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)})) == []


# -- overlay ----------------------------------------------------------------


def test_annotate_produces_a_display_sized_colour_image():
    frame = full_frame(source())
    g = sensor_geometry()
    r = detector().run(frame.data, g, "left", 1)
    view = annotate(frame.data, r, g, display_width=728)
    assert view.shape[:2] == (544, 728)
    assert view.ndim == 3


def test_detection_result_json_carries_no_corner_arrays():
    frame = full_frame(source())
    r = detector().run(frame.data, sensor_geometry(), "left", 1)
    payload = r.as_dict()
    assert payload["tiles_found"] == len(r.found_tiles)
    assert all("corners" not in t for t in payload["tiles"])


def test_nothing_in_this_module_owns_a_thread_or_a_camera():
    """The worker that used to live here is what took the rig down. Its absence
    is a property worth asserting, not just a fact about the current file."""
    import trilobite.calibration.detect as mod

    source_text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "threading" not in source_text
    assert "capture_request" not in source_text
    assert not hasattr(mod, "DetectionWorker")


# -- raw row-stride padding -------------------------------------------------
#
# The IMX296 is 1456 px wide and 1456 is not a multiple of 32, so libcamera
# pads each raw row out to 1472 and picamera2 shapes the array by that stride.
# The reported symptom was tv_read_capture refusing a 1472x1088 frame as an
# anisotropic rescale (x2.0220 across, x2.0000 down). The refusal was right;
# the padding was the bug.


class _FakeRaw:
    """Just enough of PiCameraSource to exercise _trim_stride."""

    def __init__(self, full=(1456, 1088)):
        self._full_res = full
        self.cam_id = "left"

    _trim_stride = None       # filled in below


def _trimmer(full=(1456, 1088)):
    from trilobite.cameras.picam import Picamera2Source

    fake = _FakeRaw(full)
    # Bind every method the trim reaches, not only the entry point.
    for name in ("_trim_stride", "_unexpected"):
        setattr(fake, name, getattr(Picamera2Source, name).__get__(fake, _FakeRaw))
    return fake


def test_stride_padding_is_trimmed_from_raw_frames():
    padded = np.zeros((1088, 1472), dtype=np.uint8)
    padded[:, 1456:] = 200                      # the pad, visibly not image
    out, note = _trimmer()._trim_stride(padded)

    assert out.shape == (1088, 1456)
    assert note["raw_stride_px"] == 1472
    assert note["raw_padding_px"] == 16
    assert note["image_width"] == 1456
    assert out.max() == 0, "only the padding may be removed"


def test_the_trimmed_frame_rescales_isotropically():
    """The actual failure, in one assertion. A 1472-wide frame is 2.0220x the
    728-wide preview across and 2.0000x down, so no single pitch exists for it.
    Trimmed, the two agree exactly and MLAGeometry.rescaled accepts it."""
    from trilobite.optics.mla import MLAGeometry

    preview = MLAGeometry(728, 544, 50.0)
    with pytest.raises(ValueError, match="anisotropic"):
        preview.rescaled(1472, 1088)

    out, _ = _trimmer()._trim_stride(np.zeros((1088, 1472), dtype=np.uint8))
    g = preview.rescaled(out.shape[1], out.shape[0])
    assert g.pitch == pytest.approx(100.0)


def test_padding_would_move_the_grid_origin_off_centre():
    """Why it could not simply be ignored. The grid hangs off the frame centre,
    and the centre of a 1472-wide array is 8 px right of the image's -- a
    quarter of a checkerboard square on every micro-image, which reads as a rig
    that will not detect rather than as a units bug."""
    from trilobite.optics.mla import MLAGeometry

    padded = MLAGeometry(1472, 1088, 100.0)
    trimmed = MLAGeometry(1456, 1088, 100.0)
    assert padded.origin[0] - trimmed.origin[0] == pytest.approx(8.0)


def test_a_frame_that_is_already_the_right_width_is_untouched():
    data = np.arange(1088 * 1456, dtype=np.uint8).reshape(1088, 1456)
    out, note = _trimmer()._trim_stride(data)
    assert out is data
    assert "raw_padding_px" not in note


def test_an_unexplained_shape_is_flagged_not_cropped():
    """A packed raw format has an array width that is not a pixel count at all,
    so cropping it by pixels would be nonsense. Record and leave alone."""
    packed = np.zeros((1088, 1820), dtype=np.uint8)      # 10-bit packed, 5/4
    out, note = _trimmer()._trim_stride(packed)
    assert out is packed
    assert note["raw_unexpected_shape"] is True
    assert note["raw_buffer_shape"] == [1088, 1820]


# -- raw stride, at two bytes per pixel -------------------------------------
#
# The second half of the stride story. Once the raw format stopped being the
# compressed PISP one and became 10-bit R10, the buffer arrived as uint8 with
# the row length in BYTES: 1456 px = 2912 bytes, stride-aligned to 2944. The
# trim assumed one byte per pixel, so 2944 matched nothing and the frame was
# saved untouched -- 2944 x 1088, which the MATLAB reader reported as a
# 2.022 : 1.000 aspect ratio. Cropping its width to 1456 would have been worse:
# the first 728 pixels and half of the next, structure at the wrong scale.


def _bytes16(image16, pad_px=16):
    """Lay a uint16 image out the way make_array delivers it: uint8, row
    length in bytes, stride padding on the right."""
    padded = np.concatenate(
        [image16, np.full((image16.shape[0], pad_px), 0xBEEF, np.uint16)], axis=1)
    return np.ascontiguousarray(padded).view(np.uint8)


def test_a_10_bit_buffer_is_reinterpreted_and_trimmed():
    h, w = 1088, 1456
    truth = (np.arange(h * w, dtype=np.uint32) % 1024).astype(np.uint16).reshape(h, w)
    delivered = _bytes16(truth)
    assert delivered.shape == (1088, 2944), delivered.shape

    out, note = _trimmer()._trim_stride(delivered)

    assert out.dtype == np.uint16, "the pixels are 16-bit, not pairs of bytes"
    assert out.shape == (1088, 1456)
    assert np.array_equal(out, truth), "values must survive exactly -- this is a re-view"
    assert note["raw_bytes_per_pixel"] == 2
    assert note["raw_stride_bytes"] == 2944
    assert note["raw_padding_px"] == 16


def test_the_8_bit_case_still_works():
    """The other branch, unchanged: one byte per pixel, 1472 wide."""
    padded = np.zeros((1088, 1472), dtype=np.uint8)
    padded[:, 1456:] = 200
    out, note = _trimmer()._trim_stride(padded)
    assert out.dtype == np.uint8
    assert out.shape == (1088, 1456)
    assert note["raw_bytes_per_pixel"] == 1
    assert out.max() == 0, "only padding may be removed"


def test_an_already_uint16_buffer_is_handled():
    """If picamera2 ever hands over a real uint16 array, the row length in
    bytes is the same and the answer must be the same."""
    h, w = 1088, 1456
    truth = np.full((h, w), 513, np.uint16)
    padded = np.concatenate([truth, np.full((h, 16), 7, np.uint16)], axis=1)
    out, note = _trimmer()._trim_stride(padded)
    assert out.shape == (h, w)
    assert np.array_equal(out, truth)
    assert note["raw_bytes_per_pixel"] == 2


def test_a_packed_format_is_still_refused():
    """10-bit packed is 5 bytes per 4 pixels: 1820 bytes for 1456 px. That
    matches no whole bytes-per-pixel, and cropping it would be nonsense."""
    packed = np.zeros((1088, 1824), dtype=np.uint8)
    out, note = _trimmer()._trim_stride(packed)
    assert out is packed
    assert note["raw_unexpected_shape"] is True


def test_the_trimmed_16_bit_frame_rescales_isotropically():
    """The reported error, closed. 2944 x 1088 against a 1456-wide reference is
    2.0220 : 1.0000 and has no single pitch; trimmed, it is the identity."""
    from trilobite.optics.mla import MLAGeometry

    sensor = MLAGeometry(1456, 1088, 100.0)
    with pytest.raises(ValueError, match="anisotropic"):
        sensor.rescaled(2944, 1088)

    truth = np.zeros((1088, 1456), np.uint16)
    out, _ = _trimmer()._trim_stride(_bytes16(truth))
    g = sensor.rescaled(out.shape[1], out.shape[0])
    assert g.pitch == pytest.approx(100.0)
