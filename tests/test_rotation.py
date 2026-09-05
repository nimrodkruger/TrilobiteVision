"""Quarter-turn rotation, and what it does to everything downstream.

The point of these tests is not that `np.rot90` works. It is that the four
things which have to agree about a quarter turn actually do:

  * the pixels, as `CameraSource._orient` produces them;
  * the size the camera advertises, which everything downstream binds to;
  * the grid offsets, measured from a frame centre that has moved;
  * the lattice indices, which get relabelled.

Any one of those can be right on its own while the set of them is wrong, and
the failure is not an exception -- it is every crop landing between
micro-images, which from the outside is indistinguishable from an optics
problem. So each test below pins one of them against a closed form rather than
against another part of the same code.
"""

from __future__ import annotations

import math
import threading
import time

import numpy as np
import pytest

from trilobite.app import CameraRuntime
from trilobite.calibration import readiness_report
from trilobite.cameras.offline import SyntheticSource
from trilobite.config import CameraConfig, StageConfig
from trilobite.optics.mla import MLAGeometry
from trilobite.optics.orientation import Orientation, rebase, wrap_lattice_angle
from trilobite.processing.stages.plenoptic import MLAGridOverlay

ALL = [
    Orientation(r, fh, fv)
    for r in (0, 90, 180, 270)
    for fh in (False, True)
    for fv in (False, True)
]


# -- the group ----------------------------------------------------------


def test_the_sixteen_settings_are_the_eight_elements_of_d4():
    """Four rotations times two mirrors times two is sixteen settings, but the
    symmetry group of a rectangle has only eight elements -- 180 degrees is
    both mirrors, and each quarter turn has a mirrored twin. Getting exactly
    eight distinct matrices out of the sixteen is the check that the rotation
    and the mirrors are composing as a group rather than as three independent
    switches, which is what makes the round trip below exact."""
    assert len({o.matrix for o in ALL}) == 8
    assert Orientation(180).matrix == Orientation(0, True, True).matrix


@pytest.mark.parametrize("start", ALL)
def test_every_round_trip_returns_the_original_numbers(start):
    """A -> B -> A is the identity for all 64 pairs, exactly.

    This is the property the rebase actually rests on: turning the camera and
    turning it back gives the numbers you started with, not numbers that are
    close to them.
    """
    for other in ALL:
        one = rebase(start, other, offset_x=13.5, offset_y=-7.25, rotation_deg=3.0,
                     reference_width=1456, reference_height=1088)
        back = rebase(other, start, offset_x=one.offset_x, offset_y=one.offset_y,
                      rotation_deg=one.rotation_deg,
                      reference_width=one.reference_width,
                      reference_height=one.reference_height)
        assert (back.offset_x, back.offset_y) == (13.5, -7.25)
        assert back.rotation_deg == pytest.approx(3.0)
        assert (back.reference_width, back.reference_height) == (1456, 1088)


def test_only_an_odd_quarter_turn_swaps_the_reference_frame():
    args = dict(offset_x=0.0, offset_y=0.0, rotation_deg=0.0,
                reference_width=1456, reference_height=1088)
    z = Orientation()
    assert rebase(z, Orientation(90), **args).reference_width == 1088
    assert rebase(z, Orientation(270), **args).reference_width == 1088
    assert rebase(z, Orientation(180), **args).reference_width == 1456
    assert rebase(z, Orientation(0, flip_horizontal=True), **args).reference_width == 1456


def test_the_lattice_angle_folds_into_the_window_a_square_lattice_can_see():
    # A square lattice turned by 90 degrees is the same lattice, so the
    # parameter is meaningful only modulo 90 -- and has to stay inside its own
    # +/-45 validation bound while being carried across.
    assert wrap_lattice_angle(93.0) == pytest.approx(3.0)
    assert wrap_lattice_angle(-87.0) == pytest.approx(3.0)
    assert wrap_lattice_angle(45.0) == pytest.approx(45.0)
    assert wrap_lattice_angle(-45.0) == pytest.approx(45.0)
    for deg in (-44.0, 0.0, 12.5, 44.9):
        assert wrap_lattice_angle(deg) == pytest.approx(deg)


# -- pixels, against the definition -------------------------------------


def source(**kw):
    s = SyntheticSource(CameraConfig(
        cam_id="left", backend="synthetic", fps=1000,
        full_resolution=(160, 120), preview_resolution=(80, 60),
        synthetic_drift_px=0.0, **kw,
    ))
    s.open()
    return s


