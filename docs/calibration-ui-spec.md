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
┌ header ─────────────────────────── [Imaging | ✱Calibration] ────────────┐
├─────────────────────────────┬───────────────────────────────────────────┤
│ SETUP  (left rail, narrow)  │  ┌─────────────────────────────────────┐  │
│                             │  │                                     │  │
│  Board                      │  │        HOLD STILL                   │  │
│   corners  [ 4 ]x[ 3 ]      │  │        2/4 still                    │  │
│   square   [ 7.0 ] mm       │  │        7 poses · spread 3.1 px      │  │
│                             │  └─────────────────────────────────────┘  │
│  Capture                    │  [Start session] [Finish]  ☑ sound        │
│   micro-images seen [ 5 ]   │           space = shoot now, esc = discard │
│   frames still      [ 4 ]   │                                           │
│   stillness         [0.06]  │   left camera        right camera         │
│   movement          [0.25]  │   ┌─────────────┐    ┌─────────────┐      │
│   review pause      [ 2.5]  │   │ live, tinted│    │ live, tinted│      │
│                             │   └─────────────┘    └─────────────┘      │
│  Optics, Acceptance,        │   ▓▓▒▒░░  ░░          ▓▓▓▒▒░  ░           │
│  Detection, Sensor          │   coverage lattice    coverage lattice    │
│                             │                                           │
│                             │  ┌ working distance spread ───────────┐   │
│                             │  │ ▁▁▇▇▇▁▁▁▁▂▂▁▁▁▁▁▃▃▁   3 bands ✓   │   │
│                             │  └────────────────────────────────────┘   │
└─────────────────────────────┴───────────────────────────────────────────┘
```

**The banner is the instrument.** It is thirty-four point type across the full
width of the stage because the operator is holding a board with both hands and
reading it in peripheral vision from arm's length. Everything below it —
coverage, depth spread, preconditions, the derived optics — is for between
poses and afterwards.

The live views are tinted by the presence stage: micro-images that are seeing
the pattern are brightened in the image itself, so aiming needs no second
display. During the review pause each view is replaced by the frame that was
actually recorded, outlined in green, with its cross and corners drawn.

Controls present in the rail: board, the capture loop's gates, nominal optics,
acceptance, detector flags, exposure and gain. No pipeline panels, no MLA
sliders, no save-raw buttons.

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

Board parameters, and every other calibration setting, are frozen for the life
of a session: `Application.session_start` takes a deep copy. Changing the board
halfway through would make the poses already recorded describe a different
object, and nothing in the files would say so.

---

## 4. Detection: two detectors, not one

The first design ran `findChessboardCornersSB` over every micro-image of every
frame. It was correct, and it took the Pi down. Measured, on the synthetic
lenslet array, with the Pi estimated at three times an x86 desktop:

| approach | scope | x86 | Pi est. | two cameras at 1 Hz |
|---|---|---|---|---|
| SB corners, all tiles, full res | 117 tiles | 353 ms | ~1050 ms | **210% of a core** |
| SB corners, 5-tile cross, full res | 5 tiles | 15 ms | ~44 ms | 9% |
| saddle map, full res | all tiles | 21 ms | ~63 ms | 13% |
| **saddle map, preview res** | all tiles | 3.3 ms | ~10 ms | **2%** |

`scripts/benchmark_detectors.py` produces this table on whatever machine it is
run on; the numbers above are the desktop's.

Two conclusions, and the second is the one that was missed for longer.

**It never fitted.** More than one core, continuously, for a job the rig has to
do while also streaming two previews.

**Its cost was unbounded.** Tile count is quadratic in 1/pitch: a grid left near
its default pitch yields ~900 micro-images instead of ~130, and at smaller
pitches, thousands. A parameter with a slider could make the detector do an
arbitrary amount of work.

So there are now two detectors, doing different jobs at different rates.

### 4.1 The live one: saddle counting

A checkerboard corner is a **saddle** of intensity — the surface curves up
along one diagonal and down along the other — which is exactly `det(H) < 0` on
the Hessian. So

    S = Ixy² − Ixx·Iyy

is large and positive at a checkerboard corner and near zero on everything else:
edges are cylindrical, one curvature ~0; blobs are elliptic, both curvatures the
same sign. Take the local maxima of `S` and count them per micro-image with one
`bincount` against a cached pixel-to-tile label image.

Three separable convolutions and a dilation over the whole frame. **No per-tile
loop anywhere**, so the cost is set by the frame size alone — a wrong pitch can
no longer make it expensive. `calibration/presence.py`.

Discrimination, measured on the preview, median over whole micro-images:

| scene | count | peak strength |
|---|---|---|
| board | 29 | 33 000 |
| board, 3° rotation | 32 | 33 000 |
| board, heavy noise | 27 | 34 000 |
| board, **defocused** | 45 | 5 900 |
| grating scene, no board | 0 | — |
| lens cap | 0 | 0 |

**Two gates, and the absolute one is not optional.** A peak must be a local
maximum, must clear a fraction of the frame's own strongest response
(`rel_threshold`, 0.15), *and* must clear an absolute floor. A purely relative
threshold is scale-free, which sounds like a virtue and is a defect: it
normalises whatever is in front of the lens up to "detected". Measured on the
728×544 preview,

| what | S |
|---|---|
| a board corner | 2·10⁴ – 5·10⁴ |
| a busy but flat scene (the `gratings` target) | 5·10² – 2.5·10³ |
| sensor noise, σ = 2.5 grey levels | ~3·10¹, 11 500 peaks per frame |

so with only the relative gate a blank wall produced thousands of peaks and put
roughly ten micro-images in a hundred over a count threshold of 20 — a detector
reporting a board where there is none. It also made two discrimination tests
fail about one run in three, which is how it was found.

The floor is stated as a **minimum corner contrast in grey levels**, because
that is a property of the board and the light rather than of this code. The
bridge is measured and exact to three figures over the whole 8-bit range: an
ideal step corner of contrast `C` gives

    S = (1.061 · C)²

so `min_contrast = 40` (the default) means `S > 1800`. A printed checkerboard
under usable light gives 130+; noise and smooth gradients cannot reach it.

A tile that sees the whole pattern reads about **(cols+2)×(rows+2)** — every
grid vertex, not just the inner corners — so a 4×3 board reads ~30 and two
thirds of that is a good threshold. The Preconditions panel computes it.

**The trap:** a defocused board reads *higher*, because blur spreads the
response into more local maxima. The count cannot judge focus. The peak
strength can — it falls by an order of magnitude — and comes from the same
convolutions for free.

### 4.2 The accept one: a five-tile cross

`findChessboardCornersSB`, at full resolution, on the acceptance rule's minimum:
a centre micro-image and its four edge-neighbours. About 45 ms per camera,
**once per pose**, not per frame.

The cross centre is chosen from the presence map — the best-covered micro-image
that has four neighbours — not fixed at the array centre. The board is wherever
you are holding it, and testing the middle of the array when the board is in a
corner would reject exactly the poses the fit is short of.

Full-field detection over all 130 tiles still happens: offline, on the desktop,
from the recorded frames, where there is no time budget at all.

### 4.3 Where each one runs

The presence map is a **pipeline stage** on the preview frames the cameras
already produce. That is not an implementation detail. A four-core CPU stress
test on the rig did not crash it; a detection worker with its own thread calling
`capture_request()` did, repeatedly. Two consumers of a four-deep picamera2
request pool is the difference, and the presence map opens no second consumer at
all.

The one thing that does need a full-resolution frame — the accept check — asks
the **capture thread** for one: `CameraSource.request_full_frame()` raises a
flag, and the capture loop pulls `main` out of the request it is already
holding. Nothing outside that loop ever touches the device. A side benefit
worth having: the full frame is the same exposure as the preview frame it
arrived with, not a later one.

### 4.4 Units

The MLA parameters are expressed in **preview pixels** (728 × 544) because that
is the image the alignment overlay draws on. Detection crops from the **sensor**
frame (1456 × 1088). Handled in one place:

* `MLAParams.reference_width` / `reference_height` — the frame size the numbers
  were measured against, learned from the first preview frame and carried in the
  saved state. Hidden in the UI: it is a unit, not a knob.
* `MLAGeometry.rescaled(w, h)` — exact. Under the pixel-area convention a
  preview pixel at `x` maps to `(x + ½)s − ½`, and applying that to the origin
  gives `c_s + Δx·s` with no half-pixel remainder, so the offsets simply scale.
* `MLAGridOverlay.geometry_for(w, h)` — what every non-preview caller uses.

Pinned by `test_preview_scale_geometry_on_a_full_frame_finds_almost_nothing`.

A related precondition, added after the presence map went in: the preview must
**resolve** the board's squares. At an eighth scale a micro-image is 12 px
across and its squares are two, which no saddle detector will find — and the
symptom is the console asking for a board that is plainly in shot. Blocking.

---

## 5. Acceptance

### 5.1 The rule

A pose is accepted for a camera when **a connected cross of ≥ 5 tiles** — a
centre tile and its four edge-neighbours — each return the full pattern in the
same frame.

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

Auto-capture with no guard produces fifty near-identical poses while you hold
the board still, which inflates the count without adding information and biases
the fit toward whichever position you happened to rest at.

Two gates, at two different times:

**Live**, on the rig: the presence map must change by more than
`capture.move_threshold` before another pose is allowed. No PnP, no solve,
available on every frame. Because a saddle count fluctuates by one or two per
micro-image from noise alone — which across 130 tiles sums to about the size of
a real threshold — differences below `COUNT_NOISE` are discarded before the
comparison. Without that dead band a motionless board reads as moving and the
loop never arms; it was observed doing exactly that.

**Offline**, at the fit: PnP translation changed by ≥ `move_fraction` of the
working distance, or rotation by ≥ `move_rotation_deg`. The rigorous version,
run where a solve is affordable.

### 5.3 Stereo poses

Acceptance is evaluated per camera. `capture.require_all_cameras` decides
whether a pose needs both. On by default: with two heads looking at nearly the
same scene, a pose only one of them can use is usually a sign something is
wrong rather than a bonus. Turned off, a single-camera pose is still recorded
and still constrains that camera's own parameters — it just contributes nothing
to `R_LR`.

### 5.4 The hands-free loop

**The constraint: both of your hands are on the board.** You cannot press a key
to take a shot, you cannot choose which part of the array to fill next, and you
are looking at the board rather than at the screen. Everything below follows
from that.

| phase | meaning | what you hear and see |
|---|---|---|
| `SEARCHING` | too few micro-images show the pattern | "SHOW THE BOARD", amber |
| `HOLD` | it is there; waiting for the picture to settle | "HOLD STILL", blue, with a `n/4` counter |
| `VERIFY` | full frames grabbed, cross checked (~100 ms) | "CHECKING" |
| `REVIEW` | a pose was kept; the shot is on screen | rising two-tone beep, "CAPTURED", green |
| `MOVE` | refusing to record until the board moves | "MOVE THE BOARD", violet |

You move, pause, move, pause. Nothing else is required.

**Sound is the primary channel**, not a decoration: a rising pair on capture, a
low buzz on reject, a falling pair on discard. The screen is not where the
operator is looking, and a beep needs no attention at all. The event log carries
a monotonic sequence number so a slow or briefly disconnected page plays each
tone exactly once.

**The review pause.** After each capture the display freezes for
`capture.review_s` on the frame that was actually measured, with its cross and
corners drawn — not a later preview frame that resembles it. Escape within that
window discards the pose. That is the confirmation step; it costs nothing to
skip and is there for the shot that looked wrong.

**The keyboard override.** Space forces a shot through every gate. Some of the
most valuable poses — a board at an extreme angle, or in a dim corner of the
field — will never arm on their own, and a presenter clicker makes a serviceable
shutter release.

**Nothing auto-stops.** A session ends when you press Finish. The disk guard
(`min_free_gb`) and the optional pose cap are safety stops, not targets:
coverage is reported and never acted on.

**Nothing is deleted.** A discarded pose is marked in the index and its files
stay. A pose rejected by reflex and wanted back is worth more than the 3 MB it
occupies, and the offline tool honours the flag.

---

## 6. Progress display

### 6.1 Coverage map

One cell per lenslet, laid out as the array: 14 × 10 for this rig.

| state | appearance |
|---|---|
| 0 accepted poses | grey |
| 1 … target−1 | amber |
| ≥ target | **green** |
| seeing the board *right now* | blue |
| not a whole micro-image | near-black |

Two things are layered here and keeping them apart is the point: how many poses
a micro-image has banked over the session — the thing you are filling — and
which micro-images are seeing the board this second — the thing you are aiming.
The second comes free from the presence map that is already running.

Hovering a cell gives its pose count and its current corner count.

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

**Corners plus the frame** — and the second half is the part not to drop.

Corners are the measurement and they are tiny. But you cannot re-run detection
with a different detector, a different sub-pixel window, or a corrected crop
geometry without the pixels, and at some point you will want to. A mono frame
is 1.6 MB, a pose is two of them, and a 30-pose session is **under 100 MB**.
The storage argument for corners-only does not survive contact with the numbers
at this scale.

What the session writes, under the active storage root so it follows a device
change:

```
session_<timestamp>/
  calibration_<timestamp>/
    session.json         frozen settings, MLA geometry in SENSOR px per camera,
                         camera info, start time
    poses.jsonl          one line per pose: index, time, directory, discarded,
                         and each camera's cross and square size
    pose_0001/
      left.npy           full-resolution mono frame
      left.json          cross tile indices, corners in sensor coordinates,
                         square_px, exposure, gain, whether it was forced
      right.npy
      right.json
