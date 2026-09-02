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
    # These two are the *offline* duplicate check, applied when the recorded
    # poses are fitted: they need a PnP solve, which is not something to run on
    # the rig. The live gate that stops you banking fifty identical poses while
    # holding the board still is CaptureSpec.move_threshold, which compares
    # presence maps and costs nothing.
    move_fraction: float = Field(
        0.05, gt=0.0, le=1.0, title="Min move (fraction, offline)",
        description="Offline: board must move this fraction of the working "
                    "distance between accepted poses for both to be kept")
    move_rotation_deg: float = Field(
        3.0, gt=0.0, le=90.0, title="…or rotate (deg, offline)",
        description="Offline alternative movement gate: rotation since the last accept")


class CaptureSpec(BaseModel):
    """The hands-free capture loop (calibration-ui-spec §5.4).

    The operating assumption, and the reason every default here is what it is:
    **both of your hands are on the board.** You cannot press a key to take a
    shot, you cannot choose which part of the array to fill next, and you are
    looking at the board rather than at the screen. So the rig watches, decides,
    and tells you what happened out loud.

    The loop is: see the board -> wait for it to stop moving -> check it
    properly -> record it -> tell you -> refuse to record again until it has
    moved. You move, pause, move, pause. Nothing else is required of you.
    """

    min_tiles_seeing: int = Field(
        5, ge=1, le=400, title="Micro-images seeing the board",
        description="How many must show the pattern before a pose is even "
                    "considered. Below this the rig says 'show the board'.",
        json_schema_extra={"widget": "box"})
    settle_frames: int = Field(
        4, ge=1, le=60, title="Frames held still",
        description="Consecutive ticks the presence map must be unchanged "
                    "before a shot is taken. At 8 Hz, 4 is half a second.",
        json_schema_extra={"widget": "box"})
    still_threshold: float = Field(
        0.06, gt=0.0, le=1.0, title="Stillness tolerance",
        description="Relative change in the presence map that still counts as "
                    "holding still. Larger tolerates shakier hands and risks "
                    "capturing mid-move.",
        json_schema_extra={"widget": "box"})
    move_threshold: float = Field(
        0.25, gt=0.0, le=4.0, title="Movement before re-arming",
        description="How different the picture must become before another pose "
                    "is allowed. This is what stops a held board banking fifty "
                    "near-identical poses and biasing the fit toward one place.",
        json_schema_extra={"widget": "box"})
    review_s: float = Field(
        2.5, ge=0.0, le=30.0, title="Review pause (s)",
        description="After each shot the display freezes on it with the "
                    "detected corners drawn. Press Escape within this window to "
                    "discard it. 0 disables the pause.",
        json_schema_extra={"widget": "box"})
    reject_cooldown_s: float = Field(
        1.0, ge=0.0, le=30.0, title="Pause after a rejected shot (s)",
        description="Stops a board that is present but undetectable from being "
                    "re-checked eight times a second.",
        json_schema_extra={"widget": "box"})
    require_all_cameras: bool = Field(
        True, title="Both cameras must see it",
        description="Off records a pose when either camera sees the board. Such "
                    "a pose still constrains that camera's own parameters but "
                    "contributes nothing to the stereo relation.")
    tick_hz: float = Field(
        8.0, ge=1.0, le=30.0, title="Decision rate (Hz)",
        description="How often the loop looks. It reads a map the pipeline has "
                    "already computed, so this costs almost nothing.",
        json_schema_extra={"widget": "box"})
    max_poses: int = Field(
        0, ge=0, le=10000, title="Stop after N poses (0 = never)",
        description="A session ends when you press Finish. This is a disk "
                    "guard, not a target; leave it at 0 unless you want one.",
        json_schema_extra={"widget": "box"})
    min_free_gb: float = Field(
        1.0, ge=0.0, le=1000.0, title="Stop below N GB free",
        description="Each pose is two full frames, about 3 MB. Filling the "
                    "output device mid-session is the one failure that loses "
                    "work already done.",
        json_schema_extra={"widget": "box"})


