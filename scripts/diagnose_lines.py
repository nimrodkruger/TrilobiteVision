#!/usr/bin/env python3
"""Tell apart the four things that make horizontal lines in a frame.

"Lines across the image, like a broken screen" is a symptom with several very
different causes, and swapping cables tests only one of them. Each cause leaves
a different signature in the pixels, and this measures all four so the guessing
stops:

  1. SENSOR ROW NOISE. Every row of a CMOS sensor is read through its own
     amplifier chain, so each row carries a small DC offset. On the IMX296 that
     is a couple of counts at unity gain -- invisible until you stretch the
     contrast, which a dark or low-contrast frame does for you. Signature: the
     per-row offsets are small, roughly Gaussian, DIFFERENT in every frame, and
     the pixels within a row are otherwise fine.

  2. FIXED-PATTERN ROW OFFSET. The same thing but not random: the same rows are
     off by the same amount in every frame. Signature: per-row offsets that
     correlate strongly frame to frame. This one is correctable by subtracting
     a dark frame; the others are not.

  3. A TRANSPORT FAULT -- a marginal CSI-2 link, a bad cable, a connector not
     seated. When bytes are dropped, the rest of the row arrives shifted
     sideways, so the row is not noisy, it is DISPLACED. Signature: a bad row
     correlates far better with its neighbours at a non-zero lateral shift than
     at zero. This is the one a cable swap is meant to fix, and it is the one
     with a second, independent witness: the kernel counts the errors, so
     `dmesg` will say so. Nothing else here does.

  4. A DECODE OR STRIDE ERROR -- reading a buffer at the wrong bytes per pixel,
     or with the row padding still on. Signature: not lines at all but a
     progressive diagonal shear, every row displaced one step further than the
     last, and the shift accumulates linearly with row number.

USAGE

    python scripts/diagnose_lines.py <file>.npy
    python scripts/diagnose_lines.py <directory>          # several frames
    python scripts/diagnose_lines.py <dir> --pattern 'raw_left_*.npy'

Several frames is much better than one: it is the only way to separate cause 1
from cause 2, and it makes the shift test far less likely to be fooled by a
scene that genuinely has horizontal structure.

Requires numpy only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

# A row offset worth reporting, in counts. Below about one count the estimate
# is dominated by the scene, not by the sensor.
NOTABLE_OFFSET = 1.0
# How much worse a row has to be than the typical one before it is called out
# individually, in robust standard deviations.
OUTLIER_SIGMA = 6.0
# Lateral shifts searched when testing for a dropped-byte displacement.
MAX_SHIFT = 24


def load(path: Path) -> np.ndarray:
    """The pixels, with raw stride padding removed if the sidecar allows it."""
    img = np.load(path)
    side = path.with_suffix(".json")
    if not side.exists():
        return img
    meta = json.loads(side.read_text(encoding="utf-8"))
    full = (meta.get("camera") or {}).get("full_resolution")
    if not full or img.ndim < 2:
        return img
    full_w, full_h = int(full[0]), int(full[1])
    h, w = img.shape[:2]
    if w == full_w or h != full_h:
        return img
    # Same reasoning as scripts/read_capture.py: work out the bytes per pixel
    # from the row length, re-view, then drop the padding. Untrimmed padding
    # would otherwise show up here as a column of nonsense and be mistaken for
    # part of the fault under investigation.
    for bpp in (1, 2):
        want = full_w * bpp
        if not (want <= w * img.dtype.itemsize <= want + 256):
            continue
        out = img
        if bpp == 2 and img.dtype.itemsize == 1:
            if w % 2:
                break
            out = np.ascontiguousarray(img).view(np.uint16)
        if out.shape[1] >= full_w:
            return np.ascontiguousarray(out[:, :full_w])
    return img


def row_offsets(img: np.ndarray) -> np.ndarray:
    """Per-row DC offset, with the scene's own horizontal structure removed.

    A row's mean is the sum of what the sensor did to that row and what the
    scene put in it, and a scene with a horizon in it has plenty of the second.
    Subtracting a median-filtered version of the row-mean profile removes
    anything that varies slowly down the frame -- which the scene does and
    row noise, by construction, does not.
    """
    means = img.mean(axis=1, dtype=np.float64)
    k = 9
    pad = np.pad(means, k // 2, mode="edge")
    smooth = np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)
    return means - smooth


def robust_sigma(x: np.ndarray) -> float:
    """Median absolute deviation, scaled to a Gaussian sigma.

    The mean and the standard deviation are both dragged around by exactly the
    outliers being looked for, so neither can be used to decide what counts as
    an outlier. 1.4826 is the MAD-to-sigma factor for a normal distribution.
    """
    return float(1.4826 * np.median(np.abs(x - np.median(x))) + 1e-12)


def best_shift(img: np.ndarray, row: int, span: int = MAX_SHIFT) -> tuple[int, float]:
    """The lateral shift at which `row` best matches its neighbours.

    A dropped byte on the CSI link does not corrupt a row's values, it moves
    them: everything after the loss arrives one or more pixels early. So the
    row still looks like the scene, just displaced -- and displacement is
    exactly what a cross-correlation against the rows above and below finds.

    Returns (shift, improvement), where improvement is how much better the
    best shift is than no shift at all, in units of the zero-shift residual.
    Zero or negative means no displacement: the row is noisy, not moved.
    """
    h = img.shape[0]
    above = img[max(row - 1, 0)].astype(np.float64)
    below = img[min(row + 1, h - 1)].astype(np.float64)
    neighbour = 0.5 * (above + below)
    target = img[row].astype(np.float64)

    def residual(s: int) -> float:
        if s == 0:
            a, b = target, neighbour
        elif s > 0:
            a, b = target[s:], neighbour[:-s]
        else:
            a, b = target[:s], neighbour[-s:]
        if a.size < 32:
            return np.inf
        # Compared after removing each one's mean: a row offset is cause 1 and
        # must not be allowed to masquerade as a failure to align.
        return float(np.mean(((a - a.mean()) - (b - b.mean())) ** 2))

    zero = residual(0)
    shifts = [s for s in range(-span, span + 1) if s]
    best = min(shifts, key=residual)
    return best, (zero - residual(best)) / (zero + 1e-12)


def dmesg_csi() -> list[str]:
    """What the kernel says about the camera link. The independent witness.

    Everything else here is inference from pixels. This is the driver counting
    actual errors, so if it has anything to say it outranks the rest of this
    report.
    """
    try:
        out = subprocess.run(
            ["dmesg"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    want = ("csi", "unicam", "rp1-cfe", "pisp", "imx296", "cfe", "fe_stat")
    bad = ("error", "overflow", "fifo", "timeout", "corrupt", "crc", "drop", "lost")
    lines = [ln for ln in out.splitlines()
             if any(w in ln.lower() for w in want) and any(b in ln.lower() for b in bad)]
    return lines[-12:]


def row_roughness(img: np.ndarray) -> np.ndarray:
    """How badly each row matches the average of the two beside it.

    The right probe for finding anomalous rows, and not the same thing as the
    row offset. A displaced row (cause 3) has almost the RIGHT mean -- the
    pixels are all still there, just moved -- so it is invisible to an offset
    test and glaring to this one. A row with a DC offset is invisible to this
    one, because each row has its own mean removed before comparison. Between
    them the two cover every failure listed at the top of this file.
    """
    h = img.shape[0]
    a = img.astype(np.float64)
    nb = 0.5 * (a[np.clip(np.arange(h) - 1, 0, h - 1)]
                + a[np.clip(np.arange(h) + 1, 0, h - 1)])
    d = (a - a.mean(axis=1, keepdims=True)) - (nb - nb.mean(axis=1, keepdims=True))
    return np.sqrt((d ** 2).mean(axis=1))


def report(paths: list[Path]) -> int:
    frames = []
    for p in paths:
        img = load(p)
        if img.ndim != 2:
            print(f"skipping {p.name}: {img.ndim}-dimensional", file=sys.stderr)
            continue
        frames.append((p, img))
    if not frames:
        raise SystemExit("no readable 2-D frames")

    print(f"frames      : {len(frames)}  ({frames[0][1].shape[1]}x"
          f"{frames[0][1].shape[0]} {frames[0][1].dtype})")

    offsets = []
    displaced_rows: set[int] = set()
    for p, img in frames:
        o = row_offsets(img)
        offsets.append(o)
        sigma = robust_sigma(o)
        outliers = np.flatnonzero(np.abs(o) > OUTLIER_SIGMA * sigma)
        print(f"\n{p.name}")
        print(f"  row offset  : sigma {sigma:.2f} counts, "
              f"worst {np.abs(o).max():.1f} at row {int(np.argmax(np.abs(o)))}")
        print(f"  outlier rows: {len(outliers)} beyond {OUTLIER_SIGMA} sigma"
              + (f"  {[int(v) for v in outliers[:12]]}" if len(outliers) else ""))

        # The displacement test, on the roughest rows -- roughness, not offset,
        # because a displaced row has very nearly the right mean and would not
        # appear in the offset outliers at all. Only the worst few are probed:
        # this is the expensive measurement, and a transport fault shows on the
        # worst rows if it shows anywhere.
        rough = row_roughness(img)
        probe = np.argsort(rough)[-8:][::-1]
        shifted = []
        for r in probe:
            s, gain = best_shift(img, int(r))
            if gain > 0.25:                    # a quarter of the residual, or better
                shifted.append((int(r), s, gain))
        displaced_rows.update(r for r, _, _ in shifted)
        if shifted:
            print("  displaced   : " + ", ".join(
                f"row {r} best at {s:+d} px ({gain:.0%} better)"
                for r, s, gain in shifted[:6]))
        else:
            print("  displaced   : no row matches its neighbours better when "
                  "shifted -- values are wrong, not moved")

    # Fixed pattern versus random: the only question a single frame cannot
    # answer, and the one that decides whether a dark frame would fix it.
    fixed = None
    if len(offsets) > 1:
        n = min(len(o) for o in offsets)
        stack = np.array([o[:n] for o in offsets])
        pairs = [
            float(np.corrcoef(stack[i], stack[j])[0, 1])
            for i in range(len(stack)) for j in range(i + 1, len(stack))
        ]
        fixed = float(np.mean(pairs))
        print(f"\nframe-to-frame row-offset correlation: {fixed:+.2f}")

    lines = dmesg_csi()
    if lines:
        print("\nkernel (dmesg), camera link errors -- this outranks everything above:")
        for ln in lines:
            print("  " + ln.strip())
    else:
        print("\nkernel (dmesg): no camera-link errors reported"
              + ("" if Path("/proc/version").exists() else " (not run on the Pi?)"))

    # -- the verdict ------------------------------------------------------
    #
    # Ordered by how decisive the evidence is, not by how common the cause is.
    # The kernel counting real errors beats any inference from pixels; a
    # measured displacement beats a statistic about offsets; and a statement
    # about offsets is only worth making if the offsets are big enough to see.
    worst_sigma = max(robust_sigma(o) for o in offsets)
    print("\nreading:")
    verdict = False
    if lines:
        verdict = True
        print("  The kernel is reporting camera-link errors. That is a transport")
        print("  fault and it outranks everything measured from the pixels:")
        print("  reseat both ends of the ribbon, try a shorter cable, and if two")
        print("  cameras are running, test with one at a time -- they share the")
        print("  CSI bandwidth, so a marginal link can fail only under the pair.")
    if displaced_rows:
        verdict = True
        shown = sorted(displaced_rows)[:10]
        print(f"  {len(displaced_rows)} row(s) match their neighbours markedly "
              f"better when shifted\n  sideways: {shown}"
              f"{' ...' if len(displaced_rows) > len(shown) else ''}. The pixels "
              f"in those rows are not wrong,\n  they are MOVED, which is what a "
              f"dropped byte on the CSI link does and\n  is not something a sensor "
              f"does. (Expect the two rows either side of a\n  displaced one to be "
              f"listed too -- they are being compared against it.)\n"
              f"  Same actions as for a kernel error; if dmesg is silent, check "
              f"the FFC\n  connector latches and the cable routing before "
              f"suspecting the cable.")
    if fixed is not None and fixed > 0.6 and worst_sigma >= NOTABLE_OFFSET:
        verdict = True
        print(f"  Row offsets repeat across frames (r={fixed:+.2f}). That is "
              f"fixed-pattern\n  row noise, and it subtracts out: capture a dark "
              f"frame at the same\n  exposure and gain and subtract its row-offset "
              f"profile.")
    elif fixed is not None and abs(fixed) < 0.3 and worst_sigma >= NOTABLE_OFFSET:
        verdict = True
        print(f"  Row offsets do not repeat across frames (r={fixed:+.2f}), so "
              f"this is\n  random per-frame row noise, not a fixed pattern. It "
              f"does not subtract\n  out; it falls with exposure and rises with "
              f"analogue gain.")
    if worst_sigma < NOTABLE_OFFSET and not displaced_rows:
        verdict = True
        print(f"  Row offsets are under {NOTABLE_OFFSET} count and no row is "
              f"displaced. At that\n  level you are looking at the sensor's own "
              f"read noise with the contrast\n  stretched -- normal, and not worth "
              f"chasing. Judge the frame at its real\n  contrast before deciding "
              f"there is a fault.")
    if not verdict:
        print("  Nothing decisive. The offsets are real but neither fixed nor "
              "clearly\n  random, and no row is displaced. Record more frames, "
              "and record a set\n  with the lens capped so the scene is not part "
              "of the measurement.")
    if len(frames) == 1:
        print("  Only one frame: fixed-pattern and random row noise cannot be "
              "told\n  apart from one. Record a handful and point this at the "
              "directory.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="a .npy capture, or a directory of them")
    ap.add_argument("--pattern", default="*.npy", help="glob, when path is a directory")
    ap.add_argument("--limit", type=int, default=8, help="most frames to read")
    a = ap.parse_args()

    if a.path.is_dir():
        paths = sorted(a.path.rglob(a.pattern))[: a.limit]
        if not paths:
            raise SystemExit(f"no files matching {a.pattern!r} under {a.path}")
    else:
        paths = [a.path]
    return report(paths)


if __name__ == "__main__":
    raise SystemExit(main())
