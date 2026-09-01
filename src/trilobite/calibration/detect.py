"""Checkerboard corner detection in a micro-image.

A detector and nothing else: no threads, no camera access, no files. It is
called from two places, both of which decide their own cadence:

  * `calibration/session.py`, on a five-tile cross once per pose -- the
    acceptance check that decides whether a pose is worth keeping. About 45 ms
    per camera at full resolution.
  * offline analysis, over every micro-image of a recorded frame, where there
    is no time budget at all.

The live "is the board in view" question is *not* answered here. It is answered
by `calibration/presence.py`, which counts saddle points across the whole frame
in about 3 ms without locating a single corner. The difference matters: running
this detector over every tile of every frame is what made the first live design
impossible on a Pi.

Corner positions come back in FULL-FRAME sensor coordinates, never tile-local,
so a record does not depend on the crop convention and a later change to
`crop_scale` does not invalidate it.

On refinement: `findChessboardCornersSB` with CALIB_CB_ACCURACY already runs
its own sub-pixel stage, and it is the better one here. A `cornerSubPix` pass
on top needs a search window, and at a 20 px square any window large enough to
help reaches into the neighbouring square. Refinement is therefore SB's, not
ours -- a deliberate departure from calibration-ui-spec §4.4 step 3.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..optics.mla import MLAGeometry
from .settings import AcceptanceSpec, BoardSpec

log = logging.getLogger(__name__)


def load_cv2() -> Any:
    """Import cv2, or explain precisely how to get it on this machine.

    Worth the wrapper: the two hosts this runs on need opposite advice, and a
    bare ImportError sends you to pip on the Pi, which is the one place pip is
    the wrong answer.
    """
    try:
        import cv2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on host
        raise RuntimeError(
            "OpenCV (cv2) is not importable, and corner detection needs it. "
            "On the Pi: 'sudo apt install -y python3-opencv' and make sure the "
            "venv was created with --system-site-packages -- do NOT pip install "
            "opencv, it would shadow the apt build that shares numpy's ABI with "
            "picamera2. On a desktop: pip install -e '.[desktop]'."
        ) from exc
    return cv2


# Tiles this uniform hold no pattern; skipping them costs one std() and saves a
# detector call. Set low deliberately -- a genuinely dim micro-image should be
# reported as "detected nothing", which is diagnostic, not skipped silently.
FLAT_TILE_STD = 3.0


@dataclass(frozen=True)
class TileResult:
    """One micro-image's verdict for one frame."""

    i: int
    j: int
    found: bool
    n_corners: int
    # Corner positions in FULL-FRAME sensor coordinates, never tile-local.
    # Held only for this pass; the next one replaces them.
    corners: np.ndarray | None = None
    skipped: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return (self.i, self.j)


@dataclass
class DetectionResult:
    """The whole field, for one frame, for one camera."""

    cam_id: str
    t_wall: float
    seq: int
    frame_shape: tuple[int, int]          # (height, width)
    board: tuple[int, int]                # (cols, rows) inner corners
    tiles: list[TileResult] = field(default_factory=list)
    cross_centres: list[tuple[int, int]] = field(default_factory=list)
    accepted: bool = False
    pass_ms: float = 0.0
    error: str | None = None
    # Annotated, preview-sized view of exactly the frame that was measured.
    # Kept beside the numbers so what you see and what was counted cannot be
    # one pass out of step.
    overlay: np.ndarray | None = None

    @property
    def found_tiles(self) -> list[TileResult]:
        return [t for t in self.tiles if t.found]

    def as_dict(self) -> dict[str, Any]:
        """JSON for the dashboard. Corner arrays are summarised, not sent --
        127 tiles x 12 corners twice a second is a megabyte a minute of numbers
        nobody reads, and the picture already shows where they are."""
        return {
            "cam_id": self.cam_id,
            "t_wall": self.t_wall,
            "seq": self.seq,
            "frame_shape": list(self.frame_shape),
            "board": list(self.board),
            "tiles_examined": len(self.tiles),
            "tiles_found": len(self.found_tiles),
            "pass_ms": round(self.pass_ms, 1),
            "accepted": self.accepted,
            "cross_centres": [list(c) for c in self.cross_centres],
            "error": self.error,
            "tiles": [
                {"i": t.i, "j": t.j, "found": t.found, "n": t.n_corners, "skipped": t.skipped}
                for t in self.tiles
            ],
        }


