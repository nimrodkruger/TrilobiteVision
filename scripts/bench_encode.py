#!/usr/bin/env python3
"""What does it cost to write a burst out, in each available format?

Answers the question Stage 5B turns on: once a burst is in RAM, encoding speed
no longer limits the capture rate, so the choice of output format stops being
about throughput and becomes a trade between fidelity, size and a one-off wait.
This measures the wait, and the size, on whatever machine it runs on.

**Run it on the Pi.** Numbers from an x86 desktop are not transferable -- the
Cortex-A76 has a quarter of the memory bandwidth and no AVX-512 -- so an x86
result is an order-of-magnitude reference and nothing more. The script says
which machine produced its numbers for exactly that reason.

    python scripts/bench_encode.py                 # default: 60 frames
    python scripts/bench_encode.py --frames 300    # 5 s of one head at 60 fps
    python scripts/bench_encode.py --noise 40      # dirtier data compresses worse

WHAT IS BEING COMPARED

    npy          the current plan. Exact, uint16, no dependency, loads in one
                 line in numpy and in MATLAB via tv_read_npy.
    ffv1-16      lossless, 16-bit grayscale, in an AVI or MKV container. Exact,
                 so `validity: science` survives. The only interesting question
                 is whether it compresses sensor noise enough to be worth the
                 encode time.
    x264-8       H.264, 8 bit, lossy. CANNOT carry sensor counts -- it is here
                 to size the cost of an optional viewing proxy, not as a
                 candidate for the science path.

The synthetic frames carry a plausible amount of photon and read noise, because
noise is what decides lossless compression ratio and a clean gradient would
flatter FFV1 enormously.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

W, H = 1456, 1088                      # IMX296
BYTES_PER_FRAME = W * H * 2


def synth(n: int, noise: float, seed: int = 7) -> np.ndarray:
    """A stack that compresses about as well as real sensor data.

    Structure plus noise. The structure is a coarse lenslet-like pattern so the
    frame is not uniform; the noise is what stops FFV1 from reporting a ratio
    no real capture will ever see.
    """
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:H, 0:W]
    base = (300 + 200 * np.sin(x / 40.0) * np.sin(y / 40.0)).astype(np.float32)
    out = np.empty((n, H, W), np.uint16)
    for i in range(n):
        frame = base + rng.normal(0, noise, size=(H, W)).astype(np.float32)
        out[i] = np.clip(frame, 0, 1023).astype(np.uint16)
    return out


def time_it(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def run_ffmpeg(args: list[str], raw: bytes) -> None:
    proc = subprocess.run(args, input=raw, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode()[-600:])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--noise", type=float, default=12.0,
                    help="sigma in counts; raise it to model a noisier sensor")
    ap.add_argument("--keep", action="store_true", help="do not delete outputs")
    args = ap.parse_args()

    n = args.frames
    mib = n * BYTES_PER_FRAME / 1024 / 1024
    print(f"machine     : {platform.machine()}  {platform.processor() or '?'}")
    if platform.machine() not in ("aarch64", "arm64"):
        print("              *** NOT a Pi. These numbers do not transfer. ***")
    print(f"stack       : {n} x {W}x{H} uint16 = {mib:.0f} MiB "
          f"({n / 60:.1f} s of the pair at 30 fps)")
    print(f"noise sigma : {args.noise} counts\n")

    stack = synth(n, args.noise)
    raw = stack.tobytes()
    tmp = Path(tempfile.mkdtemp(prefix="tv-bench-"))
    rows: list[tuple[str, float, float, str]] = []

    # -- npy ---------------------------------------------------------------
    p = tmp / "burst.npy"
    dt = time_it(lambda: np.save(p, stack))
    rows.append(("npy (exact)", dt, p.stat().st_size / 1024 / 1024, "lossless"))

    have_ffmpeg = shutil.which("ffmpeg") is not None
    if not have_ffmpeg:
        print("ffmpeg not found -- install it to compare the video formats\n")

    if have_ffmpeg:
        # -- FFV1, 16-bit gray, lossless -----------------------------------
        p = tmp / "burst-ffv1.mkv"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "gray16le", "-s", f"{W}x{H}",
               "-r", "30", "-i", "-", "-c:v", "ffv1", "-level", "3",
               "-coder", "1", "-context", "1", "-g", "1", str(p)]
        try:
            dt = time_it(lambda: run_ffmpeg(cmd, raw))
            rows.append(("ffv1 16-bit (exact)", dt,
                         p.stat().st_size / 1024 / 1024, "lossless"))
        except RuntimeError as exc:
            print(f"ffv1 failed: {exc}\n")

        # -- x264, 8 bit, lossy: a viewing proxy only ----------------------
        eight = (stack >> 2).astype(np.uint8).tobytes()      # 10-bit -> 8
        p = tmp / "burst-x264.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}",
               "-r", "30", "-i", "-", "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "18", "-pix_fmt", "yuv420p", str(p)]
        try:
            dt = time_it(lambda: run_ffmpeg(cmd, eight))
            rows.append(("x264 8-bit proxy", dt,
                         p.stat().st_size / 1024 / 1024, "LOSSY, 8-bit"))
        except RuntimeError as exc:
            print(f"x264 failed: {exc}\n")

    print(f"{'format':<22} {'wall s':>8} {'MiB':>9} {'MiB/s':>8} "
          f"{'ratio':>7}  fidelity")
    print("-" * 74)
    for name, dt, size, fidelity in rows:
        print(f"{name:<22} {dt:>8.2f} {size:>9.1f} {mib / dt:>8.1f} "
              f"{mib / size:>6.2f}x  {fidelity}")

    print(f"\nA 20 s burst of the pair is {20 * 60 * BYTES_PER_FRAME / 1e9:.1f} GB "
          f"raw. Scale the wall times by {20 * 60 / n:.0f}x.")
    if args.keep:
        print(f"outputs kept in {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
