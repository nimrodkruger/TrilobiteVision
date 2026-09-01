#!/usr/bin/env python3
"""How much does de-rotation actually cost in corner localisation?

Run:  python scripts/measure_derotation_cost.py

Exists because the answer was asserted before it was measured, and the
assertion was wrong. Keep it runnable so the number can be re-checked when the
detector, the board or the tile size changes.

Result at 20 px squares, 100 px tiles: resampling adds ~0.07 px RMS against a
~0.15 px baseline. Real, small, and not a reason to forbid anything.

Render a checkerboard with EXACT known corner positions (supersampled, so the
ground truth is not itself an estimate), then compare corner detection on:
  (a) the image as sampled                       -- no resampling
  (b) the image bilinearly resampled by a rotation and detected in that frame,
      with corners mapped back                   -- one resampling pass

Any difference is the cost of the resample.
"""
import math

import cv2
import numpy as np

SQ = 20.0          # square size, px
NX, NY = 6, 5      # squares
W = H = 160
SS = 8             # supersample factor for rendering

def render(cx, cy, theta_deg=0.0):
    """Checkerboard centred at (cx,cy), rotated by theta, antialiased."""
    t = math.radians(theta_deg)
    ct, st = math.cos(t), math.sin(t)
    yy, xx = np.mgrid[0:H*SS, 0:W*SS].astype(np.float64)
    x = (xx + 0.5) / SS - cx
    y = (yy + 0.5) / SS - cy
    u = ( x*ct + y*st) / SQ
    v = (-x*st + y*ct) / SQ
    inside = (np.abs(u) < NX/2) & (np.abs(v) < NY/2)
    patt = ((np.floor(u + NX/2).astype(int) + np.floor(v + NY/2).astype(int)) % 2)
    img = np.where(inside, patt * 255.0, 128.0)
    img = img.reshape(H, SS, W, SS).mean(axis=(1, 3))
    return np.clip(img, 0, 255).astype(np.uint8)

def true_corners(cx, cy, theta_deg=0.0):
    t = math.radians(theta_deg)
    ct, st = math.cos(t), math.sin(t)
    pts = []
    for j in range(1, NY):
        for i in range(1, NX):
            u = (i - NX/2) * SQ
            v = (j - NY/2) * SQ
            pts.append((cx + u*ct - v*st, cy + u*st + v*ct))
    return np.array(pts)

def detect(img):
    ok, c = cv2.findChessboardCornersSB(img, (NX-1, NY-1), flags=cv2.CALIB_CB_ACCURACY)
    if not ok:
        return None
    return c.reshape(-1, 2).astype(np.float64)

def match_rms(found, truth):
    """Scatter about the mean offset, matching each found corner to its
    nearest true corner. The mean offset is a pixel-centre convention
    difference between the renderer and the detector, constant across the
    board; the SCATTER is the localisation error that matters."""
    d = np.linalg.norm(found[:, None, :] - truth[None, :, :], axis=2)
    idx = d.argmin(axis=1)
    if len(set(idx.tolist())) != len(idx):
        return None
    resid = found - truth[idx]
    resid = resid - resid.mean(axis=0)
    return float(np.sqrt((resid ** 2).sum(axis=1).mean()))

def bilinear(img, x, y):
    h, w = img.shape
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    fx = (x - x0)[..., None] if img.ndim == 3 else (x - x0)
    fy = (y - y0)[..., None] if img.ndim == 3 else (y - y0)
    x0c, x1c = np.clip(x0,0,w-1), np.clip(x0+1,0,w-1)
    y0c, y1c = np.clip(y0,0,h-1), np.clip(y0+1,0,h-1)
    s = img.astype(np.float64)
    top = s[y0c,x0c]*(1-fx) + s[y0c,x1c]*fx
    bot = s[y1c,x0c]*(1-fx) + s[y1c,x1c]*fx
    return np.clip(np.rint(top*(1-fy) + bot*fy), 0, 255).astype(np.uint8)

def derotate(img, theta_deg):
    """Resample into the lattice axes about the image centre -- what
    crop_derotated does."""
    t = math.radians(theta_deg)
    ct, st = math.cos(t), math.sin(t)
    c = (W - 1) / 2.0
    o = np.arange(W, dtype=np.float64) - c
    bb, aa = np.meshgrid(o, o, indexing="ij")
    return bilinear(img, c + aa*ct - bb*st, c + aa*st + bb*ct)

rng = np.random.default_rng(1)
print(f"{'theta':>6} {'direct RMS':>12} {'derotated RMS':>15} {'added':>9}")
print("-" * 48)
for theta in (0.0, 1.0, 2.0, 5.0, 10.0):
    dir_errs, der_errs = [], []
    for _ in range(40):
        cx = W/2 + rng.uniform(-0.5, 0.5)
        cy = H/2 + rng.uniform(-0.5, 0.5)
        img = render(cx, cy, theta)

        f = detect(img)
        if f is not None:
            e = match_rms(f, true_corners(cx, cy, theta))
            if e is not None:
                dir_errs.append(e)

        # De-rotate, detect in the de-rotated frame, map corners back.
        d = derotate(img, theta)
        f2 = detect(d)
        if f2 is not None:
            t = math.radians(theta)
            ct, st = math.cos(t), math.sin(t)
            c0 = (W - 1) / 2.0
            a = f2[:, 0] - c0
            bq = f2[:, 1] - c0
            back = np.stack([c0 + a*ct - bq*st, c0 + a*st + bq*ct], axis=1)
            e2 = match_rms(back, true_corners(cx, cy, theta))
            if e2 is not None:
                der_errs.append(e2)

    dm = np.mean(dir_errs) if dir_errs else float("nan")
    rm = np.mean(der_errs) if der_errs else float("nan")
    add = math.sqrt(max(rm**2 - dm**2, 0)) if dir_errs and der_errs else float("nan")
    print(f"{theta:6.1f} {dm:12.4f} {rm:15.4f} {add:9.4f}   (n={len(dir_errs)}/{len(der_errs)})")

print()
print("Largest axis-aligned square inside a lattice cell rotated by theta:")
for theta in (0.0, 1.0, 2.0, 5.0, 10.0):
    t = math.radians(theta)
    frac = 1.0 / (math.cos(t) + math.sin(t))
    print(f"  {theta:5.1f} deg -> {frac*100:5.1f}% of pitch  ({frac**2*100:5.1f}% of area)"
          f"   at 100 px pitch: {frac*100:.0f} px")
