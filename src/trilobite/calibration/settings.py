"""Calibration session settings, and what they imply about the optics.

Everything here is declared before a session starts and frozen once one does.
The derived quantities are not decoration: they turn nominal optics into the
numbers that decide whether the board you are about to print is the right one,
and they are the stage-0 initialisation the fit needs (calibration-spec §4.4).
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

# IMX296 pixel pitch, mm. Fixed by the sensor, not a user setting.
PIXEL_PITCH_MM = 3.45e-3

# Where a checkerboard square should land, in sensor pixels. Below ~15 px
# sub-pixel corner localisation degrades; above ~30 px a 100 px micro-image
# holds too few corners to be worth the pose.
SQUARE_PX_MIN, SQUARE_PX_MAX = 15.0, 30.0


class BoardSpec(BaseModel):
    """The physical calibration target."""

    cols: int = Field(9, ge=3, le=40, title="Inner corners ↔",
                      description="Inner corners across the board")
    rows: int = Field(6, ge=3, le=40, title="Inner corners ↕",
                      description="Inner corners down the board")
    # A slider across 0.1-200 mm cannot land on 12.5, and this is a number you
    # measure once and type. Same reasoning throughout the optics group.
    square_mm: float = Field(20.0, gt=0.1, le=200.0, title="Square (mm)",
                             description="Physical square size on the printed board",
                             json_schema_extra={"widget": "box"})

    @property
    def corner_count(self) -> int:
        return self.cols * self.rows


class NominalOptics(BaseModel):
    """Nominal optics, from the datasheet and the mechanical drawing.

    Not fitted -- these are the starting point. The fit replaces them, and
    comparing what it returns against what you entered here is verification 3
    in calibration-spec §6.
    """

    focal_length_mm: float = Field(
        50.0, gt=1.0, le=500.0, title="F — objective (mm)",
        description="Main objective focal length, from the datasheet",
        json_schema_extra={"widget": "box"})
    lens_to_mla_mm: float = Field(
        55.0, gt=1.0, le=500.0, title="d_L — lens→MLA (mm)",
        description="Main lens principal plane to the microlens array",
        json_schema_extra={"widget": "box"})
    mla_to_sensor_mm: float = Field(
        1.2, gt=0.01, le=50.0, title="b — MLA→sensor (mm)",
        description="Microlens array to sensor spacing",
        json_schema_extra={"widget": "box"})
    working_distance_mm: float = Field(
        2000.0, gt=10.0, le=100000.0, title="Working distance (mm)",
        description="Nominal object distance for this session",
        json_schema_extra={"widget": "box"})


class AcceptanceSpec(BaseModel):
    """When a pose counts (calibration-ui-spec §5)."""

    min_corners_per_tile: int = Field(
        6, ge=1, le=400, title="Min corners / tile",
        description="Corners a tile must return for it to count towards a cross")
    min_cross_tiles: int = Field(
        5, ge=1, le=9, title="Min cross tiles",
        description="Tiles in the connected cross that must all detect (centre + 4)")
    target_per_tile: int = Field(
        8, ge=1, le=100, title="Target poses / tile",
        description="Accepted poses a tile needs before it turns green")
    move_fraction: float = Field(
        0.05, gt=0.0, le=1.0, title="Min move (fraction)",
        description="Board must move this fraction of the working distance "
                    "between accepts, so holding still does not bank duplicates")
    move_rotation_deg: float = Field(
        3.0, gt=0.0, le=90.0, title="…or rotate (deg)",
        description="Alternative movement gate: rotation since the last accept")


class CalibrationSettings(BaseModel):
    board: BoardSpec = Field(default_factory=BoardSpec)
    optics: NominalOptics = Field(default_factory=NominalOptics)
    acceptance: AcceptanceSpec = Field(default_factory=AcceptanceSpec)


class DerivedOptics(BaseModel):
    """What the nominal optics imply. All from calibration-spec eq. (3)."""

    f_px: float
    D_mm: float
    kappa_px: float
    alpha: float
    magnification_px_per_mm: float
    square_px: float
    square_verdict: Literal["ok", "too_small", "too_big", "singular"]
    baseline_mm: float
    note: str

    @classmethod
    def compute(cls, optics: NominalOptics, board: BoardSpec, pitch_px: float) -> DerivedOptics:
        F = optics.focal_length_mm
        dL = optics.lens_to_mla_mm
        b = optics.mla_to_sensor_mm
        Z = optics.working_distance_mm
        G = dL - F

        if abs(G) < 1e-6:
            return cls(
                f_px=0.0, D_mm=0.0, kappa_px=0.0, alpha=0.0,
                magnification_px_per_mm=0.0, square_px=0.0,
                square_verdict="singular", baseline_mm=0.0,
                note="d_L equals F: the MLA sits at the rear focal plane and the "
                     "object-side conjugate is at infinity. Check the drawing.",
            )

        f_px = (b * F / G) / PIXEL_PITCH_MM
        D_mm = F * dL / G
        alpha = (G + b) / G
        kappa_px = -F / (G + b)          # dimensionless: mm of centre per px of ĉ / s

        # Magnification at the working distance. Z = D is the singular plane --
        # the object-side conjugate of the MLA, where the sub-camera projection
        # centre sits and the model has no meaning.
        dZ = Z - D_mm
        if abs(dZ) < 1.0:
            return cls(
                f_px=round(f_px, 1), D_mm=round(D_mm, 1), kappa_px=round(kappa_px, 4),
                alpha=round(alpha, 5), magnification_px_per_mm=0.0, square_px=0.0,
                square_verdict="singular",
                baseline_mm=round(abs(kappa_px) * pitch_px * PIXEL_PITCH_MM, 4),
                note=f"Working distance is at the virtual-centre plane D = {D_mm:.0f} mm, "
                     f"where the projection is singular. Move the board.",
            )

        mag = abs(f_px / dZ)
        square_px = board.square_mm * mag
        verdict = (
            "too_small" if square_px < SQUARE_PX_MIN
            else "too_big" if square_px > SQUARE_PX_MAX
            else "ok"
        )

        # Adjacent virtual projection centres, in object space.
        baseline_mm = abs(kappa_px) * pitch_px * PIXEL_PITCH_MM

        if verdict == "ok":
            note = "Square size is in the good band."
        else:
            wanted_lo = SQUARE_PX_MIN / mag
            wanted_hi = SQUARE_PX_MAX / mag
            note = (
                f"At {Z:.0f} mm this board gives {square_px:.0f} px squares. "
                f"For 15-30 px use {wanted_lo:.1f}-{wanted_hi:.1f} mm squares, "
                f"or move the board."
            )

        return cls(
            f_px=round(f_px, 1),
            D_mm=round(D_mm, 1),
            kappa_px=round(kappa_px, 4),
            alpha=round(alpha, 5),
            magnification_px_per_mm=round(mag, 4),
            square_px=round(square_px, 1),
            square_verdict=verdict,
            baseline_mm=round(baseline_mm, 4),
            note=note,
        )


def invert_derived(f_px: float, kappa_px: float, D_mm: float) -> dict[str, float]:
    """Recover F, d_L, b from the three fitted scalars -- spec eq. (4).

    Used after a fit to check the result against the drawing. Kept beside the
    forward computation so the two cannot drift apart -- and there is a round
    trip test, because the units here are the easy thing to get wrong: the
    focal length must enter in **millimetres**, not pixels. Mixing them gives a
    plausible-looking F that is wrong by the pixel pitch.
    """
    if abs(1.0 - kappa_px) < 1e-9:
        return {}
    f_mm = f_px * PIXEL_PITCH_MM
    F = (D_mm + kappa_px * f_mm) / (1.0 - kappa_px)
    denom = kappa_px * (F + f_mm)
    if abs(denom) < 1e-12:
        return {}
    G = -(F**2) / denom
    if abs(F) < 1e-9:
        return {}
    b = f_mm * G / F
    return {
        "focal_length_mm": round(F, 4),
        "lens_to_mla_mm": round(G + F, 4),
        "mla_to_sensor_mm": round(b, 4),
    }


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------

class Check(BaseModel):
    """One precondition. `blocking` decides whether it stops entry or advises."""

    id: str
    ok: bool
    blocking: bool
    message: str


def readiness_report(cameras: list[Any]) -> dict[str, Any]:
    """Can a calibration session start?

    Split into blocking and advisory deliberately. Refusing to start because a
    slider happens to sit at its default value would be wrong -- 20 px is a
    legal pitch. Refusing because the tiles are being resampled would not be:
    that silently degrades every corner the session records.
    """
    checks: list[Check] = []

    for cam in cameras:
        cid = cam.cam_id
        stage = cam.mla_stage()

        if stage is None:
            checks.append(Check(
                id=f"{cid}.mla", ok=False, blocking=True,
                message=f"{cid}: no mla_grid_overlay stage in the pipeline. "
                        f"The tile geometry is what crops the micro-images.",
            ))
            continue

        p = stage.params
        checks.append(Check(
            id=f"{cid}.mla_enabled", ok=bool(p.enabled), blocking=True,
            message=(f"{cid}: MLA grid enabled" if p.enabled
                     else f"{cid}: MLA grid is disabled -- align it in live mode first"),
        ))

        derot = bool(getattr(p, "derotate_views", False))
        checks.append(Check(
            id=f"{cid}.derotate", ok=not derot, blocking=True,
            message=(f"{cid}: tiles are unresampled" if not derot else
                     f"{cid}: derotate_views is on. This rig's rotation is "
                     f"MLA-to-sensor, so tile content is not rotated; resampling "
                     f"would blur the corners the fit depends on."),
        ))

        streaming = bool(cam.source.is_open) and cam.errors == 0
        checks.append(Check(
            id=f"{cid}.stream", ok=streaming, blocking=True,
            message=(f"{cid}: streaming" if streaming else
                     f"{cid}: not streaming cleanly ({cam.errors} errors)"),
        ))

        # Advisory: the grid sitting at every default is a strong hint that
        # alignment was never done -- but 20 px is a legal pitch, so this
        # advises rather than blocks.
        untouched = (
            math.isclose(p.pitch_px, 20.0)
            and math.isclose(p.rotation_deg, 0.0)
            and math.isclose(p.offset_x, 0.0)
            and math.isclose(p.offset_y, 0.0)
        )
        checks.append(Check(
            id=f"{cid}.aligned", ok=not untouched, blocking=False,
            message=(f"{cid}: grid has been adjusted" if not untouched else
                     f"{cid}: grid is at every default value -- has alignment been done?"),
        ))

        scale = float(getattr(p, "crop_scale", 1.0))
        checks.append(Check(
            id=f"{cid}.crop_scale", ok=math.isclose(scale, 1.0), blocking=False,
            message=(f"{cid}: crop scale 1.0" if math.isclose(scale, 1.0) else
                     f"{cid}: crop scale is {scale}, not 1.0 -- tiles will not be "
                     f"one lenslet pitch"),
        ))

    if not cameras:
        checks.append(Check(
            id="cameras", ok=False, blocking=True, message="no cameras configured",
        ))

    blocking_failures = [c for c in checks if c.blocking and not c.ok]
    return {
        "ready": not blocking_failures,
        "checks": [c.model_dump() for c in checks],
        "blocking_failures": [c.message for c in blocking_failures],
        "warnings": [c.message for c in checks if not c.blocking and not c.ok],
    }