```

`session.json` carrying the frozen MLA geometry **in sensor pixels** is not
optional. A corner list without the crop geometry that produced it cannot be
interpreted, and that is the single most likely thing to be missing in six
months. Note that it is stored converted: the UI shows preview pixels, the file
records what detection actually used.

The corners in each pose are only the five-tile cross that let the pose be
accepted. Full-field detection is an offline job on `*.npy`, and the note field
in `session.json` says so in the file itself.

## 8. Session end

**Finish session** writes the manifest, runs the completeness check (tiles at
target, depth bands, stereo count) and reports what is short. It does not run
the fit — that is offline, and a session should be closeable without it.

A session left open when the process exits is recoverable: poses are written as
they are accepted, so the manifest can be regenerated from what is on disk.

---

## 9. State of the code

| area | what it does now |
|---|---|
| `optics/mla.py` | reference resolution, `rescaled`, `crop_origin`, `tile_to_frame` |
| `cameras/base.py` | the full-frame handshake. **No second camera consumer exists.** |
| `calibration/presence.py` | saddle counting: the live detector |
| `processing/stages/plenoptic.py` | `checkerboard_presence` stage, tinting the preview |
| `calibration/detect.py` | corner detection only — no threads, no camera, no files |
| `calibration/session.py` | the hands-free loop and the recorder |
| `app.py` | owns the open session, so a browser reload does not end it |
| `web/server.py` | `/api/calibration/{settings,readiness,session,start,stop,force,discard}` |
| `web/static` | the capture console: banner, tones, coverage, depth spread |
| `health.py` | temperature, load, memory, sticky under-voltage |
| `scripts/benchmark_detectors.py` | the table in §4, on your machine |

Not built: the fit itself (offline by design, calibration-spec §4), corner
identity across tiles for a board larger than one micro-image (§10.1), and the
desktop head (§11).

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

---

## 11. The desktop head — planned, not built

The Pi is a good sensor and a mediocre computer, and the split now in place
already assumes that: it runs a 2%-of-a-core presence map and a 45 ms accept
check, and everything else happens elsewhere. The natural end of that line is
to move the *console* off the rig as well, leaving the Pi doing nothing but
capture.

The shape it should take, recorded now so the current design does not quietly
close the door on it.

### 11.1 What the Pi would publish

A read-only stream of what it already computes, not a remote-control protocol.
One subscription, one message per preview frame, small:

```
{ t, seq, cam_id,
  presence: { counts, origin, whole, strength },
  sensor:   { ExposureTime, AnalogueGain, AeEnable },
  phase:    "hold",                     # if a session is running on the Pi
  jpeg:     <preview frame, optional> }
