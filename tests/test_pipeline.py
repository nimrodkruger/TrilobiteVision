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
