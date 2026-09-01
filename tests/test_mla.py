"""Geometry tests for the microlens grid.

The property that matters most is centre-anchoring: changing pitch must not
move lenslet (0,0). If it does, hand alignment becomes a two-variable chase --
you adjust pitch, the centre drifts, you re-adjust offset, the pitch looks
wrong again. These tests pin that behaviour down.
"""

import math

import numpy as np
import pytest

from trilobite.optics.mla import MLAGeometry, _bilinear


def geom(**kw):
    base = dict(width=728, height=544, pitch=20.0)
    base.update(kw)
    return MLAGeometry(**base)


def test_centre_pixel_is_frame_centre():
    g = geom()
    assert g.centre_pixel == ((728 - 1) / 2, (544 - 1) / 2)


def test_lenslet_zero_sits_on_the_origin():
    g = geom(offset_x=3.5, offset_y=-2.0)
    assert g.centre_of(0, 0) == pytest.approx(g.origin)


def test_pitch_change_does_not_move_the_centre_lenslet():
    """The whole point of anchoring on the centre pixel."""
    a = geom(pitch=20.0, offset_x=7.0, offset_y=-4.0)
    b = geom(pitch=53.7, offset_x=7.0, offset_y=-4.0)
    assert a.centre_of(0, 0) == pytest.approx(b.centre_of(0, 0))


def test_grid_expands_proportionally_about_the_centre():
    a = geom(pitch=20.0)
    b = geom(pitch=40.0)
    ox, oy = a.origin
    ax, ay = a.centre_of(3, 2)
    bx, by = b.centre_of(3, 2)
    # Doubling the pitch doubles the displacement from the origin, exactly.
    assert (bx - ox) == pytest.approx(2 * (ax - ox))
    assert (by - oy) == pytest.approx(2 * (ay - oy))


def test_rotation_is_a_rigid_rotation_about_the_origin():
    g0 = geom(rotation_deg=0.0)
    g1 = geom(rotation_deg=30.0)
    ox, oy = g0.origin
    r0 = math.hypot(*(c - o for c, o in zip(g0.centre_of(4, 0), (ox, oy), strict=True)))
    r1 = math.hypot(*(c - o for c, o in zip(g1.centre_of(4, 0), (ox, oy), strict=True)))
    assert r0 == pytest.approx(r1)


def test_offset_shifts_the_whole_grid():
    a = geom()
    b = geom(offset_x=10.0, offset_y=-6.0)
    ax, ay = a.centre_of(2, -3)
    bx, by = b.centre_of(2, -3)
    assert (bx - ax, by - ay) == pytest.approx((10.0, -6.0))


def test_named_indices_have_the_right_signs():
    """Image convention: y increases downward, so 'top' is negative j."""
    g = geom(pitch=50.0)
    named = g.named_indices()
    i, j = g.index_extent()
    assert named["centre"] == (0, 0)
    assert named["top_right"] == (i, -j)
    assert named["bottom_left"] == (-i, j)
    # And they really are up-and-right / down-and-left in pixel space.
    tr = g.centre_of(*named["top_right"])
    bl = g.centre_of(*named["bottom_left"])
    assert tr[0] > bl[0] and tr[1] < bl[1]


def test_index_extent_crops_stay_inside_the_frame():
    for pitch in (12.0, 37.5, 101.0):
        for rot in (0.0, 5.0, -12.0):
            g = geom(pitch=pitch, rotation_deg=rot)
            i, j = g.index_extent()
            img = np.zeros((g.height, g.width), dtype=np.uint8)
            for idx in g.named_indices().values():
                tile = g.crop(img, *idx)
                # A fully-contained lenslet yields a full-size square tile,
                # exactly -- tiles are stacked, so the size must not wobble.
                side = g.crop_side()
                assert tile.shape == (side, side), (pitch, rot, idx, tile.shape)


def test_crop_off_frame_returns_empty_not_an_exception():
    g = geom(pitch=20.0)
    assert g.crop(np.zeros((544, 728), np.uint8), 500, 500).size == 0


def test_grid_mask_lines_fall_between_lenslet_centres():
    g = geom(pitch=40.0, width=200, height=200)
    mask = g.grid_mask((200, 200), line_width=1.0)
    # The centre of lenslet (0,0) must NOT be on a line...
    cx, cy = (int(round(v)) for v in g.centre_of(0, 0))
    assert not mask[cy, cx]
    # ...but the midpoint to its right neighbour must be.
    mx = int(round(g.centre_of(0, 0)[0] + 20.0))
    assert mask[cy, mx]


def test_centre_marker_is_on_the_origin():
    g = geom(offset_x=12.0, offset_y=-8.0, width=200, height=200)
    mask = g.centre_marker_mask((200, 200))
    ox, oy = (int(round(v)) for v in g.origin)
    assert mask[oy, ox]


# --- wholeness, corner selection, de-rotation --------------------------------


def test_whole_predicate_matches_what_crop_actually_returns():
    """The bug this pins: a selection predicate that is even slightly more
    conservative than the extractor rejects the outermost lenslet, so you get
    the second one in -- and which one changes as the offset drifts."""
    img = np.zeros((544, 728), np.uint8)
    for pitch in (23.0, 56.0, 100.0, 137.5):
        for ox in (0.0, 7.3, -11.9):
            g = geom(pitch=pitch, offset_x=ox)
            side = g.crop_side()
            for i, j in g.whole_indices():
                tile = g.crop(img, i, j)
                assert tile.shape == (side, side), (pitch, ox, i, j, tile.shape)


