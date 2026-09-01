"""Live corner detection, end to end, with no camera and no Pi.

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
    )
    s = SyntheticSource(cfg)
    s.open()
    return s


def detector(**kw):
    board = BoardSpec(cols=BOARD[0], rows=BOARD[1], square_mm=5.0)
    acc = AcceptanceSpec(**{"min_corners_per_tile": 6, "min_cross_tiles": 5, **kw})
    return CornerDetector(board, acc)


def sensor_geometry(rotation=0.0):
    return MLAGeometry(width=1456, height=1088, pitch=PITCH, rotation_deg=rotation)


# -- the happy path ---------------------------------------------------------


def test_every_whole_tile_yields_the_full_pattern():
    frame = source().read_full_mono()
    g = sensor_geometry()
    r = detector().run(frame.data, g, "left", frame.seq)
    assert len(r.tiles) > 100
    assert len(r.found_tiles) == len(r.tiles)
    assert all(t.n_corners == BOARD[0] * BOARD[1] for t in r.found_tiles)
    assert r.accepted


def test_corners_come_back_in_frame_coordinates_inside_their_own_tile():
    """A corner reported outside the tile it was found in means the tile-to-frame
    map is wrong, which is silent: the fit would simply converge on nonsense."""
    frame = source().read_full_mono()
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
    frame = source(rotation=3.0).read_full_mono()
    g = sensor_geometry(rotation=3.0)
    r = detector().run(frame.data, g, "left", frame.seq, scale=0.9)
    assert len(r.found_tiles) > 0.9 * len(r.tiles)


def test_derotated_and_plain_crops_agree_on_corner_positions():
    """The claim that de-rotation costs precision but not validity
    (calibration-spec §2.6), as a test: both routes must put the corners in the
    same place to well under a pixel."""
    frame = source(rotation=4.0).read_full_mono()
    g = sensor_geometry(rotation=4.0)
    det = detector()
    plain = det.run(frame.data, g, "left", 1, scale=0.85, derotate=False)
    derot = det.run(frame.data, g, "left", 1, scale=0.85, derotate=True)

    by_key = {t.key: t for t in derot.found_tiles}
    compared = 0
    for t in plain.found_tiles:
        other = by_key.get(t.key)
        if other is None or other.n_corners != t.n_corners:
            continue
        # Both detectors order corners consistently for the same pattern, so a
        # direct comparison is meaningful.
        d = np.linalg.norm(t.corners - other.corners, axis=1)
        assert d.mean() < 0.5
        compared += 1
    assert compared > 20


# -- the failure the scale fix exists to prevent ----------------------------


def test_preview_scale_geometry_on_a_full_frame_finds_almost_nothing():
    frame = source().read_full_mono()
    wrong = MLAGeometry(width=1456, height=1088, pitch=PITCH / 2)   # preview pitch
    r = detector().run(frame.data, wrong, "left", 1)
    assert len(r.found_tiles) == 0


def test_geometry_for_recovers_the_right_scale():
    from trilobite.processing.stages.plenoptic import MLAGridOverlay

    stage = MLAGridOverlay("mla", enabled=True, pitch_px=PITCH / 2)
    src = source()
    stage.apply(src.read_preview())          # learns the 728x544 reference
    assert stage.reference_shape == (728, 544)

    frame = src.read_full_mono()
    g = stage.geometry_for(frame.data.shape[1], frame.data.shape[0])
    assert g.pitch == pytest.approx(PITCH)
    r = detector().run(frame.data, g, "left", 1)
    assert len(r.found_tiles) == len(r.tiles) > 100


def test_wrong_board_size_finds_nothing():
    frame = source().read_full_mono()
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


# -- acceptance -------------------------------------------------------------


def result_with(found_keys):
    r = DetectionResult(cam_id="left", t_wall=0.0, seq=1, frame_shape=(1088, 1456), board=BOARD)
    for i in range(-2, 3):
        for j in range(-2, 3):
            hit = (i, j) in found_keys
            r.tiles.append(TileResult(i, j, hit, 12 if hit else 0))
    return r


def test_a_full_cross_is_accepted():
    det = detector()
    r = result_with({(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)})
    assert det.crosses(r) == [(0, 0)]


def test_four_in_a_row_is_not_a_cross():
    det = detector()
    r = result_with({(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)})
    assert det.crosses(r) == []


def test_a_partial_cross_is_accepted_when_the_rule_is_relaxed():
    det = detector(min_cross_tiles=3)
    r = result_with({(0, 0), (1, 0), (0, 1)})
    assert (0, 0) in det.crosses(r)


def test_a_tile_below_the_corner_threshold_does_not_count():
    det = detector(min_corners_per_tile=20)
    r = result_with({(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)})   # 12 corners each
    assert det.crosses(r) == []


# -- overlay ----------------------------------------------------------------


def test_annotate_produces_a_display_sized_colour_image():
    frame = source().read_full_mono()
    g = sensor_geometry()
    r = detector().run(frame.data, g, "left", 1)
    view = annotate(frame.data, r, g, display_width=728)
    assert view.shape[:2] == (544, 728)
    assert view.ndim == 3


def test_detection_result_json_carries_no_corner_arrays():
    frame = source().read_full_mono()
    r = detector().run(frame.data, sensor_geometry(), "left", 1)
    payload = r.as_dict()
    assert payload["tiles_found"] == len(r.found_tiles)
    assert all("corners" not in t for t in payload["tiles"])
