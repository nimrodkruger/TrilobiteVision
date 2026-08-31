"""Tests that run anywhere -- no camera, no Pi.

The point of the synthetic backend is that this whole file passes on a Windows
desktop, in CI, and on the Pi identically.
"""

import numpy as np
import pytest
from pydantic import ValidationError

from flyeye.cameras.registry import build_camera
from flyeye.config import CameraConfig, StageConfig
from flyeye.processing import Pipeline, catalogue
from flyeye.processing.registry import build_stage
from flyeye.types import Frame


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