class CornerDetector:
    """Stateless per-frame detection. One instance is reusable across cameras."""

    def __init__(
        self,
        board: BoardSpec,
        acceptance: AcceptanceSpec,
        normalize: bool = False,
        accuracy: bool = False,
    ) -> None:
        self.board = board
        self.acceptance = acceptance
        self._cv2 = cv2 = load_cv2()
        # See DetectionSpec for the measurements behind both defaults being
        # off. NORMALIZE_IMAGE in particular is not free: on evenly-lit tiles
        # it costs an order of magnitude of corner precision.
        self.flags = 0
        if normalize:
            self.flags |= cv2.CALIB_CB_NORMALIZE_IMAGE
        if accuracy:
            self.flags |= cv2.CALIB_CB_ACCURACY

    # -- one tile ---------------------------------------------------------

    def detect_tile(self, tile: np.ndarray) -> tuple[bool, np.ndarray | None]:
        cv2 = self._cv2
        if tile.size == 0 or float(tile.std()) < FLAT_TILE_STD:
            return False, None
        if tile.dtype != np.uint8:
            # SB wants 8-bit. Scale rather than truncate: a 10-bit frame
            # truncated to its low byte is noise with a pattern in it.
            lo, hi = float(tile.min()), float(tile.max())
            span = max(hi - lo, 1.0)
            tile = ((tile.astype(np.float32) - lo) * (255.0 / span)).astype(np.uint8)
        ok, corners = cv2.findChessboardCornersSB(
            tile, (self.board.cols, self.board.rows), flags=self.flags
        )
        if not ok or corners is None:
            return False, None
        return True, np.asarray(corners, dtype=np.float64).reshape(-1, 2)

    # -- one frame --------------------------------------------------------

    def run(
        self,
        image: np.ndarray,
        geom: MLAGeometry,
        cam_id: str,
        seq: int,
        scale: float = 1.0,
        derotate: bool = False,
        tiles: list[tuple[int, int]] | None = None,
    ) -> DetectionResult:
        t0 = time.perf_counter()
        h, w = image.shape[:2]
        result = DetectionResult(
            cam_id=cam_id,
            t_wall=time.time(),
            seq=seq,
            frame_shape=(h, w),
            board=(self.board.cols, self.board.rows),
        )

        for i, j in (tiles if tiles is not None else geom.whole_indices(scale, derotate=derotate)):
            tile = (
                geom.crop_derotated(image, i, j, scale)
                if derotate
                else geom.crop(image, i, j, scale)
            )
            if tile.size == 0:
                result.tiles.append(TileResult(i, j, False, 0, skipped=True))
                continue
            if float(tile.std()) < FLAT_TILE_STD:
                result.tiles.append(TileResult(i, j, False, 0, skipped=True))
                continue
            found, pts = self.detect_tile(tile)
            corners = (
                geom.tile_to_frame(i, j, pts, scale, derotate) if found and pts is not None
                else None
            )
            result.tiles.append(
                TileResult(i, j, found, 0 if corners is None else len(corners), corners)
            )

        result.cross_centres = self.crosses(result)
        result.accepted = bool(result.cross_centres)
        result.pass_ms = (time.perf_counter() - t0) * 1000.0
        return result

    # -- acceptance -------------------------------------------------------

    def crosses(self, result: DetectionResult) -> list[tuple[int, int]]:
        """Centres of every connected cross that meets the acceptance rule.

        A tile counts towards a cross when it returned at least
        `min_corners_per_tile` corners. With the whole-pattern detector that is
        all-or-nothing -- a tile returns cols x rows corners or none -- so the
        threshold only bites if partial detection is added later. It is kept
        because the *rule* is about corner count, not about which detector
        happens to be in use.

        The cross is centre plus four edge-neighbours, and `min_cross_tiles`
        counts the whole thing: 5 is the full cross, lower values accept a
        partial one. See calibration-ui-spec §5.1 for why a cross and not, say,
        any five tiles: it spans both lattice directions, so one accepted pose
        constrains both columns of the lattice matrix rather than one.
        """
        need = int(self.acceptance.min_corners_per_tile)
        passing = {t.key for t in result.tiles if t.found and t.n_corners >= need}
        want = int(self.acceptance.min_cross_tiles)
        out: list[tuple[int, int]] = []
        neighbours = ((1, 0), (-1, 0), (0, 1), (0, -1))
        for i, j in sorted(passing):
            n = 1 + sum((i + di, j + dj) in passing for di, dj in neighbours)
            if n >= want:
                out.append((i, j))
        return out


