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

The MLA parameters are expressed in **preview pixels** (728 × 544), because
that is the image the alignment overlay draws on. Calibration crops from the
**sensor** frame (1456 × 1088). A factor of two, silently applied to pitch and
offsets, puts every crop in the wrong place, and the failure looks like
"detection just doesn't work".

Built as:

* `MLAParams.reference_width` / `reference_height` — the frame size the numbers
  were measured against, learned from the first preview frame and carried in
  the saved state. Hidden in the UI: it is a unit, not a knob.
* `MLAGeometry.rescaled(w, h)` — the conversion, in one place, and exact: under
  the pixel-area convention a preview pixel at `x` maps to `(x + ½)s − ½`, and
  applying that to the origin gives `c_s + Δx·s` with no half-pixel remainder,
  so the offsets simply scale and the rotation carries over untouched.
* `MLAGridOverlay.geometry_for(w, h)` — what every non-preview caller uses.
* `note_frame_size()` — if the configured preview resolution changes after an
  alignment, the parameters are **rescaled to follow it** and the change is
  logged. Keeping the numbers and quietly meaning something else would look
  like the alignment drifting on its own.

Pinned by `test_preview_scale_geometry_on_a_full_frame_finds_almost_nothing`,
which is this bug written as an assertion.

### 4.3 Rate

Per tile, `findChessboardCornersSB` on a 100 × 100 crop costs roughly 1–3 ms on
a Pi 5. For 140 tiles × 2 cameras that is 0.3–0.8 s per full pass — about
**1–2 Hz**, which is the right cadence for "hold the board, wait for the
accept" and hopeless as a per-frame operation.

So: a detection worker thread per camera, pulling the latest full-res frame at
its own rate, publishing results to a slot the UI polls. It never blocks the
preview, and dropped detection passes are harmless.

**What the first field run added.** Pressing Start on the rig took the Pi down.
The arithmetic above is right and the estimate that follows from it was never
enforced: cost is (tiles × cameras × ms), and nothing bounded any of the three
factors. Two things compounded.

*The tile count is quadratic in 1/pitch.* At a 100 px sensor pitch a 1456 px
sensor holds ~130 whole micro-images. At the 20 px default it holds ~900, and
at anything smaller, thousands. A grid left near its defaults therefore asks
for seven to fifty times the work, and the only symptom before the machine
stops answering is that it feels slow.

*Two cameras detecting in parallel doubles the current draw.* A Pi 5 with two
CSI cameras and four saturated cores is past what many nominally-5 V supplies
deliver, and the failure is a hard reset with nothing in any log.

Four bounds, all in the Detection panel:

| control | default | what it prevents |
|---|---|---|
| `max_tiles` | 320 | a wrong pitch, refused **before** starting, with the tile count and the seconds-per-pass named. Re-checked inside the worker, because the MLA parameters are live objects. |
| `concurrent_cameras` | 1 | the parallel current spike. Cameras take turns through a shared semaphore. |
| `max_duty` | 0.5 | a pass that turns out to cost a second costing a second every two seconds, rather than a permanently pinned core. |
| thread priority | `nice(10)` | a starved event loop. A rig that stops answering looks crashed; one that answers slowly looks busy. |

None of these substitute for an adequate supply. `/api/status` now reports CPU
temperature, load, free memory and the sticky under-voltage bit from
`vcgencmd get_throttled`, and the header turns red on any of them — the sticky
bit in particular survives the reset it caused, which is the only reason the
question is answerable after the fact.

### 4.4 Per tile

1. Crop the micro-image from the frozen MLA geometry.
2. Skip it if its standard deviation is below ~3 DN — there is no pattern in a
   flat tile, and the skip is *reported*, not hidden, because "too dark to hold
   a pattern" and "pattern not found" are different faults.
3. `findChessboardCornersSB` at the configured board size.
4. Map corner positions back to **full-frame sensor coordinates**
   (`MLAGeometry.tile_to_frame`), with the tile index.

Storing sensor coordinates rather than tile-local ones means the record does
not depend on the crop convention, so a later change to `crop_scale` does not
invalidate the data. It is also what makes de-rotated and plain crops
interchangeable — `test_derotated_and_plain_crops_agree_on_corner_positions`
holds them to under half a pixel of each other.

**No `cornerSubPix`.** An earlier draft of this section had one. SB with
`CALIB_CB_ACCURACY` already runs a sub-pixel stage, and `cornerSubPix` needs a
search window that, at a 20 px square, is either too small to help or large
enough to reach into the neighbouring square. Measured on the synthetic board,
117 tiles of 100 px:

