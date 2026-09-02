#!/usr/bin/env python3
"""Read a captured .npy and its .json sidecar, off the rig.

The desktop end of the split this project is built around: the Pi captures and
decides, the desktop looks and measures. Handles every file the rig writes.

    raw_left_000001_143022_918440.npy   full sensor data, ISP bypassed
    view_left_000002_143110_204871.npy  the processed preview as displayed
    pose_0007/left.npy                  one camera of a calibration pose

The three carry different sidecars and this works out which from the content,
so you never have to say which kind you have.

USAGE

    # what is in this file
    python scripts/read_capture.py raw_left_000001_143022_918440.npy

    # look at it, with the MLA grid the sidecar recorded drawn on top
    python scripts/read_capture.py <file>.npy --show --grid

    # save a viewable PNG (raw is often 10-bit; this stretches it)
    python scripts/read_capture.py <file>.npy --save out.png

    # pull one micro-image out and enlarge it
    python scripts/read_capture.py <file>.npy --tile 0,0 --save tile.png

    # THE ONE THAT MATTERS: full-field corner detection, every micro-image,
    # which is the job deliberately not done on the Pi
    python scripts/read_capture.py pose_0007/left.npy --detect --show

    # a whole session at once, to CSV
    python scripts/read_capture.py calibration_20260901_143000/ --detect \\
        --csv corners.csv

WHAT THE SIDECARS CARRY

A capture sidecar (`raw_*`/`view_*`) has `pipeline`, so the MLA parameters are
under `pipeline.mla` **in preview pixels** with `reference_width` alongside;
this script converts them to the frame it is looking at, the same way the rig
does. A pose sidecar has `geometry` already converted to sensor pixels, plus
the cross corners that let the pose be accepted.

Requires numpy, and OpenCV for --detect/--show/--save.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _cv2():
    try:
        import cv2
    except ImportError:
        raise SystemExit(
            "This needs OpenCV. Desktop: pip install -e '.[desktop]'. "
            "Pi: sudo apt install -y python3-opencv."
        ) from None
    return cv2


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@dataclass
class Capture:
    """A frame and everything the rig recorded about it."""

    path: Path
    image: np.ndarray
    meta: dict[str, Any]
    kind: str                      # "raw" | "view" | "pose" | "bare"

    # MLA geometry converted to THIS frame's pixels, or None if unrecorded.
    pitch: float | None = None
    rotation_deg: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    crop_scale: float = 1.0

    @property
    def has_geometry(self) -> bool:
        return self.pitch is not None and self.pitch > 1.0

    def geometry(self):
        """An MLAGeometry for this frame, from the recorded parameters."""
        from trilobite.optics.mla import MLAGeometry

        if not self.has_geometry:
            raise SystemExit(
                f"{self.path.name}: no MLA geometry in the sidecar. The stage was "
                f"probably disabled when this was captured, so there is nothing "
                f"to crop micro-images with."
            )
        h, w = self.image.shape[:2]
        return MLAGeometry(width=w, height=h, pitch=self.pitch,
                           rotation_deg=self.rotation_deg,
                           offset_x=self.offset_x, offset_y=self.offset_y)


def load(npy_path: Path) -> Capture:
    path = Path(npy_path)
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    image = np.load(path)
    side = path.with_suffix(".json")
    meta = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    if not side.exists():
        print(f"warning: no sidecar beside {path.name} — pixels only", file=sys.stderr)

    cap = Capture(path=path, image=image, meta=meta, kind="bare")
    h, w = image.shape[:2]

    if "geometry" in meta:
        # A calibration pose. Geometry is already in SENSOR pixels, recorded
        # at the resolution the detector ran on.
        g = meta["geometry"]
        cap.kind = "pose"
        cap.pitch = float(g.get("pitch_px", 0)) or None
        cap.rotation_deg = float(g.get("rotation_deg", 0.0))
        cap.offset_x = float(g.get("offset_x", 0.0))
        cap.offset_y = float(g.get("offset_y", 0.0))
        cap.crop_scale = float(g.get("crop_scale", 1.0))
        if g.get("width") and int(g["width"]) != w:
            print(f"warning: sidecar geometry is for {g['width']} px wide, this "
                  f"frame is {w} — rescaling", file=sys.stderr)
            s = w / float(g["width"])
            cap.pitch *= s
            cap.offset_x *= s
            cap.offset_y *= s
    else:
        cap.kind = str(meta.get("tag", "bare"))
        # A capture sidecar. The MLA stage's parameters are in PREVIEW pixels
        # and carry the resolution they were measured against, so they have to
        # be converted -- the same factor of two that has bitten this project
        # once already.
        mla = None
        for name, values in (meta.get("pipeline") or {}).items():
            if isinstance(values, dict) and "pitch_px" in values:
                mla = values
                if name == "mla":
                    break
        if mla:
            ref_w = int(mla.get("reference_width") or 0)
            scale = (w / ref_w) if ref_w else 1.0
            if ref_w and ref_w != w:
                print(f"note: MLA parameters recorded against a {ref_w} px wide "
                      f"frame, scaling by {scale:.3f} for this {w} px one")
            cap.pitch = float(mla["pitch_px"]) * scale or None
            cap.rotation_deg = float(mla.get("rotation_deg", 0.0))
            cap.offset_x = float(mla.get("offset_x", 0.0)) * scale
            cap.offset_y = float(mla.get("offset_y", 0.0)) * scale
            cap.crop_scale = float(mla.get("crop_scale", 1.0))
    return cap


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def describe(cap: Capture) -> None:
    img, m = cap.image, cap.meta
    print(f"file        : {cap.path}")
    print(f"kind        : {cap.kind}")
    print(f"array       : {img.shape} {img.dtype}")
    lo, hi = int(img.min()), int(img.max())
    print(f"levels      : min {lo}  max {hi}  mean {img.mean():.1f}  "
          f"std {img.std():.1f}")
    if img.dtype == np.uint8 and hi >= 255:
        frac = float((img >= 255).mean())
        print(f"              {frac:.2%} of pixels saturated")
    if not m:
        return

    print(f"camera      : {m.get('camera_label') or m.get('cam_id', '?')}"
          f"  ({m.get('cam_id', '?')})")
    if m.get("t_iso"):
        print(f"captured    : {m['t_iso']}")
    print(f"space       : {m.get('space', '?')}"
          + ("   <-- ISP bypassed, this is measurement data"
             if m.get("space") == "raw" else
             "   <-- processed for viewing, do NOT fit anything to it"
             if cap.kind == "view" else ""))

    sensor = m.get("sensor") or m.get("sensor_metadata") or {}
    keep = {k: sensor[k] for k in
            ("ExposureTime", "AnalogueGain", "DigitalGain", "AeLocked",
             "SensorTimestamp") if k in sensor}
    if keep:
        print("sensor      : " + "  ".join(f"{k}={v}" for k, v in keep.items()))

    if cap.has_geometry:
        print(f"MLA         : pitch {cap.pitch:.2f} px  rot {cap.rotation_deg:g}°  "
              f"offset ({cap.offset_x:.1f}, {cap.offset_y:.1f})  "
              f"crop ×{cap.crop_scale:g}")
        g = cap.geometry()
        whole = g.whole_indices(cap.crop_scale, derotate=False)
        print(f"              {len(whole)} whole micro-images of "
              f"{g.crop_side(cap.crop_scale)} px")
    else:
        print("MLA         : not recorded (stage disabled at capture time)")

    if cap.kind == "pose":
        print(f"accepted    : {m.get('ok')}  cross {m.get('cross')}")
        print(f"square      : {m.get('square_px')} px  "
              f"(apparent size — the depth proxy)")
        n = sum(len(v) for v in (m.get("corners") or {}).values())
        print(f"corners     : {n} recorded across {len(m.get('corners') or {})} tiles")
        if m.get("forced"):
            print("              taken with the keyboard override")

    pipe = m.get("pipeline") or {}
    if pipe and cap.kind != "pose":
        print("pipeline    : " + ", ".join(
            f"{k}({'on' if v.get('enabled', True) else 'off'})"
            for k, v in pipe.items() if isinstance(v, dict)))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def to_display(img: np.ndarray, stretch: bool = True) -> np.ndarray:
    """8-bit, viewable. Raw frames are often 10- or 12-bit in a 16-bit array,
    which renders as near-black unless it is scaled."""
    if img.dtype == np.uint8 and not stretch:
        return img
    a = img.astype(np.float32)
    lo, hi = float(np.percentile(a, 0.5)), float(np.percentile(a, 99.5))
    if hi - lo < 1e-6:
        hi = lo + 1.0
    return np.clip((a - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def draw(cap: Capture, grid: bool = False, corners: bool = False,
         detections: list[dict[str, Any]] | None = None,
         stretch: bool = True) -> np.ndarray:
    cv2 = _cv2()
    view = to_display(cap.image, stretch)
    view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR) if view.ndim == 2 else view

    if grid and cap.has_geometry:
        g = cap.geometry()
        side = g.crop_side(cap.crop_scale)
        for i, j in g.whole_indices(cap.crop_scale, derotate=False):
            x0, y0 = g.crop_origin(i, j, cap.crop_scale)
            cv2.rectangle(view, (x0, y0), (x0 + side, y0 + side), (70, 90, 120), 1)

    if corners:
        for pts in (cap.meta.get("corners") or {}).values():
            for x, y in pts:
                cv2.circle(view, (int(round(x)), int(round(y))), 3, (60, 220, 255), -1)

    for d in detections or []:
        x0, y0 = d["origin"]
        side = d["side"]
        cv2.rectangle(view, (x0, y0), (x0 + side, y0 + side), (90, 210, 120), 2)
        for x, y in d["corners"]:
            cv2.circle(view, (int(round(x)), int(round(y))), 2, (60, 220, 255), -1)
    return view


# --------------------------------------------------------------------------
# Full-field detection — the job the Pi deliberately does not do
# --------------------------------------------------------------------------


def detect_all(cap: Capture, cols: int, rows: int, accuracy: bool = True,
               normalize: bool = False) -> list[dict[str, Any]]:
    """Corners in every whole micro-image.

    About a third of a second per frame on a desktop and a second on a Pi,
    which is why this lives here and not on the rig. Corner positions come back
    in full-frame coordinates, matching what the session records for its
    five-tile cross, so the two are directly comparable.
    """
    from trilobite.calibration.detect import CornerDetector
    from trilobite.calibration.settings import AcceptanceSpec, BoardSpec

    det = CornerDetector(BoardSpec(cols=cols, rows=rows, square_mm=1.0),
                         AcceptanceSpec(), normalize=normalize, accuracy=accuracy)
    g = cap.geometry()
    scale = cap.crop_scale
    out: list[dict[str, Any]] = []
    for i, j in g.whole_indices(scale, derotate=False):
        found, pts = det.detect_tile(g.crop(cap.image, i, j, scale))
        if not found:
            continue
        xy = g.tile_to_frame(i, j, pts, scale, derotate=False)
        out.append({
            "tile": [i, j],
            "origin": list(g.crop_origin(i, j, scale)),
            "side": g.crop_side(scale),
            "corners": xy.round(4).tolist(),
            "square_px": round(_nn_spacing(xy), 3),
        })
    return out


def _nn_spacing(xy: np.ndarray) -> float:
    if len(xy) < 2:
        return 0.0
    d = np.linalg.norm(xy[:, None, :] - xy[None, :, :], axis=2)
    np.fill_diagonal(d, np.inf)
    return float(np.median(d.min(axis=1)))


def cross_count(detections: list[dict[str, Any]]) -> int:
    """How many micro-images sit at the centre of a complete cross of five."""
    found = {tuple(d["tile"]) for d in detections}
    return sum(
        1 for (i, j) in found
        if all((i + di, j + dj) in found for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    )


# --------------------------------------------------------------------------


def board_from_session(path: Path) -> tuple[int, int] | None:
    """Read the board size out of a session manifest beside a pose."""
    for parent in (path.parent, path.parent.parent):
        manifest = parent / "session.json"
        if manifest.exists():
            try:
                b = json.loads(manifest.read_text(encoding="utf-8"))["settings"]["board"]
                return int(b["cols"]), int(b["rows"])
            except (KeyError, ValueError, json.JSONDecodeError):
                return None
    return None


def gather(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files = sorted(target.rglob("*.npy"))
    if not files:
        raise SystemExit(f"no .npy files under {target}")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path,
                    help=".npy file, or a directory to walk (a pose or session folder)")
    ap.add_argument("--show", action="store_true", help="open a window")
    ap.add_argument("--save", type=Path, help="write a PNG (a directory for many inputs)")
    ap.add_argument("--grid", action="store_true", help="draw the recorded MLA grid")
    ap.add_argument("--corners", action="store_true",
                    help="draw the corners recorded in a pose sidecar")
    ap.add_argument("--tile", metavar="I,J",
                    help="extract one micro-image instead of the whole frame")
    ap.add_argument("--zoom", type=int, default=6, help="scale for --tile (default 6)")
    ap.add_argument("--detect", action="store_true",
                    help="find corners in EVERY micro-image (needs --board or a session)")
    ap.add_argument("--board", metavar="COLSxROWS",
                    help="board inner corners, e.g. 4x3. Read from session.json if absent")
    ap.add_argument("--normalize", action="store_true",
                    help="CALIB_CB_NORMALIZE_IMAGE — for unevenly lit micro-images")
    ap.add_argument("--csv", type=Path, help="write all detected corners to a CSV")
    ap.add_argument("--json", dest="json_out", type=Path, help="write detections as JSON")
    ap.add_argument("--no-stretch", action="store_true",
                    help="do not rescale levels for display")
    args = ap.parse_args()

    files = gather(args.path)
    many = len(files) > 1
    if many and args.save and not args.save.is_dir():
        args.save.mkdir(parents=True, exist_ok=True)

    rows_csv: list[list[Any]] = []
    all_detections: dict[str, Any] = {}

    for n, f in enumerate(files):
        if many:
            print("=" * 70)
        cap = load(f)
        describe(cap)

        detections = None
        if args.detect:
            board = None
            if args.board:
                try:
                    c, r = args.board.lower().split("x")
                    board = (int(c), int(r))
                except ValueError:
                    raise SystemExit("--board wants COLSxROWS, e.g. 4x3") from None
            board = board or board_from_session(f)
            if board is None:
                raise SystemExit(
                    "--detect needs the board size. Pass --board 4x3, or run this "
                    "inside a session folder that has a session.json.")
            detections = detect_all(cap, board[0], board[1], normalize=args.normalize)
            g = cap.geometry()
            whole = len(g.whole_indices(cap.crop_scale, derotate=False))
            squares = [d["square_px"] for d in detections if d["square_px"]]
            print(f"detected    : {len(detections)}/{whole} micro-images, "
                  f"{cross_count(detections)} complete crosses"
                  + (f", square {np.median(squares):.2f} px" if squares else ""))
            key = str(f)
            all_detections[key] = {"board": list(board), "tiles": detections}
            for d in detections:
                for k, (x, y) in enumerate(d["corners"]):
                    rows_csv.append([f.name, cap.meta.get("cam_id", ""),
                                     d["tile"][0], d["tile"][1], k, x, y])

        if args.tile:
            try:
                i, j = (int(v) for v in args.tile.split(","))
            except ValueError:
                raise SystemExit("--tile wants I,J, e.g. --tile 0,0") from None
            cv2 = _cv2()
            g = cap.geometry()
            tile = g.crop(cap.image, i, j, cap.crop_scale)
            if tile.size == 0:
                raise SystemExit(f"micro-image ({i}, {j}) falls outside the frame")
            out = to_display(tile, not args.no_stretch)
            out = cv2.resize(out, None, fx=args.zoom, fy=args.zoom,
                             interpolation=cv2.INTER_NEAREST)
            print(f"tile        : ({i}, {j})  {tile.shape} -> {out.shape}")
            image = out
        else:
            image = draw(cap, grid=args.grid, corners=args.corners,
                         detections=detections, stretch=not args.no_stretch)

        if args.save:
            cv2 = _cv2()
            dest = (args.save / f"{f.stem}.png") if (many or args.save.is_dir()) else args.save
            dest.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(dest), image)
            print(f"wrote       : {dest}")
        if args.show:
            cv2 = _cv2()
            cv2.imshow(f.name, image)
            print("(any key for the next image, q to stop)")
            if (cv2.waitKey(0) & 0xFF) in (ord("q"), 27):
                cv2.destroyAllWindows()
                break
            cv2.destroyAllWindows()
        if not many and n == 0 and not (args.show or args.save):
            pass

    if args.csv and rows_csv:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "cam_id", "tile_i", "tile_j", "corner", "x_px", "y_px"])
            w.writerows(rows_csv)
        print(f"\nwrote {len(rows_csv)} corners to {args.csv}")

    if args.json_out and all_detections:
        args.json_out.write_text(json.dumps(all_detections, indent=2), encoding="utf-8")
        print(f"wrote detections to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
