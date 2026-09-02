"""Tests that run anywhere -- no camera, no Pi.

The point of the synthetic backend is that this whole file passes on a Windows
desktop, in CI, and on the Pi identically.
"""

import numpy as np
import pytest
from pydantic import ValidationError

from trilobite.cameras.registry import build_camera
from trilobite.config import CameraConfig, StageConfig
from trilobite.processing import Pipeline, catalogue
from trilobite.processing.registry import build_stage
from trilobite.types import Frame


def make_frame(w=64, h=48):
    return Frame.now(np.full((h, w), 100, dtype=np.uint8), "test", 1)


def test_stage_catalogue_is_populated():
    cat = catalogue()
    assert {"passthrough", "levels", "crop", "downsample", "stats"} <= set(cat)
    for spec in cat.values():
        assert "properties" in spec["schema"]


def test_disabled_stage_is_a_noop():
    st = build_stage("levels", gain=4.0, enabled=False)
    f = make_frame()
    assert st(f) is f


def test_levels_applies_gain():
    st = build_stage("levels", gain=2.0)
    out = st(make_frame())
    assert out.data[0, 0] == 200


def test_levels_clips_rather_than_wrapping():
    st = build_stage("levels", gain=8.0)
    out = st(make_frame())
    assert out.data.max() == 255


def test_param_update_validates():
    st = build_stage("levels")
    st.update({"gain": 2.0})
    assert st.params.gain == 2.0
    with pytest.raises(ValidationError):
        st.update({"gain": 999.0})       # outside declared range
    with pytest.raises(ValidationError):
        st.update({"nonexistent": 1})    # extra fields forbidden


def test_crop_is_resolution_agnostic():
    st = build_stage("crop", x0=0.25, x1=0.75, y0=0.0, y1=1.0)
    small = st(make_frame(64, 48))
    large = st(make_frame(640, 480))
    assert small.data.shape == (48, 32)
    assert large.data.shape == (480, 320)


def test_downsample_shape():
    st = build_stage("downsample", factor=4)
    assert st(make_frame(64, 48)).data.shape == (12, 16)


def test_stats_attaches_metadata_without_touching_pixels():
    st = build_stage("stats")
    f = make_frame()
    out = st(f)
    assert out.meta["stat_mean"] == 100.0
    assert np.array_equal(out.data, f.data)


def test_pipeline_snapshot_records_everything():
    p = Pipeline.from_config([
        StageConfig(type="stats", name="stats"),
        StageConfig(type="levels", name="display", params={"gain": 1.5}),
    ])
    p(make_frame())
    snap = p.settings_snapshot()
    assert snap["display"]["gain"] == 1.5
    assert snap["stats"]["type"] == "stats"


def test_broken_stage_does_not_kill_the_pipeline():
    p = Pipeline.from_config([StageConfig(type="lenslet_extract", name="lf",
                                          params={"enabled": True})])
    f = make_frame()
    # lenslet_extract raises NotImplementedError; the frame must still come out.
    assert p(f) is f


def test_synthetic_camera_roundtrip():
    cfg = CameraConfig(cam_id="c", backend="synthetic", fps=1000,
                       preview_resolution=(64, 48))
    with build_camera(cfg) as cam:
        f = cam.read_preview()
        assert f is not None and f.data.shape == (48, 64)
        full = cam.capture_full(raw=True)
        assert full.space == "raw"
        assert cam.describe().backend == "synthetic"


class _FakeCam:
    """Stands in for a Picamera2 object exposing only a mono sensor's controls."""

    camera_controls = {
        "ExposureTime": (1, 1000000, 8000),
        "AnalogueGain": (1.0, 16.0, 1.0),
        "AeEnable": (False, True, True),
        "FrameDurationLimits": (100, 1000000, 33333),
    }


