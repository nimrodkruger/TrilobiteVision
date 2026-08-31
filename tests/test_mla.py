"""Geometry tests for the microlens grid.

The property that matters most is centre-anchoring: changing pitch must not
move lenslet (0,0). If it does, hand alignment becomes a two-variable chase --
you adjust pitch, the centre drifts, you re-adjust offset, the pitch looks
wrong again. These tests pin that behaviour down.
"""

import math

import numpy as np
import pytest

from trilobite.optics.mla import MLAGeometry


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
