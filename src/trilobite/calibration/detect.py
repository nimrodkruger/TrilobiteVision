"""Live per-tile checkerboard detection. **Nothing here writes to disk.**

That constraint is the point of this stage, not an omission. Before any pose is
recorded there is a prior question to answer at the bench: *does the detector
see a board in the micro-images at all, and in enough of them at once?* A rig
that records unusable corners for twenty minutes and reveals it at the fit is
far worse than one that shows you, live, that eleven tiles are finding the
pattern and the other hundred and sixteen are not.

So this module answers exactly that question and holds the answer in memory
until the next pass overwrites it. There is no session, no accumulator, no
file. Recording is the next piece and it goes somewhere else.

What runs, per pass, per camera:

  1. one full-resolution frame (`read_full_mono`) -- not the preview, which is
     half-scale and would halve corner precision for nothing;
  2. the MLA geometry converted to that frame's size (`geometry_for`) -- this
     is where the preview-vs-sensor factor of two is dealt with, once;
  3. every whole tile cropped and put through `findChessboardCornersSB`;
  4. corners mapped back into frame coordinates;
  5. the cross-of-five acceptance rule evaluated over the passing tiles.

Cost is about 2 ms per tile for a 100 px micro-image, so 127 whole tiles is a
quarter of a second on a desktop and closer to a second on a Pi 5. That is a
1-2 Hz instrument, which is the right cadence for "hold the board still and
watch" and hopeless as a per-frame operation -- hence a worker thread with its
own clock rather than anything hung off the capture loop.

On refinement: `findChessboardCornersSB` with CALIB_CB_ACCURACY already runs
its own sub-pixel stage, and it is the better one here. A `cornerSubPix` pass
on top needs a search window, and at a 20 px square any window large enough to
help reaches into the neighbouring square. Refinement is therefore SB's, not
ours -- a deliberate departure from calibration-ui-spec §4.4 step 3.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
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


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


class DetectionWorker:
    """One thread per camera, publishing the latest pass and nothing else.

    Deliberately not fed by the capture loop. The capture thread's contract is
    read-process-publish and nothing slow, and a detection pass is three orders
    of magnitude slower than a preview frame. This pulls its own full-resolution
    frame at its own rate, and a pass that takes too long simply produces fewer
    updates -- it can never perturb the preview or the frame rate.

    `latest()` returns the most recent completed pass, or None. There is no
    history, because nothing is being recorded.

    **Three limits on how hard this is allowed to work**, added after the first
    run on the real rig took the Pi down:

      * a shared semaphore, so by default only one camera detects at a time.
        Two full-frame passes in parallel double the peak current draw, and a
        Pi 5 with two cameras is already the wrong side of a marginal supply.
      * a duty cycle. After a pass lasting T the worker sleeps until it has
        been idle for the configured fraction of the time, so a pass that
        turns out to cost a second costs a second every two seconds rather
        than a permanently pinned core.
      * a lowered thread priority, so the capture threads and the web server
        keep the CPU when it is contended. A starved event loop is what makes
        a busy rig look hung rather than slow.

    None of these make a wrong pitch cheap -- that is caught before the start,
    by the tile-count precondition. They bound what a *correct* configuration
    can cost.
    """

    def __init__(
        self,
        cam: Any,
        board: BoardSpec,
        acceptance: AcceptanceSpec,
        min_interval: float = 0.4,
        annotate_overlay: bool = True,
        normalize: bool = False,
        accuracy: bool = False,
        max_tiles: int = 320,
        max_duty: float = 0.5,
        gate: threading.Semaphore | None = None,
    ) -> None:
        self.cam = cam
        self.min_interval = float(min_interval)
        self.annotate_overlay = annotate_overlay
        self.detector = CornerDetector(board, acceptance, normalize, accuracy)
        self.max_tiles = int(max_tiles)
        self.max_duty = min(max(float(max_duty), 0.05), 1.0)
        self.gate = gate or threading.Semaphore(1)
        self.passes = 0
        self.started_at = time.time()
        self.last_pass_ms = 0.0
        self._latest: DetectionResult | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"detect-{self.cam.cam_id}", daemon=True
        )
        self._thread.start()
        log.info("%s: detection worker started", self.cam.cam_id)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        log.info("%s: detection worker stopped after %d passes", self.cam.cam_id, self.passes)

    def latest(self) -> DetectionResult | None:
        with self._lock:
            return self._latest

    # -- the loop ---------------------------------------------------------

    def _run(self) -> None:
        # Detection is the least urgent thing this process does. Give the
        # capture threads and the event loop the CPU when it is contended --
        # a preview that stutters and an API that stops answering are how a
        # busy rig comes to look like a crashed one.
        with contextlib.suppress(AttributeError, OSError):
            os.nice(10)

        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                # Only one camera measures at a time by default. The semaphore
                # rather than a single shared worker so that each camera keeps
                # its own cadence, error state and result slot.
                if not self.gate.acquire(timeout=5.0):
                    continue
                try:
                    self._one_pass()
                finally:
                    self.gate.release()
            except Exception as exc:
                log.exception("%s: detection pass failed", self.cam.cam_id)
                self._publish(
                    DetectionResult(
                        cam_id=self.cam.cam_id, t_wall=time.time(), seq=-1,
                        frame_shape=(0, 0), board=(0, 0),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                # A failure that repeats every 400 ms fills the log and helps
                # nobody. Back off to once a second and let the message stand.
                self._stop.wait(1.0)
                continue
            spent = time.monotonic() - t0
            self.last_pass_ms = spent * 1000.0
            # Idle for long enough that the busy fraction stays under max_duty,
            # and in any case for min_interval. At duty 0.5 a one-second pass
            # is followed by a second of sleep, so the worst case is half a
            # core per camera instead of all of it.
            duty_sleep = spent * (1.0 / self.max_duty - 1.0)
            self._stop.wait(max(0.0, self.min_interval - spent, duty_sleep))

    def _one_pass(self) -> None:
        stage = self.cam.mla_stage()
        if stage is None:
            raise RuntimeError(f"{self.cam.cam_id}: no mla_grid_overlay stage")
        frame = self.cam.source.read_full_mono()
        if frame is None:
            self._stop.wait(0.2)
            return
        image = frame.data
        h, w = image.shape[:2]
        # The one place the preview-vs-sensor scale is applied. Everything
        # downstream -- crops, corner coordinates, the overlay -- is in this
        # frame's own pixels.
        geom = stage.geometry_for(w, h)
        scale = float(stage.params.crop_scale)
        derot = bool(getattr(stage.params, "derotate_views", False))

        # Second line of defence. The tile count is a precondition, checked
        # before the run starts -- but the MLA parameters are live objects and
        # nothing stops a pitch being changed underneath a running detector.
        # Refusing here turns "the machine became unresponsive" into a message.
        tiles = geom.whole_indices(scale, derotate=derot)
        if len(tiles) > self.max_tiles:
            raise RuntimeError(
                f"grid now yields {len(tiles)} whole tiles, over the limit of "
                f"{self.max_tiles}. The pitch changed while detection was "
                f"running. Stopping rather than saturating the CPU."
            )

        result = self.detector.run(
            image, geom, self.cam.cam_id, frame.seq, scale, derot, tiles=tiles
        )
        if self.annotate_overlay:
            try:
                result.overlay = annotate(image, result, geom, scale)
            except Exception:
                log.exception("%s: overlay failed", self.cam.cam_id)
        self.passes += 1
        self._publish(result)

    def _publish(self, result: DetectionResult) -> None:
        with self._lock:
            self._latest = result