```

The presence map is about 130 small integers — under a kilobyte. A preview JPEG
at 728 × 544 is 40–80 kB, so 10 Hz of both is under a megabyte a second, which
Ethernet does not notice. Full-resolution frames stay on the Pi and are pulled
on demand, or synced after the session.

WebSocket is the obvious transport and fits the existing FastAPI layer. The
current MJPEG endpoint stays for the case where a browser points straight at the
Pi with nothing else running.

### 11.2 What the head would do

* Render the console. Same page, pointed at a different origin.
* Run **full-field** corner detection on frames pulled from the Pi, at whatever
  rate the desktop can manage — the 353 ms per pass that does not fit on a Pi is
  a comfortable 3 Hz here. That gives back the thing the cheap detector gives
  up: which micro-images find complete patterns, live, over the whole array.
* Own the fit, the coverage history across sessions, and the analysis.
* Subscribe to more than one Pi, if there is ever more than one.

### 11.3 What must stay true on the rig for this to work

Three properties the current code has, worth not losing:

1. **The session lives in `Application`, not in the page.** A head that
   disconnects and reconnects must find the session still running. This is
   already true, and it is why closing the browser mid-session is safe.
2. **The Pi decides.** Capture triggering must not depend on a network round
   trip; a dropped link during a session should cost you the display, not the
   poses. The state machine therefore stays on the rig and the head observes
   it. If the head ever wants to force a shot, that is one more endpoint, not a
   relocation of the loop.
3. **Every result is already serialisable.** `PresenceMap.as_dict`,
   `CaptureSession.state` and the pose sidecars are all plain JSON today
   because the browser needs them; a socket needs exactly the same objects.

### 11.4 What it does not solve

The rig still has to be aimed by someone standing at it, and that person is
still holding the board with both hands. The desktop head improves the analysis
and the record; it does not change the ergonomics of the capture loop, which is
why the loop was built to work with no head at all.
