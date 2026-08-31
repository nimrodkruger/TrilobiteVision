"""Plenoptic-specific stages.

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

from ...optics.mla import MLAGeometry
from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register


class MLAParams(StageParams):
    """Shared by the overlay and by sub-aperture extraction.

    Offsets are in pixels **from the centre pixel of the frame**, which is the
    anchor the whole grid hangs from. Positive x is right, positive y is down.
    """

    enabled: bool = False
    pitch_px: float = Field(20.0, gt=1.0, le=1000.0, description="Lenslet pitch, pixels")
    rotation_deg: float = Field(0.0, ge=-45.0, le=45.0, description="Grid rotation, degrees")
    offset_x: float = Field(0.0, ge=-2000.0, le=2000.0, description="Grid origin x from centre, px")
    offset_y: float = Field(0.0, ge=-2000.0, le=2000.0, description="Grid origin y from centre, px")
    crop_scale: float = Field(
        1.0, gt=0.1, le=4.0, description="Sub-aperture crop side, as a multiple of pitch"
    )


@register("mla_grid_overlay")
class MLAGridOverlay(Stage):
    """Draw the estimated lenslet grid and its anchor over the preview."""

    accepts = ("mono8", "mono16", "rgb8")

    class Params(MLAParams):
        line_width: float = Field(1.0, ge=0.5, le=6.0, description="Grid line width, pixels")
        intensity: float = Field(0.9, ge=0.0, le=1.0, description="Overlay opacity")
        show_centre: bool = Field(True, description="Crosshair on the grid origin")

    def __init__(self, name: str | None = None, **params) -> None:
        super().__init__(name, **params)
        # Building the mask costs ~20 ms at preview resolution -- more than the
        # rest of the pipeline combined -- and it changes only when a parameter
        # or the frame size changes. Cache it.
        self._key: tuple | None = None
        self._mask: np.ndarray | None = None

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

    def _overlay_mask(self, h: int, w: int) -> np.ndarray:
        p = self.params
        key = (
            h, w, p.pitch_px, p.rotation_deg, p.offset_x, p.offset_y, p.line_width, p.show_centre,
        )
        if key == self._key and self._mask is not None:
            return self._mask
        geom = self.geometry(w, h)
        mask = geom.grid_mask((h, w), line_width=float(p.line_width))
        if p.show_centre:
            mask = mask | geom.centre_marker_mask((h, w))
        self._key, self._mask = key, mask
        return mask

    def apply(self, frame: Frame) -> Frame:
        d = frame.data
        h, w = d.shape[:2]
        mask = self._overlay_mask(h, w)
        out = d.copy()

        peak = 65535 if d.dtype == np.uint16 else 255
        alpha = float(self.params.intensity)
        out[mask] = (out[mask] * (1 - alpha) + peak * alpha).astype(out.dtype)

        geom = self.geometry(w, h)
        i_max, j_max = geom.index_extent(float(self.params.crop_scale))
        return frame.derive(
            out,
            mla_pitch_px=float(self.params.pitch_px),
            mla_rotation_deg=float(self.params.rotation_deg),
            mla_origin_px=list(geom.origin),
            mla_index_extent=[i_max, j_max],
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
