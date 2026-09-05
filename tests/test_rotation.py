"""Quarter-turn rotation, mirroring, and the rule that they are a setup step.

Two things are being pinned here, and they pull in opposite directions.

The first is that orientation reaches EVERYTHING: the pixels, the size the
camera advertises, the MLA reference frame, the readiness arithmetic, the
sidecar. Any one of those can be right while the set of them is wrong, and the
failure is not an exception -- it is every crop landing between micro-images,
which from the outside is indistinguishable from an optics problem.

The second is that changing it is NOT something the software should smooth
over. An earlier version carried an MLA alignment across a change of
orientation by transforming its offsets. The arithmetic was right and the
feature was wrong: it produced a grid that claimed to be aligned when nobody
had looked at it. So the alignment is now reset, `pitch_px` is kept, and the
whole thing is locked while the grid is enabled. The tests below assert the
reset and the lock as behaviour, not as an implementation detail.
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
from trilobite.optics.orientation import Orientation
from trilobite.processing.stages.plenoptic import MLAGridOverlay

ALL = [
    Orientation(r, fh, fv)
    for r in (0, 90, 180, 270)
    for fh in (False, True)
    for fv in (False, True)
]


def source(**kw):
    s = SyntheticSource(CameraConfig(
        cam_id="left", backend="synthetic", fps=1000,
        full_resolution=(160, 120), preview_resolution=(80, 60),
        synthetic_drift_px=0.0, **kw,
    ))
    s.open()
    return s


# -- pixels, against the definition -------------------------------------


@pytest.mark.parametrize("deg", [0, 90, 180, 270])
def test_rotation_is_clockwise_on_screen(deg):
    """One marked pixel, tracked by hand. The sign convention lives or dies here.

    A clockwise turn takes the top-left corner to the top-right, then to the
    bottom-right, then to the bottom-left. Stated as corners rather than as a
    matrix, because there is no matrix to appeal to: this IS the definition,
    and every other statement about rotation in the codebase is downstream of
    it.
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
    turned landscape sensor and a portrait one are identical on disk, and the
    difference decides whether the MLA offsets have had their axes swapped."""
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


# -- the Orientation value ------------------------------------------------


def test_an_orientation_knows_whether_it_transposes_the_frame():
    assert Orientation(90).portrait
    assert Orientation(270).portrait
    assert not Orientation(180).portrait
    assert not Orientation(0, flip_horizontal=True).portrait
    assert Orientation(0).swaps_axes_relative_to(Orientation(90))
    assert not Orientation(90).swaps_axes_relative_to(Orientation(270))
    assert not Orientation(0).swaps_axes_relative_to(Orientation(0, True, True))


def test_an_alignment_with_no_recorded_orientation_reads_as_unturned():
    """Which is what it was. Configs and state files predate this field, and
    treating a missing value as "unknown" would reset every existing grid."""
    stage = MLAGridOverlay("mla", pitch_px=100.0)
    assert stage.reference_orientation == Orientation()


# -- bind_sensor: reset, do not transform ---------------------------------


def _stage(**kw):
    base = dict(enabled=True, pitch_px=100.0, rotation_deg=2.0,
                offset_x=20.0, offset_y=10.0,
                reference_width=1456, reference_height=1088)
    base.update(kw)
    return MLAGridOverlay("mla", **base)


def test_turning_the_camera_resets_the_alignment_and_keeps_the_pitch():
    """The rule, in one test.

    Offsets and lattice rotation were measured by eye against a frame that no
    longer exists, so they go. Pitch is `pitch_um / pixel_pitch_um` -- a
    property of the lens array and the sensor, the same number whichever way
    the camera is bolted -- so it stays, and it is the tedious one to re-enter.
    """
    stage = _stage()
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert (stage.params.offset_x, stage.params.offset_y) == (0.0, 0.0)
    assert stage.params.rotation_deg == 0.0
    assert stage.params.pitch_px == pytest.approx(100.0)
    assert stage.reference_shape == (1088, 1456)
    assert stage.params.reference_rotate_deg == 90


def test_a_mirror_resets_the_alignment_too():
    """A flip does not change the frame SIZE, so nothing raises and nothing
    looks wrong -- which is exactly why it has to be handled explicitly. The
    grid offsets are measured from the frame centre along an axis the flip
    negates."""
    stage = _stage()
    stage.bind_sensor(1456, 1088, Orientation(0, flip_horizontal=True))
    assert (stage.params.offset_x, stage.params.offset_y) == (0.0, 0.0)
    assert stage.params.rotation_deg == 0.0
    assert stage.params.pitch_px == pytest.approx(100.0)
    assert stage.reference_shape == (1456, 1088)
    assert stage.params.reference_flip_horizontal is True


def test_binding_the_same_orientation_twice_changes_nothing():
    """Idempotence, and not a nicety: bind_sensor is called at camera start AND
    again after the state restore. A reset that fired on the second call would
    wipe the alignment the state file had just restored."""
    stage = _stage()
    stage.bind_sensor(1088, 1456, Orientation(90))
    stage.params.offset_x = 7.5           # stand in for a fresh alignment
    stage.params.offset_y = -3.25
    snapshot = stage.params.model_dump()
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.params.model_dump() == snapshot


def test_a_turn_and_a_resolution_change_compose_in_that_order():
    """A preview-referenced alignment meeting a turned sensor: both fixes.

    The orientation is dealt with first, which is what keeps the pitch rescale
    isotropic. Taken the other way round, a 728x544 reference meets a 1088x1456
    frame, `rescaled` raises on the anisotropy, and the handler for that stamps
    the new size while leaving pitch verbatim -- so a rig that was both turned
    and migrated would keep a preview-sized pitch and detect nothing.
    """
    stage = _stage(pitch_px=50.0, offset_x=10.0, offset_y=5.0,
                   reference_width=728, reference_height=544)
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.reference_shape == (1088, 1456)
    assert stage.params.pitch_px == pytest.approx(100.0)
    assert (stage.params.offset_x, stage.params.offset_y) == (0.0, 0.0)


def test_an_unrotated_bind_still_takes_the_old_migration_path():
    """The 728 -> 1456 migration has to keep working exactly as it did, offsets
    and all: no orientation changed, so there is nothing to reset."""
    stage = _stage(pitch_px=50.0, rotation_deg=0.0, offset_x=10.0, offset_y=5.0,
                   reference_width=728, reference_height=544)
    stage.bind_sensor(1456, 1088)
    assert stage.params.pitch_px == pytest.approx(100.0)
    assert (stage.params.offset_x, stage.params.offset_y) == (20.0, 10.0)


def test_a_grid_that_has_never_been_bound_just_adopts_the_frame():
    stage = _stage(reference_width=0, reference_height=0)
    stage.bind_sensor(1088, 1456, Orientation(90))
    assert stage.reference_shape == (1088, 1456)
    assert stage.params.offset_x == 20.0, "there was nothing to reset"
    assert stage.params.reference_rotate_deg == 90


@pytest.mark.parametrize("o", ALL)
def test_no_orientation_leaves_the_grid_claiming_a_frame_it_is_not_in(o):
    """Whatever the orientation, what comes out of bind_sensor is consistent:
    the reference frame equals the frame it was handed, and the recorded
    orientation equals the one it was handed. Those two are what every offline
    reader trusts."""
    stage = _stage()
    size = (1088, 1456) if o.portrait else (1456, 1088)
    stage.bind_sensor(*size, o)
    assert stage.reference_shape == size
    assert stage.reference_orientation == o


# -- readiness on a portrait rig ------------------------------------------


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
    # 1088 x 1456 at a 100 px pitch is a 10 x 14 lattice. A width-only ratio
    # would have read the pitch as 100 * 1088/1456 = 75 px and counted about
    # 200. Both numbers are "plausible", which is the problem.
    assert ids["left.tile_count"]["ok"], ids["left.tile_count"]["message"]
    assert "117 whole" in ids["left.tile_count"]["message"], \
        ids["left.tile_count"]["message"]
    assert ids["left.preview_resolves"]["ok"], ids["left.preview_resolves"]["message"]
    assert "50 px in the preview" in ids["left.preview_resolves"]["message"]
    cam.source.close()


# -- the lock, and surviving a restart ------------------------------------


def _app(rotate_deg=0, grid_on=True, **grid):
    """One camera with an MLA stage, through the real web layer."""
    from trilobite.config import AppConfig
    from trilobite.web.server import create_app

    params = {"enabled": grid_on, "pitch_px": 100.0,
              "offset_x": 20.0, "offset_y": 10.0}
    params.update(grid)
    cfg = AppConfig(cameras=[CameraConfig(
        cam_id="left", backend="synthetic", rotate_deg=rotate_deg,
        full_resolution=(1456, 1088), preview_resolution=(728, 544),
        synthetic_drift_px=0.0,
        pipeline=[StageConfig(type="mla_grid_overlay", name="mla", params=params)],
    )])
    from trilobite.app import Application

    app = Application(cfg, state_path=None, restore=False)
    cam = app.cameras["left"]
    cam.source.open()
    # What CameraRuntime.start() does after opening the device. Done here
    # rather than starting the capture thread: these tests are about the
    # endpoints, and a running thread would make them wall-clock dependent.
    w, h = cam.source.describe().full_resolution
    cam.mla_stage().bind_sensor(int(w), int(h), Orientation.of(cam.cfg))
    return app, create_app(app)


def _client(api):
    from fastapi.testclient import TestClient

    return TestClient(api)


def test_the_orientation_is_locked_while_the_grid_is_on():
    """The rule, enforced rather than documented.

    Everything after the alignment -- the poses, the corners, the fit --
    assumes a fixed frame. Allowing the frame to change mid-calibration and
    then being clever about the grid would fix one of those and silently
    invalidate the rest.
    """
    app, api = _app(grid_on=True)
    c = _client(api)
    assert c.get("/api/orientation/left").json()["locked"] is True
    r = c.post("/api/orientation/left", json={"flip_horizontal": True})
    assert r.status_code == 409, r.text
    assert "MLA grid is enabled" in r.text
    assert app.cameras["left"].cfg.flip_horizontal is False, "and nothing changed"


def test_asking_for_the_orientation_it_already_has_is_not_a_conflict():
    """A page re-rendering its own state must not be refused. Only a real
    change is a change."""
    _, api = _app(grid_on=True)
    c = _client(api)
    r = c.post("/api/orientation/left", json={"flip_horizontal": False,
                                              "rotate_deg": 0})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == []


def test_turning_the_grid_off_unlocks_it_and_the_change_resets_the_alignment():
    _, api = _app(grid_on=True)
    c = _client(api)
    c.post("/api/pipeline/left/mla", json={"values": {"enabled": False}})
    assert c.get("/api/orientation/left").json()["locked"] is False

    r = c.post("/api/orientation/left", json={"rotate_deg": 90})
    assert r.status_code == 200, r.text
    assert r.json()["rotate_deg"] == 90
    assert "reset" in r.json()["rebased"]

    mla = c.get("/api/pipeline/left").json()[0]["values"]
    assert (mla["offset_x"], mla["offset_y"]) == (0.0, 0.0)
    assert mla["pitch_px"] == pytest.approx(100.0), "pitch is hardware, it stays"
    assert (mla["reference_width"], mla["reference_height"]) == (1088, 1456)


def test_a_quarter_turn_is_the_only_rotation_offered():
    """Anything else is a resample rather than a relabelling of pixels, and
    would cost resolution on every frame for the life of the rig."""
    _, api = _app(grid_on=False)
    c = _client(api)
    assert c.post("/api/orientation/left", json={"rotate_deg": 45}).status_code == 422
    assert c.post("/api/orientation/left", json={"rotate_deg": "sideways"}
                  ).status_code == 422


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


def test_the_orientation_survives_a_restart_and_the_grid_is_not_reset_again():
    """The whole restore path, end to end, because the failure is a silent one.

    The saved pipeline carries the alignment's `reference_rotate_deg`. If the
    orientation itself is not saved beside it, a restart finds a
    portrait-referenced grid on a camera the config calls landscape, decides
    the orientation has changed, and resets an alignment that took twenty
    minutes to dial in. Deleting `"orientation"` from the snapshot is the
    mutation this catches.
    """
    live = _turned_camera(rotate_deg=90)
    live.mla_stage().params.offset_x = 12.5      # an alignment made after the turn
    live.mla_stage().params.offset_y = -4.0
    saved = live.state_snapshot()
    assert saved["orientation"]["rotate_deg"] == 90

    # A fresh process: the config is back at its on-disk value of 0.
    fresh = _turned_camera(rotate_deg=0)
    fresh.apply_state(saved)
    assert fresh.cfg.rotate_deg == 90
    w, h = fresh.source.describe().full_resolution
    assert (w, h) == (1088, 1456)
    fresh.mla_stage().bind_sensor(w, h, Orientation.of(fresh.cfg))

    p = fresh.mla_stage().params
    assert (p.offset_x, p.offset_y) == (12.5, -4.0), "the alignment survived"
    assert (p.reference_width, p.reference_height) == (1088, 1456)
    assert p.pitch_px == pytest.approx(100.0)
    live.source.close()
    fresh.source.close()


def test_the_sensor_controls_reported_are_the_live_ones_not_the_config():
    """What made a restored exposure look unrestored.

    The camera really was at the restored value and the box really did say the
    YAML one, because the read endpoint returned `cfg.controls`. The first
    nudge of any slider then sent the sensor back to the config.
    """
    _, api = _app(grid_on=False)
    c = _client(api)
    c.post("/api/controls/left", json={"controls": {"ExposureTime": 12345}})
    got = c.get("/api/controls/left").json()
    assert got["requested"]["ExposureTime"] == 12345
    assert got["config"].get("ExposureTime") != 12345, \
        "the config is reported separately, and unchanged"


def test_an_orientation_change_is_marked_for_the_state_file(tmp_path):
    """It was the one setting the snapshot never marked dirty.

    Autosave writes only when something says it should, so a flip set in the
    UI lived until the next restart and then reverted to the YAML -- which
    reads exactly like "settings are not saved", because it is.
    """
    from trilobite.app import Application
    from trilobite.config import AppConfig
    from trilobite.web.server import create_app

    cfg = AppConfig(cameras=[CameraConfig(
        cam_id="left", backend="synthetic", full_resolution=(64, 48),
        preview_resolution=(64, 48), synthetic_drift_px=0.0)])
    path = tmp_path / "rig.state.json"
    app = Application(cfg, state_path=path, restore=False)
    app.cameras["left"].source.open()
    c = _client(create_app(app))

    assert c.post("/api/orientation/left",
                  json={"flip_vertical": True}).status_code == 200
    assert app.state._dirty.is_set(), "nothing asked for the change to be saved"
    app.state.save()
    import json as _json

    saved = _json.loads(path.read_text())
    assert saved["cameras"]["left"]["orientation"]["flip_vertical"] is True


# -- the page the browser is actually running -----------------------------


def test_the_page_is_served_revalidating_and_stamped_with_its_build():
    """Why a rotate control that existed was not on screen.

    Deployment is `git pull` on the Pi with the browser left open. A
    FileResponse carries an ETag but no Cache-Control, so a browser applies
    heuristic freshness and can serve the page from its own cache without ever
    asking -- producing a UI missing controls the server already implements,
    which is indistinguishable from the feature being broken.
    """
    _, api = _app(grid_on=False)
    c = _client(api)
    r = c.get("/")
    assert "no-cache" in r.headers.get("cache-control", "")
    assert "__UI_BUILD__" not in r.text, "the build stamp was not substituted"
    build = c.get("/api/ui-build").json()["build"]
    assert build and build in r.text
    # And the control the whole exchange was about is in fact in the page.
    assert 'textContent = "Rotate"' in r.text


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
