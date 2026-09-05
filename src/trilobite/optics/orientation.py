"""How a change of frame orientation moves an MLA alignment.

Rotating or mirroring the camera at acquisition does not move the lenslets; it
relabels the pixels. So an alignment measured under one orientation is still a
valid alignment under another -- but only after its numbers are carried across,
and doing that by hand is where the sign errors live.

The eight orientations of a rectangle form the dihedral group D4, and every one
of them is a 2x2 matrix with entries in {-1, 0, 1} acting on **centred** image
coordinates (x right, y DOWN, origin at ((W-1)/2, (H-1)/2)). Composing and
inverting orientations is then matrix arithmetic rather than case analysis, and
the three things an MLA alignment needs follow from the same matrix:

  * **offsets** transform as the vector they are: (ox, oy) -> M (ox, oy).
  * **the reference frame** swaps width and height exactly when M is
    off-diagonal.
  * **the lattice rotation** negates under a reflection (det M = -1) and is
    otherwise unchanged. It does *not* pick up the +/-90 the matrix carries,
    because the lattice is square: rotating a square lattice by a quarter turn
    gives the same lattice, so `rotation_deg` is only ever meaningful modulo 90
    and is wrapped into (-45, 45] to stay inside the parameter's range.
  * **pitch** is invariant. Every element of D4 is an isometry.

The one thing this cannot do is guess. It needs the orientation the alignment
was made under, which is why MLAParams records `reference_rotate_deg` and the
two reference flips alongside `reference_width`. Without them a 1456x1088
reference meeting a 1088x1456 frame is indistinguishable from a sensor-mode
change, and the old code took the only option left to it: keep the numbers,
stamp the new size, and produce a grid that is confidently wrong.

Sign conventions, stated once because they are the whole content:

  * `rotate_deg` is CLOCKWISE as the image is displayed, matching
    CameraConfig.rotate_deg.
  * y increases downward, so a clockwise quarter turn sends (dx, dy) to
    (-dy, dx). Check it on a corner: the top-left (dx<0, dy<0) must land at the
    top-right (dx>0, dy<0), and (-dy, dx) = (+, -). It does.
  * orientation is applied as rotate first, then mirror -- the order
    CameraSource._orient uses -- so M = F R, not R F.
"""

from __future__ import annotations

from dataclasses import dataclass

Matrix = tuple[tuple[int, int], tuple[int, int]]


def _rotation(rotate_deg: int) -> Matrix:
    """Clockwise quarter turn as a matrix on centred (x, y), y down."""
    k = (int(rotate_deg) // 90) % 4
    return (
        ((1, 0), (0, 1)),      # 0
        ((0, -1), (1, 0)),     # 90 CW:  (dx, dy) -> (-dy, dx)
        ((-1, 0), (0, -1)),    # 180
        ((0, 1), (-1, 0)),     # 270 CW: (dx, dy) -> (dy, -dx)
    )[k]


def _matmul(a: Matrix, b: Matrix) -> Matrix:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(2)) for j in range(2))
        for i in range(2)
    )  # type: ignore[return-value]


def wrap_lattice_angle(deg: float) -> float:
    """Fold an angle into (-45, 45], the range a square lattice distinguishes.

    A square lattice rotated by 90 degrees is the same lattice, so every angle
    has a representative in a 90-degree window and the parameter is bounded to
    the symmetric one. Without this, carrying an alignment through a quarter
    turn would push `rotation_deg` outside its own validation range.
    """
    x = (float(deg) + 45.0) % 90.0 - 45.0
    # (-45, 45] rather than [-45, 45): the modulo lands exactly -45 on a
    # boundary angle, and -45 and +45 are the same lattice.
    return 45.0 if x == -45.0 else x


@dataclass(frozen=True)
class Orientation:
    """One of the eight ways a frame can be presented, as applied at acquisition."""

    rotate_deg: int = 0
    flip_horizontal: bool = False
    flip_vertical: bool = False

    @classmethod
    def of(cls, obj: object) -> Orientation:
        """Read an orientation off anything carrying the three attributes.

        A CameraConfig, an MLAParams (via its `reference_*` names, see
        `reference_of`), or a plain namespace -- the point is that callers do
        not have to unpack it field by field and cannot unpack it in the wrong
        order.
        """
        return cls(
            rotate_deg=int(getattr(obj, "rotate_deg", 0) or 0),
            flip_horizontal=bool(getattr(obj, "flip_horizontal", False)),
            flip_vertical=bool(getattr(obj, "flip_vertical", False)),
        )

    @classmethod
    def reference_of(cls, params: object) -> Orientation:
        """The orientation an MLA alignment was measured under."""
        return cls(
            rotate_deg=int(getattr(params, "reference_rotate_deg", 0) or 0),
            flip_horizontal=bool(getattr(params, "reference_flip_horizontal", False)),
            flip_vertical=bool(getattr(params, "reference_flip_vertical", False)),
        )

    @property
    def matrix(self) -> Matrix:
        """Sensor-centred coordinates to image-centred coordinates.

        Rotation first, then the mirrors -- the order CameraSource._orient
        applies them in. The mirrors are diagonal, so they commute with each
        other and only their order relative to the rotation matters.
        """
        mirror: Matrix = (
            (-1 if self.flip_horizontal else 1, 0),
            (0, -1 if self.flip_vertical else 1),
        )
        return _matmul(mirror, _rotation(self.rotate_deg))

    def transform_to(self, other: Orientation) -> Matrix:
        """The matrix taking coordinates in this orientation to `other`.

        `other.matrix @ self.matrix^-1`, and every element of D4 is orthogonal
        with integer entries, so the inverse is the transpose.
        """
        m = self.matrix
        inverse: Matrix = ((m[0][0], m[1][0]), (m[0][1], m[1][1]))
        return _matmul(other.matrix, inverse)


@dataclass(frozen=True)
class Rebased:
    """An MLA alignment carried from one orientation to another."""

    offset_x: float
    offset_y: float
    rotation_deg: float
    reference_width: int
    reference_height: int
    swapped: bool
    mirrored: bool

    @property
    def changed(self) -> bool:
        return self.swapped or self.mirrored or bool(
            self.offset_x or self.offset_y
        )


def rebase(
    frm: Orientation,
    to: Orientation,
    *,
    offset_x: float,
    offset_y: float,
    rotation_deg: float,
    reference_width: int,
    reference_height: int,
) -> Rebased:
    """Express an alignment measured under `frm` in the frame of `to`.

    Pitch is not an argument because a quarter turn and a mirror are both
    isometries: the lenslets are the same distance apart however the frame is
    labelled. If pitch ever needs to change, the frame has been *resampled*,
    which is `MLAGeometry.rescaled`'s job and a separate step.
    """
    m = frm.transform_to(to)
    ox = m[0][0] * offset_x + m[0][1] * offset_y
    oy = m[1][0] * offset_x + m[1][1] * offset_y
    swapped = m[0][0] == 0
    det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
    w, h = int(reference_width), int(reference_height)
    return Rebased(
        offset_x=float(ox),
        offset_y=float(oy),
        # Reflection reverses the sense of the lattice tilt; a pure rotation
        # only adds a multiple of 90, which a square lattice does not see.
        rotation_deg=wrap_lattice_angle(det * float(rotation_deg)),
        reference_width=h if swapped else w,
        reference_height=w if swapped else h,
        swapped=swapped,
        mirrored=det < 0,
    )
