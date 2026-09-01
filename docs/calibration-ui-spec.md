# Calibration mode — UI specification

The acquisition front end for `docs/calibration-spec.md`. A separate dashboard,
entered deliberately, that does one job: collect enough accepted checkerboard
poses to make the fit in §4 of that document well-determined, and show you
which part of the field still needs work.

---

## 1. Mode switch and preconditions

A **Calibrate** toggle in the header replaces the whole `<main>` region. Live
mode and calibration mode share the header, the footer log and the camera
streams; nothing else.

Entry is refused, with the reason named, unless all of:

| precondition | why |
|---|---|
| every camera has an `mla_grid_overlay` stage, enabled | the tile geometry is what crops the micro-images |
| pitch, rotation and offsets have been changed from defaults | an untouched grid means alignment was never done |
| `derotate_views` is off | corners must be measured on unresampled pixels |
| no camera reporting errors, both streaming | half a rig produces half a calibration |

On entry the MLA parameters are **frozen and shown read-only**. They define the
crops that produce the corners; changing them mid-session would silently
invalidate every pose already recorded. The exit path warns if a session is
open.

Live mode is unchanged and remains where alignment happens.

---

## 2. Layout

```
┌ header ─────────────────────────── [Live | ✱Calibrate] ─────────────────┐
├─────────────────────────────┬───────────────────────────────────────────┤
│ SETUP  (left rail, narrow)  │  COVERAGE  (dominant)                     │
│                             │                                           │
│  Board                      │   left camera        right camera         │
│   corners  [ 9 ]x[ 6 ]      │   ┌─────────────┐    ┌─────────────┐      │
│   square   [ 20.0 ] mm      │   │▓▓▒▒░░  ░░   │    │▓▓▓▒▒░  ░    │      │
│   ⓘ 21 px on sensor         │   │▓▓▓▒▒░░░░    │    │▓▓▒▒▒░░░     │      │
│                             │   │▓▓▓▓▒▒░░     │    │▓▓▓▒▒░░      │      │
│  Acceptance                 │   └─────────────┘    └─────────────┘      │
│   target/tile   [ 8 ]       │   14 × 10 tiles, one cell each            │
│   min cross     [ 5 ]       │                                           │
│   min corners   [ 6 ]       │  ┌ depth spread ──────────────────────┐   │
│                             │  │ ▁▁▇▇▇▁▁▁▁▂▂▁▁▁▁▁▃▃▁   3 bands ✓   │   │
│  Sensor                     │  └────────────────────────────────────┘   │
│   ExposureTime [      ]     │                                           │
│   AnalogueGain [──●───]     │  ┌ live, both cameras ────────────────┐   │
│   AeEnable     [ ]          │  │  preview with detected corners      │   │
│                             │  │  drawn per tile, this frame         │   │
│  [ Start detection ]        │  └────────────────────────────────────┘   │
│  [ Finish session ]         │                                           │
└─────────────────────────────┴───────────────────────────────────────────┘
```

**The coverage map is the instrument, not the live image.** During a session
you are not looking at the picture; you are looking at which tiles still need
poses and moving the board accordingly. The live view is a small confirmation
strip, inverting live mode's priority.

Controls present: exposure, gain, auto-exposure. Nothing else. No pipeline
panels, no MLA sliders, no save-raw buttons.

---

## 3. Board setup

| field | validation |
|---|---|
| inner corners, cols × rows | ≥ 3 each |
| square size, mm | > 0 |
| (derived) square size in sensor px | **warn outside 15–30 px** |

The derived figure is computed from the current `f` estimate and the board's
distance from the last PnP solve, and it is the field that catches a badly
chosen board before the session rather than after. Below ~10 px corner
localisation degrades badly; above ~30 px a tile holds too few corners.

Board parameters lock when detection starts. Changing them mid-session means
the corners already recorded refer to a different object.

---

## 4. Detection

### 4.1 Where it runs

Detection runs on the **full-resolution frame**, not the preview. The preview
is half-scale, so corners found there carry half the precision and the whole
exercise is pointless. This needs a capture path that pulls `main` at
1456 × 1088 for detection while the preview continues to serve the display.

