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

import logging

import numpy as np
from pydantic import Field

from ...optics.mla import UI_SUBAPERTURES, MLAGeometry
from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register

log = logging.getLogger(__name__)


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
    # The frame size these numbers were measured against. Learned from the
    # first frame and thereafter carried in the saved state, because "pitch =
    # 100 px" is meaningless on its own: the same physical grid is 100 px on the
    # 728-wide preview you aligned it against and 200 px on the 1456-wide sensor
    # frame that calibration crops from. Hidden in the UI -- it is a unit, not a
    # knob.
    reference_width: int = Field(
        0, ge=0, description="Frame width these parameters were set against (0 = learn it)",
        json_schema_extra={"widget": "hidden"},
    )
    reference_height: int = Field(
        0, ge=0, description="Frame height these parameters were set against (0 = learn it)",
        json_schema_extra={"widget": "hidden"},
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
        """Geometry with the parameters taken **verbatim**, at this frame size.

        Correct only when (width, height) is the reference resolution -- the
        preview. Anything reading a different-sized frame wants geometry_for().
        """
        p = self.params
        return MLAGeometry(
            width=width,
            height=height,
            pitch=float(p.pitch_px),
            rotation_deg=float(p.rotation_deg),
            offset_x=float(p.offset_x),
            offset_y=float(p.offset_y),
        )

    # -- resolution ------------------------------------------------------

    @property
    def reference_shape(self) -> tuple[int, int] | None:
        """(width, height) the parameters are expressed in, if known yet."""
        w = int(self.params.reference_width)
        h = int(self.params.reference_height)
        return (w, h) if w > 0 and h > 0 else None

    def note_frame_size(self, width: int, height: int) -> None:
        """Record, or follow, the resolution the parameters are measured in.

        Called once per preview frame. Three cases:

          * no reference yet -- adopt this frame's size. First run, or a state
            file written before this field existed.
          * reference matches -- nothing to do, the common case.
          * reference differs -- the preview resolution was changed in the
            config since the grid was aligned. **Rescale the parameters** so
            the grid stays on the same physical lenslets, and say so. The
            alternative, keeping the numbers and quietly meaning something
            else, would look like the alignment spontaneously drifting.
        """
        ref = self.reference_shape
        if ref == (width, height):
            return
        if ref is None:
            self.params.reference_width = int(width)
            self.params.reference_height = int(height)
            return
        try:
            g = self.geometry(*ref).rescaled(width, height)
        except ValueError as exc:
            log.warning("%s: cannot follow a resolution change: %s", self.name, exc)
            self.params.reference_width = int(width)
            self.params.reference_height = int(height)
            return
        log.warning(
            "%s: preview resolution changed %dx%d -> %dx%d; rescaling the grid "
            "(pitch %.3f -> %.3f px) so it stays on the same lenslets",
            self.name, ref[0], ref[1], width, height, self.params.pitch_px, g.pitch,
        )
        self.params.pitch_px = g.pitch
        self.params.offset_x = g.offset_x
        self.params.offset_y = g.offset_y
        self.params.reference_width = int(width)
        self.params.reference_height = int(height)
        self.reset()

    def geometry_for(self, width: int, height: int) -> MLAGeometry:
        """Geometry converted to whatever frame size the caller is holding.

        This is the entry point for anything that is not the preview -- above
        all the calibration detector, which crops from the full-resolution
        sensor frame while these parameters were tuned on the half-scale
        preview. See MLAGeometry.rescaled for why the conversion is exact.
        """
        ref = self.reference_shape or (width, height)
        if ref == (width, height):
            return self.geometry(width, height)
        return self.geometry(*ref).rescaled(width, height)

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
        # The preview is the frame these parameters are measured against, so
        # this is where the reference resolution is established.
        self.note_frame_size(w, h)
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
            mla_reference_shape=list(self.reference_shape or (w, h)),
        )


