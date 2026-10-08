"""Provenance that is frozen at execution, not assembled at save time.

Review finding F4, and the one concrete bug behind it: `save_frame` called
`pipeline.settings_snapshot()` **when the file was written**. Edit a gain
between capture and save and the sidecar described a value that never touched
the pixels. Worse, a `capture_full` frame never enters the pipeline at all and
still got a full parameter block -- a record of processing that did not happen,
indistinguishable in the file from one that did.

The property under test is therefore not "the sidecar has fields". It is:

    the same retained frame, saved twice across a live edit, must produce
    byte-identical acquisition and processing evidence

which is the test the original plan got backwards -- it asked for the two
saves to DIFFER.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from trilobite.app import CameraRuntime
from trilobite.config import CameraConfig, StageConfig, StorageConfig
from trilobite.processing.base import Stage, StageParams
from trilobite.processing.pipeline import Pipeline
from trilobite.processing.registry import register
from trilobite.processing.stages.basic import Levels
from trilobite.storage.writer import SIDECAR_SCHEMA, SessionWriter
from trilobite.types import Frame


class _Boom(StageParams):
    pass


@register("provenance_explode")
class _Explode(Stage):
    """A stage that always throws, so the catch and the record are the real
    ones rather than a mock of them."""

    Params = _Boom

    def apply(self, frame: Frame) -> Frame:
        raise RuntimeError("deliberate")


def runtime(tmp_path, stages=None):
    writer = SessionWriter(StorageConfig(root=str(tmp_path / "d")), tmp_path / "d")
    cfg = CameraConfig(
        cam_id="left", backend="synthetic", fps=200,
        full_resolution=(96, 72), preview_resolution=(48, 36),
        synthetic_drift_px=0.0,
        pipeline=stages if stages is not None else [
            StageConfig(type="stats", name="stats"),
            StageConfig(type="levels", name="display", params={"gain": 1.0}),
        ],
    )
    return CameraRuntime(cfg, writer=writer)


def sidecar_of(out) -> dict:
    return json.loads(Path(out["metadata"]).read_text(encoding="utf-8"))


# -- 1. frozen at execution -------------------------------------------------


def test_the_same_frame_saved_twice_across_an_edit_keeps_one_provenance(tmp_path):
    """The acceptance test for this stage, and the one the plan had inverted.

    Retain a processed frame, change the pipeline, save the frame again. The
    pixels did not change, so the record of what produced them must not change
    either. Only the `saved` block may differ.
    """
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.1)
        frame = cam.preview.get()[1]
        assert frame is not None and frame.processing is not None

        first = sidecar_of(cam.save_frame(frame, "view"))
        cam.pipeline.update_params("display", {"gain": 4.0})
        second = sidecar_of(cam.save_frame(frame, "view"))
    finally:
        cam.stop()

    assert first["processing"] == second["processing"], (
        "a live edit rewrote the provenance of a frame captured before it")
    assert first["acquisition"] == second["acquisition"]
    assert first["pipeline"] == second["pipeline"]
    # Gain 1.0 is what the pixels went through; 4.0 is what the pipeline says
    # NOW, and must appear in neither file.
    assert first["pipeline"]["display"]["gain"] == 1.0
    assert second["pipeline"]["display"]["gain"] == 1.0

    # And the one block that is allowed to differ, does.
    assert first["saved"]["save_id"] != second["saved"]["save_id"]
    assert first["saved"]["path"] != second["saved"]["path"]


def test_a_frame_processed_after_the_edit_records_the_new_value(tmp_path):
    """The counterpart. Freezing must not mean staleness: a frame that really
    was processed under the new parameters says so, with a later revision."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.1)
        before = cam.preview.get()[1].processing
        cam.pipeline.update_params("display", {"gain": 3.5})
        version = cam.preview.get()[0]
        deadline = time.monotonic() + 5
        while cam.preview.get()[0] <= version + 2:
            assert time.monotonic() < deadline, "no new frames"
            time.sleep(0.01)
        after = cam.preview.get()[1].processing
    finally:
        cam.stop()

    assert after["revision"] > before["revision"]
    got = {s["name"]: s["params"] for s in after["stages"]}
    assert got["display"]["gain"] == 3.5


