"""Plenoptic-specific stages.

`derotate_views` defaults to **off**, and that is a physical statement about
this rig, not a display preference.

An apparent rotation of the lenslet grid has two possible causes:

  * **Sensor rotated relative to the optical assembly** -- the sensor samples a
    rotated version of everything, so each micro-image is rotated by the same
    angle as the lattice, and de-rotating restores the true field.

  * **MLA rotated relative to the sensor**, main objective square to it. Only
    the *lattice of centres* rotates. Each lenslet is a rotationally symmetric
    element re-imaging an intermediate image that has not moved, so the tile
    content is NOT rotated. De-rotating introduces a rotation that was never
    there and resamples the data for nothing.

**This rig is the second case** (confirmed for TrilobiteVision), so the default
is off: the tiles are plain crops, unresampled, and the rotation lives entirely
in the lattice matrix. See docs/calibration-spec.md section 1 -- there is no
rotation term anywhere in the projection model, for exactly this reason.

**This does not restrict the calibration.** The grid rotation is a fitted
parameter -- it lives in the 2x2 lattice matrix U -- and corners are recorded
in sensor coordinates whichever way the tile was extracted, so a de-rotated
tile yields the same corner positions after mapping back. Measured cost of the
extra resampling: about 0.07 px RMS of corner-localisation noise against a
~0.15 px baseline (scripts/measure_derotation_cost.py). Small, avoidable for
free by leaving this off, and not a reason to forbid anything.

Turn it on whenever comparing tiles side by side helps.

The grid overlay is an alignment aid, not a measurement -- it draws on the
preview only. Its parameters (pitch, rotation, offset) are the same numbers the
sub-aperture extraction uses, which is the point: you tune them by eye against
the live image, and the crops below the preview update from the same values, so
what you see aligned is what gets extracted.

Geometry lives in trilobite.optics.mla, shared with the web layer, so the
overlay and the crops cannot disagree.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field

from ...optics.mla import UI_SUBAPERTURES, MLAGeometry
from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register


class MLAParams(StageParams):
    """Shared by the overlay and by sub-aperture extraction.

    Offsets are in pixels **from the centre pixel of the frame**, which is the
    anchor the whole grid hangs from. Positive x is right, positive y is down.
    """

    enabled: bool = False
    # Ranges are chosen so the slider has usable resolution, not so they are
    # theoretically permissive. A pitch above ~400 px leaves three lenslets on
    # a 1456 px sensor, and the origin never needs to move more than a pitch or
    # two from centre -- the box takes exact values within these bounds anyway.
    pitch_px: float = Field(20.0, gt=1.0, le=400.0, description="Lenslet pitch, pixels")
    rotation_deg: float = Field(0.0, ge=-45.0, le=45.0, description="Grid rotation, degrees")
    offset_x: float = Field(0.0, ge=-500.0, le=500.0, description="Grid origin x from centre, px")
    offset_y: float = Field(0.0, ge=-500.0, le=500.0, description="Grid origin y from centre, px")
    crop_scale: float = Field(
        1.0, gt=0.1, le=4.0, description="Sub-aperture crop side, as a multiple of pitch"
    )
    derotate_views: bool = Field(
        False,
        description=(
            "Resample sub-aperture tiles into the lattice axes. Off for this rig: "
            "the MLA is rotated relative to the sensor, so tile content is not "
            "rotated and resampling would only blur it. See the module docstring."
        ),
    )


@register("mla_grid_overlay")
class MLAGridOverlay(Stage):
    """Draw the estimated lenslet grid and its anchor over the preview."""

    accepts = ("mono8", "mono16", "rgb8")

    class Params(MLAParams):
        # widget="box": a slider over a 0.5-6 px range is all travel and no
        # precision, and it costs a row of width that the useful parameters
        # want. Declared here rather than special-cased in the page, so the
        # presentation stays a property of the parameter.
        line_width: float = Field(
            1.0, ge=0.5, le=6.0, description="Grid line width, pixels",
            json_schema_extra={"widget": "box"},
        )
        intensity: float = Field(
            0.9, ge=0.0, le=1.0, description="Overlay opacity",
            json_schema_extra={"widget": "box"},
        )
        show_centre: bool = Field(True, description="Crosshair on the grid origin")
        highlight_views: bool = Field(
            True, description="Outline the sub-apertures shown below the preview"
        )

    def __init__(self, name: str | None = None, **params) -> None:
        super().__init__(name, **params)
        # Building the mask costs ~20 ms at preview resolution -- more than the
        # rest of the pipeline combined -- and it changes only when a parameter
        # or the frame size changes. Cache it.
        self._key: tuple | None = None
        self._mask: tuple[np.ndarray, np.ndarray] | None = None

    def reset(self) -> None:
        self._key = None
        self._mask = None

    def geometry(self, width: int, height: int) -> MLAGeometry:
        p = self.params
        return MLAGeometry(
            width=width,
            height=height,
            pitch=float(p.pitch_px),
            rotation_deg=float(p.rotation_deg),
            offset_x=float(p.offset_x),
            offset_y=float(p.offset_y),
        )

    def _masks(self, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
        """(grid mask, highlight mask), cached on the parameters and frame size.

        Building these costs ~20 ms at preview resolution -- more than the rest
        of the pipeline combined -- and they change only when a parameter does.
        """
        p = self.params
        key = (
            h, w, p.pitch_px, p.rotation_deg, p.offset_x, p.offset_y,
            p.line_width, p.show_centre, p.crop_scale, p.highlight_views,
        )
        if key == self._key and self._mask is not None:
            return self._mask
        geom = self.geometry(w, h)
        grid = geom.grid_mask((h, w), line_width=float(p.line_width))
        if p.show_centre:
            grid = grid | geom.centre_marker_mask((h, w))

        if p.highlight_views:
            named = geom.named_indices(float(p.crop_scale))
            picked = [named[n] for n in UI_SUBAPERTURES if n in named]
            hi = geom.highlight_mask((h, w), picked, float(p.crop_scale), thickness=2)
        else:
            hi = np.zeros((h, w), dtype=bool)

        self._key, self._mask = key, (grid, hi)
        return grid, hi

    def apply(self, frame: Frame) -> Frame:
        d = frame.data
        h, w = d.shape[:2]
        grid, hi = self._masks(h, w)
        out = d.copy()

        peak = 65535 if d.dtype == np.uint16 else 255
        alpha = float(self.params.intensity)
        # Grid lines are blended at the chosen opacity; the highlight boxes are
        # drawn at full intensity over the top, so which lenslets are being
        # zoomed is unambiguous even against a bright scene.
        out[grid] = (out[grid] * (1 - alpha) + peak * alpha).astype(out.dtype)
        out[hi] = peak

        geom = self.geometry(w, h)
        named = geom.named_indices(float(self.params.crop_scale))
        return frame.derive(
            out,
            mla_pitch_px=float(self.params.pitch_px),
            mla_rotation_deg=float(self.params.rotation_deg),
            mla_origin_px=list(geom.origin),
            mla_index_extent=list(geom.index_extent(float(self.params.crop_scale))),
            mla_views={k: list(v) for k, v in named.items()},
        )


@register("lenslet_extract")
class LensletExtract(Stage):
    """Placeholder: resample a raw plenoptic frame into per-lenslet views.

    Not implemented. Its output is a 4D light field, which does not fit the
    current Frame contract (2D or 3D arrays). The decision to make before
    writing it: widen Frame to carry an arbitrary-rank array plus an axis-label
    tuple, or introduce a LightField type and a second pipeline consuming
    Frames and emitting LightFields. The second is cleaner, the first is less
    work.

    Sub-aperture *previews* do not need this -- they are plain crops, served by
    the web layer straight from trilobite.optics.mla.
    """

    accepts = ("raw",)

    class Params(MLAParams):
        pass

    def apply(self, frame: Frame) -> Frame:
        raise NotImplementedError(
            "lenslet_extract is a placeholder; see the docstring for the design "
            "decision that needs making first."
        )
