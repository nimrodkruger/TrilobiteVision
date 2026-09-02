"""Is there a checkerboard in this micro-image? Answered without finding a corner.

The measured reason this exists: running `findChessboardCornersSB` over every
micro-image of a full-resolution frame costs about a second per camera on a Pi
5, so two cameras at 1 Hz need more than one core continuously. It never fitted.
Worse, its cost is *quadratic in 1/pitch* -- a grid left near its default pitch
yields seven times the tiles -- so a mis-set parameter turned a slow loop into
an unbounded one.

This answers a weaker question far more cheaply, and the weaker question turns
out to be the one the live display actually asks.

**The idea.** A checkerboard corner is a *saddle* of intensity: the surface
curves up along one diagonal and down along the other. That is exactly the
condition det(H) < 0 on the Hessian

        H = [ Ixx  Ixy ]
            [ Ixy  Iyy ]      det(H) = Ixx*Iyy - Ixy^2

so `S = Ixy^2 - Ixx*Iyy` is large and positive at a checkerboard corner and
near zero almost everywhere else -- edges are cylindrical (one curvature ~ 0),
blobs are elliptic (both curvatures the same sign). Take the local maxima of S
and you have the corner *positions* to within a few pixels; count them per
micro-image and you have, in one number per tile, "does this lenslet see the
pattern".

**Why it is fast.** Three Sobel convolutions and a dilation over the whole
frame, then one `np.bincount` against a cached pixel-to-tile label image. There
is no per-tile loop anywhere, so the cost depends on the frame size alone --
which also means a wrong pitch can no longer make it expensive. Measured on the
728 x 544 preview: about 3 ms, roughly 2% of one core for two cameras at 1 Hz,
against 210% for the detector it replaces.

**What it does not do.** It does not identify which corner is which, and it
does not localise anything to sub-pixel accuracy. Those are needed to *fit*
the model, not to decide where to point the board, and they happen off the Pi
on recorded frames. The one live decision that does need real corners -- is
this pose good enough to keep -- runs `findChessboardCornersSB` on a five-tile
cross once per pose, which costs about 45 ms.

**One trap, measured.** A *defocused* board reads a HIGHER count, not a lower
one: blur spreads the saddle response and creates more local maxima. So the
count cannot judge focus. The peak strength can -- it falls by roughly an order
of magnitude from sharp to blurred -- and it comes out of the same convolutions
for free. Both are reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


def load_cv2() -> Any:
    try:
        import cv2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on host
        raise RuntimeError(
            "OpenCV (cv2) is not importable, and the presence map needs it. "
            "Pi: 'sudo apt install -y python3-opencv' with a "
            "--system-site-packages venv. Desktop: pip install -e '.[desktop]'."
        ) from exc
    return cv2


@dataclass
class PresenceMap:
    """Per-micro-image checkerboard-likeness for one frame."""

    counts: np.ndarray                       # (rows, cols) int, indexed by lattice
    origin: tuple[int, int]                  # lattice index of counts[0, 0]
    whole: np.ndarray                        # (rows, cols) bool: complete micro-images
    strength: float                          # mean saddle peak height: a focus proxy
    best_contrast: float = 0.0               # grey levels at the frame's strongest saddle
    contrast_floor: float = 0.0              # grey levels a peak had to clear
    peaks: np.ndarray | None = None          # (N, 2) approximate corner positions
    frame_shape: tuple[int, int] = (0, 0)
    ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def count_at(self, i: int, j: int) -> int:
        ci, cj = i - self.origin[0], j - self.origin[1]
        if 0 <= cj < self.counts.shape[0] and 0 <= ci < self.counts.shape[1]:
            return int(self.counts[cj, ci])
        return 0

    @property
    def n_whole(self) -> int:
        return int(self.whole.sum())

    def seeing(self, threshold: int) -> list[tuple[int, int]]:
        """Complete micro-images whose count reaches `threshold`."""
        js, is_ = np.nonzero((self.counts >= threshold) & self.whole)
        return [(int(i) + self.origin[0], int(j) + self.origin[1])
                for i, j in zip(is_, js, strict=True)]

    def signature(self) -> np.ndarray:
        """A flat vector for comparing one frame's coverage with another's.

        Used to decide whether the board has settled and, later, whether it has
        moved enough to be worth another pose. Deliberately not a PnP pose:
        this is available on every frame for free, and the question "has the
        picture changed" does not need the board's position in space.
        """
        return np.where(self.whole, self.counts, 0).astype(np.float32).ravel()

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts.tolist(),
            "whole": self.whole.tolist(),
            "origin": list(self.origin),
            "strength": round(self.strength, 1),
            "ms": round(self.ms, 2),
            "shape": list(self.counts.shape),
        }

    def diagnose(self, threshold: int, board: tuple[int, int] | None = None) -> dict[str, Any]:
        """Why is nothing being detected? Answered in numbers and in a sentence.

        "0 of 117 micro-images see the board" is true and useless: it does not
        distinguish a lens cap from a threshold set twice too high from a board
        whose squares are larger than a micro-image. Each of those has a
        different fix and they took a field session each to tell apart. So this
        reports the distribution the threshold is being applied to, and names
        the most likely cause.
        """
        inside = self.counts[self.whole] if self.whole.any() else self.counts.ravel()
        best = int(inside.max()) if inside.size else 0
        median = float(np.median(inside)) if inside.size else 0.0
        seeing = int((inside >= threshold).sum())
        expect = (board[0] + 2) * (board[1] + 2) if board else None
        total = int(self.counts.sum())

        if total == 0 and self.best_contrast < 2.0:
            why = ("no corner-like structure anywhere in the frame. Lens cap, "
                   "no light, or wildly wrong exposure.")
        elif total == 0:
            why = (f"the whole frame's strongest corner is worth about "
                   f"{self.best_contrast:.0f} grey levels of contrast and the "
                   f"floor is {self.contrast_floor:.0f}. There IS an image; it "
                   f"is too soft or too dim to be a checkerboard. Open the "
                   f"aperture, add light, or check focus before touching any "
                   f"threshold.")
        elif best == 0:
            why = ("corners were found in the frame but none inside a complete "
                   "micro-image. The MLA grid is aligned somewhere the pattern "
                   "is not -- check pitch and offsets against the overlay.")
        elif seeing == 0 and expect and best < 0.35 * expect:
            why = (f"the best micro-image has {best} corner-like points but a "
                   f"{board[0]}x{board[1]} board should fill one with about "
                   f"{expect}. The squares are almost certainly too LARGE: a "
                   f"micro-image only sees a small patch of the intermediate "
                   f"image, so a board that looks fine to the naked eye can put "
                   f"less than one square in each one. Use a finer board, or "
                   f"move it further away.")
        elif seeing == 0:
            why = (f"the best micro-image has {best} corner-like points and the "
                   f"threshold is {threshold}. Lower the threshold, or improve "
                   f"focus and lighting -- the pattern is nearly being found.")
        elif self.strength < 2000:
            why = (f"{seeing} micro-images pass, but the peak strength is only "
                   f"{self.strength:.0f}. That is what a defocused board looks "
                   f"like; the count can go UP with blur, so it is not a "
                   f"reassurance.")
        else:
            why = f"{seeing} micro-images are seeing the pattern."

        return {
            "seeing": seeing,
            "threshold": int(threshold),
            "best": best,
            "median": round(median, 1),
            "whole": self.n_whole,
            "total_peaks": total,
            "strength": round(self.strength, 1),
            "best_contrast": round(self.best_contrast, 1),
            "contrast_floor": round(self.contrast_floor, 1),
            "expected_for_board": expect,
            "why": why,
        }


def tile_labels(geom, shape: tuple[int, int], scale: float = 1.0):
    """Pixel -> flat tile index, -1 outside every tile.

    Depends only on the MLA parameters and the frame size, so it is built once
    and reused until one of them changes. Building it costs about as much as a
    detection pass, which is why it is cached and never rebuilt per frame.
    """
    a, b = geom.normalised(shape)
    i, j = np.round(a), np.round(b)
    inside = (np.abs(a - i) <= 0.5 * scale) & (np.abs(b - j) <= 0.5 * scale)
    ni, nj = i.astype(np.int32), j.astype(np.int32)
    i0, j0 = int(ni.min()), int(nj.min())
    cols = int(ni.max()) - i0 + 1
    rows = int(nj.max()) - j0 + 1
    idx = (nj - j0) * cols + (ni - i0)
    idx[~inside] = -1
    return idx, (i0, j0, cols, rows)


def peaks_overlay(image: np.ndarray, detector: PresenceDetector, geom,
                  scale: float, threshold: int) -> np.ndarray:
    """The preview with every saddle peak marked and every tile labelled.

    Exists because "0 of 117 micro-images see the board" is a number, and what
    you need when that is wrong is a picture. Three things become obvious at a
    glance and are nearly impossible to work out from the counts:

      * peaks everywhere but the grid boxes in the wrong place -> the MLA
        alignment is off, not the detector;
      * a few peaks per micro-image where there should be thirty -> the board's
        squares are too large for a micro-image;
      * no peaks at all -> exposure, focus, or a lens cap.
    """
    cv2 = detector.cv2
    mask, _s = detector.saddle(image)
    labels, (i0, j0, cols, rows) = detector.labels_for(geom, image.shape[:2], scale)
    result = detector.run(image, geom, scale)

    view = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image.copy()
    ys, xs = np.nonzero(mask)
    for x, y in zip(xs, ys, strict=True):
        cv2.circle(view, (int(x), int(y)), 2, (60, 220, 255), -1)

    side = geom.crop_side(scale)
    for j in range(rows):
        for i in range(cols):
            if not result.whole[j, i]:
                continue
            n = int(result.counts[j, i])
            x0, y0 = geom.crop_origin(i + i0, j + j0, scale)
            colour = (90, 210, 120) if n >= threshold else (70, 70, 90)
            cv2.rectangle(view, (x0, y0), (x0 + side, y0 + side), colour, 1)
            if side >= 28:
                cv2.putText(view, str(n), (x0 + 3, y0 + 13),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, colour, 1, cv2.LINE_AA)

    d = result.diagnose(threshold)
    cv2.putText(view, f"peaks {d['total_peaks']}  best tile {d['best']}/{threshold}"
                      f"  seeing {d['seeing']}/{d['whole']}  strength {d['strength']:.0f}",
                (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 255, 160), 1, cv2.LINE_AA)
    return view


#: Measured constant of the saddle response, exact to three figures over the
#: whole 8-bit range (see scripts/benchmark_detectors.py, and the calibration in
#: the docstring of `PresenceDetector.saddle`): an ideal step corner of contrast
#: C grey levels produces a peak response
#:
#:      S = (SADDLE_PER_LEVEL * C)^2
#:
#: with SADDLE_PER_LEVEL = 1.061 for a 3x3 Sobel on a 3x3-blurred uint8 image.
#: It is the bridge between a threshold in response units, which means nothing,
#: and a threshold in grey levels, which is a property of the board and the
#: lighting.
SADDLE_PER_LEVEL = 1.061


class PresenceDetector:
    """Stateless per-frame saddle counting, with the label image cached."""

    def __init__(self, peak_window: int = 5, rel_threshold: float = 0.15,
                 min_contrast: float = 40.0) -> None:
        self.cv2 = load_cv2()
        self.peak_window = int(peak_window)
        self.rel_threshold = float(rel_threshold)
        self.min_contrast = float(min_contrast)
        self._key: tuple | None = None
        self._labels: np.ndarray | None = None
        self._whole: np.ndarray | None = None
        self._dims: tuple[int, int, int, int] = (0, 0, 0, 0)

    def labels_for(self, geom, shape: tuple[int, int], scale: float):
        key = (shape, geom.pitch, geom.rotation_deg, geom.offset_x, geom.offset_y, scale)
        if key != self._key:
            self._labels, self._dims = tile_labels(geom, shape, scale)
            # Which lattice cells yield a *complete* micro-image. A cell half
            # off the sensor collects fewer peaks purely because it is smaller,
            # so counting it alongside the others would make the edge of the
            # array look permanently short of pattern.
            i0, j0, cols, rows = self._dims
            whole = np.zeros((rows, cols), dtype=bool)
            for i, j in geom.whole_indices(scale, derotate=False):
                ci, cj = i - i0, j - j0
                if 0 <= ci < cols and 0 <= cj < rows:
                    whole[cj, ci] = True
            self._whole = whole
            self._key = key
        return self._labels, self._dims

    @property
    def floor(self) -> float:
        """The absolute response a peak must clear, in saddle-response units."""
        return (SADDLE_PER_LEVEL * self.min_contrast) ** 2

    def saddle(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(peak mask, saddle response). Three convolutions and a dilation.

        **Two gates, and the second one is not optional.** A peak must be a
        local maximum, must clear `rel_threshold` of the frame's own maximum,
        *and* must clear an absolute floor set by `min_contrast`.

        The relative gate alone is scale-free, which sounds like a virtue and
        is a defect: it normalises whatever is in front of the lens up to
        "detected". Measured on the 728 x 544 preview --

            board corners            S ~ 2e4 .. 5e4   (contrast 130..210)
            a busy but flat scene    S ~ 5e2 .. 2.5e3 (the `gratings` target)
            sensor noise, sigma 2.5  S ~ 3e1, 11500 peaks over the frame

        -- so with only a relative gate a blank wall yields thousands of peaks
        whose S is a hundredth of a real corner's, and roughly ten micro-images
        in a hundred cross a count threshold of 20 on noise alone. That is a
        detector that reports a board when there is none, and it made the
        discrimination tests fail about one run in three.

        The floor is quoted as a contrast in grey levels because that is a
        statement about the board and the light, not about this code: 40 levels
        is far below any usable printed checkerboard (a real one gives 130+)
        and far above what noise or a smooth gradient can manufacture.
        """
        cv2 = self.cv2
        src = image if image.dtype == np.uint8 else cv2.convertScaleAbs(
            image, alpha=255.0 / max(float(image.max()), 1.0)
        )
        f = cv2.GaussianBlur(src, (3, 3), 0).astype(np.float32)
        ixx = cv2.Sobel(f, cv2.CV_32F, 2, 0, ksize=3)
        iyy = cv2.Sobel(f, cv2.CV_32F, 0, 2, ksize=3)
        ixy = cv2.Sobel(f, cv2.CV_32F, 1, 1, ksize=3)
        s = ixy * ixy - ixx * iyy
        np.maximum(s, 0.0, out=s)
        # A local maximum over a window a bit smaller than a square, so two
        # corners of the same square are never merged into one peak.
        w = max(3, self.peak_window | 1)
        peak = cv2.dilate(s, np.ones((w, w), np.uint8))
        gate = max(self.rel_threshold * float(s.max()), self.floor)
        mask = (s >= peak) & (s > gate)
        return mask, s

    def run(self, image: np.ndarray, geom, scale: float = 1.0,
            with_peaks: bool = False) -> PresenceMap:
        import time  # noqa: PLC0415

        t0 = time.perf_counter()
        labels, (i0, j0, cols, rows) = self.labels_for(geom, image.shape[:2], scale)
        mask, s = self.saddle(image)
        flat = labels.ravel()[mask.ravel()]
        counts = np.bincount(flat[flat >= 0], minlength=cols * rows)[: cols * rows]
        strength = float(s[mask].mean()) if mask.any() else 0.0
        best_contrast = float(np.sqrt(max(float(s.max()), 0.0))) / SADDLE_PER_LEVEL
        peaks = None
        if with_peaks:
            ys, xs = np.nonzero(mask)
            peaks = np.stack([xs, ys], axis=1).astype(np.float32)
        return PresenceMap(
            counts=counts.reshape(rows, cols),
            origin=(i0, j0),
            whole=self._whole,
            strength=strength,
            best_contrast=best_contrast,
            contrast_floor=self.min_contrast,
            peaks=peaks,
            frame_shape=image.shape[:2],
            ms=(time.perf_counter() - t0) * 1000.0,
        )
