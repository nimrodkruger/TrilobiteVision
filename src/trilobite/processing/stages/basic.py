"""Starter stages.

These are deliberately simple. They exist to prove the parameter plumbing
end-to-end and to give the UI something to render. The plenoptic work replaces
and extends them; nothing about the framework changes when it does.

Note the pattern in each: a Params model with Field(...) carrying range and
description metadata, then an apply() that reads self.params. The ranges reach
the browser as slider bounds automatically via the JSON schema.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, model_validator

from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register


@register("passthrough")
class Passthrough(Stage):
    """Does nothing. Useful as a pipeline placeholder and as a timing baseline."""

    def apply(self, frame: Frame) -> Frame:
        return frame


@register("levels")
class Levels(Stage):
    """Linear gain and offset followed by gamma. Display shaping only.

    Never put this in front of a measurement path: it destroys the linear
    relationship between photons and pixel values that calibration relies on.
    It is here for the preview stream.
    """

    accepts = ("mono8", "mono16", "rgb8")

    class Params(StageParams):
        # The floor on gain is 0.05, not 0. A gain of exactly zero multiplies
        # the frame to black, and a black preview is what a dead camera, a
        # closed shutter and a crashed capture thread all look like -- an hour
        # of the wrong diagnosis available from one slider at its left stop.
        # 0.05 is dark and unmistakably alive.
        gain: float = Field(1.0, ge=0.05, le=8.0, description="Multiplicative gain")
        # DN, and the preview can be mono16, so the range is the 16-bit one.
        # +-128 was an 8-bit assumption and made the control useless on a
        # 10-bit frame promoted to 16.
        offset: float = Field(
            0.0, ge=-4096.0, le=4096.0, description="Additive offset, DN")
        gamma: float = Field(1.0, ge=0.1, le=4.0, description="Display gamma")

    def apply(self, frame: Frame) -> Frame:
        p: Levels.Params = self.params  # type: ignore[assignment]
        peak = 65535.0 if frame.data.dtype == np.uint16 else 255.0
        x = frame.data.astype(np.float32)
        x = x * p.gain + p.offset
        if abs(p.gamma - 1.0) > 1e-6:
            np.clip(x, 0.0, peak, out=x)
            x = peak * np.power(x / peak, 1.0 / p.gamma)
        np.clip(x, 0.0, peak, out=x)
        return frame.derive(x.astype(frame.data.dtype), levels_applied=True)


@register("crop")
class Crop(Stage):
    """Region of interest, expressed as fractions so it is resolution-agnostic.

    Fractional coordinates mean the same ROI works on the preview stream and
    the full-resolution science frame without rescaling arithmetic at the call
    site -- which is exactly the kind of off-by-a-factor-of-two bug that
    wastes an afternoon.
    """

    class Params(StageParams):
        x0: float = Field(0.0, ge=0.0, le=1.0)
        y0: float = Field(0.0, ge=0.0, le=1.0)
        x1: float = Field(1.0, ge=0.0, le=1.0)
        y1: float = Field(1.0, ge=0.0, le=1.0)

        @model_validator(mode="after")
        def _non_empty(self) -> Crop.Params:
            """An inverted or empty rectangle is refused, not silently ignored.

            `apply` already declines to crop when the result would be under a
            pixel, because a zero-width array breaks every stage after it. But
            declining leaves the control looking broken: the numbers say one
            thing, the image shows another, and nothing anywhere says why.
            Refusing the value at the point of entry puts the reason in front
            of whoever typed it, which is a 422 in the UI.
            """
            if self.x1 <= self.x0 or self.y1 <= self.y0:
                raise ValueError(
                    f"empty crop: x {self.x0}..{self.x1}, y {self.y0}..{self.y1}. "
                    f"The second corner has to be past the first in both axes."
                )
            return self

    def apply(self, frame: Frame) -> Frame:
        p: Crop.Params = self.params  # type: ignore[assignment]
        h, w = frame.data.shape[:2]
        x0, x1 = sorted((int(p.x0 * w), int(p.x1 * w)))
        y0, y1 = sorted((int(p.y0 * h), int(p.y1 * h)))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return frame
        return frame.derive(
            np.ascontiguousarray(frame.data[y0:y1, x0:x1]),
            crop_px=(x0, y0, x1, y1),
        )


@register("downsample")
class Downsample(Stage):
    """Integer-factor decimation by block averaging.

    Block averaging rather than nearest-neighbour because the preview is also
    used to judge focus, and nearest-neighbour aliases high spatial
    frequencies into something that looks sharper than the optics are.
    """

    class Params(StageParams):
        factor: int = Field(2, ge=1, le=8, description="Decimation factor")

    def apply(self, frame: Frame) -> Frame:
        n = self.params.factor  # type: ignore[attr-defined]
        if n <= 1:
            return frame
        d = frame.data
        h, w = d.shape[:2]
        h2, w2 = (h // n) * n, (w // n) * n
        if h2 == 0 or w2 == 0:
            return frame
        d = d[:h2, :w2]
        if d.ndim == 2:
            out = d.reshape(h2 // n, n, w2 // n, n).mean(axis=(1, 3))
        else:
            out = d.reshape(h2 // n, n, w2 // n, n, d.shape[2]).mean(axis=(1, 3))
        return frame.derive(out.astype(frame.data.dtype), downsample=n)


@register("stats")
class Stats(Stage):
    """Attach per-frame statistics to the metadata. Pixels pass through.

    Cheap saturation and mean tracking is the fastest way to notice that an
    exposure sweep has clipped, without having to look at every frame.
    """

    class Params(StageParams):
        # Bounded above as well as below, so the UI can draw a slider at all:
        # an unbounded number gets a bare box and no sense of scale. 65535 is
        # the widest frame this pipeline carries; the useful settings are just
        # under the peak for the depth in use (250 for 8-bit, ~1000 for 10-bit
        # promoted to 16).
        saturation_level: float = Field(
            250.0, ge=1.0, le=65535.0,
            description="DN at or above which a pixel counts as saturated",
            json_schema_extra={"widget": "box"},
        )

    def apply(self, frame: Frame) -> Frame:
        d = frame.data
        thresh = self.params.saturation_level  # type: ignore[attr-defined]
        return frame.derive(
            d,
            stat_mean=float(d.mean()),
            stat_max=float(d.max()),
            stat_min=float(d.min()),
            stat_saturated_fraction=float((d >= thresh).mean()),
        )
