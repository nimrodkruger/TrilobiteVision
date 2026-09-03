"""The hands-free capture session.

**The constraint that shaped all of this: both of your hands are on the board.**
You cannot press a key, you cannot pick which part of the array to fill next,
and you are looking at the board rather than at the screen. Every design choice
below follows from that.

So the rig watches and decides:

    SEARCHING   too few micro-images show the pattern       "show the board"
    HOLD        it does; waiting for the picture to settle  "hold still"
    VERIFY      full frames grabbed, real corners checked    (about 100 ms)
    REVIEW      a pose was kept; the shot is on screen      "escape to discard"
    MOVE        it was kept; waiting for you to move on     "move the board"

and announces each transition, because the screen is not where you are looking.
The web layer turns these into tones.

Three things are worth explaining because they are not the obvious choices.

**Settling is measured on the presence map, not on the image.** Two consecutive
presence maps differing by less than a few percent means the board is not
moving relative to the lenslet array -- which is the thing that matters -- and
it costs a vector subtraction. Frame differencing would also catch a passing
shadow; optical flow would cost more than the detector.

**Movement is measured the same way.** After a pose is kept, another is refused
until the presence map has changed substantially. That is the live counterpart
of calibration-ui-spec §5.2: without it, standing still banks fifty identical
poses and biases the fit toward wherever you happened to rest. The offline
duplicate check still uses PnP translation and rotation; this one needs no
solve and is available on every frame.

**Only VERIFY costs anything.** Everything above reads a map the preview
pipeline has already computed. VERIFY asks the capture thread for full frames
and runs `findChessboardCornersSB` on a five-tile cross -- the acceptance rule's
minimum, about 45 ms per camera -- rather than on all 130 tiles, which is what
made the earlier design impossible on a Pi. Full-field detection happens
offline, on the frames this records.

**Nothing is deleted.** Discarding a pose marks it in the index and leaves the
files. A pose you rejected by reflex and want back later is worth more than the
3 MB it occupies.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from ..storage.writer import verify_size, write_durably
from ..types import Frame
from .detect import CornerDetector
from .settings import CalibrationSettings

log = logging.getLogger(__name__)

CROSS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))

# Per-micro-image saddle-count fluctuation that means nothing: a marginal peak
# clearing or missing the threshold from frame to frame. Differences smaller
# than this are discarded before the stillness and movement tests, because
# across a hundred and thirty tiles they otherwise sum to about the size of a
# real change. See CaptureSession._relative_change.
COUNT_NOISE = 2.0


class Phase(StrEnum):
    IDLE = "idle"
    SEARCHING = "searching"
    HOLD = "hold"
    VERIFY = "verify"
    REVIEW = "review"
    MOVE = "move"
    STOPPED = "stopped"


# What the operator is told, in the fewest words that fit on a banner read from
# arm's length while holding something with both hands.
BANNER = {
    Phase.IDLE: ("READY", "press start"),
    Phase.SEARCHING: ("SHOW THE BOARD", "not enough micro-images see the pattern"),
    Phase.HOLD: ("HOLD STILL", "settling"),
    Phase.VERIFY: ("CHECKING", ""),
    Phase.REVIEW: ("CAPTURED", "escape to discard"),
    Phase.MOVE: ("MOVE THE BOARD", "somewhere new"),
    Phase.STOPPED: ("FINISHED", ""),
}


@dataclass
class Pose:
    """One accepted capture, across all cameras."""

    index: int
    t_wall: float
    directory: str
    cameras: dict[str, Any] = field(default_factory=dict)
    discarded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "t_wall": self.t_wall,
            "directory": self.directory,
            "discarded": self.discarded,
            "cameras": self.cameras,
        }


@dataclass
class Event:
    """Something the operator should hear about."""

    seq: int
    kind: str            # armed | captured | rejected | discarded | stopped | warning
    text: str
    t_wall: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "text": self.text, "t": self.t_wall}


def _square_px(corners: np.ndarray) -> float:
    """Apparent checkerboard square size, from detected corners.

    A depth proxy that costs nothing: the board's squares subtend fewer pixels
    the further away it is, so the spread of this number across a session is
    the spread of working distances. It matters because kappa and D enter the
    model only through kappa/(Z - D) and are one parameter at a single depth --
    a fit at one distance converges perfectly and is wrong (calibration-spec
    §2.5). The real distances come from PnP offline; this is what makes the
    problem visible while there is still time to fix it.
    """
    if corners is None or len(corners) < 2:
        return 0.0
    d = np.linalg.norm(corners[:, None, :] - corners[None, :, :], axis=2)
    np.fill_diagonal(d, np.inf)
    return float(np.median(d.min(axis=1)))


def review_image(image: np.ndarray, result: dict[str, Any], width: int = 728) -> np.ndarray:
    """The shot, with its cross and corners drawn, for the confirmation pause.

    Downscaled to display width before drawing, but the corners are placed from
    their full-resolution coordinates, so what you see carries the precision of
    the measurement rather than of the display.
    """
    from .detect import load_cv2  # noqa: PLC0415

    cv2 = load_cv2()
    h, w = image.shape[:2]
    s = min(1.0, width / float(w)) if w else 1.0
    view = cv2.resize(image, (int(round(w * s)), int(round(h * s))),
                      interpolation=cv2.INTER_AREA)
    view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR) if view.ndim == 2 else view

    g = result.get("geometry") or {}
    side = int(round(g.get("pitch_px", 0) * g.get("crop_scale", 1.0) * s))
    for i, j in result.get("cross", []):
        # Reconstruct the tile box from the recorded geometry rather than
        # holding on to the MLAGeometry object: this must still draw correctly
        # if it is ever fed a pose loaded back from disk.
        cx = g.get("width", w) / 2.0 - 0.5 + g.get("offset_x", 0.0)
        cy = g.get("height", h) / 2.0 - 0.5 + g.get("offset_y", 0.0)
        t = np.radians(g.get("rotation_deg", 0.0))
        p = float(g.get("pitch_px", 0.0))
        x = cx + p * (i * np.cos(t) - j * np.sin(t))
        y = cy + p * (i * np.sin(t) + j * np.cos(t))
        p0 = (int(round((x * s) - side / 2)), int(round((y * s) - side / 2)))
        p1 = (p0[0] + side, p0[1] + side)
        cv2.rectangle(view, p0, p1, (255, 220, 60), 2)

    for pts in result.get("corners", {}).values():
        for x, y in pts:
            cv2.circle(view, (int(round(x * s)), int(round(y * s))), 2, (60, 220, 255), -1)

    banner = (f"{result.get('cam_id', '')}  {result.get('found', 0)} cross tiles  "
              f"square {result.get('square_px', 0):.1f} px")
    cv2.putText(view, banner, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (40, 255, 160), 1, cv2.LINE_AA)
    return view


class CaptureSession:
    """The state machine, its recorder, and the numbers the console shows."""

    def __init__(
        self,
        cameras: dict[str, Any],
        settings: CalibrationSettings,
        root: Path,
        storage_free_bytes=None,
    ) -> None:
        self.cameras = cameras
        self.settings = settings
        self.spec = settings.capture
        self.root = Path(root)
        self._free_bytes = storage_free_bytes or (lambda: shutil.disk_usage(self.root).free)

        self.detector = CornerDetector(
            settings.board, settings.acceptance,
            normalize=settings.detection.normalize_illumination,
            accuracy=True,   # once per pose, so the extra refinement is free
        )

        self.phase = Phase.IDLE
        self.phase_since = time.monotonic()
        self.poses: list[Pose] = []
        self.events: deque[Event] = deque(maxlen=32)
        self.note = ""
        self.started_at = 0.0
        self._event_seq = 0
        self._still_run = 0
        self._last_sig: dict[str, np.ndarray] = {}
        self._armed_sig: dict[str, np.ndarray] = {}
        self._force = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.dir: Path | None = None
        # Per-lattice-cell accepted-pose counts, for the coverage display.
        self.coverage: dict[str, dict[tuple[int, int], int]] = {}
        self.last_shot: dict[str, np.ndarray] = {}     # annotated review images

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self.dir = self.root / time.strftime("calibration_%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest()
        self.started_at = time.time()
        self.poses.clear()
        self.coverage = {cid: {} for cid in self.cameras}
        self._stop.clear()
        self._set_phase(Phase.SEARCHING)
        self._emit("armed", "session started")
        self._thread = threading.Thread(target=self._run, name="capture-session", daemon=True)
        self._thread.start()
        log.info("calibration session recording to %s", self.dir)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._set_phase(Phase.STOPPED)
        self._emit("stopped", f"{self.kept} poses kept")
        self._write_index()
        log.info("calibration session finished: %d poses in %s", self.kept, self.dir)

    def force(self) -> None:
        """Take a shot now, whatever the state machine thinks.

        The keyboard override. You may be able to free a finger, or be using a
        presenter clicker as a shutter, and there are poses -- a board at an
        extreme angle, in a dim corner -- that the settle test will never arm on
        but that are the most valuable ones in the session.
        """
        self._force.set()

    def discard_last(self) -> bool:
        """Mark the most recent pose discarded. Files stay on disk."""
        with self._lock:
            for pose in reversed(self.poses):
                if not pose.discarded:
                    pose.discarded = True
                    for cid, info in pose.cameras.items():
                        for key in info.get("cross", []):
                            k = tuple(key)
                            if self.coverage.get(cid, {}).get(k):
                                self.coverage[cid][k] -= 1
                    self._emit("discarded", f"pose {pose.index} discarded")
                    self._write_index()
                    return True
        return False

    # -- what the console shows -------------------------------------------

    @property
    def kept(self) -> int:
        return sum(1 for p in self.poses if not p.discarded)

    def state(self) -> dict[str, Any]:
        title, hint = BANNER[self.phase]
        cams: dict[str, Any] = {}
        board = (self.settings.board.cols, self.settings.board.rows)
        for cid, cam in self.cameras.items():
            stage = cam.presence_stage()
            result = getattr(stage, "result", None) if stage else None
            thresh = int(stage.params.min_corners) if stage else 0
            cams[cid] = {
                "diagnosis": (
                    result.diagnose(thresh, board) if result else
                    {"why": ("no presence map. The checkerboard_presence stage is "
                             "missing, disabled, or not yet producing frames."),
                     "seeing": 0, "best": 0, "threshold": thresh, "median": 0,
                     "whole": 0, "total_peaks": 0, "strength": 0,
                     "expected_for_board": (board[0] + 2) * (board[1] + 2)}
                ),
                "seeing": len(result.seeing(thresh)) if result else 0,
                "whole": result.n_whole if result else 0,
                "strength": round(result.strength) if result else 0,
                "counts": result.counts.tolist() if result else [],
                "whole_mask": result.whole.tolist() if result else [],
                "origin": list(result.origin) if result else [0, 0],
                "coverage": [[i, j, n] for (i, j), n in self.coverage.get(cid, {}).items()],
            }
        # One number per POSE, not per camera. The two heads see the same board
        # at the same distance, so counting both would double the apparent
        # sample size of the one distribution this is meant to police.
        depths = []
        for pose in self.poses:
            if pose.discarded:
                continue
            per_cam = [c["square_px"] for c in pose.cameras.values() if c.get("square_px")]
            if per_cam:
                depths.append(float(np.mean(per_cam)))
        return {
            "running": self.running,
            "phase": self.phase.value,
            "title": title,
            "hint": self.note or hint,
            "since_s": round(time.monotonic() - self.phase_since, 2),
            "poses": self.kept,
            "recorded": len(self.poses),
            "directory": str(self.dir) if self.dir else None,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "review_s": float(self.spec.review_s),
            "depth_px": [round(d, 1) for d in depths],
            "depth_spread": round(float(np.std(depths)), 2) if len(depths) > 1 else 0.0,
            "events": [e.as_dict() for e in self.events],
            "cameras": cams,
        }

    # -- the loop ----------------------------------------------------------

    def _set_phase(self, phase: Phase, note: str = "") -> None:
        if phase != self.phase:
            self.phase = phase
            self.phase_since = time.monotonic()
        self.note = note

    def _emit(self, kind: str, text: str) -> None:
        self._event_seq += 1
        self.events.append(Event(self._event_seq, kind, text))
        log.info("session: %s -- %s", kind, text)

    def _run(self) -> None:
        period = 1.0 / max(float(self.spec.tick_hz), 1.0)
        while not self._stop.wait(period):
            try:
                self._tick()
            except Exception as exc:
                log.exception("capture tick failed")
                self._emit("warning", f"{type(exc).__name__}: {exc}")
                self._set_phase(Phase.SEARCHING, str(exc))
                self._stop.wait(1.0)

    def _presence(self) -> dict[str, Any]:
        out = {}
        for cid, cam in self.cameras.items():
            stage = cam.presence_stage()
            if stage is not None and stage.result is not None:
                out[cid] = (stage.result, int(stage.params.min_corners))
        return out

    @staticmethod
    def _relative_change(a: np.ndarray, b: np.ndarray) -> float:
        """How different two presence maps are, ignoring counting noise.

        The dead band is not a nicety. A saddle count fluctuates by one or two
        per micro-image between frames purely from sensor noise deciding
        whether a marginal peak clears the threshold, and across a hundred and
        thirty tiles that sums to about five percent of the total -- which is
        the same order as a real stillness threshold. Without the dead band a
        perfectly motionless board reads as moving and the loop never arms.
        Observed on the synthetic rig before it was added: "0/4 still",
        indefinitely, with nothing moving.

        Real movement is nothing like that size: a micro-image that stops
        seeing the board loses its whole count, twenty or thirty at once.
        """
        if a is None or b is None or a.shape != b.shape:
            return 1.0
        delta = np.abs(a - b) - COUNT_NOISE
        np.maximum(delta, 0.0, out=delta)
        scale = max(float(np.abs(a).sum()), 1.0)
        return float(delta.sum() / scale)

    def _tick(self) -> None:
        forced = self._force.is_set()
        maps = self._presence()

        if self.phase is Phase.REVIEW:
            if time.monotonic() - self.phase_since >= float(self.spec.review_s):
                self._set_phase(Phase.MOVE)
            return

        seen = {cid: len(m.seeing(t)) for cid, (m, t) in maps.items()}
        need = int(self.spec.min_tiles_seeing)
        enough = (
            bool(seen) and (all(v >= need for v in seen.values())
                            if self.spec.require_all_cameras
                            else any(v >= need for v in seen.values()))
        )

        sigs = {cid: m.signature() for cid, (m, _t) in maps.items()}

        if self.phase is Phase.MOVE and not forced:
            moved = max(
                (self._relative_change(sigs.get(cid), self._armed_sig.get(cid))
                 for cid in sigs), default=0.0
            )
            if moved >= float(self.spec.move_threshold):
                self._set_phase(Phase.SEARCHING)
            else:
                want = float(self.spec.move_threshold)
                self._set_phase(Phase.MOVE, f"moved {moved:.0%} of the {want:.0%} needed")
                self._last_sig = sigs
                return

        if not enough and not forced:
            self._still_run = 0
            self._last_sig = sigs
            # Not "0/5 seeing", which is true and says nothing. The number that
            # localises the problem is how close the BEST micro-image got to the
            # threshold: 0 is a lens cap or a misplaced grid, 4 against 20 is a
            # board whose squares are too large, 18 against 20 is a threshold to
            # lower. Each has a different fix and they cost a session each to
            # tell apart when the console only reported the count.
            parts = []
            for cid, (m, thresh) in sorted(maps.items()):
                d = m.diagnose(thresh, (self.settings.board.cols, self.settings.board.rows))
                parts.append(f"{cid} {d['seeing']}/{need} seeing, best tile "
                             f"{d['best']}/{thresh} corners")
            self._set_phase(Phase.SEARCHING, "  ·  ".join(parts) if parts
                            else "no presence map -- is the stage enabled?")
            return

        change = max(
            (self._relative_change(sigs.get(cid), self._last_sig.get(cid)) for cid in sigs),
            default=1.0,
        )
        self._last_sig = sigs
        if change <= float(self.spec.still_threshold):
            self._still_run += 1
        else:
            self._still_run = 0

        if not forced and self._still_run < int(self.spec.settle_frames):
            self._set_phase(
                Phase.HOLD, f"{self._still_run}/{int(self.spec.settle_frames)} still")
            return

        self._force.clear()
        self._set_phase(Phase.VERIFY)
        self._attempt(sigs, forced)

    # -- the expensive part, once per pose ---------------------------------

    def _attempt(self, sigs: dict[str, np.ndarray], forced: bool) -> None:
        free = self._free_bytes()
        if free < float(self.spec.min_free_gb) * 1e9:
            self._emit("warning", f"only {free / 1e9:.1f} GB free -- stopping")
            self._stop.set()
            self._set_phase(Phase.STOPPED, "out of space")
            return
        if self.spec.max_poses and self.kept >= int(self.spec.max_poses):
            self._emit("warning", f"pose limit {int(self.spec.max_poses)} reached")
            self._stop.set()
            self._set_phase(Phase.STOPPED, "pose limit reached")
            return

        results: dict[str, Any] = {}
        for cid, cam in self.cameras.items():
            frame = cam.grab_full(timeout=3.0)
            if frame is None:
                self._reject(f"{cid}: no full frame in 3 s")
                return
            results[cid] = self._check(cam, cid, frame)

        good = [cid for cid, r in results.items() if r["ok"]]
        need_all = bool(self.spec.require_all_cameras)
        if not good or (need_all and len(good) < len(self.cameras)):
            worst = min(results.values(), key=lambda r: r["found"])
            need = int(self.settings.acceptance.min_cross_tiles)
            self._reject(
                f"{worst['cam_id']}: {worst['found']}/{need} cross tiles detected")
            return

        self._record(results, sigs, forced)

    def _check(self, cam, cid: str, frame: Frame) -> dict[str, Any]:
        """Corners on a five-tile cross, at full resolution. ~45 ms."""
        stage = cam.mla_stage()
        h, w = frame.data.shape[:2]
        geom = stage.geometry_for(w, h)
        scale = float(stage.params.crop_scale)
        centre = self._cross_centre(cam, geom, scale)
        out: dict[str, Any] = {
            "cam_id": cid, "ok": False, "found": 0, "centre": list(centre) if centre else None,
            "cross": [], "corners": {}, "square_px": 0.0, "frame": frame,
            "geometry": {
                "pitch_px": geom.pitch, "rotation_deg": geom.rotation_deg,
                "offset_x": geom.offset_x, "offset_y": geom.offset_y,
                "width": w, "height": h, "crop_scale": scale,
            },
        }
        if centre is None:
            return out

        whole = set(geom.whole_indices(scale, derotate=False))
        squares: list[float] = []
        for di, dj in CROSS:
            i, j = centre[0] + di, centre[1] + dj
            if (i, j) not in whole:
                continue
            found, pts = self.detector.detect_tile(geom.crop(frame.data, i, j, scale))
            if not found:
                continue
            corners = geom.tile_to_frame(i, j, pts, scale, derotate=False)
            out["cross"].append([i, j])
            out["corners"][f"{i},{j}"] = corners.round(3).tolist()
            squares.append(_square_px(corners))
        out["found"] = len(out["cross"])
        out["ok"] = out["found"] >= int(self.settings.acceptance.min_cross_tiles)
        out["square_px"] = round(float(np.median(squares)), 2) if squares else 0.0
        return out

    def _cross_centre(self, cam, geom, scale: float) -> tuple[int, int] | None:
        """Where to look. The best-covered micro-image that has four neighbours.

        Chosen from the presence map rather than fixed at the array centre: the
        board is wherever you are holding it, and testing the middle of the
        array when the board is in a corner would reject every pose that only
        covers an edge -- which are exactly the poses the fit is short of.
        """
        stage = cam.presence_stage()
        result = getattr(stage, "result", None) if stage else None
        if result is None:
            return None
        seeing = set(result.seeing(int(stage.params.min_corners)))
        if not seeing:
            return None
        candidates = [
            (result.count_at(i, j), i, j) for (i, j) in seeing
            if all((i + di, j + dj) in seeing for di, dj in CROSS[1:])
        ]
        if not candidates:
            # No complete cross anywhere. Fall back to the strongest tile so the
            # verify step still reports how close it got, rather than silently
            # doing nothing.
            best = max(seeing, key=lambda ij: (result.count_at(*ij), ij))
            return best
        return max(candidates)[1:]

    # -- recording ---------------------------------------------------------

    def _reject(self, why: str) -> None:
        self._emit("rejected", why)
        self._set_phase(Phase.SEARCHING, why)
        self._stop.wait(float(self.spec.reject_cooldown_s))

    def _record(self, results: dict[str, Any], sigs: dict[str, np.ndarray], forced: bool) -> None:
        with self._lock:
            index = len(self.poses) + 1
            pose_dir = self.dir / f"pose_{index:04d}"
            pose_dir.mkdir(parents=True, exist_ok=True)
            entry = Pose(index=index, t_wall=time.time(), directory=pose_dir.name)

            for cid, r in results.items():
                frame: Frame = r.pop("frame")
                # The review image is built from the frame that was measured,
                # with the corners drawn where they were found. During the pause
                # you are looking at the actual evidence, not at a later preview
                # frame that merely resembles it.
                self.last_shot[cid] = review_image(frame.data, r)
                # Durable, and verified. A pose written into the page cache and
                # never flushed is a pose that is not there -- see the header of
                # storage/writer.py for the session this cost.
                buf = io.BytesIO()
                np.save(buf, frame.data, allow_pickle=False)
                body = buf.getvalue()
                write_durably(pose_dir / f"{cid}.npy", body)
                verify_size(pose_dir / f"{cid}.npy", len(body))
                sidecar = {
                    "cam_id": cid,
                    "seq": frame.seq,
                    "t_wall": frame.t_wall,
                    "shape": list(frame.data.shape),
                    "dtype": str(frame.data.dtype),
                    "space": frame.space,
                    "forced": forced,
                    "sensor": {k: v for k, v in frame.meta.items()
                               if k in ("ExposureTime", "AnalogueGain", "DigitalGain",
                                        "SensorTimestamp", "AeLocked")},
                    **{k: v for k, v in r.items() if k != "corners"},
                    "corners": r["corners"],
                }
                meta = json.dumps(sidecar, indent=2).encode("utf-8")
                write_durably(pose_dir / f"{cid}.json", meta)
                verify_size(pose_dir / f"{cid}.json", len(meta))
                entry.cameras[cid] = {k: v for k, v in r.items() if k != "corners"}
                for key in r["cross"]:
                    k = (int(key[0]), int(key[1]))
                    self.coverage.setdefault(cid, {})[k] = \
                        self.coverage.setdefault(cid, {}).get(k, 0) + 1

            self.poses.append(entry)
            self._armed_sig = sigs
            self._write_index()

        depth = next((c.get("square_px") for c in entry.cameras.values()), 0.0)
        self._emit("captured", f"pose {index} kept -- square {depth:.1f} px")
        self._set_phase(Phase.REVIEW)

    def _write_manifest(self) -> None:
        """Everything needed to interpret the poses, written before the first one.

        The frozen MLA geometry is the part not to omit: a corner list without
        the crop geometry that produced it cannot be interpreted, and that is
        the single most likely thing to be missing in six months.
        """
        geoms = {}
        for cid, cam in self.cameras.items():
            stage = cam.mla_stage()
            if stage is None:
                continue
            info = cam.source.describe()
            w, h = info.full_resolution
            g = stage.geometry_for(w, h)
            geoms[cid] = {
                "sensor": [w, h],
                "reference": list(stage.reference_shape or (w, h)),
                "pitch_px": g.pitch, "rotation_deg": g.rotation_deg,
                "offset_x": g.offset_x, "offset_y": g.offset_y,
                "crop_scale": float(stage.params.crop_scale),
                "derotate_views": bool(stage.params.derotate_views),
                "camera": info.as_dict(),
            }
        manifest = json.dumps({
            "started": time.time(),
            "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "settings": self.settings.model_dump(),
            "mla_geometry_sensor_px": geoms,
            "note": "Corners are in FULL-RESOLUTION sensor coordinates. "
                    "Full-field detection and the fit are done offline from the "
                    "frames in each pose directory; the cross recorded here is "
                    "only the acceptance check that let the pose be kept.",
        }, indent=2).encode("utf-8")
        write_durably(self.dir / "session.json", manifest)
        verify_size(self.dir / "session.json", len(manifest))

    def _write_index(self) -> None:
        if self.dir is None:
            return
        lines = [json.dumps(p.as_dict()) for p in self.poses]
        body = ("\n".join(lines) + "\n").encode("utf-8")
        write_durably(self.dir / "poses.jsonl", body)
        verify_size(self.dir / "poses.jsonl", len(body))
