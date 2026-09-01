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

# The sub-apertures the UI displays, in left-to-right display order. Defined
# here rather than in the web layer so the overlay's highlight boxes and the
# extracted tiles are guaranteed to name the same lenslets.
#
# named_indices() resolves more names than these -- all four corners plus the
# centre. Signs are in image convention: x right, y DOWN, so "top_right" is
# +i, -j.
UI_SUBAPERTURES: tuple[str, ...] = ("top_right", "centre", "bottom_left")


def _bilinear(image: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Sample `image` at fractional coordinates, clamping at the border.

    Written out rather than pulled from cv2 or scipy because the tiles are
    small, this is the only resampling in the project, and it keeps the optics
    module free of an image-processing dependency that the Pi build would then
    have to guarantee.
    """
    h, w = image.shape[:2]
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0).astype(np.float32)
    fy = (y - y0).astype(np.float32)

    x0c, x1c = np.clip(x0, 0, w - 1), np.clip(x0 + 1, 0, w - 1)
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)

    src = image.astype(np.float32)
    if src.ndim == 3:
        fx, fy = fx[..., None], fy[..., None]
    top = src[y0c, x0c] * (1 - fx) + src[y0c, x1c] * fx
    bot = src[y1c, x0c] * (1 - fx) + src[y1c, x1c] * fx
    out = top * (1 - fy) + bot * fy

    if np.issubdtype(image.dtype, np.integer):
        info = np.iinfo(image.dtype)
        out = np.clip(np.rint(out), info.min, info.max)
    return out.astype(image.dtype)


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

    # -- resolution -------------------------------------------------------

    def rescaled(self, width: int, height: int) -> MLAGeometry:
        """The same physical grid, expressed in a differently-sized frame.

        The grid is aligned by eye against the **preview** (728 x 544 on this
        rig) but the calibration crops come out of the **sensor** frame
        (1456 x 1088). That is a factor of two applied silently to pitch and to
        both offsets, and the symptom of getting it wrong is not an exception:
        it is every crop landing between micro-images and detection simply
        never working. So the conversion lives here, in one place, rather than
        at each call site.

        The arithmetic is exact, which is worth showing because it looks as
        though it should not be. Under the pixel-area convention a preview
        pixel at x_p covers sensor coordinates (x_p + 1/2)s - 1/2, with
        s = W_s / W_p. Applying that to the origin:

            (c_p + dx + 1/2)s - 1/2   where  c_p = (W_p - 1)/2
          = (W_p/2 + dx)s - 1/2
          = (W_s - 1)/2 + dx*s
          = c_s + dx*s

        so the offset simply scales, with no half-pixel remainder. Rotation is
        scale-invariant and carries over unchanged.

        Raises on a non-uniform scale: an anisotropic resample would make
        `pitch` two different numbers, and this class has only one.
        """
        if self.width <= 0 or self.height <= 0:
            raise ValueError("cannot rescale a geometry with no reference size")
        sx = width / self.width
        sy = height / self.height
        if abs(sx - sy) > 1e-6 * max(sx, sy):
            raise ValueError(
                f"anisotropic rescale {self.width}x{self.height} -> {width}x{height} "
                f"(x{sx:.4f} vs x{sy:.4f}). The preview and full-resolution streams "
                f"must share an aspect ratio, or pitch has no single value."
            )
        return MLAGeometry(
            width=int(width),
            height=int(height),
            pitch=self.pitch * sx,
            rotation_deg=self.rotation_deg,
            offset_x=self.offset_x * sx,
            offset_y=self.offset_y * sy,
        )

    # -- wholeness and selection ------------------------------------------

    def is_whole(self, i: int, j: int, scale: float = 1.0, derotate: bool = True) -> bool:
        """Does lenslet (i, j) yield a complete tile?

        This is the *exact* predicate the extraction uses -- it tests the same
        sampling window that crop() will read, not a padded approximation of
        it. That matters: an approximate bound that is a pixel or two
        conservative rejects the outermost lenslet most of the time, so the
        view you get is the second one in from the edge, and which one it picks
        changes as the offset moves by a fraction of a pixel. Both symptoms
        come from the predicate disagreeing with the extractor, so the fix is
        to make them the same code.
        """
        cx, cy = self.centre_of(i, j)
        half = self.crop_side(scale) / 2.0
        if derotate and abs(self.rotation_deg) > 1e-9:
            # The sampling window is a square rotated with the lattice, so all
            # four of its corners must be inside the frame.
            t = math.radians(self.rotation_deg)
            ct, st = math.cos(t), math.sin(t)
            pts = [
                (cx + sx * half * ct - sy * half * st, cy + sx * half * st + sy * half * ct)
                for sx in (-1.0, 1.0)
                for sy in (-1.0, 1.0)
            ]
        else:
            # Axis-aligned integer window, exactly as crop() computes it.
            x0 = int(round(cx - half))
            y0 = int(round(cy - half))
            side = self.crop_side(scale)
            return x0 >= 0 and y0 >= 0 and x0 + side <= self.width and y0 + side <= self.height
        return all(
            0.0 <= x <= self.width - 1 and 0.0 <= y <= self.height - 1 for x, y in pts
        )

    def _search_radius(self, scale: float = 1.0) -> tuple[int, int]:
        """Generous index bounds to enumerate over. Cheap; correctness first."""
        reach = math.hypot(self.width, self.height)
        n = int(reach / max(self.pitch, 1e-6)) + 2
        return n, n

    def whole_indices(self, scale: float = 1.0, derotate: bool = True) -> list[tuple[int, int]]:
        ni, nj = self._search_radius(scale)
        return [
            (i, j)
            for i in range(-ni, ni + 1)
            for j in range(-nj, nj + 1)
            if self.is_whole(i, j, scale, derotate)
        ]

    def index_extent(self, scale: float = 1.0, derotate: bool = True) -> tuple[int, int]:
        """Largest |i| and |j| among whole lenslets. Reporting only."""
        whole = self.whole_indices(scale, derotate)
        if not whole:
            return 0, 0
        return max(abs(i) for i, _ in whole), max(abs(j) for _, j in whole)

    def nearest_whole_to(
        self, target: tuple[float, float], scale: float = 1.0, derotate: bool = True
    ) -> tuple[int, int] | None:
        """The whole lenslet whose centre is closest to a point on the sensor.

        Used with a sensor corner as the target, this is a direct statement of
        "the furthest lenslet towards that corner that is still complete" --
        no per-axis bound, no symmetry assumption, and independent for each
        corner, so an off-centre grid picks the genuinely best lenslet in each
        direction rather than a symmetric pair chosen by whichever side ran out
        first.

        Ties are broken deterministically by index, so a value sitting exactly
        between two candidates does not flicker between frames.
        """
        whole = self.whole_indices(scale, derotate)
        if not whole:
            return None
        tx, ty = target
        return min(
            whole,
            key=lambda ij: (
                round((self.centre_of(*ij)[0] - tx) ** 2 + (self.centre_of(*ij)[1] - ty) ** 2, 6),
                ij[0],
                ij[1],
            ),
        )

    def named_indices(
        self, scale: float = 1.0, derotate: bool = True
    ) -> dict[str, tuple[int, int]]:
        """Map the UI's sub-aperture names onto concrete lenslet indices.

        Corners are resolved independently, each as the whole lenslet nearest
        that corner of the sensor. The corners are what tell you whether the
        grid is right: pitch and rotation error accumulates with distance from
        the anchor, so a centre lenslet looks correct under almost any wrong
        pitch while a corner one does not.
        """
        w, h = self.width - 1, self.height - 1
        targets = {
            "centre": self.origin,
            "top_right": (w, 0.0),
            "bottom_left": (0.0, h),
            "top_left": (0.0, 0.0),
            "bottom_right": (w, h),
        }
        out: dict[str, tuple[int, int]] = {}
        for name, pt in targets.items():
            idx = self.nearest_whole_to(pt, scale, derotate)
            if idx is not None:
                out[name] = idx
        return out

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

    def highlight_mask(
        self, shape: tuple[int, int], indices, scale: float = 1.0, thickness: float = 2.0
    ) -> np.ndarray:
        """Outline the given lenslets, following the rotated tile boundary.

        Drawn in the lattice's own coordinates, so under rotation the box sits
        on the tile that is actually extracted rather than on its axis-aligned
        bounding box -- which is both larger and visibly off-register against
        the grid, and would misrepresent what the zoom is showing.

        Seeing which lenslets are selected is what makes a shifting selection
        diagnosable instead of mysterious: when the choice jumps as you drag
        the pitch, you can watch where it jumped to.
        """
        h, w = shape
        mask = np.zeros((h, w), dtype=bool)
        idx = list(indices)
        if not idx:
            return mask
        a, b = self.normalised((h, w))
        half = 0.5 * float(scale)
        t = max(float(thickness), 0.5) / self.pitch    # thickness in pitch units
        for i, j in idx:
            da, db = np.abs(a - i), np.abs(b - j)
            inside = (da <= half) & (db <= half)
            inner = (da <= half - t) & (db <= half - t)
            mask |= inside & ~inner
        return mask

    def max_safe_crop_scale(self, aperture: str = "square") -> float:
        """Largest crop_scale whose axis-aligned tile stays inside one lenslet.

        The constraint is not to stray into a neighbouring micro-image, which
        would feed a different scene patch to the corner detector. What bounds
        it depends on the lenslet aperture shape, and the two answers differ in
        kind:

          square apertures  -- the cell rotates with the lattice, so an
            axis-aligned crop must fit inside a rotated square:
            1/(|cos θ| + |sin θ|). Costs 3% at 2°, 8% at 5°.

          circular apertures -- a circle has no orientation, so rotation costs
            nothing at all; the bound is the inscribed square of the footprint
            circle, 1/√2 ≈ 0.707, whatever θ is.

        Worth knowing which you have: with circular lenslets there is no crop
        reason to minimise the grid rotation physically.
        """
        if aperture == "circle":
            return 1.0 / math.sqrt(2.0)
        t = math.radians(self.rotation_deg)
        return 1.0 / (abs(math.cos(t)) + abs(math.sin(t)))

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
        side = self.crop_side(scale)
        x0, y0 = self.crop_origin(i, j, scale)
        x1, y1 = x0 + side, y0 + side
        h, w = image.shape[:2]
        x0c, x1c = max(0, x0), min(w, x1)
        y0c, y1c = max(0, y0), min(h, y1)
        if x1c <= x0c or y1c <= y0c:
            return np.zeros((0, 0), dtype=image.dtype)
        return np.ascontiguousarray(image[y0c:y1c, x0c:x1c])

    def crop_origin(self, i: int, j: int, scale: float = 1.0) -> tuple[int, int]:
        """Top-left pixel of the axis-aligned crop for lenslet (i, j).

        Exposed because anything that measures inside a tile has to put the
        measurement back into frame coordinates, and doing that arithmetic at
        the call site is how a crop convention and a measurement convention
        drift half a pixel apart.
        """
        cx, cy = self.centre_of(i, j)
        side = self.crop_side(scale)
        return int(round(cx - side / 2.0)), int(round(cy - side / 2.0))

    def tile_to_frame(
        self,
        i: int,
        j: int,
        points: np.ndarray,
        scale: float = 1.0,
        derotate: bool = False,
    ) -> np.ndarray:
        """Map (x, y) points measured in a tile back into frame coordinates.

        The inverse of whichever extraction produced the tile, so corners
        recorded from a de-rotated tile and corners recorded from a plain crop
        land in the same place. That equivalence is the reason de-rotation is
        allowed at all (calibration-spec §2.6): what the fit consumes is frame
        coordinates, and both routes deliver them.
        """
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        side = self.crop_side(scale)
        if not derotate or abs(self.rotation_deg) < 1e-9:
            x0, y0 = self.crop_origin(i, j, scale)
            return pts + np.array([x0, y0], dtype=np.float64)
        cx, cy = self.centre_of(i, j)
        t = math.radians(self.rotation_deg)
        ct, st = math.cos(t), math.sin(t)
        o = pts - (side - 1) / 2.0
        out = np.empty_like(o)
        out[:, 0] = cx + o[:, 0] * ct - o[:, 1] * st
        out[:, 1] = cy + o[:, 0] * st + o[:, 1] * ct
        return out

    def crop_derotated(self, image: np.ndarray, i: int, j: int, scale: float = 1.0) -> np.ndarray:
        """Sub-aperture tile resampled into the lenslet's own axes.

        The array is rotated relative to the sensor, so an axis-aligned crop
        shows the lenslet's field tilted by that angle -- and every tile tilted
        the same way, which is exactly the artefact you are trying to null out
        by eye. Sampling along the lattice basis instead gives the tile as the
        lenslet actually sees it, so the three views become directly
        comparable.

        Sampled once, straight from the full frame, rather than cropping and
        then rotating: two resampling steps would blur a 100 px tile
        noticeably, and blur is the thing that makes a focus judgement wrong.
        """
        if abs(self.rotation_deg) < 1e-9:
            return self.crop(image, i, j, scale)

        side = self.crop_side(scale)
        cx, cy = self.centre_of(i, j)
        t = math.radians(self.rotation_deg)
        ct, st = math.cos(t), math.sin(t)

        # Output pixel (a, b), measured from the tile centre, corresponds to
        # the point c + a*u_hat + b*v_hat in the frame -- the lattice basis
        # directions, which is what "the lenslet's own axes" means.
        o = np.arange(side, dtype=np.float32) - (side - 1) / 2.0
        bb, aa = np.meshgrid(o, o, indexing="ij")
        sx = cx + aa * ct - bb * st
        sy = cy + aa * st + bb * ct
        return _bilinear(image, sx, sy)