### 4.2 Units — a required fix first

The MLA parameters are currently expressed in **preview pixels** (728 × 544),
because that is the image the alignment overlay draws on. Calibration crops
from the **sensor** frame (1456 × 1088). A factor of two, silently applied to
pitch and offsets, would put every crop in the wrong place and the failure
would look like "detection just doesn't work".

Fix before building any of this: add `reference_width`/`reference_height` to
the MLA parameters, recorded when they are set, and have `MLAGeometry` scale
itself to whatever frame it is applied to. The geometry then has one
unambiguous meaning and the scaling happens in one place instead of at every
call site.

### 4.3 Rate

Per tile, `findChessboardCornersSB` on a 100 × 100 crop costs roughly 1–3 ms on
a Pi 5. For 140 tiles × 2 cameras that is 0.3–0.8 s per full pass — about
**1–2 Hz**, which is the right cadence for "hold the board, wait for the
accept" and hopeless as a per-frame operation.

So: a detection worker thread per camera, pulling the latest full-res frame at
its own rate, publishing results to a slot the UI polls. It never blocks the
preview, and dropped detection passes are harmless.

### 4.4 Per tile

1. Crop the micro-image from the frozen MLA geometry.
2. `findChessboardCornersSB` at the configured board size.
3. `cornerSubPix` refinement.
4. Record corner positions **in full-frame sensor coordinates**, with the tile
   index and the corner IDs.

Storing sensor coordinates rather than tile-local ones means the record does
not depend on the crop convention, so a later change to `crop_scale` does not
invalidate the data.

---

## 5. Acceptance

### 5.1 The rule

A pose is accepted for a camera when **a connected cross of ≥ 5 tiles** — a
centre tile and its four edge-neighbours — each return ≥ `min corners`
identified corners in the same frame.

Two notes on why this rule, since the reasoning is not what it first appears:

**Overlap is not required.** The tiles in the cross do not need to see the same
part of the board. What ties them together is the board's *rigidity*: one board
pose is a single unknown shared by every tile that sees the board, so tiles
viewing disjoint regions still constrain their relative geometry. This matters
because minimal overlap would otherwise make a "5 tiles see the same pattern"
rule impossible to satisfy.

**The cross earns its place for two other reasons.** It spans both lattice
directions, so one accepted pose constrains both columns of `κ·U` rather than
one. And contiguity makes corner identity chainable: once one tile's fragment
is placed on the board, its neighbours' fragments are strongly predicted.

### 5.2 Duplicate suppression

Auto-save with no guard produces fifty near-identical poses while you hold the
board still, which inflates the count without adding information and biases the
fit toward whichever position you happened to rest at.

After each accept, the next is blocked until the board has *moved*: PnP
translation changed by ≥ 5 % of the working distance, **or** rotation by ≥ 3°.
Measured, not timed — a timer rewards waiting, whereas this rewards the thing
that actually helps.

The UI shows "move the board" until the gate clears.

### 5.3 Stereo poses

Acceptance is evaluated per camera. A pose accepted by only one camera is kept
— it still constrains that camera's own parameters — but it contributes nothing
to `R_LR`. Two counters, tracked separately, with a target on the stereo count.

---

## 6. Progress display

### 6.1 Coverage map

One cell per lenslet, laid out as the array: 14 × 10 for this rig.

| state | appearance |
|---|---|
| 0 accepted | grey |
| 1 … target−1 | amber, filling |
| ≥ target | **green** |
| never returned a detection | outlined red |

That last state is the one worth having. A tile that has *never* detected
anything, over a whole session, is not short of poses — it is broken: a bad
crop, an obstruction, a dead corner of the array. Distinguishing "not yet" from
"never" turns a session-long puzzle into a glance.

Hovering a cell gives its count and last detection time.

### 6.2 Depth histogram

Accepted poses binned by PnP distance, with the **≥ 3 well-separated bands**
requirement from §2.5 of the calibration spec shown as met or not.