def test_outermost_whole_lenslet_is_actually_selected():
    """No off-by-one: the chosen corner view must be the furthest one that is
    still complete, not its inboard neighbour."""
    g = geom(pitch=100.0)          # 728x544 -> centres at 363.5 + 100i
    named = g.named_indices()
    i, j = named["top_right"]
    assert g.is_whole(i, j)
    # The next one further out must NOT be whole -- otherwise we left one behind.
    assert not g.is_whole(i + 1, j), "a further-out lenslet was still whole"
    assert not g.is_whole(i, j - 1), "a further-out lenslet was still whole"


def test_corners_are_resolved_independently_under_offset():
    """With the grid pushed off-centre, the two corners are NOT symmetric.
    Forcing (i,-j) / (-i,j) throws away a usable lenslet on one side."""
    g = geom(pitch=100.0, offset_x=45.0)
    named = g.named_indices()
    tr, bl = named["top_right"], named["bottom_left"]
    assert abs(tr[0]) != abs(bl[0]), (tr, bl)
    assert g.is_whole(*tr) and g.is_whole(*bl)


def test_selection_is_stable_against_tiny_offset_changes():
    """Jitter check: nudging the offset by a hundredth of a pixel must not
    flip the chosen lenslet."""
    base = geom(pitch=100.0).named_indices()
    for eps in (1e-3, -1e-3, 5e-3):
        assert geom(pitch=100.0, offset_x=eps).named_indices() == base, eps


def test_derotated_tile_is_axis_aligned():
    """Build an image whose intensity ramps along the ROTATED lattice u axis.
    After de-rotation the tile must ramp purely along its own x axis, so every
    row is identical. This is the property that says the resampling used the
    lattice basis and not the sensor basis."""
    W = H = 400
    for rot in (0.0, 7.0, -13.5):
        g = MLAGeometry(width=W, height=H, pitch=120.0, rotation_deg=rot)
        a, _ = g.normalised((H, W))
        img = np.clip((a - a.min()) / (a.max() - a.min()) * 255, 0, 255).astype(np.uint8)
        tile = g.crop_derotated(img, 0, 0).astype(np.float32)
        row_spread = tile.std(axis=0).max()      # variation down each column
        assert row_spread < 1.5, (rot, row_spread)


def test_derotation_is_a_noop_at_zero_rotation():
    img = np.random.default_rng(0).integers(0, 255, (400, 400), dtype=np.uint8)
    g = MLAGeometry(width=400, height=400, pitch=100.0, rotation_deg=0.0)
    assert np.array_equal(g.crop_derotated(img, 0, 0), g.crop(img, 0, 0))


def test_highlight_mask_outlines_only_the_named_lenslets():
    g = geom(pitch=100.0)
    named = g.named_indices()
    mask = g.highlight_mask((544, 728), [named["centre"]], thickness=2)
    cx, cy = g.centre_of(0, 0)
    half = g.crop_side() / 2
    # Border pixels marked, interior clean.
    assert mask[int(cy - half) + 1, int(cx)]
    assert not mask[int(cy), int(cx)]


# -- resolution ------------------------------------------------------------
#
# The preview is half the sensor's width, and the parameters are aligned by eye
# against the preview while calibration crops from the sensor frame. That
# factor of two is silent when it is wrong -- every crop lands between
# micro-images and detection simply never works -- so it is pinned here.


def test_rescaling_keeps_lenslet_centres_on_the_same_physical_point():
    g = geom(width=728, height=544, pitch=50.0, offset_x=7.25, offset_y=-3.5, rotation_deg=3.0)
    big = g.rescaled(1456, 1088)
    assert big.pitch == pytest.approx(100.0)
    assert big.rotation_deg == pytest.approx(3.0)
    for i, j in ((0, 0), (3, -2), (-5, 4)):
        x, y = g.centre_of(i, j)
        bx, by = big.centre_of(i, j)
        # Pixel-area convention: preview pixel x maps to (x + 1/2)*s - 1/2.
        assert bx == pytest.approx((x + 0.5) * 2.0 - 0.5)
        assert by == pytest.approx((y + 0.5) * 2.0 - 0.5)


def test_rescaling_is_its_own_inverse():
    g = geom(width=728, height=544, pitch=50.0, offset_x=11.0, offset_y=6.5, rotation_deg=-2.0)
    back = g.rescaled(1456, 1088).rescaled(728, 544)
    assert back == g


def test_anisotropic_rescale_is_refused():
    with pytest.raises(ValueError, match="anisotropic"):
        geom(width=728, height=544).rescaled(1456, 600)


def test_tile_to_frame_inverts_a_plain_crop():
    g = geom(width=400, height=400, pitch=40.0)
    x0, y0 = g.crop_origin(2, -1)
    pts = np.array([[0.0, 0.0], [5.5, 7.25]])
    out = g.tile_to_frame(2, -1, pts)
    assert out[0] == pytest.approx([x0, y0])
    assert out[1] == pytest.approx([x0 + 5.5, y0 + 7.25])


def test_tile_to_frame_inverts_a_derotated_crop():
    """Sample the de-rotated tile at a known pixel, map that pixel back, and
    the value at the mapped frame position must be the same -- which is the
    statement that corners measured either way land in the same place."""
    g = geom(width=300, height=300, pitch=60.0, rotation_deg=12.0)
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, size=(300, 300), dtype=np.uint8)
    tile = g.crop_derotated(img, 1, 1)
    side = tile.shape[0]
    for (r, c) in ((0, 0), (side // 2, side // 3), (side - 1, side - 1)):
        fx, fy = g.tile_to_frame(1, 1, np.array([[c, r]]), derotate=True)[0]
        assert 0 <= fx < 300 and 0 <= fy < 300
        # The mapped position must be exactly where crop_derotated read from,
        # so re-sampling the frame there reproduces the tile pixel.
        got = _bilinear(img, np.array([fx], np.float32), np.array([fy], np.float32))[0]
        assert int(got) == int(tile[r, c])