class DetectionSpec(BaseModel):
    """Flags for the corner detector used by the accept check.

    Two switches, and both defaults are measured rather than assumed. On the
    synthetic board, 117 micro-images of 100 px:

        flags              per tile   corner straightness
        (none)              1.95 ms      0.0055 px
        NORMALIZE_IMAGE     2.13 ms      0.0440 px      <- eight times worse
        ACCURACY            4.86 ms      0.0040 px

    NORMALIZE_IMAGE costing precision is the surprise, and it is why it is not
    on despite sounding like an unambiguous good. It earns its place only when
    a micro-image is too unevenly lit for the pattern to be found at all --
    a real possibility with vignetted lenslets, which is why it is a switch and
    not a decision made here.

    The rate limits that used to live in this class -- pass interval, CPU duty,
    concurrent cameras, tile cap -- are gone with the worker they bounded.
    Nothing runs the detector on a loop of its own any more: the live map is a
    pipeline stage (calibration/presence.py) and this detector runs on five
    tiles once per pose. See docs/cleanup-log.md, 2026-09-01 (c).
    """

    normalize_illumination: bool = Field(
        False, title="Normalise illumination",
        description="CALIB_CB_NORMALIZE_IMAGE. Turn on only if vignetting stops "
                    "tiles being found: it costs about eight times the corner "
                    "precision on evenly-lit tiles. Finding the pattern beats "
                    "locating it well, but not when it was already being found.")
    high_accuracy: bool = Field(
        True, title="High accuracy",
        description="CALIB_CB_ACCURACY: an extra sub-pixel refinement, about "
                    "2.5x the cost per tile. On by default now that this runs "
                    "on five tiles once per pose rather than on every tile of "
                    "every frame -- at that rate the cost is not measurable.")


class CalibrationSettings(BaseModel):
    board: BoardSpec = Field(default_factory=BoardSpec)
    optics: NominalOptics = Field(default_factory=NominalOptics)
    acceptance: AcceptanceSpec = Field(default_factory=AcceptanceSpec)
    detection: DetectionSpec = Field(default_factory=DetectionSpec)
    capture: CaptureSpec = Field(default_factory=CaptureSpec)


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


def _full_resolution(cam: Any) -> tuple[int, int]:
    """(width, height) of the frame detection will actually run on."""
    try:
        info = cam.source.describe()
        w, h = info.full_resolution
        if w and h:
            return int(w), int(h)
    except Exception:
        pass
    return (1456, 1088)


# A sanity bound on the lattice, not a cost bound. Nothing scans tile-by-tile
# any more -- the live map is one pass over the frame whatever the pitch -- but
# `whole_indices` is quadratic in 1/pitch and a coverage display of ten
# thousand cells is not a display. A real 14x10 array is ~130.
MAX_SANE_TILES = 1200

# Below this, a micro-image in the preview cannot hold a resolvable
# checkerboard. A 4x3 board needs six squares across the tile, and a saddle
# detector needs a few pixels per square, so 24 px is about the floor. On this
# rig the preview is half scale and a micro-image is 50 px, comfortably clear.
MIN_PREVIEW_TILE_PX = 24.0


def _preview_resolution(cam: Any) -> tuple[int, int] | None:
    """(width, height) of the stream the MLA parameters were set against.

    Needed because the stage only learns its reference resolution from the
    first preview frame, and readiness is asked before that frame exists --
    at which point falling back to the *sensor* resolution silently divides the
    pitch by two and reports thousands of tiles. The configured preview size is
    known without waiting for anything.
    """
    try:
        w, h = cam.source.describe().preview_resolution
        return (int(w), int(h)) if w and h else None
    except Exception:
        return None


def _cv2_check() -> Check:
    """Is the corner detector's one hard dependency actually here?

    Blocking, and checked here rather than at the point of use, because the
    failure otherwise arrives as a traceback inside a worker thread half a
    second after the button is pressed -- at which point it reads as "detection
    is broken" rather than "OpenCV is not installed".
    """
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return Check(
            id="opencv", ok=False, blocking=True,
            message="OpenCV (cv2) not importable. Pi: 'sudo apt install -y "
                    "python3-opencv' with a --system-site-packages venv. "
                    "Desktop: pip install -e '.[desktop]'.",
        )
    return Check(id="opencv", ok=True, blocking=True, message=f"OpenCV {cv2.__version__}")


