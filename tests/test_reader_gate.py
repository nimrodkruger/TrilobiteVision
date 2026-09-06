"""The offline reader must fail CLOSED. Supervisory review R1.

The first version asked one question -- "is the validity string exactly
`diagnostic`?" -- and admitted everything else. The review's probes returned
true for `unrecorded` and for `typo`. So a sidecar with a missing field, a
misspelling, or a value from a schema this code has never seen all passed as
measurable, on precisely the field whose job is to refuse.

`scripts/` is not a package, so the module is loaded by path. That is worth
doing rather than skipping: the reader is the last thing standing between a
refused buffer and a fitted model, and it was the least tested part of the
boundary.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "read_capture.py"


@pytest.fixture(scope="module")
def rc():
    spec = importlib.util.spec_from_file_location("read_capture", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["read_capture"] = mod
    spec.loader.exec_module(mod)
    return mod


def capture(tmp_path, stem="raw_left_000001", *, validity=None,
            source_kind=None, admitted=None, refusal=None, extra_sensor=None,
            omit_validity=False):
    """Write a .npy and the sidecar a caller wants to test the reader against."""
    arr = np.arange(12 * 16, dtype=np.uint16).reshape(12, 16)
    np.save(tmp_path / f"{stem}.npy", arr)
    sensor = {"rotate_deg": 0}
    if admitted is not None:
        sensor["raw_admitted"] = admitted
    if refusal:
        sensor["raw_refusal"] = refusal
    sensor.update(extra_sensor or {})
    side = {
        "file": f"{stem}.npy",
        "cam_id": "left",
        "tag": "raw",
        "space": "raw",
        "shape": [12, 16],
        "sensor_metadata": sensor,
        "camera": {"full_resolution": [16, 12]},
    }
    if not omit_validity:
        side["validity"] = validity
    if source_kind is not None:
        side["source_kind"] = source_kind
    (tmp_path / f"{stem}.json").write_text(json.dumps(side), encoding="utf-8")
    return tmp_path / f"{stem}.npy"


# -- what counts as science -------------------------------------------------


def test_an_admitted_capture_is_science(rc, tmp_path):
    cap = rc.load(capture(tmp_path, validity="science", admitted=True,
                          source_kind="raw"))
    assert cap.is_science and cap.geometry_ok
    cap.require_science("a radiometric fit")       # must not raise


def test_a_science_label_with_no_admission_record_is_refused(rc, tmp_path):
    """A label is not the evidence. Otherwise the whole boundary is bypassable
    with a text editor, which is not a threat model so much as an accident
    waiting to happen when somebody hand-fixes a sidecar."""
    cap = rc.load(capture(tmp_path, validity="science", admitted=None,
                          source_kind="raw"))
    assert not cap.is_science
    with pytest.raises(SystemExit, match="no admission record"):
        cap.require_science("a radiometric fit")


def test_an_explicitly_false_admission_is_refused(rc, tmp_path):
    cap = rc.load(capture(tmp_path, validity="science", admitted=False))
    assert not cap.is_science


# -- the cases that used to pass --------------------------------------------


def test_a_missing_validity_field_is_not_science(rc, tmp_path):
    """The archive case. Nothing in a pre-boundary file establishes that anyone
    ever checked it, so it cannot be `science` -- but it is perfectly readable,
    so positions stay measurable and the reader says what is missing rather
    than refusing to open it."""
    cap = rc.load(capture(tmp_path, omit_validity=True, admitted=True))
    assert cap.validity == "unknown"
    assert not cap.is_science
    assert cap.geometry_ok, "readable, so positions are measurable"
    with pytest.raises(SystemExit, match="no recognised validity"):
        cap.require_science("reading these values as sensor counts")
    cap.require_geometry("corner detection")       # must not raise


def test_a_misspelled_validity_is_refused_and_reported(rc, tmp_path, capsys):
    """`scienece`. The old check tested for the absence of one string, so every
    typo read as an admission."""
    cap = rc.load(capture(tmp_path, validity="scienece", admitted=True))
    assert cap.validity == "unknown"
    assert not cap.is_science
    assert "does not recognise" in capsys.readouterr().err


def test_a_validity_from_a_newer_schema_is_not_science(rc, tmp_path):
    """Forward compatibility that fails open is not compatibility -- for the
    strict question. The frame is still a frame."""
    cap = rc.load(capture(tmp_path, validity="provisional", admitted=True))
    assert not cap.is_science
    with pytest.raises(SystemExit):
        cap.require_science("a radiometric fit")


def test_a_null_validity_is_refused(rc, tmp_path):
    cap = rc.load(capture(tmp_path, validity=None, admitted=True))
    assert cap.validity == "unknown"
    assert not cap.is_science


# -- diagnostic -------------------------------------------------------------


def test_a_diagnostic_capture_is_the_one_thing_refused_outright(rc, tmp_path):
    """The only hard stop left, and the only one that earns it: `diagnostic`
    means the rig established these bytes are not pixel values, so there is no
    reading of them to measure."""
    cap = rc.load(capture(tmp_path, validity="diagnostic", admitted=False,
                          refusal="MONO_PISP_COMP1 is a COMPRESSED transport"))
    assert not cap.is_science and not cap.geometry_ok
    with pytest.raises(SystemExit, match="COMPRESSED"):
        cap.require_geometry("corner detection")
    with pytest.raises(SystemExit, match="COMPRESSED"):
        cap.require_science("a radiometric fit")


# -- geometry is a different question from radiometry -----------------------


def test_an_isp_pose_is_geometrically_usable_but_not_radiometrically(rc, tmp_path):
    """The workflow this distinction exists to keep working. A calibration pose
    is ISP mono output: `unvalidated` by construction, and a perfectly faithful
    picture of WHERE the corners are. Refusing it would have broken the one
    documented use of this script to no purpose."""
    cap = rc.load(capture(tmp_path, validity="unvalidated",
                          source_kind="isp_main"))
    assert cap.geometry_ok
    cap.require_geometry("corner detection")       # must not raise

    assert not cap.is_science
    with pytest.raises(SystemExit, match="refusing"):
        cap.require_science("reading these values as sensor counts")


def test_an_unreconciled_raw_frame_is_still_measurable_for_geometry(rc, tmp_path, capsys):
    """The loosening that unblocked the bench. A raw buffer whose stride or
    values did not reconcile is `unvalidated` with the reasons recorded -- and
    it is still a frame. Refusing it would have been protection against
    nothing: a wrong stride skews the aspect ratio and a wrong alignment is 64x
    too bright, both of which you find by LOOKING."""
    cap = rc.load(capture(
        tmp_path, validity="unvalidated", source_kind="raw",
        extra_sensor={"raw_reservations": [
            "peak value 64000 exceeds the 1023 maximum for a 10-bit R10 "
            "sample lsb-aligned in a 16-bit container"]}))
    assert cap.geometry_ok
    cap.require_geometry("corner detection")
    assert "64000 exceeds" in capsys.readouterr().err, (
        "the reason has to be said, not merely not enforced")


def test_the_reservations_are_carried_through_to_the_reader(rc, tmp_path):
    cap = rc.load(capture(
        tmp_path, validity="unvalidated",
        extra_sensor={"raw_reservations": ["one", "two"]}))
    assert cap.reservations == ("one", "two")
    assert "one" in cap.why_not_science and "two" in cap.why_not_science


# -- what the report says ---------------------------------------------------


def test_the_report_names_the_alignment_and_that_no_shift_was_applied(rc, tmp_path, capsys):
    """The one number a reader must not miss. With msb alignment the stored
    value is 64x the sample, and the pixels on disk are deliberately NOT
    shifted -- so the report has to say so where it cannot be skimmed past."""
    cap = rc.load(capture(
        tmp_path, validity="science", admitted=True, source_kind="raw",
        extra_sensor={"raw_format": "R10", "raw_bits_nominal": 10,
                      "raw_container_bits": 16, "raw_alignment": "msb",
                      "raw_sample_shift": 6, "raw_observed_max": 64000,
                      "raw_value_ceiling": 65472}))
    rc.describe(cap)
    out = capsys.readouterr().out
    assert "msb-aligned in 16" in out
    assert "NOT applied" in out
    assert "observed max 64000" in out


def test_the_report_lists_every_reservation(rc, tmp_path, capsys):
    """A grade with no reason attached makes the next person rediscover it."""
    rc.describe(rc.load(capture(
        tmp_path, validity="unvalidated",
        extra_sensor={"raw_reservations": ["stride disagrees", "peak too high"]})))
    out = capsys.readouterr().out
    assert "stride disagrees" in out and "peak too high" in out


def test_the_report_distinguishes_unrecorded_from_diagnostic(rc, tmp_path, capsys):
    rc.describe(rc.load(capture(tmp_path, omit_validity=True)))
    assert "NOT RECORDED" in capsys.readouterr().out

    rc.describe(rc.load(capture(tmp_path, stem="d", validity="diagnostic",
                                refusal="packed")))
    assert "DIAGNOSTIC" in capsys.readouterr().out


# -- the legacy trim is not re-derived over an admitted frame ---------------


def test_an_admitted_capture_is_not_trimmed_again(rc, tmp_path):
    """The rig already did this, against the negotiated format rather than by
    inferring the pixel size from the row length. Re-deriving it here could
    only disagree."""
    cap = rc.load(capture(tmp_path, validity="science", admitted=True))
    assert cap.trimmed_padding == 0
    assert cap.image.shape == (12, 16)