def test_unsupported_controls_are_dropped_not_raised():
    """Regression: a mono IMX296 advertises no AwbEnable, and picamera2 raises
    on an unknown control name rather than ignoring it. Startup must survive."""
    from trilobite.cameras.picam import Picamera2Source

    supported, dropped = Picamera2Source._split_controls(
        _FakeCam(),
        {"ExposureTime": 5000, "AnalogueGain": 2.0, "AwbEnable": False, "Saturation": 0.0},
    )
    assert supported == {"ExposureTime": 5000, "AnalogueGain": 2.0}
    assert dropped == ["AwbEnable", "Saturation"]


def test_all_supported_controls_pass_through():
    from trilobite.cameras.picam import Picamera2Source

    supported, dropped = Picamera2Source._split_controls(
        _FakeCam(), {"ExposureTime": 100, "AeEnable": False}
    )
    assert supported == {"ExposureTime": 100, "AeEnable": False}
    assert dropped == []


# --- auto-exposure transitions and state persistence -------------------------


def _syn(cam_id="c", **kw):
    from trilobite.cameras.registry import build_camera
    cfg = CameraConfig(cam_id=cam_id, backend="synthetic", fps=1000,
                       preview_resolution=(32, 24), **kw)
    return build_camera(cfg)


def test_turning_ae_off_pins_the_value_it_converged_to():
    """The reported bug: AeEnable=False alone does not stop auto-exposure,
    because the exposure is left unpinned. Switching off must freeze the
    sensor at the value AE had reached, not jump elsewhere."""
    with _syn() as cam:
        cam.set_controls({"AeEnable": True})
        for _ in range(30):
            cam.read_preview()
        converged = cam.get_controls()["ExposureTime"]
        assert converged != 5000, "AE never moved the exposure; test is not exercising it"

        cam.set_controls({"AeEnable": False})
        assert cam.auto_exposure is False
        assert cam.get_controls()["ExposureTime"] == converged

        for _ in range(30):
            cam.read_preview()
        assert cam.get_controls()["ExposureTime"] == converged, "exposure drifted after AE off"


def test_setting_exposure_by_hand_turns_ae_off():
    """Setting an exposure while AE runs is contradictory -- AE overwrites it on
    the next frame and the control looks dead. Asking for manual implies manual."""
    with _syn() as cam:
        cam.set_controls({"AeEnable": True})
        cam.read_preview()
        cam.set_controls({"ExposureTime": 12345})
        assert cam.auto_exposure is False
        for _ in range(20):
            cam.read_preview()
        assert cam.get_controls()["ExposureTime"] == 12345


def test_enabling_ae_ignores_a_manual_exposure_in_the_same_call():
    with _syn() as cam:
        cam.set_controls({"AeEnable": True, "ExposureTime": 999})
        assert cam.auto_exposure is True
        assert cam.get_controls()["ExposureTime"] != 999


def test_requested_controls_survive_for_state():
    with _syn() as cam:
        cam.set_controls({"AnalogueGain": 3.0})
        assert cam.requested_controls()["AnalogueGain"] == 3.0


def test_state_round_trip(tmp_path):
    """Parameters set during a session must come back after a restart."""
    from trilobite.app import Application
    from trilobite.config import AppConfig, StageConfig, StorageConfig

    def make_cfg():
        return AppConfig(
            cameras=[CameraConfig(
                cam_id="left", backend="synthetic", fps=1000,
                preview_resolution=(32, 24),
                pipeline=[StageConfig(type="levels", name="display")],
            )],
            storage=StorageConfig(root=str(tmp_path / "data")),
        )

    state = tmp_path / "rig.state.json"

    app = Application(make_cfg(), state_path=state)
    app.start()
    app.camera("left").pipeline.update_params("display", {"gain": 2.75, "gamma": 1.4})
    app.camera("left").source.set_controls({"AnalogueGain": 4.0})
    app.stop()
    assert state.exists()

    app2 = Application(make_cfg(), state_path=state)
    app2.start()
    try:
        vals = app2.camera("left").pipeline.stage("display").params.model_dump()
        assert vals["gain"] == 2.75 and vals["gamma"] == 1.4
        assert app2.camera("left").source.requested_controls()["AnalogueGain"] == 4.0
    finally:
        app2.stop()