# --------------------------------------------------------------------------
# Overlay
# --------------------------------------------------------------------------


def annotate(
    image: np.ndarray,
    result: DetectionResult,
    geom: MLAGeometry,
    scale: float = 1.0,
    display_width: int = 728,
) -> np.ndarray:
    """Draw the pass, showing where every corner was actually measured.

    Downscaled to preview width first: the browser displays it at that size
    anyway, and shipping 1456 x 1088 JPEGs twice a second for both cameras is
    bandwidth spent on pixels nobody can see. The corners are drawn *after* the
    resize, from their full-resolution positions, so their placement carries
    the precision of the measurement rather than of the display.

    Colour is the whole message:
      grey box    tile examined, nothing found
      green box   full pattern found
      cyan box    a tile at the centre of a qualifying cross
      yellow dots corners, at their measured positions
    """
    cv2 = load_cv2()
    h, w = image.shape[:2]
    s = min(1.0, display_width / float(w)) if w else 1.0
    view = cv2.resize(image, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
    view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR) if view.ndim == 2 else view

    side = geom.crop_side(scale)
    centres = set(result.cross_centres)
    for t in result.tiles:
        x0, y0 = geom.crop_origin(t.i, t.j, scale)
        p0 = (int(round(x0 * s)), int(round(y0 * s)))
        p1 = (int(round((x0 + side) * s)), int(round((y0 + side) * s)))
        if t.key in centres:
            colour, thick = (255, 220, 60), 2      # BGR cyan-ish
        elif t.found:
            colour, thick = (90, 210, 120), 1
        else:
            colour, thick = (70, 70, 80), 1
        cv2.rectangle(view, p0, p1, colour, thick)

    for t in result.tiles:
        if t.corners is None:
            continue
        for x, y in t.corners:
            cv2.circle(view, (int(round(x * s)), int(round(y * s))), 1, (60, 220, 255), -1)

    banner = (
        f"{result.cam_id}  {len(result.found_tiles)}/{len(result.tiles)} tiles  "
        f"{len(result.cross_centres)} cross  {result.pass_ms:.0f} ms"
    )
    cv2.putText(view, banner, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                (40, 255, 160) if result.accepted else (200, 200, 210), 1, cv2.LINE_AA)
    return view


# The worker that used to live here is gone. It ran this detector over every
# micro-image of a full-resolution frame, on its own thread, per camera --
# about a second per pass on a Pi 5, so two cameras at 1 Hz needed more than
# one core, and its second claim on the picamera2 request pool took the rig
# down repeatedly. A four-core CPU stress test did not, which is how the camera
# access rather than the load was identified.
#
# What replaced it, and why each piece is where it is:
#
#   the live map        calibration/presence.py, running as a pipeline stage on
#                       preview frames that already flow. ~3 ms, no camera
#                       access of its own, and its cost does not grow with the
#                       number of tiles.
#   the accept check    calibration/session.py, calling detect_tile() below on a
#                       five-tile cross, once per pose. ~45 ms.
#   full-field corners  offline, on the desktop, from the recorded frames.
#
# This module is now a detector and nothing else: nothing here owns a thread or
# touches a camera.
