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
import math

import numpy as np
from pydantic import Field, model_validator

from ...optics.mla import UI_SUBAPERTURES, MLAGeometry
from ...optics.orientation import Orientation
from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register

log = logging.getLogger(__name__)


class MLAParams(StageParams):
    """Shared by the overlay and by sub-aperture extraction.

    **Every length here is in FULL-RESOLUTION SENSOR PIXELS**, whatever frame
    happens to be on screen. Offsets are from the centre pixel of the sensor
    frame, which is the anchor the whole grid hangs from; positive x is right,
    positive y is down.

    That choice is deliberate and it was made the second way round first. The
    grid is *aligned* against the preview, so the obvious thing is to store the
    numbers in preview pixels and convert on the way out. It works, and it puts
    a conversion between the stored value and every consumer of it -- the
    detector, the crops, the recorded corners, the offline readers -- so that
    forgetting the conversion anywhere is a silent factor-of-two, and changing
    the preview resolution silently invalidates a stored alignment.

    Sensor pixels have no such dependency. The MLA pitch is a physical property
    of the lens array: pitch_um / pixel_pitch_um, fixed by hardware, the same
    number tomorrow whatever the preview is set to. Everything that measures
    works in sensor pixels already, so for them the conversion disappears
    entirely, and the only place one remains is drawing the overlay -- where
    being wrong is visible immediately rather than six months later in a fit.

    The cost is that one preview pixel of nudge is two sensor pixels of grid.
    The boxes take fractional values, so the resolution is still there; it is
    the sliders that are coarser, on a rig where the pitch is ~100 sensor px.
    """

    enabled: bool = False
    # Ranges are chosen so the slider has usable resolution and so that every
    # value inside them produces a grid that can actually be drawn and cropped
    # -- not so that they are theoretically permissive.
    #
    # The floor on pitch is a tractability bound, not an optical one. Lattice
    # enumeration is quadratic in 1/pitch with trigonometry per candidate, so a
    # pitch small enough to be a typo is not slow, it is a hang: 1.5 px on this
    # sensor is 1.4 million positions to test, inside a request handler. At
    # 10 px it is 33,000, which the cache in optics/mla.py pays for once. Below
    # ~10 px a micro-image also cannot hold a resolvable checkerboard, so
    # nothing is being forbidden that the rig could have used.
    pitch_px: float = Field(
        20.0, ge=10.0, le=800.0, description="Lenslet pitch, SENSOR pixels")
    rotation_deg: float = Field(0.0, ge=-45.0, le=45.0, description="Grid rotation, degrees")
    # Bounded by the pitch, and folded into it -- see the validator below. The
    # static bound is what the largest allowed pitch can produce: folding puts
    # the offset inside half a cell, so |offset| <= pitch/sqrt(2) = 566 px at
    # pitch 800. The UI narrows the slider to the CURRENT pitch, which is the
    # bound that means anything; this one only has to be wide enough not to
    # reject a value the fold would have accepted.
    offset_x: float = Field(
        0.0, ge=-600.0, le=600.0, description="Grid origin x from centre, SENSOR px")
    offset_y: float = Field(
        0.0, ge=-600.0, le=600.0, description="Grid origin y from centre, SENSOR px")
    # Above 2 a "sub-aperture" spans four or more lenslets and is not a
    # sub-aperture. Below ~0.3 it is a handful of pixels. The useful range is
    # a little under 1 (trimming vignetted edges) to a little over (seeing the
    # boundary while aligning); the readiness check adds the rotation-dependent
    # ceiling, which is tighter still.
    crop_scale: float = Field(
        1.0, ge=0.25, le=2.0, description="Sub-aperture crop side, as a multiple of pitch"
    )
    derotate_views: bool = Field(
        False,
        description=(
            "Resample sub-aperture tiles into the lattice axes. Off for this rig: "
            "the MLA is rotated relative to the sensor, so tile content is not "
            "rotated and resampling would only blur it. See the module docstring."
        ),
    )
    # The frame these numbers are expressed in: the SENSOR frame, bound once
    # from the camera at start-up rather than learned from whatever frame
    # happens to arrive. It stays recorded because "pitch = 100 px" is
    # meaningless on its own, and because a stored alignment made against a
    # different sensor mode has to be detectable. Hidden in the UI -- it is a
    # unit, not a knob.
    reference_width: int = Field(
        0, ge=0, description="Sensor width these parameters are in (0 = not bound yet)",
        json_schema_extra={"widget": "hidden"},
    )
    reference_height: int = Field(
        0, ge=0, description="Sensor height these parameters are in (0 = not bound yet)",
        json_schema_extra={"widget": "hidden"},
    )
    # ...and the ORIENTATION they were aligned under, for the same reason and
    # recorded the same way. Without these three, a 1456x1088 reference meeting
    # a 1088x1456 frame is indistinguishable from a sensor-mode change, and an
    # alignment made against a mirrored image is indistinguishable from one
    # made against the plain image -- so a change of orientation could not even
    # be DETECTED, let alone acted on. With them, `bind_sensor` resets the
    # alignment it can no longer vouch for and says so.
    reference_rotate_deg: int = Field(
        0, ge=0, le=270, description="Frame rotation these parameters were aligned under",
        json_schema_extra={"widget": "hidden"},
    )
    reference_flip_horizontal: bool = Field(
        False, description="Horizontal mirror these parameters were aligned under",
        json_schema_extra={"widget": "hidden"},
    )
    reference_flip_vertical: bool = Field(
        False, description="Vertical mirror these parameters were aligned under",
        json_schema_extra={"widget": "hidden"},
    )

    @model_validator(mode="after")
    def _fold_offset_into_one_cell(self) -> MLAParams:
        """Reduce the origin offset modulo the lattice.

        The offset says which physical lenslet is index (0, 0). Moving it by a
        whole lattice vector gives the SAME grid with the indices renumbered,
        so offsets differing by a multiple of the pitch are not two settings --
        they are one setting written two ways. Every distinct alignment is
        reachable within half a pitch of centre.

        Folding rather than clamping matters, and the difference shows the
        moment you drag the slider. A clamp at +pitch/2 stops the grid moving
        while the number keeps changing, so the control appears dead at one
        end. Folding sends the number to -pitch/2 while the grid slides on
        exactly as before -- which is what the geometry actually does.

        The reduction is in the LATTICE basis, not per-axis: u and v are
        rotated with the grid, so subtracting `round(offset . u_hat / pitch)`
        lots of u is exact at any rotation, where folding x and y separately
        modulo the pitch is only correct at zero rotation.

        Written through `__dict__` because this runs under
        `validate_assignment=True`, and assigning to the field here would
        re-enter validation on this same model.
        """
        pitch = float(self.pitch_px)
        if pitch <= 0:
            return self
        t = math.radians(float(self.rotation_deg))
        ct, st = math.cos(t), math.sin(t)
        ox, oy = float(self.offset_x), float(self.offset_y)
        # Components along the lattice axes, in units of the pitch.
        a = (ox * ct + oy * st) / pitch
        b = (-ox * st + oy * ct) / pitch
        na, nb = round(a), round(b)
        if na == 0 and nb == 0:
            return self
        a -= na
        b -= nb
        self.__dict__["offset_x"] = round(pitch * (a * ct - b * st), 9)
        self.__dict__["offset_y"] = round(pitch * (a * st + b * ct), 9)
        return self


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

        Correct only when (width, height) is the reference resolution -- which
        is now the SENSOR frame. Anything holding a different-sized frame, the
        preview included, wants geometry_for().
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

    @property
    def reference_orientation(self) -> Orientation:
        """The orientation the parameters were aligned under."""
        return Orientation.reference_of(self.params)

    def _stamp(self, width: int, height: int, orientation: Orientation) -> None:
        """Record the frame these parameters are now expressed in."""
        self.params.reference_width = int(width)
        self.params.reference_height = int(height)
        self.params.reference_rotate_deg = int(orientation.rotate_deg)
        self.params.reference_flip_horizontal = bool(orientation.flip_horizontal)
        self.params.reference_flip_vertical = bool(orientation.flip_vertical)

    def bind_sensor(
        self, width: int, height: int, orientation: Orientation | None = None
    ) -> None:
        """Declare the sensor frame these parameters are expressed in.

        Called ONCE, at camera start, with the camera's full resolution **after
        orientation** -- never from a frame flowing through the pipeline. That
        is the whole difference between this and what it replaced: the old
        version took its reference from whatever frame it first saw, which was
        always the preview, so the stored pitch silently meant preview pixels
        and every consumer needed a conversion it could forget.

        Four cases, in the order they are tried:

          * not bound yet -- adopt the sensor size and orientation. First run,
            or a config written before the parameters were sensor-native.
          * already bound to this frame and orientation -- the common case,
            nothing to do.
          * bound under a different ORIENTATION -- a quarter turn or a mirror.
            **The alignment is discarded**: offsets and lattice rotation go to
            zero. `pitch_px` survives, because it is `pitch_um /
            pixel_pitch_um`, a property of the hardware and not of the
            alignment, and it is the tedious number to re-enter. The reference
            frame transposes if the turn was odd, so the pitch rescale below
            stays isotropic. See optics/orientation.py for why this resets
            rather than transforms.
          * bound to a different SIZE -- an older alignment stored in preview
            pixels, or a genuine sensor-mode change. **Rescale** so the grid
            stays on the same physical lenslets, and say so loudly. This is the
            migration path: `pitch_px: 50, reference_width: 728` becomes
            `pitch_px: 100, reference_width: 1456` the first time it meets the
            camera, once.

        Orientation is handled before size deliberately. Taken the other way
        round, a turned frame reaches `rescaled` as a 1456x1088 reference
        against a 1088x1456 frame, which is anisotropic, which raises -- and
        the handler for that stamps the new size while leaving pitch verbatim,
        so a rig that was turned *and* migrated would keep a preview-sized
        pitch. Transposing the reference first makes the rescale a clean
        factor of two again.
        """
        now = orientation or self.reference_orientation
        was = self.reference_orientation
        ref = self.reference_shape

        if ref is None:
            self._stamp(width, height, now)
            return

        if was != now:
            if was.swaps_axes_relative_to(now):
                ref = (ref[1], ref[0])
            had = (float(self.params.offset_x), float(self.params.offset_y),
                   float(self.params.rotation_deg))
            log.warning(
                "%s: the frame orientation changed (%s -> %s), so the grid "
                "alignment has been RESET: offsets (%.1f, %.1f) and lattice "
                "rotation %.2f deg discarded, reference frame now %dx%d. Pitch "
                "stays at %.2f px -- that is the lens array and the pixel size, "
                "not the alignment. Re-align the grid against the new frame.",
                self.name, was.describe(), now.describe(),
                had[0], had[1], had[2], ref[0], ref[1], float(self.params.pitch_px),
            )
            self.params.offset_x = 0.0
            self.params.offset_y = 0.0
            self.params.rotation_deg = 0.0
            self._stamp(*ref, now)
            self.reset()

        if ref == (width, height):
            return

        try:
            g = self.geometry(*ref).rescaled(width, height)
        except ValueError as exc:
            log.warning("%s: cannot rebase the grid onto the sensor frame: %s", self.name, exc)
            self._stamp(width, height, now)
            return
        log.warning(
            "%s: rebasing the grid from a %dx%d reference onto the %dx%d sensor "
            "frame (pitch %.3f -> %.3f px). Grid parameters are sensor pixels now; "
            "this happens once.",
            self.name, ref[0], ref[1], width, height, self.params.pitch_px, g.pitch,
        )
        self.params.pitch_px = g.pitch
        self.params.offset_x = g.offset_x
        self.params.offset_y = g.offset_y
        self._stamp(width, height, now)
        self.reset()

    def geometry_for(self, width: int, height: int) -> MLAGeometry:
        """Geometry converted to whatever frame size the caller is holding.

        The entry point for **everything**. For the detector and the crops,
        which hold the full sensor frame, it is now an identity -- that is the
        point of storing sensor pixels. For the overlay and the preview-sized
        views it scales down. See MLAGeometry.rescaled for why the conversion
        is exact either way.
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
        # geometry_for, not geometry: the parameters are in sensor pixels and
        # this is drawing on the preview, so the grid has to be scaled down to
        # it. Using the parameters verbatim here would draw a grid at half the
        # pitch it means, which is the one place this mistake is instantly
        # visible -- and the reason the overlay is a safe place to keep the
        # only remaining conversion.
        geom = self.geometry_for(w, h)
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
        # No reference is learned here. The parameters are sensor pixels, bound
        # once from the camera by Application.start(); a preview frame arriving
        # is not evidence of anything.
        grid, hi = self._masks(h, w)
        out = d.copy()

        peak = 65535 if d.dtype == np.uint16 else 255
        alpha = float(self.params.intensity)
        # Grid lines are blended at the chosen opacity; the highlight boxes are
        # drawn at full intensity over the top, so which lenslets are being
        # zoomed is unambiguous even against a bright scene.
        out[grid] = (out[grid] * (1 - alpha) + peak * alpha).astype(out.dtype)
        out[hi] = peak

        geom = self.geometry_for(w, h)
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
