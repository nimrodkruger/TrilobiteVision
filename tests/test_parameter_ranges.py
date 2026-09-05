"""What the sliders are allowed to ask for, and why each bound is where it is.

A parameter range is not decoration. Every one of these bounds exists because
a value inside the old range did something the operator could not have wanted
and could not diagnose: a frame that went black, a grid that vanished, a
request that never returned. The tests are written against the *symptom* in
each case, so that widening a bound later fails for the reason it should.

The one that matters most is the lattice enumeration. It is quadratic in
1/pitch with trigonometry per candidate, and it runs inside a request handler
on a Pi, so "slow" and "hung" are the same thing from the browser.
"""

from __future__ import annotations

import math
import time

import pytest
from pydantic import ValidationError

from trilobite.optics.mla import MAX_LATTICE_CANDIDATES, MLAGeometry
from trilobite.processing.stages.basic import Crop, Levels, Stats
from trilobite.processing.stages.plenoptic import MLAGridOverlay

# -- the lattice enumeration ---------------------------------------------


def test_a_tiny_pitch_is_refused_before_it_can_be_enumerated():
    """The bound that stops the hang from being reachable through the UI."""
    with pytest.raises(ValidationError):
        MLAGridOverlay("mla", pitch_px=1.5)
    with pytest.raises(ValidationError):
        MLAGridOverlay("mla", pitch_px=9.9)
    MLAGridOverlay("mla", pitch_px=10.0)          # the floor itself is legal


def test_a_tiny_pitch_reaching_the_geometry_anyway_is_capped_not_run():
    """The backstop, because a bound on one parameter is not a guarantee about
    the product of three -- and `MLAGeometry` is constructed directly by the
    offline readers and the tests as well as by the stage.

    A hang inside a request handler is the worst available failure: the browser
    shows nothing, the log shows nothing, and the rig looks dead. An empty list
    is wrong in a way every caller already handles.
    """
    g = MLAGeometry(width=1456, height=1088, pitch=1.5)
    t = time.perf_counter()
    assert g.whole_indices(1.0) == []
    assert time.perf_counter() - t < 0.5, "the cap did not short-circuit"


def test_the_cap_is_above_what_a_real_rig_asks_for():
    """A guard that fires on legitimate settings is worse than no guard."""
    g = MLAGeometry(width=1456, height=1088, pitch=100.0)
    ni, nj = g._search_radius(1.0)
    assert (2 * ni + 1) * (2 * nj + 1) < MAX_LATTICE_CANDIDATES / 20
    assert len(g.whole_indices(1.0)) == 117


@pytest.mark.parametrize("pitch", [40.0, 63.5, 100.0, 210.0])
@pytest.mark.parametrize("rot", [0.0, 2.0, -13.0, 44.0])
@pytest.mark.parametrize("off", [(0.0, 0.0), (37.0, -21.0), (-90.0, 90.0)])
def test_the_tightened_search_radius_still_finds_every_whole_lenslet(pitch, rot, off):
    """The risk in making the bound tighter, tested directly.

    A search radius that is too small does not raise -- it silently drops the
    outermost ring of lenslets, which are exactly the ones the corner
    sub-apertures use and exactly the ones that tell you whether the pitch is
    right. So the tightened bound is compared against a deliberately absurd one
    on the same geometry: the sets must be identical, not merely similar.
    """
    g = MLAGeometry(width=1456, height=1088, pitch=pitch, rotation_deg=rot,
                    offset_x=off[0], offset_y=off[1])
    tight = set(g.whole_indices(1.0))

    # The reference: every index whose centre could conceivably be in frame,
    # by a bound nobody would ship -- the full diagonal plus the offset, twice.
    reach = 2 * (math.hypot(g.width, g.height) + math.hypot(*off))
    n = int(reach / pitch) + 3
    brute = {
        (i, j)
        for i in range(-n, n + 1)
        for j in range(-n, n + 1)
        if g.is_whole(i, j, 1.0, True)
    }
    assert tight == brute, f"missed {sorted(brute - tight)[:8]}"


def test_the_enumeration_is_memoised_on_the_geometry():
    """`apply()` reaches this every frame through `named_indices`, with
    parameters that have not changed since the last one. Without the cache that
    is 800,000 trigonometric tests a second across two cameras at 12 Hz."""
    a = MLAGeometry(width=1456, height=1088, pitch=25.0)
    b = MLAGeometry(width=1456, height=1088, pitch=25.0)
    first = a.whole_indices(1.0)
    # A frozen dataclass hashes by value, so an equal geometry is a cache hit
    # even though it is a different object -- which is the case that matters,
    # because geometry_for() builds a fresh one every call.
    assert b.whole_indices(1.0) is first


# -- the MLA offsets ------------------------------------------------------


def _grid(stage, w=1456, h=1088):
    p = stage.params
    return MLAGeometry(width=w, height=h, pitch=p.pitch_px,
                       rotation_deg=p.rotation_deg,
                       offset_x=p.offset_x, offset_y=p.offset_y)


def test_an_offset_past_one_pitch_is_folded_not_clamped():
    """Folding and clamping look the same until you drag the slider.

    A clamp stops the grid moving while the number keeps changing, so the
    control appears dead at one end. Folding sends the number to the other end
    and the grid carries on -- which is what the geometry does, because moving
    the origin by a whole lattice vector renames the lenslets and draws the
    identical grid.
    """
    stage = MLAGridOverlay("mla", pitch_px=100.0, offset_x=120.0, offset_y=-260.0)
    assert (stage.params.offset_x, stage.params.offset_y) == (20.0, 40.0)
    # And it is the same grid: the lenslet that was (0,0) is still there.
    before = MLAGeometry(width=1456, height=1088, pitch=100.0,
                         offset_x=120.0, offset_y=-260.0)
    assert _grid(stage).centre_of(1, -3) == pytest.approx(before.centre_of(0, 0))


