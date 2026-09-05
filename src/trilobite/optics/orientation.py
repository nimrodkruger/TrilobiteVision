"""The orientation a frame was acquired in, and what changing it costs.

Rotation and mirroring are applied once, at acquisition, and are treated as a
**setup decision**: you bolt the camera down, decide which way round its output
should be, and then leave it alone for the rest of the calibration. Everything
after that point -- the grid alignment, the poses, the fit -- assumes a fixed
frame.

That is a deliberate restriction rather than a limitation of the code. An
earlier version carried an MLA alignment across a change of orientation by
transforming its offsets as an element of the dihedral group. The arithmetic
was correct and the feature was wrong: it invited the operator to change the
frame mid-calibration, and no amount of correct arithmetic makes the *rest* of
a calibration survive that. A grid alignment is a measurement of where the
lenslets fall on the sensor, made by eye against a particular image; re-deriving
it from a transform and calling it aligned asserts something nobody checked.

So the rule is simpler and it is enforced rather than documented:

  * while the MLA grid is enabled, the orientation is locked;
  * changing the orientation with the grid off **resets** the alignment --
    offsets and lattice rotation to zero -- because it was made against a frame
    that no longer exists;
  * `pitch_px` survives, because it is a property of the lens array and the
    pixel size, not of the alignment: `pitch_um / pixel_pitch_um`, the same
    number whichever way the sensor is turned;
  * the frame SIZE carries forward to everything downstream, which is what a
    quarter turn actually changes.

This module is therefore small. It exists to say what an orientation is, so
that "has it changed?" is one comparison in one place rather than three
attribute reads at each call site.

Conventions, stated once:

  * `rotate_deg` is CLOCKWISE as the image is displayed, in quarter turns,
    matching CameraConfig.rotate_deg.
  * Rotation is applied before the mirrors, matching CameraSource._orient, so
    the mirrors always mean "flip what I am looking at".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Orientation:
    """One of the eight ways a frame can be presented, as applied at acquisition."""

    rotate_deg: int = 0
    flip_horizontal: bool = False
    flip_vertical: bool = False

    @classmethod
    def of(cls, obj: object) -> Orientation:
        """Read an orientation off anything carrying the three attributes.

        A CameraConfig, or a plain namespace. Callers do not unpack it field by
        field, so they cannot unpack it inconsistently.
        """
        return cls(
            rotate_deg=int(getattr(obj, "rotate_deg", 0) or 0) % 360,
            flip_horizontal=bool(getattr(obj, "flip_horizontal", False)),
            flip_vertical=bool(getattr(obj, "flip_vertical", False)),
        )

    @classmethod
    def reference_of(cls, params: object) -> Orientation:
        """The orientation an MLA alignment was measured under.

        Alignments written before this was recorded read as 0 with no mirrors,
        which is what they were.
        """
        return cls(
            rotate_deg=int(getattr(params, "reference_rotate_deg", 0) or 0) % 360,
            flip_horizontal=bool(getattr(params, "reference_flip_horizontal", False)),
            flip_vertical=bool(getattr(params, "reference_flip_vertical", False)),
        )

    @property
    def quarter_turns(self) -> int:
        return (self.rotate_deg // 90) % 4

    @property
    def portrait(self) -> bool:
        """True when this orientation swaps the sensor's width and height."""
        return self.quarter_turns % 2 == 1

    def swaps_axes_relative_to(self, other: Orientation) -> bool:
        """Does moving between these two orientations transpose the frame?

        The one geometric fact still needed downstream: a reference frame
        recorded as 1456x1088 has to become 1088x1456 when the camera is turned
        an odd number of quarter turns, so that the pitch rescale onto the new
        sensor size stays isotropic instead of raising.
        """
        return self.portrait != other.portrait

    def describe(self) -> str:
        """A phrase for a log line or a UI note. Empty when nothing is applied."""
        bits = []
        if self.rotate_deg:
            bits.append(f"rotated {self.rotate_deg}° CW")
        if self.flip_horizontal:
            bits.append("flipped horizontally")
        if self.flip_vertical:
            bits.append("flipped vertically")
        return ", ".join(bits) or "unrotated, unmirrored"