def readiness_report(
    cameras: list[Any], settings: CalibrationSettings | None = None
) -> dict[str, Any]:
    """Can a calibration session start?

    Split into blocking and advisory deliberately. Refusing to start because a
    slider happens to sit at its default value would be wrong -- 20 px is a
    legal pitch. Refusing because OpenCV is absent would not be: there is
    nothing to run.
    """
    checks: list[Check] = [_cv2_check()]

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

        # Advisory, not blocking. Corners are recorded in sensor coordinates
        # whichever way the tile was extracted, so de-rotation does not
        # invalidate anything -- it costs about 0.07 px of extra corner
        # localisation noise against a ~0.15 px baseline (measured; see
        # scripts/measure_derotation_cost.py). Worth avoiding, not worth
        # refusing to start over.
        derot = bool(getattr(p, "derotate_views", False))
        checks.append(Check(
            id=f"{cid}.derotate", ok=not derot, blocking=False,
            message=(f"{cid}: tiles unresampled" if not derot else
                     f"{cid}: derotate_views is on -- adds ~0.07 px corner noise "
                     f"for no benefit here, since the rotation is MLA-to-sensor "
                     f"and the tile content is not rotated. Valid either way."),
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

        # The crop must stay inside its own lenslet: straying into a
        # neighbour feeds a different scene patch to the corner detector.
        # The bound depends on the aperture shape and, for square apertures,
        # on the grid rotation.
        # Depends only on the rotation, so it does not wait for a frame --
        # an earlier version read the live frame and silently passed before the
        # first preview arrived.
        scale = float(getattr(p, "crop_scale", 1.0))
        geom = stage.geometry(1, 1)
        safe_sq = geom.max_safe_crop_scale("square")
        safe_cir = geom.max_safe_crop_scale("circle")
        checks.append(Check(
            id=f"{cid}.crop_scale", ok=scale <= safe_sq + 1e-9, blocking=False,
            message=(
                f"{cid}: crop scale {scale:g} fits the cell "
                f"(square apertures allow {safe_sq:.3f} at {p.rotation_deg:g}°, "
                f"circular {safe_cir:.3f})"
                if scale <= safe_sq + 1e-9 else
                f"{cid}: crop scale {scale:g} exceeds {safe_sq:.3f} -- at "
                f"{p.rotation_deg:g}° rotation an axis-aligned tile this size "
                f"reaches into the neighbouring micro-image. Circular apertures "
                f"cap it at {safe_cir:.3f} regardless of rotation."
            ),
        ))

        # How much work is one pass, actually? This is the check the first
        # field run needed and did not have. Detection cost is linear in the
        # number of whole tiles, and the number of whole tiles is quadratic in
        # 1/pitch: at the default 20 px preview pitch a 1456 px sensor yields
        # about 900 tiles rather than the ~130 a correctly aligned 100 px grid
        # gives. Seven times the work, on a board that was already close to its
        # thermal and power limits with two cameras running.
        #
        # Blocking, because the number is knowable before anything starts and
        # the only thing that produces it is a wrong pitch.
        full_w, full_h = _full_resolution(cam)
        ref = (getattr(stage, "reference_shape", None)
               or _preview_resolution(cam) or (full_w, full_h))
        limit = MAX_SANE_TILES
        det_geom = None
        scale_error = ""
        try:
            det_geom = stage.geometry(*ref).rescaled(full_w, full_h)
            n_tiles = len(det_geom.whole_indices(scale, derotate=derot))
        except (ValueError, ZeroDivisionError) as exc:
            n_tiles, scale_error = 0, str(exc)

        if scale_error:
            message = (
                f"{cid}: the preview ({ref[0]}x{ref[1]}) and the sensor frame "
                f"({full_w}x{full_h}) do not share an aspect ratio, so the grid "
                f"you aligned cannot be transferred to the frame detection runs "
                f"on. Fix preview_resolution in the config. ({scale_error})"
            )
        elif n_tiles == 0:
            message = (
                f"{cid}: the grid yields no whole tiles at {det_geom.pitch:.0f} px "
                f"on the sensor. Check pitch and offsets -- a pitch larger than "
                f"the frame gives exactly this."
            )
        elif n_tiles > limit:
            message = (
                f"{cid}: {n_tiles} whole tiles at {det_geom.pitch:.0f} px on the "
                f"sensor. A real 100 px grid gives about 130, so this is a wrong "
                f"pitch rather than a large array. Nothing scans tile-by-tile any "
                f"more, so this no longer costs a core -- but the lattice "
                f"enumeration is quadratic in 1/pitch and the coverage display "
                f"becomes unreadable."
            )
        else:
            message = (
                f"{cid}: {n_tiles} whole micro-images at {det_geom.pitch:.0f} px "
                f"on the sensor"
            )
        checks.append(Check(
            id=f"{cid}.tile_count", ok=0 < n_tiles <= limit, blocking=True,
            message=message,
        ))

        # The live presence map runs on the PREVIEW, and it can only count
        # corners it can resolve. At an eighth scale a micro-image is 12 px
        # across and its squares are two, which no saddle detector will find --
        # and the symptom is the console saying "show the board" while the
        # board is plainly in shot. Blocking, because in that state the whole
        # hands-free loop is inert.
        # The whole hands-free loop reads this stage and nothing else. It ships
        # DISABLED in config/pi.yaml, because it is meaningless until the MLA is
        # aligned -- and the first field run of the loop went straight into
        # "SHOW THE BOARD" forever with a board plainly in shot, because the
        # stage was still off and nothing said so. The MLA stage had a blocking
        # check; this one did not. Now it does.
        presence = cam.presence_stage() if hasattr(cam, "presence_stage") else None
        if presence is None:
            checks.append(Check(
                id=f"{cid}.presence", ok=False, blocking=True,
                message=f"{cid}: no checkerboard_presence stage in the pipeline. "
                        f"Add it AFTER the mla_grid_overlay stage -- it counts "
                        f"corners into that grid's micro-images, and without it "
                        f"the capture loop can never see a board.",
            ))
        else:
            checks.append(Check(
                id=f"{cid}.presence_enabled", ok=bool(presence.params.enabled),
                blocking=True,
                message=(f"{cid}: presence map enabled" if presence.params.enabled
                         else f"{cid}: the checkerboard_presence stage is DISABLED. "
                              f"It ships off because it is meaningless before the "
                              f"MLA is aligned. Enable it in the imaging page -- "
                              f"nothing can be detected until you do."),
            ))
            bound = getattr(presence, "_geometry_source", None) is not None
            checks.append(Check(
                id=f"{cid}.presence_bound", ok=bound, blocking=True,
                message=(f"{cid}: presence map reads the MLA grid" if bound else
                         f"{cid}: the presence stage is not bound to an MLA stage. "
                         f"It must come after mla_grid_overlay in the pipeline."),
            ))

        # Only when the stage is actually running. A disabled presence stage
        # is a rig not using the hands-free loop, and telling it its preview is
        # too small is noise about a feature it is not using.
        if presence is not None and presence.params.enabled:
            prev = _preview_resolution(cam) or (full_w, full_h)
            prev_pitch = float(p.pitch_px) * (
                prev[0] / ref[0] if ref[0] else 1.0)
            ok_res = prev_pitch >= MIN_PREVIEW_TILE_PX
            checks.append(Check(
                id=f"{cid}.preview_resolves", ok=ok_res, blocking=True,
                message=(
                    f"{cid}: micro-images are {prev_pitch:.0f} px in the preview "
                    f"-- enough for the presence map"
                    if ok_res else
                    f"{cid}: micro-images are only {prev_pitch:.0f} px in the "
                    f"{prev[0]}x{prev[1]} preview, below the {MIN_PREVIEW_TILE_PX} px "
                    f"the presence map needs to resolve a board's squares. Raise "
                    f"preview_resolution in the config -- the console would "
                    f"otherwise ask you to show a board that is already in shot."
                ),
            ))

            if settings is not None:
                # A tile that sees the whole pattern reads about one saddle per
                # grid vertex, which is (cols+2)x(rows+2) -- not cols x rows.
                # Getting this wrong by that factor makes the loop either never
                # arm or arm on an empty bench.
                expect = (settings.board.cols + 2) * (settings.board.rows + 2)
                thresh = int(presence.params.min_corners)
                sane = 0.35 * expect <= thresh <= 0.95 * expect
                checks.append(Check(
                    id=f"{cid}.presence_threshold", ok=sane, blocking=False,
                    message=(
                        f"{cid}: presence threshold {thresh} suits a "
                        f"{settings.board.cols}x{settings.board.rows} board (~{expect} "
                        f"vertices)"
                        if sane else
                        f"{cid}: presence threshold is {thresh}, but a "
                        f"{settings.board.cols}x{settings.board.rows} board fills a "
                        f"micro-image with about {expect} saddle points. Try "
                        f"{int(0.66 * expect)}: too high never arms, too low arms "
                        f"on any textured scene."
                    ),
                ))

        # Can the board even fit inside a micro-image? This is the check that
        # would otherwise cost an afternoon: every tile detects nothing, the
        # detector looks broken, and the actual answer is that a 9x6 board at
        # 21 px per square needs 210 px of tile and the tile is 100 px wide.
        #
        # Everything here is in FULL-RESOLUTION pixels, because that is where
        # detection runs -- see MLAGridOverlay.geometry_for.
        if settings is not None:
            full_w, full_h = _full_resolution(cam)
            ref = (getattr(stage, "reference_shape", None)
               or _preview_resolution(cam) or (full_w, full_h))
            factor = (full_w / ref[0]) if ref[0] else 1.0
            tile_px = float(p.pitch_px) * factor * scale
            derived = DerivedOptics.compute(settings.optics, settings.board, tile_px)
            need_w = (settings.board.cols + 1) * derived.square_px
            need_h = (settings.board.rows + 1) * derived.square_px
            fits = derived.square_px > 0 and max(need_w, need_h) <= tile_px
            checks.append(Check(
                id=f"{cid}.board_fits", ok=fits, blocking=False,
                message=(
                    f"{cid}: board needs {need_w:.0f}x{need_h:.0f} px, tile is "
                    f"{tile_px:.0f} px -- fits"
                    if fits else
                    f"{cid}: board needs {need_w:.0f}x{need_h:.0f} px at the nominal "
                    f"working distance but a micro-image is only {tile_px:.0f} px "
                    f"across. No tile can contain the whole pattern. Use a smaller "
                    f"board, smaller squares, or a nearer working distance."
                ),
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