@pytest.mark.parametrize("deg", [0, 90, 180, 270])
def test_rotation_is_clockwise_on_screen(deg):
    """One marked pixel, tracked by hand. The sign convention lives or dies here.

    A clockwise turn takes the top-left corner to the top-right, then to the
    bottom-right, then to the bottom-left. Stated as corners rather than as a
    matrix, because a matrix is what is under test.
    """
    from trilobite.cameras.base import CameraSource

    src = source(rotate_deg=deg)
    a = np.zeros((3, 5), dtype=np.uint8)
    a[0, 0] = 1
    out = CameraSource._orient(src, a)
    want = {0: (0, 0),
            90: (0, out.shape[1] - 1),
            180: (out.shape[0] - 1, out.shape[1] - 1),
            270: (out.shape[0] - 1, 0)}[deg]
    assert tuple(int(v) for v in np.argwhere(out == 1)[0]) == want


def test_a_quarter_turn_swaps_the_frame_and_a_half_turn_does_not():
    assert source(rotate_deg=90).read_preview().data.shape == (80, 60)
    assert source(rotate_deg=270).read_preview().data.shape == (80, 60)
    assert source(rotate_deg=180).read_preview().data.shape == (60, 80)


def test_rotation_is_applied_before_the_mirrors():
    """The documented order, pinned. Reversing it is a silent transposition.

    Rotate-then-mirror makes the two flip checkboxes mean "mirror what I am
    looking at", the only reading an operator can check by eye. The other order
    makes which screen axis a flip acts on depend on the rotation.
    """
    from trilobite.cameras.base import CameraSource

    a = np.arange(15, dtype=np.uint8).reshape(3, 5)
    got = CameraSource._orient(source(rotate_deg=90, flip_horizontal=True), a)
    assert np.array_equal(got, np.flip(np.rot90(a, k=-1), axis=1))
    assert not np.array_equal(got, np.rot90(np.flip(a, axis=1), k=-1))


@pytest.mark.parametrize("o", ALL)
def test_the_matrix_agrees_with_where_the_pixels_actually_go(o):
    """The seam between the two halves of this feature, tested directly.

    `CameraSource._orient` moves pixels; `Orientation.matrix` claims to say
    where they went, and the grid rebase trusts that claim completely. Nothing
    else compares the two, so a sign error in one and a matching one in the
    other would leave every test above passing and the grid off by a
    reflection. Here a marked pixel is followed through the real orient and
    checked against the matrix acting on its centred coordinates.
    """
    from trilobite.cameras.base import CameraSource

    w, h = 9, 5
    a = np.zeros((h, w), dtype=np.uint8)
    x, y = 7, 1
    a[y, x] = 1
    out = CameraSource._orient(
        source(rotate_deg=o.rotate_deg, flip_horizontal=o.flip_horizontal,
               flip_vertical=o.flip_vertical), a)
    yy, xx = (int(v) for v in np.argwhere(out == 1)[0])

    m = o.matrix
    dx, dy = x - (w - 1) / 2, y - (h - 1) / 2
    want_dx = m[0][0] * dx + m[0][1] * dy
    want_dy = m[1][0] * dx + m[1][1] * dy
    ow, oh = out.shape[1], out.shape[0]
    assert xx - (ow - 1) / 2 == pytest.approx(want_dx)
    assert yy - (oh - 1) / 2 == pytest.approx(want_dy)


def test_the_rotation_reaches_the_saved_frame_not_only_the_preview():
    """The same trap the flip had: turning what you look at and not what you
    measure, discovered when the calibration comes back transposed."""
    plain = source().capture_full(raw=True).data
    turned = source(rotate_deg=90).capture_full(raw=True).data
    assert turned.shape == plain.shape[::-1]
    assert np.array_equal(turned, np.rot90(plain, k=-1))


def test_the_full_frame_served_to_calibration_is_rotated_too():
    """Three paths produce pixels -- preview, capture_full, and the full frame
    the capture loop serves for pose detection. All three must agree, or the
    corners are recorded in a different frame from the one saved beside them."""
    s = source(rotate_deg=90)
    s.request_full_frame()
    s.read_preview()
    served = s.take_full_frame()
    assert served is not None
    assert served.data.shape == (160, 120)


def test_a_rotated_frame_says_so_in_its_metadata():
    """A portrait .npy that does not record the turn cannot be read back: a
    turned landscape sensor and a portrait one are otherwise identical on disk,
    and the difference decides whether the MLA offsets need their axes swapped."""
    f = source(rotate_deg=270).capture_full()
    assert f.meta["rotate_deg"] == 270
    assert f.meta["flip_horizontal"] is False