def test_one_execution_uses_one_revision_even_under_a_concurrent_edit():
    """The barrier test from the plan. A parameter update landing between two
    stages used to give that frame a mixture of two revisions, with nothing
    recording which. The lock is now held across the whole pass."""
    pipe = Pipeline([Levels("a", enabled=True, gain=1.0),
                     Levels("b", enabled=True, gain=1.0)])
    seen: list[int] = []
    gate = threading.Event()

    class Blocking(Levels):
        def apply(self, frame):
            gate.set()
            time.sleep(0.15)          # editor runs during this window
            return super().apply(frame)

    pipe._stages[0] = Blocking("a", enabled=True, gain=1.0)

    def editor():
        gate.wait(2.0)
        pipe.update_params("b", {"gain": 7.0})

    t = threading.Thread(target=editor)
    t.start()
    out = pipe(Frame.now(np.full((8, 8), 20, np.uint8), "left", 1))
    t.join()

    params = {s["name"]: s["params"] for s in out.processing["stages"]}
    assert params["b"]["gain"] == 1.0, (
        "stage b ran under the edited value, so this frame is a mixture of two "
        "revisions and cannot be reproduced from its own record")
    seen.append(out.processing["revision"])
    assert pipe.revision > seen[0], "the edit did land, just not mid-frame"


def test_mutating_the_live_pipeline_cannot_change_a_retained_record():
    """The record is a plain snapshot, not a view onto the stage objects."""
    pipe = Pipeline([Levels("display", enabled=True, gain=2.0)])
    out = pipe(Frame.now(np.full((8, 8), 20, np.uint8), "left", 1))
    recorded = json.dumps(out.processing, sort_keys=True)
    pipe.update_params("display", {"gain": 8.0})
    assert json.dumps(out.processing, sort_keys=True) == recorded


# -- 2. three blocks, one version ------------------------------------------


