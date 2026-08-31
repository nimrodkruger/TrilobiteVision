"""Plenoptic-specific stages.

Stubs with the right shape, not implementations. They are here so the seam
exists from day one and so the UI already has somewhere to put the controls.
Fill them in once the microlens array is physically mounted and you know what
the raw frames actually look like -- guessing the geometry before then is
wasted effort.

The calibration parameters that belong here (MLA pitch, rotation, centre
offset, per-lenslet centres) are exactly the kind of thing that should live in
a stage's Params model rather than a global: they are per-camera, they need
validating, they need saving alongside every captured frame, and they need a
UI. All four come free from the Stage contract.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field

from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register


@register("mla_grid_overlay")
class MLAGridOverlay(Stage):
    """Draw the estimated microlens grid over the preview.

    Purely a visual aid for physically aligning the MLA: you nudge the array
    until the drawn grid sits on the lenslet centres. Preview path only --
    never let this touch a frame that will be saved.
    """

    accepts = ("mono8", "rgb8")

    class Params(StageParams):
        enabled: bool = False
        pitch_px: float = Field(20.0, gt=1.0, le=500.0, description="Lenslet pitch, pixels")
        rotation_deg: float = Field(0.0, ge=-45.0, le=45.0, description="Grid rotation")
        offset_x: float = Field(0.0, ge=-500.0, le=500.0, description="Grid origin x, pixels")
        offset_y: float = Field(0.0, ge=-500.0, le=500.0, description="Grid origin y, pixels")
        intensity: float = Field(1.0, ge=0.0, le=1.0, description="Overlay opacity")

    def __init__(self, name: str | None = None, **params) -> None:
        super().__init__(name, **params)
        # The grid only changes when a parameter or the frame size changes, but
        # building it costs ~20 ms at preview resolution -- more than the rest
        # of the pipeline combined. Caching it is the difference between this
        # stage being free and it halving the preview rate. Any stage with
        # frame-independent intermediate state should do the same.
        self._cache_key: tuple | None = None
        self._mask: np.ndarray | None = None

    def reset(self) -> None:
        self._cache_key = None
        self._mask = None

    def _grid_mask(self, h: int, w: int) -> np.ndarray:
        p = self.params
        key = (h, w, p.pitch_px, p.rotation_deg, p.offset_x, p.offset_y)  # type: ignore[attr-defined]
        if key == self._cache_key and self._mask is not None:
            return self._mask

        theta = np.deg2rad(p.rotation_deg)  # type: ignore[attr-defined]
        ct, st = np.cos(theta), np.sin(theta)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        xr = (xx - p.offset_x) * ct + (yy - p.offset_y) * st  # type: ignore[attr-defined]
        yr = -(xx - p.offset_x) * st + (yy - p.offset_y) * ct  # type: ignore[attr-defined]

        pitch = float(p.pitch_px)  # type: ignore[attr-defined]
        mask = (np.abs((xr % pitch) - pitch / 2) < 0.6) | (np.abs((yr % pitch) - pitch / 2) < 0.6)
        self._cache_key, self._mask = key, mask
        return mask

    def apply(self, frame: Frame) -> Frame:
        p = self.params
        d = frame.data
        h, w = d.shape[:2]
        out = d.copy()
        mask = self._grid_mask(h, w)

        peak = 65535 if d.dtype == np.uint16 else 255
        alpha = float(p.intensity)  # type: ignore[attr-defined]
        if out.ndim == 2:
            out[mask] = (out[mask] * (1 - alpha) + peak * alpha).astype(out.dtype)
        else:
            out[mask] = (out[mask] * (1 - alpha) + peak * alpha).astype(out.dtype)
        return frame.derive(out, mla_overlay=True)


@register("lenslet_extract")
class LensletExtract(Stage):
    """Placeholder: resample a raw plenoptic frame into per-lenslet views.

    Not implemented. The output of this stage is a 4D light field, which does
    not fit the current Frame contract (2D or 3D arrays). When you get here,
    the decision to make is whether to widen Frame to carry an arbitrary-rank
    array plus an axis-label tuple, or to introduce a separate LightField type
    and a second pipeline that consumes Frames and emits LightFields. The
    second is probably cleaner but the first is less work.
    """

    accepts = ("raw",)

    class Params(StageParams):
        enabled: bool = False
        pitch_px: float = Field(20.0, gt=1.0, le=500.0)
        rotation_deg: float = Field(0.0, ge=-45.0, le=45.0)
        offset_x: float = Field(0.0, ge=-500.0, le=500.0)
        offset_y: float = Field(0.0, ge=-500.0, le=500.0)

    def apply(self, frame: Frame) -> Frame:
        raise NotImplementedError(
            "lenslet_extract is a placeholder. See the docstring for the design "
            "decision that needs making before implementing it."
        )