def test_the_rotated_array_is_contiguous():
    """np.rot90 returns a permuted-stride view; anything handing the buffer to
    a C library gets a surprise, so it is copied back."""
    assert source(rotate_deg=90).capture_full().data.flags["C_CONTIGUOUS"]


def test_the_camera_advertises_the_size_it_actually_delivers():
    """describe() is the single place the post-rotation size is decided, and
    every consumer -- the MLA reference frame, the readiness arithmetic, the
    session manifest -- reads it from there rather than from the config."""
    src = source(rotate_deg=90)
    info = src.describe()
    assert info.full_resolution == (120, 160)
    assert info.preview_resolution == (60, 80)
    assert src.read_preview().data.shape == (info.preview_resolution[1],
                                             info.preview_resolution[0])


def test_the_preview_and_the_full_frame_are_turned_the_same_way():
    """Not that both are turned -- that they stay in proportion.

    MLAGeometry.rescaled refuses an anisotropic pair, so a mismatch here
    surfaces three layers away as an exception about pitch having no single
    value, with nothing pointing back at the rotation.
    """
    info = source(rotate_deg=270).describe()
    fw, fh = info.full_resolution
    pw, ph = info.preview_resolution
    assert math.isclose(fw / pw, fh / ph)


# -- the grid, against a closed form ------------------------------------


def test_a_rotated_grid_lands_on_the_same_lenslets():
    """The assertion the whole rebase exists to satisfy.

    Take a lenslet centre in the sensor frame. Rotate the *image*. The rebased
    grid must put a lenslet centre exactly on the rotated pixel -- not near it.
    The lenslet's *index* changes, because a quarter turn relabels the lattice
    axes: u becomes v and v becomes -u, so (i, j) becomes (-j, i). That index
    map is part of the claim, and is asserted alongside the position.
    """
    w, h = 1456, 1088
    g = MLAGeometry(width=w, height=h, pitch=100.0, rotation_deg=4.0,
                    offset_x=11.0, offset_y=-6.0)
    r = rebase(Orientation(0), Orientation(90),
               offset_x=g.offset_x, offset_y=g.offset_y,
               rotation_deg=g.rotation_deg, reference_width=w, reference_height=h)
    turned = MLAGeometry(width=r.reference_width, height=r.reference_height,
                         pitch=g.pitch, rotation_deg=r.rotation_deg,
                         offset_x=r.offset_x, offset_y=r.offset_y)

    for i, j in [(0, 0), (1, 0), (0, 1), (-3, 2), (5, -4)]:
        cx, cy = g.centre_of(i, j)
        # A clockwise quarter turn sends pixel (x, y) of a w x h frame to
        # (h - 1 - y, x) of the h x w frame that results.
        want = (h - 1 - cy, cx)
        got = turned.centre_of(-j, i)
        assert got[0] == pytest.approx(want[0], abs=1e-9)
        assert got[1] == pytest.approx(want[1], abs=1e-9)


def test_a_mirrored_grid_lands_on_the_same_lenslets():
    """The same claim for a reflection, where the lattice tilt also negates."""
    w, h = 1456, 1088
    g = MLAGeometry(width=w, height=h, pitch=100.0, rotation_deg=4.0,
                    offset_x=11.0, offset_y=-6.0)
    r = rebase(Orientation(0), Orientation(0, flip_horizontal=True),
               offset_x=g.offset_x, offset_y=g.offset_y,
               rotation_deg=g.rotation_deg, reference_width=w, reference_height=h)
    assert r.rotation_deg == pytest.approx(-4.0)
    flipped = MLAGeometry(width=w, height=h, pitch=g.pitch,
                          rotation_deg=r.rotation_deg,
                          offset_x=r.offset_x, offset_y=r.offset_y)
    for i, j in [(0, 0), (1, 0), (0, 1), (-3, 2)]:
        cx, cy = g.centre_of(i, j)
        got = flipped.centre_of(-i, j)
        assert got[0] == pytest.approx(w - 1 - cx, abs=1e-9)
        assert got[1] == pytest.approx(cy, abs=1e-9)


# -- bind_sensor, where it all has to come together ----------------------


def _stage(**kw):
    base = dict(enabled=True, pitch_px=100.0, rotation_deg=0.0,
                offset_x=20.0, offset_y=10.0,
                reference_width=1456, reference_height=1088)
    base.update(kw)
    return MLAGridOverlay("mla", **base)


