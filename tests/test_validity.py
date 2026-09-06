"""Validity as a property of the whole stack, not of one function.

Supervisory review R2. The admission boundary was correct in isolation and the
claim it produced did not survive contact with the rest of the software: the
`Frame` default asserted `science`, the replay backend read pixels and ignored
the sidecar beside them, and the ISP path never went near admission at all. So
a capture the rig had refused could be written to disk as `diagnostic`, replayed
through the replay backend, and come back out labelled `science` -- the stack
laundering a refusal into an admission by round-tripping through a file.

The test that matters here is not "does admit() work". It is "can anything
anywhere produce a science frame without having established one".
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from trilobite.cameras.offline import ReplaySource, SyntheticSource
from trilobite.config import CameraConfig
from trilobite.types import (
    DIAGNOSTIC,
    SCIENCE,
    UNVALIDATED,
    Frame,
)


def write_capture(directory, stem, *, validity, admitted=True, shape=(16, 12)):
    """A .npy plus the sidecar the rig would have written beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    arr = np.arange(shape[0] * shape[1], dtype=np.uint16).reshape(shape)
    np.save(directory / f"{stem}.npy", arr)
    sidecar = {
        "file": f"{stem}.npy",
        "cam_id": "left",
        "validity": validity,
        "source_kind": "raw",
        "space": "raw",
        "shape": list(shape),
        "sensor_metadata": {
            "raw_admitted": admitted,
            "raw_format": "R10",
            "raw_alignment": "lsb",
            "raw_sample_shift": 0,
        },
    }
    (directory / f"{stem}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return arr


def replay(directory, **kw):
    s = ReplaySource(CameraConfig(cam_id="left", backend="replay",
                                  source_dir=str(directory), fps=1000, **kw))
    s.open()
    return s


# -- the default ------------------------------------------------------------


def test_nothing_becomes_science_by_omission():
    """The single change that closes most of R2. A producer that never
    considered the question used to make the strongest claim in the system."""
    assert Frame.now(np.zeros((4, 4), np.uint8), "left", 1).validity == UNVALIDATED


def test_a_frame_constructed_directly_is_also_unvalidated():
    """Not only the `now` convenience. The dataclass default matters because
    `replace()` and every test helper go through it."""
    f = Frame(data=np.zeros((4, 4), np.uint8), cam_id="left", seq=1,
              t_mono=0.0, t_wall=0.0)
    assert f.validity == UNVALIDATED and not f.is_science


# -- replay -----------------------------------------------------------------


def test_replaying_a_diagnostic_capture_does_not_launder_it(tmp_path):
    """The exact probe from the review. A refused capture, written to disk,
    read back by the replay backend, came out marked `science`."""
    write_capture(tmp_path, "diagnostic_raw_left_000001", validity=DIAGNOSTIC,
                  admitted=False)
    frame = replay(tmp_path).read_preview()
    assert frame.validity == DIAGNOSTIC
    assert frame.meta["replay_recorded_validity"] == DIAGNOSTIC


def test_replaying_an_admitted_capture_carries_its_evidence(tmp_path):
    """The other direction has to work too, or replay is useless for the thing
    it exists for -- debugging a calibration deterministically off recorded
    frames. Carried, not re-derived: the admission happened once, on the rig."""
    write_capture(tmp_path, "raw_left_000001", validity=SCIENCE, admitted=True)
    frame = replay(tmp_path).read_preview()
    assert frame.validity == SCIENCE
    assert frame.meta["raw_format"] == "R10"
    assert frame.meta["raw_sample_shift"] == 0


def test_a_science_label_without_admission_evidence_is_not_believed(tmp_path):
    """A sidecar saying `science` while carrying no admission record is either
    hand-edited or from a schema this code does not know. Either way the label
    alone is not the evidence, and treating it as such would make the whole
    boundary bypassable with a text editor."""
    write_capture(tmp_path, "raw_left_000001", validity=SCIENCE, admitted=False)
    assert replay(tmp_path).read_preview().validity == UNVALIDATED


def test_an_image_with_no_sidecar_replays_as_unvalidated(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    np.save(tmp_path / "loose.npy", np.zeros((8, 8), np.uint16))
    frame = replay(tmp_path).read_preview()
    assert frame.validity == UNVALIDATED
    assert frame.meta["replay_sidecar"] is None


def test_an_unreadable_sidecar_is_unvalidated_not_an_error(tmp_path):
    """Replay must not fall over on a truncated file, and must not shrug and
    carry on as though the file said something good."""
    np.save(tmp_path / "x.npy", np.zeros((8, 8), np.uint16))
    (tmp_path / "x.json").write_text("{ not json", encoding="utf-8")
    frame = replay(tmp_path).read_preview()
    assert frame.validity == UNVALIDATED
    assert "replay_sidecar_error" in frame.meta


def test_an_unrecognised_validity_string_is_not_admitted(tmp_path):
    """A typo, a newer schema, a hand edit. The reader fails closed on all
    three rather than pattern-matching for the absence of 'diagnostic'."""
    write_capture(tmp_path, "raw_left_000001", validity="scienece", admitted=True)
    assert replay(tmp_path).read_preview().validity == UNVALIDATED


def test_the_replayed_frame_says_it_came_from_replay(tmp_path):
    write_capture(tmp_path, "raw_left_000001", validity=SCIENCE)
    assert replay(tmp_path).read_preview().source_kind == "replay"


def test_the_full_frame_served_from_replay_keeps_the_validity(tmp_path):
    """`_to_mono` goes through `derive`, which is exactly the path a claim can
    be lost on if it lives in a metadata dictionary."""
    write_capture(tmp_path, "diagnostic_x", validity=DIAGNOSTIC, admitted=False)
    src = replay(tmp_path)
    src.request_full_frame()
    src.read_preview()
    assert src.take_full_frame().validity == DIAGNOSTIC


# -- the synthetic backend --------------------------------------------------


def synthetic(**kw):
    s = SyntheticSource(CameraConfig(
        cam_id="left", backend="synthetic", fps=1000,
        full_resolution=(64, 48), preview_resolution=(32, 24),
        synthetic_drift_px=0.0, **kw))
    s.open()
    return s


@pytest.mark.parametrize("raw", [True, False])
def test_a_rendered_frame_is_never_science(raw):
    """`space='raw'` on the synthetic backend means "stands in for the raw
    path", so the rest of the stack can be exercised with no sensor. It does
    not mean sensor counts, because there is no sensor."""
    f = synthetic().capture_full(raw=raw)
    assert f.validity == UNVALIDATED
    assert f.source_kind == "synthetic"


def test_the_synthetic_preview_and_served_frame_agree():
    s = synthetic()
    s.request_full_frame()
    preview = s.read_preview()
    assert preview.validity == UNVALIDATED
    assert s.take_full_frame().validity == UNVALIDATED


# -- the pipeline -----------------------------------------------------------


def test_a_pipeline_stage_cannot_promote_what_it_processes():
    """Derived frames go through `derive`, which carries the claim rather than
    letting each stage restate it. A stage that could raise the validity of its
    own output would defeat the boundary from inside."""
    from trilobite.processing.pipeline import Pipeline
    from trilobite.processing.stages.basic import Levels

    pipe = Pipeline([Levels("display", enabled=True, gain=2.0)])
    out = pipe(Frame.now(np.full((8, 8), 20, np.uint8), "left", 1,
                         validity=DIAGNOSTIC))
    assert out.validity == DIAGNOSTIC


def test_the_pipeline_does_not_invent_a_claim_for_an_unvalidated_frame():
    from trilobite.processing.pipeline import Pipeline
    from trilobite.processing.stages.basic import Levels

    pipe = Pipeline([Levels("display", enabled=True, gain=2.0)])
    assert pipe(Frame.now(np.full((8, 8), 20, np.uint8), "left", 1)).validity \
        == UNVALIDATED