def test_restore_can_be_declined(tmp_path):
    from trilobite.app import Application
    from trilobite.config import AppConfig, StageConfig, StorageConfig

    def make_cfg():
        return AppConfig(
            cameras=[CameraConfig(cam_id="left", backend="synthetic", fps=1000,
                                  preview_resolution=(32, 24),
                                  pipeline=[StageConfig(type="levels", name="display")])],
            storage=StorageConfig(root=str(tmp_path / "data")),
        )

    state = tmp_path / "rig.state.json"
    app = Application(make_cfg(), state_path=state)
    app.start()
    app.camera("left").pipeline.update_params("display", {"gain": 3.0})
    app.stop()

    app2 = Application(make_cfg(), state_path=state, restore=False)
    app2.start()
    try:
        assert app2.camera("left").pipeline.stage("display").params.gain == 1.0
    finally:
        app2.stop()


def test_stale_state_does_not_prevent_startup(tmp_path):
    """A state file written before a config change names things that no longer
    exist. That must degrade to a warning, not a failure to start."""
    import json

    from trilobite.app import Application
    from trilobite.config import AppConfig, StageConfig, StorageConfig

    state = tmp_path / "rig.state.json"
    state.write_text(json.dumps({
        "schema": 1, "saved": "then",
        "cameras": {
            "left": {"pipeline": {"gone": {"type": "levels", "gain": 2.0}}, "controls": {}},
            "vanished": {"pipeline": {}, "controls": {}},
        },
    }))
    cfg = AppConfig(
        cameras=[CameraConfig(cam_id="left", backend="synthetic", fps=1000,
                              preview_resolution=(32, 24),
                              pipeline=[StageConfig(type="levels", name="display")])],
        storage=StorageConfig(root=str(tmp_path / "data")),
    )
    app = Application(cfg, state_path=state)
    app.start()
    try:
        assert len(app.restore_notes) == 2
        assert app.camera("left").pipeline.stage("display").params.gain == 1.0
    finally:
        app.stop()


def test_corrupt_state_file_is_ignored(tmp_path):
    from trilobite.state import StateStore
    bad = tmp_path / "x.state.json"
    bad.write_text("{not json")
    assert StateStore(bad, dict).load() == {}


# --- calibration settings and readiness --------------------------------------


def test_derived_optics_match_the_hand_derivation():
    """Spec eq. (3) with F=50, d_L=55, b=1.2 -> the worked example in §1.3."""
    from trilobite.calibration.settings import BoardSpec, DerivedOptics, NominalOptics

    d = DerivedOptics.compute(
        NominalOptics(focal_length_mm=50.0, lens_to_mla_mm=55.0,
                      mla_to_sensor_mm=1.2, working_distance_mm=2000.0),
        BoardSpec(square_mm=20.0), pitch_px=100.0,
    )
    assert abs(d.f_px - 3478.3) < 0.5
    assert abs(d.D_mm - 550.0) < 0.5
    assert abs(d.kappa_px - (-8.0645)) < 0.001
    assert abs(d.alpha - 1.24) < 1e-4
    assert abs(d.baseline_mm - 2.782) < 0.01
    assert d.square_verdict == "too_big"          # 48 px at 2 m


def test_derived_optics_invert_back_to_the_physical_parameters():
    """Eq. (4). The fit's three scalars must recover F, d_L, b, or the check
    against the datasheet in §6 is not available."""
    from trilobite.calibration.settings import (
        BoardSpec,
        DerivedOptics,
        NominalOptics,
        invert_derived,
    )

    optics = NominalOptics(focal_length_mm=35.0, lens_to_mla_mm=41.5,
                           mla_to_sensor_mm=0.9, working_distance_mm=1500.0)
    d = DerivedOptics.compute(optics, BoardSpec(), pitch_px=100.0)
    back = invert_derived(d.f_px, d.kappa_px, d.D_mm)
    assert abs(back["focal_length_mm"] - 35.0) < 0.05
    assert abs(back["lens_to_mla_mm"] - 41.5) < 0.05
    assert abs(back["mla_to_sensor_mm"] - 0.9) < 0.005


