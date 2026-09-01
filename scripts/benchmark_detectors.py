#!/usr/bin/env python3
"""How expensive is each way of asking "is the board in this micro-image?"

Run it on the Pi. Every estimate in this project's design notes so far has been
an x86 measurement multiplied by a guess, and the guess has now been wrong
twice, so this exists to replace it with the number from the machine that has
to do the work:

    source ~/.venvs/trilobite/bin/activate
    python scripts/benchmark_detectors.py

It needs no cameras. It renders the same synthetic lenslet array the desktop
config uses -- a complete checkerboard in every micro-image -- and times four
approaches on it, then prints what each would cost with two cameras at 1 Hz.

The four:

  1. SB corners, every tile          what the calibration dashboard does now
  2. SB corners, one 5-tile cross    the same detector, on the acceptance rule's
                                     minimum, once per pose rather than always
  3. saddle map, full resolution     corner *counting* without corner finding
  4. saddle map, preview resolution  the same, on frames the pipeline already has

Approaches 3 and 4 answer a weaker question -- how many corner-like features
are in each micro-image, not where they are -- which happens to be the exact
question the live display asks. Their cost does not depend on the number of
tiles, which also means a mis-set pitch cannot make them expensive.

Add --detail for the discrimination numbers: what the saddle count reads on a
board, on a scene with no board, on a defocused board, and under noise.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import cv2
except ImportError:
    raise SystemExit(
        "OpenCV (cv2) is needed. Pi: sudo apt install -y python3-opencv, with a "
        "--system-site-packages venv. Desktop: pip install -e '.[desktop]'."
    ) from None

from trilobite.calibration.detect import CornerDetector  # noqa: E402
from trilobite.calibration.settings import AcceptanceSpec, BoardSpec  # noqa: E402
from trilobite.cameras.offline import SyntheticSource  # noqa: E402
from trilobite.config import CameraConfig  # noqa: E402
from trilobite.optics.mla import MLAGeometry  # noqa: E402

FULL = (1456, 1088)
PREVIEW = (728, 544)
PITCH = 100.0
BOARD = (4, 3)


# --------------------------------------------------------------------------
# The cheap detector, in full. It is this short.
# --------------------------------------------------------------------------


def tile_labels(geom: MLAGeometry, shape, scale: float = 1.0):
    """Pixel -> tile index, -1 outside any tile.

    Depends only on the MLA parameters and the frame size, so it is built once
    and reused until one of them changes -- the same caching the grid overlay
    already does.
    """
    a, b = geom.normalised(shape)
    i, j = np.round(a), np.round(b)
    inside = (np.abs(a - i) <= 0.5 * scale) & (np.abs(b - j) <= 0.5 * scale)
    ni, nj = i.astype(np.int32), j.astype(np.int32)
    i0, j0 = int(ni.min()), int(nj.min())
    w = int(ni.max()) - i0 + 1
    idx = (nj - j0) * w + (ni - i0)
    idx[~inside] = -1
    return idx, (i0, j0, w, int(nj.max()) - j0 + 1)


def saddle_peaks(img: np.ndarray, peak_win: int = 5, rel: float = 0.15):
    """Local maxima of the saddle response, over the whole frame at once.

    A checkerboard corner is a *saddle* of intensity -- curved up along one
    diagonal and down along the other -- and det(Hessian) < 0 is exactly that
    condition. Almost nothing else in a scene produces it strongly, which is
    why counting saddles is a usable proxy for counting checkerboard corners
    without solving the much harder problem of identifying which corner is
    which.

    Three separable convolutions and a dilation. No per-tile loop anywhere, so
    the cost is set by the frame size alone.
    """
    f = cv2.GaussianBlur(img, (3, 3), 0).astype(np.float32)
    ixx = cv2.Sobel(f, cv2.CV_32F, 2, 0, ksize=3)
    iyy = cv2.Sobel(f, cv2.CV_32F, 0, 2, ksize=3)
    ixy = cv2.Sobel(f, cv2.CV_32F, 1, 1, ksize=3)
    s = ixy * ixy - ixx * iyy
    np.maximum(s, 0, out=s)
    peak = cv2.dilate(s, np.ones((peak_win, peak_win), np.uint8))
    return (s >= peak) & (s > rel * float(s.max())), s


def counts_per_tile(mask, labels, ntiles):
    return np.bincount(labels.ravel()[mask.ravel()], minlength=ntiles)


# --------------------------------------------------------------------------


def make_source(pattern="plenoptic_board", rotation=0.0):
    src = SyntheticSource(CameraConfig(
        cam_id="bench", backend="synthetic",
        full_resolution=FULL, preview_resolution=PREVIEW,
        synthetic_pattern=pattern, synthetic_pitch_px=PITCH,
        synthetic_rotation_deg=rotation, synthetic_board=BOARD,
    ))
    src.open()
    return src


def timed(fn, repeats=5):
    fn()                                   # warm caches and any lazy init
    t0 = time.perf_counter()
    for _ in range(repeats):
        fn()
    return (time.perf_counter() - t0) / repeats * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cameras", type=int, default=2)
    ap.add_argument("--hz", type=float, default=1.0)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--detail", action="store_true",
                    help="also report how well the saddle count discriminates")
    args = ap.parse_args()

    src = make_source()
    full = src.read_full_mono().data
    preview = src.read_preview().data

    gf = MLAGeometry(width=FULL[0], height=FULL[1], pitch=PITCH)
    gp = MLAGeometry(width=PREVIEW[0], height=PREVIEW[1],
                     pitch=PITCH * PREVIEW[0] / FULL[0])
    whole = gf.whole_indices(1.0, derotate=False)
    cross = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]

    detector = CornerDetector(BoardSpec(cols=BOARD[0], rows=BOARD[1], square_mm=7.0),
                              AcceptanceSpec())
    lab_f, dims_f = tile_labels(gf, full.shape)
    lab_p, dims_p = tile_labels(gp, preview.shape)
    n_f, n_p = dims_f[2] * dims_f[3], dims_p[2] * dims_p[3]

    def sb_all():
        detector.run(full, gf, "bench", 1)

    def sb_cross():
        for i, j in cross:
            detector.detect_tile(gf.crop(full, i, j, 1.0))

    def saddle_full():
        counts_per_tile(saddle_peaks(full)[0], lab_f, n_f)

    def saddle_preview():
        counts_per_tile(saddle_peaks(preview)[0], lab_p, n_p)

    print(f"machine        : {' '.join(Path('/proc/device-tree/model').read_text().split())}"
          if Path("/proc/device-tree/model").exists() else "machine        : (not a Pi)")
    print(f"frame          : {FULL[0]}x{FULL[1]} full, {PREVIEW[0]}x{PREVIEW[1]} preview")
    print(f"grid           : {PITCH:.0f} px pitch -> {len(whole)} whole micro-images")
    print(f"budget         : {args.cameras} cameras at {args.hz:g} Hz\n")

    rows = [
        ("SB corners, all tiles, full res", sb_all, f"{len(whole)} tiles"),
        ("SB corners, 5-tile cross, full res", sb_cross, "5 tiles"),
        ("saddle map, full res", saddle_full, "all tiles"),
        ("saddle map, preview res", saddle_preview, "all tiles"),
    ]
    print(f"{'approach':40s} {'ms/pass':>9s} {'scope':>11s} {'% of one core':>15s}")
    print("-" * 78)
    for name, fn, scope in rows:
        ms = timed(fn, args.repeats)
        duty = args.cameras * args.hz * ms / 1000.0 * 100.0
        flag = "  <-- over budget" if duty > 100 else ""
        print(f"{name:40s} {ms:9.1f} {scope:>11s} {duty:14.1f}%{flag}")

    print(f"\nlabel image, built once per parameter change: "
          f"{timed(lambda: tile_labels(gf, full.shape), 2):.0f} ms full, "
          f"{timed(lambda: tile_labels(gp, preview.shape), 2):.0f} ms preview")

    if args.detail:
        print("\n--- what the saddle count actually reads "
              "(median over the whole micro-images) ---")
        cases = [
            ("board, preview", dict(), None),
            ("board, preview, 3 deg rotation", dict(rotation=3.0), None),
            ("board, preview, heavy noise", dict(), "noise"),
            ("board, preview, DEFOCUSED", dict(), "blur"),
            ("no board (grating scene)", dict(pattern="gratings"), None),
        ]
        for label, kw, damage in cases:
            s = make_source(**kw)
            img = s.read_preview().data
            if damage == "noise":
                rng = np.random.default_rng(0)
                img = np.clip(img + rng.normal(0, 12, img.shape), 0, 255).astype(np.uint8)
            elif damage == "blur":
                img = cv2.GaussianBlur(img, (7, 7), 0)
            g = MLAGeometry(width=img.shape[1], height=img.shape[0],
                            pitch=PITCH * img.shape[1] / FULL[0],
                            rotation_deg=kw.get("rotation", 0.0))
            lab, dims = tile_labels(g, img.shape)
            mask, strength = saddle_peaks(img)
            counts = counts_per_tile(mask, lab, dims[2] * dims[3])
            here = np.array([counts[(j - dims[1]) * dims[2] + (i - dims[0])]
                             for i, j in g.whole_indices(1.0, derotate=False)])
            print(f"  {label:34s} count {int(np.median(here)):4d} "
                  f"(min {here.min()}, max {here.max()})   "
                  f"peak strength {strength[mask].mean():9.0f}")
        print("\n  A board reads far above an empty scene, so a presence threshold is easy.")
        print("  A DEFOCUSED board reads HIGH too -- blur spreads the response and creates")
        print("  more local maxima -- so the count alone cannot judge focus. The peak")
        print("  strength can: it falls by roughly an order of magnitude, and it comes")
        print("  from the same convolutions at no extra cost.")

    print("\nFlat grey and lens-cap frames read 0. Whatever you decide, the numbers")
    print("above are this machine's, not an estimate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