def test_binding_a_rotated_sensor_transfers_the_alignment():
    stage = _stage()
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.reference_shape == (1088, 1456)
    assert stage.params.pitch_px == pytest.approx(100.0)
    # (dx, dy) -> (-dy, dx) for a clockwise quarter turn with y downward.
    assert stage.params.offset_x == pytest.approx(-10.0)
    assert stage.params.offset_y == pytest.approx(20.0)
    assert stage.params.reference_rotate_deg == 90


def test_pitch_survives_every_orientation_untouched():
    """A turn and a mirror are isometries. If pitch moves, something resampled."""
    stage = _stage(rotation_deg=3.0)
    for deg in (90, 180, 270, 0):
        size = (1088, 1456) if deg % 180 else (1456, 1088)
        stage.bind_sensor(*size, Orientation(deg))
        assert stage.params.pitch_px == pytest.approx(100.0)


def test_binding_the_same_rotated_sensor_twice_changes_nothing():
    """Idempotence, and not a nicety: bind_sensor is called at camera start AND
    again after the state restore, which is how a stored alignment used to get
    rebased twice."""
    stage = _stage()
    stage.bind_sensor(1088, 1456, Orientation(90))
    snapshot = stage.params.model_dump()
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.params.model_dump() == snapshot


def test_a_turn_and_a_resolution_change_compose_in_that_order():
    """A preview-referenced alignment meeting a rotated sensor: both fixes.

    Orientation is dealt with first, deliberately. The other way round, a
    728x544 reference meets a 1088x1456 frame, which is anisotropic, which
    raises -- and the handler for that stamps the new size while leaving pitch
    and both offsets verbatim. That path produced a grid that looked bound,
    reported a plausible tile count, and put every crop in the wrong place.
    """
    stage = _stage(pitch_px=50.0, offset_x=10.0, offset_y=5.0,
                   reference_width=728, reference_height=544)
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.reference_shape == (1088, 1456)
    assert stage.params.pitch_px == pytest.approx(100.0)
    # Turn first: (10, 5) -> (-5, 10) in the 544x728 frame. Then scale by two.
    assert stage.params.offset_x == pytest.approx(-10.0)
    assert stage.params.offset_y == pytest.approx(20.0)


def test_an_unrotated_bind_still_takes_the_old_migration_path():
    """The 728 -> 1456 migration has to keep working exactly as it did."""
    stage = _stage(pitch_px=50.0, offset_x=10.0, offset_y=5.0,
                   reference_width=728, reference_height=544)
    stage.bind_sensor(1456, 1088)
    assert stage.params.pitch_px == pytest.approx(100.0)
    assert (stage.params.offset_x, stage.params.offset_y) == (20.0, 10.0)


def test_a_grid_that_has_never_been_bound_just_adopts_the_frame():
    stage = _stage(reference_width=0, reference_height=0)
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.reference_shape == (1088, 1456)
    assert stage.params.offset_x == 20.0, "there was nothing to carry across"
    assert stage.params.reference_rotate_deg == 90


def test_the_readiness_arithmetic_survives_a_portrait_frame():
    """No width-only ratio anywhere in the chain, so a turned rig is legal.

    A tile count computed from a width ratio is out by (h/w)^2 on a portrait
    frame -- about 1.8x on this sensor -- which is enough to fail the
    sane-tile-count check on a rig that is perfectly fine.
    """
    cfg = CameraConfig(
        cam_id="left", backend="synthetic", rotate_deg=90,
        full_resolution=(1456, 1088), preview_resolution=(728, 544),
        pipeline=[
            StageConfig(type="mla_grid_overlay", name="mla",
                        params={"enabled": True, "pitch_px": 100.0}),
            StageConfig(type="checkerboard_presence", name="presence",
                        params={"enabled": True, "min_corners": 20}),
        ],
    )
    cam = CameraRuntime(cfg, writer=None)
    cam.source.open()
    w, h = cam.source.describe().full_resolution
    assert (w, h) == (1088, 1456), "portrait, because the camera is turned"
    cam.mla_stage().bind_sensor(w, h, Orientation.of(cfg))

    ids = {c["id"]: c for c in readiness_report([cam])["checks"]}
    # 1088 x 1456 at a 100 px pitch is a 10 x 14 lattice; a width-only ratio
    # would have read the pitch as 100 * 1088/1456 = 75 px and counted about
    # 200. Both numbers are "plausible", which is the problem.
    assert ids["left.tile_count"]["ok"], ids["left.tile_count"]["message"]
    assert "117 whole" in ids["left.tile_count"]["message"], \
        ids["left.tile_count"]["message"]
    assert ids["left.preview_resolves"]["ok"], ids["left.preview_resolves"]["message"]
    assert "50 px in the preview" in ids["left.preview_resolves"]["message"]
    cam.source.close()