def test_square_size_advice_is_actionable():
    from trilobite.calibration.settings import BoardSpec, DerivedOptics, NominalOptics

    optics = NominalOptics(working_distance_mm=2000.0)
    too_big = DerivedOptics.compute(optics, BoardSpec(square_mm=20.0), 100.0)
    assert too_big.square_verdict == "too_big"
    # The advice must actually land in the band when followed.
    lo = float(too_big.note.split("use ")[1].split("-")[0])
    fixed = DerivedOptics.compute(optics, BoardSpec(square_mm=lo * 1.2), 100.0)
    assert fixed.square_verdict == "ok", (lo, fixed.square_px)


def test_working_distance_at_the_centre_plane_is_flagged_not_infinite():
    """Z = D is where the sub-camera projection centre sits: the model is
    singular there and must say so rather than emit a huge number."""
    from trilobite.calibration.settings import BoardSpec, DerivedOptics, NominalOptics

    d = DerivedOptics.compute(
        NominalOptics(focal_length_mm=50.0, lens_to_mla_mm=55.0,
                      mla_to_sensor_mm=1.2, working_distance_mm=550.0),
        BoardSpec(), 100.0,
    )
    assert d.square_verdict == "singular"
    assert "singular" in d.note


def _cal_app(tmp_path, enabled=True, derotate=False, pitch=50.0, presence=True):
    """A one-camera app for the readiness checks.

    The preview is 728 x 544 -- half the 1456 x 1088 sensor, exactly as the rig
    is configured. Two checks depend on that being realistic: the grid is
    converted to the frame detection runs on, so a preview of some unrelated
    aspect ratio is refused; and the presence map needs the preview to resolve
    a board's squares, so a tiny preview is refused too. The default pitch of
    50 preview px is 100 px on the sensor.
    """
    from trilobite.app import Application
    from trilobite.config import AppConfig, StageConfig, StorageConfig

    stages = [StageConfig(type="mla_grid_overlay", name="mla", params={
        "enabled": enabled, "pitch_px": pitch, "derotate_views": derotate,
    })]
    if presence:
        stages.append(StageConfig(type="checkerboard_presence", name="presence",
                                  params={"enabled": True, "min_corners": 20}))
    cfg = AppConfig(
        cameras=[CameraConfig(
            cam_id="left", backend="synthetic", fps=1000, preview_resolution=(728, 544),
            pipeline=stages,
        )],
        storage=StorageConfig(root=str(tmp_path / "d")),
    )
    return Application(cfg)


def test_readiness_blocks_on_disabled_mla(tmp_path):
    app = _cal_app(tmp_path, enabled=False)
    app.start()
    try:
        r = app.calibration_readiness()
        assert r["ready"] is False
        assert any("disabled" in m for m in r["blocking_failures"])
    finally:
        app.stop()


def test_derotation_warns_but_does_not_block(tmp_path):
    """De-rotation does not invalidate a calibration: corners are recorded in
    sensor coordinates either way, so they map back identically. It costs about
    0.07 px of extra localisation noise (measured -- see
    scripts/measure_derotation_cost.py), which is worth avoiding and not worth
    refusing to start over. This test exists because the first version blocked
    on it, which was wrong."""
    app = _cal_app(tmp_path, derotate=True)
    app.start()
    try:
        r = app.calibration_readiness()
        assert r["ready"] is True
        assert any("derotate" in w for w in r["warnings"])
    finally:
        app.stop()