@register("checkerboard_presence")
class CheckerboardPresence(Stage):
    """Count checkerboard corners per micro-image, on every preview frame.

    Not a corner detector: it counts saddle points of intensity, which is what
    a checkerboard corner is, without identifying or localising any of them.
    See trilobite.calibration.presence for the reasoning and the measurements.

    It lives in the pipeline rather than in a worker thread of its own, and
    that is the whole point. The frames it works on are the ones the preview
    already produces, so it opens no second consumer of the camera -- which was
    what took the rig down when full-field corner detection had its own thread
    reaching into picamera2 on its own schedule. Its cost, about 3 ms at
    preview resolution, appears in this stage's timing in the UI like any
    other stage's, where it can be seen rather than assumed.

    The MLA stage must come before it in the pipeline: this reads the same
    geometry, so the tiles it counts into are the tiles the crops come from.
    """

    accepts = ("mono8", "mono16")

    class Params(StageParams):
        enabled: bool = False
        min_corners: int = Field(
            20, ge=1, le=400, title="Corners for 'seeing the board'",
            description="A micro-image counts as seeing the pattern at this many "
                        "saddle peaks. A tile showing the whole pattern reads "
                        "about (cols+2)x(rows+2) -- every grid vertex, not just "
                        "the inner corners -- so 4x3 reads ~30 and two thirds of "
                        "that is a good threshold. The Preconditions panel says "
                        "what your board implies.",
            json_schema_extra={"widget": "box"})
        peak_window: int = Field(
            5, ge=3, le=21, title="Peak separation, px",
            description="Local-maximum window. Smaller than one square, so two "
                        "corners of the same square are never merged.",
            json_schema_extra={"widget": "box"})
        rel_threshold: float = Field(
            0.15, gt=0.0, le=1.0, title="Relative peak threshold",
            description="Fraction of the frame's strongest saddle response a peak "
                        "must reach. Lower finds fainter patterns and more noise.",
            json_schema_extra={"widget": "box"})
        min_contrast: float = Field(
            40.0, ge=2.0, le=200.0, title="Minimum corner contrast, grey levels",
            description="A peak is ignored unless the corner under it is worth this "
                        "many grey levels. Without an absolute floor the relative "
                        "threshold normalises whatever is in front of the lens up to "
                        "'detected' -- sensor noise alone puts thousands of peaks on "
                        "a blank wall. A printed board gives 130+, so 40 rejects "
                        "noise with a wide margin. Lower it only after checking "
                        "focus and light.",
            json_schema_extra={"widget": "box"})
        tint: bool = Field(
            True, title="Tint the preview",
            description="Brighten micro-images that are seeing the pattern, so "
                        "aiming the board needs no second display.")

    def __init__(self, name: str | None = None, **params) -> None:
        super().__init__(name, **params)
        self._detector = None
        self._geometry_source = None
        # The latest map, for the calibration layer to read. One frame deep:
        # nothing here accumulates, and nothing here writes to disk.
        self.result = None
        self._tint_key: tuple | None = None
        self._tint_labels: np.ndarray | None = None

    def bind_geometry(self, mla_stage) -> None:
        """Point this stage at the MLA stage whose grid it should count into."""
        self._geometry_source = mla_stage

    def reset(self) -> None:
        self.result = None
        self._tint_key = None
        self._tint_labels = None

    def apply(self, frame: Frame) -> Frame:
        if self._geometry_source is None:
            # Nothing to count into. Not an error -- the pipeline may simply
            # not have an MLA stage yet -- but say so once rather than silently
            # producing an empty map for ever.
            if self.result is None:
                log.warning("%s: no MLA stage bound; presence map disabled", self.name)
            return frame

        from ...calibration.presence import PresenceDetector  # noqa: PLC0415 - needs cv2

        p = self.params
        if self._detector is None:
            self._detector = PresenceDetector(p.peak_window, p.rel_threshold, p.min_contrast)
        self._detector.peak_window = int(p.peak_window)
        self._detector.rel_threshold = float(p.rel_threshold)
        self._detector.min_contrast = float(p.min_contrast)

        h, w = frame.data.shape[:2]
        geom = self._geometry_source.geometry_for(w, h)
        scale = float(self._geometry_source.params.crop_scale)
        self.result = self._detector.run(frame.data, geom, scale)
        seeing = len(self.result.seeing(int(p.min_corners)))

        out = frame.data
        if p.tint:
            out = self._tinted(frame.data, geom, scale, int(p.min_corners))

        return frame.derive(
            out,
            presence_tiles_seeing=seeing,
            presence_strength=round(self.result.strength, 1),
            presence_ms=round(self.result.ms, 2),
        )

    def _tinted(self, data, geom, scale: float, min_corners: int):
        """Brighten the micro-images that are seeing the pattern.

        Aiming a board while watching a numeric readout is not workable with
        both hands full, so the feedback goes on the image itself.
        """
        det = self._detector
        labels, (i0, j0, cols, rows) = det.labels_for(geom, data.shape[:2], scale)
        good = ((self.result.counts >= min_corners) & self.result.whole).ravel()
        if not good.any():
            return data
        lut = np.zeros(cols * rows + 1, dtype=bool)
        lut[:-1] = good
        mask = lut[np.where(labels < 0, cols * rows, labels)]
        out = data.copy()
        peak = 65535 if out.dtype == np.uint16 else 255
        out[mask] = (out[mask] * 0.75 + peak * 0.25).astype(out.dtype)
        return out


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