# -- surviving a restart --------------------------------------------------


def _turned_camera(rotate_deg=0):
    cfg = CameraConfig(
        cam_id="left", backend="synthetic", rotate_deg=rotate_deg,
        full_resolution=(1456, 1088), preview_resolution=(728, 544),
        pipeline=[StageConfig(type="mla_grid_overlay", name="mla",
                              params={"enabled": True, "pitch_px": 100.0,
                                      "offset_x": 20.0, "offset_y": 10.0})],
    )
    cam = CameraRuntime(cfg, writer=None)
    cam.source.open()
    w, h = cam.source.describe().full_resolution
    cam.mla_stage().bind_sensor(w, h, Orientation.of(cfg))
    return cam


def test_the_orientation_survives_a_restart_and_the_grid_is_not_turned_twice():
    """The whole restore path, end to end, because the failure is a silent one.

    The saved pipeline carries the alignment's `reference_rotate_deg`. If the
    orientation itself is not saved beside it, a restart finds a
    portrait-referenced grid on a camera the config calls landscape, rebases it
    back, and undoes the turn -- with a warning that reads like the 728->1456
    migration it is not. Deleting `"orientation"` from the snapshot is the
    mutation this catches.
    """
    live = _turned_camera(rotate_deg=90)
    saved = live.state_snapshot()
    assert saved["orientation"]["rotate_deg"] == 90
    before = (live.mla_stage().params.offset_x, live.mla_stage().params.offset_y)

    # A fresh process: the config is back at its on-disk value of 0.
    fresh = _turned_camera(rotate_deg=0)
    fresh.apply_state(saved)
    assert fresh.cfg.rotate_deg == 90
    w, h = fresh.source.describe().full_resolution
    assert (w, h) == (1088, 1456)
    fresh.mla_stage().bind_sensor(w, h, Orientation.of(fresh.cfg))

    p = fresh.mla_stage().params
    assert (p.offset_x, p.offset_y) == pytest.approx(before)
    assert (p.reference_width, p.reference_height) == (1088, 1456)
    assert p.pitch_px == pytest.approx(100.0)
    live.source.close()
    fresh.source.close()


# -- the pipeline rate cap ----------------------------------------------


def test_the_pipeline_runs_at_the_capped_rate_and_the_sensor_does_not():
    """Skipped frames are counted, and the pipeline sees only the rest.

    The sensor has to be drained at its own rate -- an unreleased picamera2
    request starves a four-deep pool -- so the cap cannot be applied by reading
    more slowly. It is applied by not *processing*, and the two counts are what
    prove it: frames published at about the cap, and `skipped` making up the
    rest of what the sensor produced.
    """
    cfg = CameraConfig(cam_id="left", backend="synthetic", fps=40.0,
                       full_resolution=(64, 48), preview_resolution=(64, 48),
                       synthetic_drift_px=0.0, process_fps=10.0)
    cam = CameraRuntime(cfg, writer=None)
    assert cam.process_interval == pytest.approx(0.1)
    cam.source.open()
    cam._stop.clear()
    t = threading.Thread(target=cam._run, daemon=True)
    t.start()
    time.sleep(1.0)
    cam._stop.set()
    t.join(timeout=2.0)
    cam.source.close()

    published = cam.preview.get()[0]
    # Wide bounds on purpose: this is a wall-clock test and the assertion worth
    # making is "roughly the cap, not roughly the sensor rate", a factor of
    # four apart. A tight bound here would be a flake generator.
    assert 6 <= published <= 15, f"pipeline ran {published} times in a second"
    assert cam.skipped >= published, "most of the sensor's frames were skipped"


def test_an_explicit_zero_means_process_every_frame():
    """The old behaviour stays reachable, and a per-camera setting overrides
    the server's rate rather than being merged with it."""
    cam = CameraRuntime(
        CameraConfig(cam_id="left", backend="synthetic", process_fps=0),
        writer=None, process_fps=12.0)
    assert cam.process_interval == 0.0


def test_the_server_preview_rate_is_the_default_cap():
    cam = CameraRuntime(CameraConfig(cam_id="left", backend="synthetic"),
                        writer=None, process_fps=12.0)
    assert cam.process_interval == pytest.approx(1.0 / 12.0)


def test_a_skipped_frame_is_still_taken_from_the_source():
    """Otherwise the cap would starve the request pool instead of the pipeline,
    which is the one thing it must not do."""
    src = source()
    before = src._seq
    src.skip_preview()
    assert src._seq > before, "the default skip is a real read"
    src.close()