def test_a_raw_capture_says_processing_was_bypassed(tmp_path):
    """It used to carry a full parameter block describing a pipeline it never
    entered. The parameters are still recorded -- the MLA alignment describes
    the OPTICS and `--grid` needs it -- but `ran: false` says they were not
    applied to these pixels."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        side = sidecar_of(cam.capture_still(raw=True, tag="raw"))
    finally:
        cam.stop()

    assert side["processing"]["ran"] is False
    assert set(side["processing"]["bypassed"]) == {"stats", "display"}
    # Context, not a claim of processing -- and present, because a raw capture
    # needs the alignment that was in force at exposure.
    assert side["pipeline"]["display"]["gain"] == 1.0


def test_a_preview_capture_says_processing_ran(tmp_path):
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.1)
        cam.preview.get()
        side = sidecar_of(cam.capture_preview(tag="view"))
    finally:
        cam.stop()
    assert side["processing"]["ran"] is True
    assert [s["name"] for s in side["processing"]["stages"]] == ["stats", "display"]
    assert all(s["outcome"] == "ok" for s in side["processing"]["stages"])


def test_every_sidecar_carries_the_schema_version(tmp_path):
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        side = sidecar_of(cam.capture_still(raw=True, tag="raw"))
    finally:
        cam.stop()
    assert side["schema"] == SIDECAR_SCHEMA


def test_the_three_blocks_are_present_and_separate(tmp_path):
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        side = sidecar_of(cam.capture_still(raw=True, tag="raw"))
    finally:
        cam.stop()
    for block in ("acquisition", "processing", "saved"):
        assert isinstance(side[block], dict) and side[block], block
    assert side["acquisition"]["validity"] == "unvalidated"
    assert side["saved"]["bytes"] == side["bytes"]


def test_a_failed_stage_is_named_in_the_record(tmp_path):
    """A stage that throws is still invisible in the pixels -- the frame passes
    through. The record is the only place it can be seen afterwards."""
    cam = runtime(tmp_path, stages=[
        StageConfig(type="provenance_explode", name="boom"),
        StageConfig(type="levels", name="display"),
    ])
    cam.start()
    try:
        time.sleep(0.1)
        cam.preview.get()
        side = sidecar_of(cam.capture_preview(tag="view"))
    finally:
        cam.stop()
    by_name = {s["name"]: s for s in side["processing"]["stages"]}
    assert by_name["boom"]["outcome"] == "failed"
    assert by_name["boom"]["reason"]
    assert by_name["display"]["outcome"] == "ok", "the rest of the pass still ran"


def test_a_disabled_stage_reads_as_skipped_not_ok(tmp_path):
    """A presence stage that shipped switched off looked identical to one that
    was working. `skipped` with a reason is what makes those different."""
    cam = runtime(tmp_path, stages=[
        StageConfig(type="levels", name="display", params={"enabled": False}),
    ])
    cam.start()
    try:
        time.sleep(0.1)
        cam.preview.get()
        side = sidecar_of(cam.capture_preview(tag="view"))
    finally:
        cam.stop()
    stage = side["processing"]["stages"][0]
    assert stage["outcome"] == "skipped"
    assert stage["reason"] == "disabled"


# -- 3. requested is not effective -----------------------------------------


def test_requested_and_effective_controls_are_kept_apart(tmp_path):
    """A request is a preference; the frame's own metadata is the fact. Under
    auto-exposure they routinely differ, and conflating them is how a sidecar
    comes to assert an exposure the sensor never used."""
    cam = runtime(tmp_path)
    cam.start()
    try:
        time.sleep(0.05)
        cam.set_controls({"ExposureTime": 4321})
        side = sidecar_of(cam.capture_still(raw=True, tag="raw"))
    finally:
        cam.stop()

    controls = side["acquisition"]["controls"]
    assert controls["requested"]["ExposureTime"] == 4321
    # The synthetic backend reports its own exposure in frame metadata, so the
    # two are present and separate rather than merged.
    assert "requested" in controls and "effective" in controls
    assert controls["requested"] is not controls["effective"]


def test_a_requested_control_the_frame_cannot_confirm_is_listed_unknown():
    """Never backfilled from the request. Presenting a preference as a
    measurement is the same move the raw boundary refuses for pixel values."""
    from trilobite.storage.writer import _controls_block

    block = _controls_block(
        {"ExposureTime": 5000, "AnalogueGain": 2.0},
        {"ExposureTime": 4997},            # the driver reported only one
    )
    assert block["effective"] == {"ExposureTime": 4997}
    assert block["unknown"] == ["AnalogueGain"]
    assert "AnalogueGain" not in block["effective"]


def test_nothing_requested_is_still_a_well_formed_block():
    from trilobite.storage.writer import _controls_block

    block = _controls_block(None, {"ExposureTime": 100})
    assert block["requested"] == {}
    assert block["effective"] == {"ExposureTime": 100}
    assert block["unknown"] == []


# -- the reader's half ------------------------------------------------------


def test_the_reader_refuses_a_schema_it_does_not_know(tmp_path):
    """Forward compatibility that fails open is not compatibility. A reader
    that half-parses a newer file reads the fields it recognises and ignores
    the ones whose meaning changed, which is the worst of both."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "rc4", Path(__file__).resolve().parent.parent / "scripts" / "read_capture.py")
    rc = importlib.util.module_from_spec(spec)
    sys.modules["rc4"] = rc
    spec.loader.exec_module(rc)

    np.save(tmp_path / "x.npy", np.zeros((4, 4), np.uint16))
    (tmp_path / "x.json").write_text(json.dumps({
        "schema": SIDECAR_SCHEMA + 1, "file": "x.npy", "shape": [4, 4],
    }), encoding="utf-8")

    with pytest.raises(SystemExit, match="understands up to"):
        rc.load(tmp_path / "x.npy")

    # And the current version loads.
    (tmp_path / "x.json").write_text(json.dumps({
        "schema": SIDECAR_SCHEMA, "file": "x.npy", "shape": [4, 4],
        "acquisition": {"validity": "science", "sensor_metadata":
                        {"raw_admitted": True}},
    }), encoding="utf-8")
    cap = rc.load(tmp_path / "x.npy")
    assert cap.schema == SIDECAR_SCHEMA
    assert cap.is_science, "the block's fields must reach the reader"
