"""Microlens array geometry.

The single reference point is the **centre pixel of the frame**. Everything --
the drawn grid, the lenslet indices, the sub-aperture crops -- is defined
relative to it:

    origin  g = centre_pixel + (offset_x, offset_y)
    lenslet (i, j) centre  =  g + i*u + j*v

where u and v are the rotated basis vectors of length `pitch`:

    u = pitch * ( cos t,  sin t)      # increasing i moves right
    v = pitch * (-sin t,  cos t)      # increasing j moves down

Lenslet (0, 0) therefore sits exactly on the origin, and changing `pitch`
expands or contracts the grid *about that point* rather than about the top-left
corner. That is the property that makes alignment tractable by hand: you set
the offset once to put (0,0) on a real lenslet, then adjust pitch and rotation
and watch the outer lenslets converge, without the centre wandering.

Working in the normalised coordinates

    a = ( (x-gx) cos t + (y-gy) sin t ) / pitch
    b = ( -(x-gx) sin t + (y-gy) cos t ) / pitch

makes both operations trivial: lenslet centres are the integer points of
(a, b), and the boundaries between lenslets are the half-integers.

Crops are axis-aligned in image space, not rotated. For the small rotations
this rig will have (a degree or two), the difference is sub-pixel at the crop
edge and not worth the interpolation cost or the resampling it would impose on
data headed for calibration. If rotation ever exceeds a few degrees, rotate the
*sensor*, not the pixels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Named sub-apertures the UI shows. Signs are in image convention: x right,
# y DOWN -- so "top right" is +i, -j.
NAMED_SUBAPERTURES: tuple[str, ...] = (
    "top_left", "centre", "top_right", "bottom_left", "bottom_right",
)


@dataclass(frozen=True)
class MLAGeometry:
    """Grid geometry for one frame size and one set of MLA parameters."""

    width: int
    height: int
    pitch: float
    rotation_deg: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    # -- basis ----------------------------------------------------------

    @property
    def centre_pixel(self) -> tuple[float, float]:
        """Geometric centre of the frame. Uses (n-1)/2 so that for an odd
        dimension it lands exactly on a pixel, and for an even one it lands
        between the two central pixels -- which is the honest answer."""
        return ((self.width - 1) / 2.0, (self.height - 1) / 2.0)

    @property
    def origin(self) -> tuple[float, float]:
        cx, cy = self.centre_pixel
        return (cx + self.offset_x, cy + self.offset_y)

    @property
    def basis(self) -> tuple[tuple[float, float], tuple[float, float]]:
        t = math.radians(self.rotation_deg)
        c, s = math.cos(t), math.sin(t)
        u = (self.pitch * c, self.pitch * s)
        v = (-self.pitch * s, self.pitch * c)
        return u, v

    def centre_of(self, i: int, j: int) -> tuple[float, float]:
        """Pixel coordinates of the centre of lenslet (i, j)."""
        gx, gy = self.origin
        (ux, uy), (vx, vy) = self.basis
        return (gx + i * ux + j * vx, gy + i * uy + j * vy)

    # -- extent ---------------------------------------------------------

    def index_extent(self, scale: float = 1.0) -> tuple[int, int]:
        """Largest (|i|, |j|) for which all four corner lenslets fit the frame.

        The axes must be checked *together*, not separately. Under rotation the
        corner lenslet (i, j) is further from the origin than either (i, 0) or
        (0, j), so a per-axis bound happily returns indices whose corner tile
        hangs off the sensor. That surfaces as a sub-aperture tile that is a
        sliver instead of a square.

        Conservative by construction: it shrinks until every corner fits, so a
        rotated grid loses an outer lenslet rather than returning a clipped
        crop.
        """
        half = self.crop_side(scale) / 2.0
        t = abs(math.radians(self.rotation_deg))
        # Half-extent of an axis-aligned box containing the rotated tile, plus
        # a pixel for the rounding inside crop().
        pad = half * (abs(math.cos(t)) + abs(math.sin(t))) + 1.0

        def axis_bound(step: tuple[int, int]) -> int:
            n = 0
            for k in range(1, 4096):
                if all(
                    self._fits(self.centre_of(sgn * k * step[0], sgn * k * step[1]), pad)
                    for sgn in (-1, 1)
                ):
                    n = k
                else:
                    break
            return n

        i = axis_bound((1, 0))
        j = axis_bound((0, 1))

        def corners_fit(i: int, j: int) -> bool:
            return all(
                self._fits(self.centre_of(si * i, sj * j), pad)
                for si in (-1, 1)
                for sj in (-1, 1)
            )

        while (i > 0 or j > 0) and not corners_fit(i, j):
            if i >= j and i > 0:
                i -= 1
            elif j > 0:
                j -= 1
            else:
                i -= 1
        return i, j

    def _fits(self, centre: tuple[float, float], pad: float) -> bool:
        x, y = centre
        return (
            x - pad >= 0
            and y - pad >= 0
            and x + pad <= self.width
            and y + pad <= self.height
        )

    def named_indices(self, scale: float = 1.0) -> dict[str, tuple[int, int]]:
        """Map the UI's sub-aperture names onto concrete lenslet indices.

        The corners use the *outermost fully-contained* lenslet, because those
        are the ones whose alignment error is largest and therefore the ones
        worth looking at while tuning pitch and rotation. A centre lenslet that
        looks right tells you almost nothing; the corners tell you everything.
        """
        i, j = self.index_extent(scale)
        return {
            "centre": (0, 0),
            "top_right": (i, -j),
            "bottom_left": (-i, j),
            "top_left": (-i, -j),
            "bottom_right": (i, j),
        }

    # -- rendering and extraction ----------------------------------------

    def normalised(self, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
        """(a, b) arrays over a frame of the given (h, w). Integers are centres."""
        h, w = shape
        gx, gy = self.origin
        t = math.radians(self.rotation_deg)
        c, s = math.cos(t), math.sin(t)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        dx, dy = xx - gx, yy - gy
        return ((dx * c + dy * s) / self.pitch, (-dx * s + dy * c) / self.pitch)

    def grid_mask(self, shape: tuple[int, int], line_width: float = 1.0) -> np.ndarray:
        """Boolean mask of the lenslet boundaries -- the half-integer contours."""
        a, b = self.normalised(shape)
        half_px = (line_width / 2.0) / self.pitch
        # Boundaries are the HALF-integer contours: a lenslet centre is an
        # integer point, and the wall between two of them is at n + 1/2. So the
        # quantity to threshold is the distance from the fractional part to 0.5.
        da = np.abs((a % 1.0) - 0.5)
        db = np.abs((b % 1.0) - 0.5)
        return (da < half_px) | (db < half_px)

    def centre_marker_mask(self, shape: tuple[int, int], arm: int = 9) -> np.ndarray:
        """Crosshair on the grid origin, so the anchor point is never in doubt."""
        h, w = shape
        gx, gy = self.origin
        mask = np.zeros((h, w), dtype=bool)
        xi, yi = int(round(gx)), int(round(gy))
        if 0 <= yi < h:
            mask[yi, max(0, xi - arm) : min(w, xi + arm + 1)] = True
        if 0 <= xi < w:
            mask[max(0, yi - arm) : min(h, yi + arm + 1), xi] = True
        return mask

    def crop_side(self, scale: float = 1.0) -> int:
        """Side length in pixels of every sub-aperture tile.

        Fixed for a given pitch, independent of where the lenslet centre falls
        between pixels. Tiles get stacked and compared, so a tile that is 37 px
        for one lenslet and 38 for its neighbour is a defect, not a rounding
        detail.
        """
        return max(1, int(round(self.pitch * scale)))

    def crop(self, image: np.ndarray, i: int, j: int, scale: float = 1.0) -> np.ndarray:
        """Axis-aligned square crop of side crop_side() centred on lenslet (i, j).

        Returns an empty array if the crop falls entirely outside the frame;
        callers render that as a blank tile rather than raising, because a
        mid-adjustment pitch value routinely puts a corner lenslet off-frame.
        """
        cx, cy = self.centre_of(i, j)
        side = self.crop_side(scale)
        x0 = int(round(cx - side / 2.0))
        y0 = int(round(cy - side / 2.0))
        x1, y1 = x0 + side, y0 + side
        h, w = image.shape[:2]
        x0c, x1c = max(0, x0), min(w, x1)
        y0c, y1c = max(0, y0), min(h, y1)
        if x1c <= x0c or y1c <= y0c:
            return np.zeros((0, 0), dtype=image.dtype)
        return np.ascontiguousarray(image[y0c:y1c, x0c:x1c])