| flags | per tile | corner straightness |
|---|---|---|
| none | 1.95 ms | 0.0055 px |
| `NORMALIZE_IMAGE` | 2.13 ms | 0.0440 px |
| `ACCURACY` | 4.86 ms | 0.0040 px |

`NORMALIZE_IMAGE` costing eight times the precision is the surprising one, and
it is why neither flag is on by default. Both are switches in the Detection
panel: normalisation earns its place only when vignetting stops a tile being
found at all, and accuracy when recording rather than watching.

### 4.5 What exists now — detection without recording

The current build stops deliberately short of a session. **Start detection**
runs the loop above and writes nothing: no poses, no frames, no accumulators.
The response says `recording: false` rather than leaving it to be inferred.

The reason is that a rig which records unusable corners for twenty minutes and
reveals it at the fit is much worse than one that shows, live, that eleven
tiles find the pattern and a hundred and sixteen do not. This stage answers
that question and only that question.

What it shows, per camera:

* the annotated frame — the *same* frame the numbers came from, tiles boxed by
  verdict and corners drawn where they were measured, downscaled to preview
  width and polled at ~3 Hz;
* the coverage lattice — one cell per lattice position, which the image cannot
  substitute for: a dead column at one edge of the array is obvious as a column
  of cells and invisible as a few missing boxes in a busy picture. Four states,
  and the fourth is the useful one: *cross*, *found*, *nothing found*, and
  *not a whole tile* (off the sensor, not a fault);
* tiles found, qualifying crosses, pass duration, and whether the pose would be
  accepted.

Off-rig, `config/desktop-plenoptic.yaml` renders a synthetic lenslet array with
a complete checkerboard in every micro-image, so the whole path is exercisable
with no camera. Without it "the detector runs" and "the detector works" look
identical, since every failure mode — wrong crop scale, wrong offset, wrong
board size — produces the same symptom: no corners.

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

## 9. Changes in the existing code

| area | change | state |
|---|---|---|
| `optics/mla.py` | reference resolution; `rescaled`, `crop_origin`, `tile_to_frame` (§4.2) | done |
| `cameras/base.py` | `read_full_mono()` — full-res frame for detection, ISP output, no mode switch, no disturbance to the preview cadence | done |
| `app.py` | detection worker per camera, start/stop/status; storage watch thread | done |
| `web/server.py` | `/api/calibration/{settings,readiness,detection,start,stop}`, `/calibration/detection/{cam}.jpg`, `/api/storage*` | done |
| `web/static` | second dashboard, mode switch, coverage lattice, storage panel | done |
| `calibration/detect.py` | detector, acceptance, overlay, worker | done |
| `pyproject.toml` | opencv as a `desktop` extra so the detector is testable off-rig | done |
| `cameras/offline.py` | `plenoptic_board` synthetic pattern, so all of the above is testable with no camera | done |
| `calibration/session.py` | pose recording, duplicate suppression, coverage accumulation | **not built** |

Nothing in §7 (what is saved) or §8 (session end) exists yet, and §6.1's
"never detected" state cannot exist until something accumulates across passes.
The coverage lattice today is per-frame only.

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

   **The detector as built assumes (a) or (b)** — it asks
   `findChessboardCornersSB` for the exact `cols × rows` pattern in each tile,
   which succeeds only on a board that fits whole inside one micro-image. That
   is not a decision made on your behalf: it is the only thing that can be
   built before the target exists, and it is also the thing to test the rig
   with first. Choosing (c) later replaces `CornerDetector.detect_tile` and
   nothing else — the geometry, the worker, the acceptance rule and the display
   are all indifferent to how a tile's corners were found.

   The `board_fits` readiness check exists for this: it multiplies the board
   size by the predicted square size and says, before you start, whether the
   pattern can physically fit in a micro-image. Without it, choosing (c)'s
   large board by accident produces exactly the same symptom as every other
   mistake — no corners — and costs an afternoon.

2. **`min corners` default.** 6 is a guess. It should be set from how many the
   detector actually returns reliably at your square size — worth measuring in
   the first session rather than fixing now.

3. **Target count per tile.** 8 is a guess too. §3.5 of the calibration spec
   says ~145 observations per lenslet against 2 unknowns at 15 poses, so the
   requirement is much weaker than 8 accepted poses per *tile*. 4–5 may be
   plenty; the coverage map will tell you.