def test_safe_crop_scale_depends_on_aperture_shape(tmp_path):
    """Square apertures rotate with the lattice, so the usable axis-aligned
    crop shrinks with the angle. A circular aperture has no orientation, so
    rotation costs nothing -- which removes the only crop-related reason to
    minimise the grid rotation physically."""
    from trilobite.optics.mla import MLAGeometry

    flat = MLAGeometry(728, 544, 100.0, rotation_deg=0.0)
    tilted = MLAGeometry(728, 544, 100.0, rotation_deg=5.0)

    assert flat.max_safe_crop_scale("square") == pytest.approx(1.0)
    assert tilted.max_safe_crop_scale("square") == pytest.approx(0.9235, abs=1e-3)

    # Rotation-independent for a circle.
    assert flat.max_safe_crop_scale("circle") == pytest.approx(0.7071, abs=1e-3)
    assert tilted.max_safe_crop_scale("circle") == pytest.approx(0.7071, abs=1e-3)


def test_crop_scale_warning_reports_the_bound(tmp_path):
    app = _cal_app(tmp_path)
    app.camera("left").pipeline.update_params("mla", {"rotation_deg": 10.0, "crop_scale": 1.0})
    app.start()
    try:
        r = app.calibration_readiness()
        assert r["ready"] is True                      # advisory only
        assert any("neighbouring micro-image" in w for w in r["warnings"]), r["warnings"]
    finally:
        app.stop()


def test_default_grid_warns_but_does_not_block(tmp_path):
    """20 px is a legal pitch, so an untouched-looking grid is advice, not a
    refusal -- blocking on it would be wrong for a rig that genuinely wants it.

    Asserted per check rather than on r["ready"]: this rig deliberately has no
    presence stage, which is separately blocking. The claim under test is only
    that *alignment* never blocks."""
    app = _cal_app(tmp_path, pitch=20.0, presence=False)
    app.start()
    try:
        r = app.calibration_readiness()
        assert any("alignment" in w for w in r["warnings"])
        blocked = {c["id"] for c in r["checks"] if c["blocking"] and not c["ok"]}
        assert not any(cid.endswith(".aligned") for cid in blocked), blocked
    finally:
        app.stop()


def test_missing_presence_stage_blocks(tmp_path):
    """The hands-free loop is driven by checkerboard_presence. A pipeline
    without it starts, shows a preview, and silently never sees a board -- which
    is exactly the failure reported from the rig. Make it a refusal to start."""
    app = _cal_app(tmp_path, presence=False)
    app.start()
    try:
        r = app.calibration_readiness()
        assert r["ready"] is False
        ids = {c["id"] for c in r["checks"] if c["blocking"] and not c["ok"]}
        assert "left.presence" in ids, ids
    finally:
        app.stop()


def test_disabled_presence_stage_blocks(tmp_path):
    """Present but switched off is the same silence, and was the shipped state
    of config/pi.yaml -- so it gets its own check with its own message."""
    app = _cal_app(tmp_path)
    app.camera("left").pipeline.update_params("presence", {"enabled": False})
    app.start()
    try:
        r = app.calibration_readiness()
        assert r["ready"] is False
        ids = {c["id"] for c in r["checks"] if c["blocking"] and not c["ok"]}
        assert "left.presence_enabled" in ids, ids
    finally:
        app.stop()


def test_readiness_passes_when_aligned(tmp_path):
    app = _cal_app(tmp_path)
    app.start()
    try:
        assert app.calibration_readiness()["ready"] is True
    finally:
        app.stop()


def test_calibration_settings_persist(tmp_path):
    from trilobite.app import Application
    from trilobite.config import AppConfig, StageConfig, StorageConfig

    def cfg():
        return AppConfig(
            cameras=[CameraConfig(cam_id="left", backend="synthetic", fps=1000,
                                  preview_resolution=(32, 24),
                                  pipeline=[StageConfig(type="levels", name="display")])],
            storage=StorageConfig(root=str(tmp_path / "d")),
        )

    state = tmp_path / "s.state.json"
    a = Application(cfg(), state_path=state)
    a.start()
    a.calibration.board.square_mm = 12.5
    a.calibration.acceptance.target_per_tile = 4
    a.mark_dirty()
    a.stop()

    b = Application(cfg(), state_path=state)
    b.start()
    try:
        assert b.calibration.board.square_mm == 12.5
        assert b.calibration.acceptance.target_per_tile == 4
    finally:
        b.stop()