@pytest.mark.parametrize("rot", [0.0, 7.0, -22.5, 44.0])
def test_the_fold_is_exact_under_rotation(rot):
    """Reducing x and y separately modulo the pitch is only right at zero
    rotation. The reduction is in the LATTICE basis, so it holds at any angle,
    and the check is that some lenslet of the folded grid lands exactly on the
    original origin."""
    stage = MLAGridOverlay("mla", pitch_px=100.0, rotation_deg=rot,
                           offset_x=213.0, offset_y=-147.0)
    original = MLAGeometry(width=1456, height=1088, pitch=100.0, rotation_deg=rot,
                           offset_x=213.0, offset_y=-147.0)
    folded = _grid(stage)
    target = original.centre_of(0, 0)
    hits = [
        (i, j)
        for i in range(-5, 6) for j in range(-5, 6)
        if abs(folded.centre_of(i, j)[0] - target[0]) < 1e-6
        and abs(folded.centre_of(i, j)[1] - target[1]) < 1e-6
    ]
    assert len(hits) == 1, f"folded grid does not contain the original origin ({rot}°)"


@pytest.mark.parametrize("rot", [0.0, 7.0, -22.5])
def test_a_folded_offset_is_inside_half_a_cell(rot):
    stage = MLAGridOverlay("mla", pitch_px=100.0, rotation_deg=rot,
                           offset_x=213.0, offset_y=-147.0)
    r = math.hypot(stage.params.offset_x, stage.params.offset_y)
    assert r <= 100.0 / math.sqrt(2) + 1e-9


def test_folding_is_idempotent():
    """It runs on construction AND on assignment, so a value that has already
    been folded must survive being folded again unchanged -- otherwise a stage
    would drift a pitch every time anything touched it."""
    stage = MLAGridOverlay("mla", pitch_px=100.0, offset_x=213.0, offset_y=-147.0)
    once = (stage.params.offset_x, stage.params.offset_y)
    stage.params.offset_x = once[0]
    stage.params.offset_y = once[1]
    assert (stage.params.offset_x, stage.params.offset_y) == once


def test_assigning_a_far_offset_does_not_recurse():
    """The fold runs inside an after-validator under validate_assignment, so it
    writes through __dict__. Written the obvious way it re-enters validation on
    the same model."""
    stage = MLAGridOverlay("mla", pitch_px=100.0)
    stage.params.offset_x = 350.0
    assert stage.params.offset_x == -50.0


def test_an_offset_beyond_any_reachable_cell_is_refused():
    """The static bound still exists, and is what the largest allowed pitch can
    produce after folding (800/sqrt2 = 566)."""
    with pytest.raises(ValidationError):
        MLAGridOverlay("mla", pitch_px=100.0, offset_x=1000.0)


# -- the other stage bounds ----------------------------------------------


def test_a_crop_scale_beyond_two_is_refused():
    """Above 2 a 'sub-aperture' spans four or more lenslets, which is not a
    sub-aperture -- it is a crop of the raw frame with a misleading name."""
    MLAGridOverlay("mla", pitch_px=100.0, crop_scale=2.0)
    with pytest.raises(ValidationError):
        MLAGridOverlay("mla", pitch_px=100.0, crop_scale=4.0)
    with pytest.raises(ValidationError):
        MLAGridOverlay("mla", pitch_px=100.0, crop_scale=0.1)


def test_a_display_gain_of_zero_is_refused():
    """A black preview is what a dead camera, a closed shutter and a crashed
    capture thread all look like. One slider at its left stop should not be
    able to counterfeit all three."""
    with pytest.raises(ValidationError):
        Levels("display", gain=0.0)
    Levels("display", gain=0.05)


def test_the_display_offset_covers_a_16_bit_frame():
    """+-128 DN was an 8-bit assumption. The preview carries mono16 from a
    10-bit sensor, where 128 counts is a rounding error."""
    Levels("display", offset=2000.0)
    with pytest.raises(ValidationError):
        Levels("display", offset=1e6)


def test_an_empty_crop_is_refused_rather_than_ignored():
    """`apply` already declines to make a zero-width array. Declining silently
    leaves the numbers saying one thing and the image showing another, with
    nothing anywhere saying why."""
    with pytest.raises(ValidationError):
        Crop("roi", x0=0.8, x1=0.2)
    with pytest.raises(ValidationError):
        Crop("roi", y0=0.5, y1=0.5)
    Crop("roi", x0=0.1, x1=0.9, y0=0.1, y1=0.9)


def test_every_numeric_stage_parameter_has_both_bounds():
    """So the UI can draw a slider for it at all.

    A parameter with a lower bound and no upper one got a 0..1 slider with the
    value pinned off the end -- `saturation_level` at 250 was doing exactly
    that. Anything genuinely unbounded has to say so by asking for
    widget:"box", which is a decision rather than an omission.
    """
    for stage in (MLAGridOverlay, Levels, Crop, Stats):
        schema = stage.Params.model_json_schema()
        for key, spec in schema.get("properties", {}).items():
            if spec.get("type") not in ("number", "integer"):
                continue
            has_min = {"minimum", "exclusiveMinimum"} & spec.keys()
            has_max = {"maximum", "exclusiveMaximum"} & spec.keys()
            # A parameter that is never drawn needs no drawable range. Both
            # exemptions are explicit declarations in the Field, so leaving a
            # range off has to be a decision rather than an oversight.
            exempt = spec.get("widget") in ("box", "hidden")
            assert (has_min and has_max) or exempt, (
                f"{stage.__name__}.{key} has no finite range and does not ask "
                f"for a box; the UI will draw it a 0..1 slider"
            )