This is a guard against the one failure mode that does not announce itself: `κ`
and `D` enter the model only through `κ/(Z − D)`, so at a single depth they are
one number. The fit will converge perfectly and be wrong. Nothing in the
residuals reveals it. The histogram is the only place it can be caught, which
is why it is on the main screen and not in a report.

### 6.3 Session summary line

Poses accepted, stereo poses, tiles at target, tiles never seen, depth bands.
Everything needed to answer "can I stop yet".

---

## 7. What is saved

You asked whether corners alone are enough. **Corners plus the frame** — and
the second half is the part I would not drop.

Corners are the measurement, and they are tiny: ~16 corners × 140 tiles × 2
floats ≈ 36 KB per pose. But you cannot re-run detection with a different
detector, different sub-pixel window, or a corrected crop geometry without the
pixels, and at some point you will want to. A raw mono frame is 1.6 MB; a
15-pose two-camera session is **under 50 MB**. The storage argument for
corners-only does not survive contact with the numbers at this scale.

Corners-only becomes right when sessions run to hundreds of poses. Revisit it
then.

Per accepted pose:

```
session_<timestamp>/
  session.json           board spec, MLA geometry (frozen), acceptance
                         settings, camera info, software version
  pose_0007/
    left.npy             full-res mono frame
    left.json            corners: [{tile:[i,j], ids:[...], xy:[[u,v],...]}],
                         exposure, gain, timestamp, PnP pose, accept reason
    right.npy
    right.json
```

`session.json` carrying the frozen MLA geometry is not optional: a corner list
without the crop geometry that produced it cannot be interpreted, and that is
the single most likely thing to be missing in six months.

---

## 8. Session end

**Finish session** writes the manifest, runs the completeness check (tiles at
target, depth bands, stereo count) and reports what is short. It does not run
the fit — that is offline, and a session should be closeable without it.

A session left open when the process exits is recoverable: poses are written as
they are accepted, so the manifest can be regenerated from what is on disk.

---

## 9. Changes required in the existing code

| area | change |
|---|---|
| `optics/mla.py` | reference resolution on the geometry; scale to the target frame (§4.2) |
| `cameras/base.py` | a way to pull a full-res frame for detection without disturbing the preview cadence |
| `app.py` | detection worker per camera; calibration session object |
| `web/server.py` | `/api/calibration/*` — start, stop, settings, coverage, last detection |
| `web/static` | the second dashboard, mode switch |
| new | `calibration/detect.py`, `calibration/session.py` |
| `pyproject.toml` | opencv is currently apt-only on the Pi; add it as a desktop extra so the detector is testable off-rig |

---

## 10. Open

1. **Corner identity in a fragment.** A tile sees ~4 × 4 corners of a 9 × 6
   board. `findChessboardCornersSB` needs a complete rectangular pattern of the
   size you specify, so a fragment of a larger board is not directly
   detectable, and ChArUco markers do not survive at 20 px squares. Three ways
   out, and this decides the physical target:

   - **(a) Small board, whole in one tile.** Specify the board as the small
     pattern; every tile that sees it detects it completely. Simplest detector,
     no ambiguity. Needs the board to subtend ≤ one micro-image, so it covers
     little of the field per pose and you need more poses.
   - **(b) Array of small boards** on one target, each sized to a micro-image,
     each identified by position. Covers the field in one pose, keeps the
     detector trivial. Needs a custom printed target.
   - **(c) One large board, identity from the model.** Detect corners without
     pattern topology, assign IDs by predicting each tile's field from the
     current parameter estimate. What most of the plenoptic literature does.
     No special target, best coverage, but it bootstraps — and a mis-assignment
     is a structured outlier, the worst kind.

   My inclination is **(b)**: it makes the detector a solved problem and moves
   the difficulty to a printing job you do once. But it is your target to make,
   so it is your call.

2. **`min corners` default.** 6 is a guess. It should be set from how many the
   detector actually returns reliably at your square size — worth measuring in
   the first session rather than fixing now.

3. **Target count per tile.** 8 is a guess too. §3.5 of the calibration spec
   says ~145 observations per lenslet against 2 unknowns at 15 poses, so the
   requirement is much weaker than 8 accepted poses per *tile*. 4–5 may be
   plenty; the coverage map will tell you.
