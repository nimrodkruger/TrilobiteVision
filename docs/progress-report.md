# TrilobiteVision — orientation and progress

**For:** an incoming supervisor with a software and systems background.
**Purpose:** what the instrument is, why it is built the way it is, and where
it stands. Written to be read once, in order.
**Date:** 5 September 2026.

This assumes no optics. It does assume you will recognise a thread model, a
seam, and an argument about where validation belongs.

---

## 1. What the instrument has to produce

Two cameras, each with a microlens array behind the main objective, on one
Raspberry Pi 5. The deliverable is a **geometric calibration**: a statement,
for every sensor pixel, of the ray in object space that lands on it — valid at
any object distance, not fitted at one.

That phrasing is the requirement, and the words "at any distance" are the
whole difficulty. A conventional stereo rig is calibrated by registering two
images of a target at known positions, and the result is trustworthy near
those positions. Here the requirement is a ray model, so that registration
between views is a *consequence* of the calibration rather than a separate
thing measured per depth.

Everything below is downstream of that sentence.

---

## 2. The optics, in the amount needed to follow the software

### 2.1 What a plenoptic camera is

An ordinary camera puts one image on the sensor. A plenoptic camera puts a
grid of small ones there: a **microlens array (MLA)** sits just in front of the
sensor, and each lenslet forms its own tiny image. On this rig the lenslet
pitch is about 100 sensor pixels on a 1456 × 1088 sensor, so roughly **14 × 10
= 140 small images**, side by side, minimal overlap.

The point is that each small image sees the scene through a slightly different
part of the main lens. So a single exposure captures not just where light
landed but roughly which direction it arrived from — which is what makes depth
and re-registration possible from one shot.

"Focused plenoptic" (also Plenoptic 2.0) describes where the MLA sits relative
to the intermediate image the main lens forms. It matters for the arithmetic;
it does not change anything structural in the software.

### 2.2 The two ways to index the same pixels

This distinction is the single most common source of confusion, and it is
worth ten minutes now.

- A **micro-image** is what one lenslet puts on the sensor: an X × Y patch of
  pixels, and there are M × N of them (about 100 × 100 pixels, 14 × 10 of
  them). It is the physical structure on the sensor. It is what you look at to
  judge focus and alignment.
- A **sub-aperture image** takes *the same pixel* out of every micro-image —
  pixel (u, v) from all 140 lenslets, assembled in lattice order. It is an
  M × N picture (14 × 10) of the scene through one small patch of the main
  aperture.

These are the same numbers indexed two ways: one `permute` apart, nothing
interpolated, applying it twice returns the input.

**Why it matters:** a sub-aperture image is geometrically an *ordinary pinhole
camera*. A calibration model has a projection matrix for a pinhole camera and
nothing at all for a micro-image. One exposure yields X · Y of them, each with
a slightly different centre of projection.

The trade is resolution. A sub-aperture image is only as large as the lenslet
array — about 14 × 10 pixels. That is tiny, and it is exactly why the
calibration target has to put detectable structure *inside a micro-image*
rather than relying on the sub-aperture views being detailed. This drives an
open decision in §7.

### 2.3 The modelling decision everything rests on

> **The camera is a planar array of 140 identical pinhole cameras, differing
> only in where their centres sit.**

Each sub-camera has its own principal point (its micro-image centre) and its
own projection centre, but they **share** one focal length and one distortion
model, and their centres lie on a plane at a fixed depth in front of the lens,
arranged on a lattice that is a scaled copy of the micro-image lattice.

Three consequences, and they are why the model is small enough to fit:

- **Depth-independence is structural, not achieved.** The model is a
  projection from a fixed centre, so rays are defined for all Z. Nothing is
  calibrated "at a distance".
- **Baselines are not free parameters.** The whole array geometry is six
  lattice numbers and one scalar.
- **Grid rotation costs nothing.** The lattice matrix is a general 2 × 2, so
  it already carries rotation and skew. There is no rotation term in the
  projection and no decision about whether to include one.

The parameter count for the stereo pair is about **678, against roughly 80,000
observations**. That ratio is the reason this is tractable at all, and it comes
directly from the "identical pinholes" decision. The full derivation is in
`docs/calibration-spec.md`.

---

## 3. Architecture

15,900 lines: 10,600 of application, 3,900 of tests, 2,200 of scripts, 1,300
of MATLAB.

### 3.1 The split that shapes everything else

**The Pi captures and decides; the desk measures.**

The Pi is a four-core ARM machine running two 30 Hz sensors, a software JPEG
encoder (there is no hardware encoder on a Pi 5), a web server and a
processing pipeline. Full-field corner detection over 140 micro-images costs
about a second per camera. It does not fit in a live loop, and an earlier
attempt to make it fit took the rig down repeatedly.

So the heavy processing is offline and deliberately so. The Pi runs a cheap
live detector — a saddle-point counter, about 3 ms a frame, roughly 2 % of a
core — that answers only "is a board visible in this micro-image", which is
enough to drive capture. Everything that fits a model runs on a desktop
afterwards, from recorded `.npy` files.

### 3.2 Four seams

Each is a place a change is expected, and each is documented at the point it
would be changed.

| seam | what varies behind it |
|---|---|
| `CameraSource` (ABC) | the hardware. A new sensor is a subclass. Two synthetic backends implement it, which is why the entire stack — UI, pipeline, storage, detection — runs on a laptop with no camera attached. |
| `Stage` + `StageParams` | a processing stage *declares* its parameters as a pydantic model, and validation, the browser controls, and the settings block recorded in every image's sidecar all follow from that one declaration. Adding a stage is one file; the UI updates itself. |
| `LatestFrame` | the capture thread only reads, processes and publishes into a one-slot bus. It never encodes, never writes to disk, never waits on the network, so a slow browser cannot perturb capture timing. |
| `create_app` | MJPEG is the starting transport because it needs no client library. Replacing it with WebRTC touches this module and the page and nothing else. |

### 3.3 Threading, and the rule that is not negotiable

**Exactly one consumer touches a camera: its capture thread.**

An earlier design had a detection worker calling `capture_request()` on its own
schedule while the preview loop did the same. Two threads pulling from a
four-deep picamera2 buffer pool, one at 30 Hz and one at 1 Hz, took the rig
down repeatedly — and a four-core CPU stress test did *not*, which is how the
camera path rather than the load was identified as the cause.

Anything wanting a full-resolution frame now raises a flag; the capture loop
pulls it out of the request it is already holding and publishes it. Two things
fall out beyond not crashing: the full frame is the *same exposure* as the
preview frame it arrived with, and a request made while the camera is stopped
simply never completes instead of raising inside a thread with no way to
report.

### 3.4 Three frame rates, which are not the same number

A recent lesson worth generalising:

| rate | set by | governs |
|---|---|---|
| sensor | `cameras[].fps` (30) | how often the camera produces an exposure. Must be drained at this rate whatever else happens, or the request pool starves. |
| pipeline | `cameras[].process_fps` (follows the browser rate) | how often stats, levels, the grid overlay and the presence map actually run. |
| browser | `server.preview_fps` (12) | how often a JPEG is encoded and pushed. |

The reported symptom was "everything lags, including updating parameters".
The browser stream was already capped at 12 Hz. What was uncapped was the
*pipeline*, running the full stage chain on every sensor frame: 60 passes a
second across two cameras, on cores that also encode JPEG and answer the API.
Frames arriving early are now released without being decoded, and
`/api/status` reports all three rates plus the skipped count so the cap can be
seen working rather than assumed.

### 3.5 The dashboard

Six tabs, split by job rather than by what happened to be built when: System
(storage, host health, addresses, camera rates), one tab per camera (that
sensor's orientation and MLA alignment), Imaging (both sensors, capture),
Video (placeholder), Calibration (parked — §6).

The split is not cosmetic. Aligning a lenslet grid is a job you do once per
camera staring at one image; capturing a stereo set is a job you do with both.
Sharing one screen meant half the controls were wrong at any moment, and made
it possible to nudge a pitch mid-session without noticing which camera.

It is also the connection budget, which is a hard constraint rather than a
tuning parameter: a browser allows about six concurrent HTTP/1.1 connections
per origin and an MJPEG stream holds one open forever. Only the tab on screen
streams — one per camera tab, two on Imaging, none elsewhere.

---

## 4. Design decisions, and what each one buys

These are the load-bearing ones. Each was made the other way first, or nearly
was.

**MLA parameters are in full-resolution sensor pixels.** They were in preview
pixels first, which works and puts a conversion between the stored value and
every consumer of it — the detector, the crops, the recorded corners, the
offline readers. Forgetting the conversion anywhere is a silent factor of two,
and changing the preview resolution silently invalidates a stored alignment.
Sensor pixels have no such dependency: the pitch is `pitch_µm / pixel_pitch_µm`,
fixed by hardware. The only remaining conversion is drawing the overlay, where
being wrong is visible immediately rather than six months later in a fit.

**Orientation (rotation and mirroring) is applied at acquisition, and locked
once the grid is on.** Applied once, before anything else sees the pixels, so
the preview, the saved raw, the sub-aperture crops and the recorded corners
cannot disagree about which way round the image is. An orientation that lived
in the display pipeline would turn what you look at and not what you measure —
a difference nobody notices until the calibration comes back mirrored.

An earlier version carried an MLA alignment across a change of orientation by
transforming its offsets as an element of the dihedral group. The arithmetic
was right and the feature was wrong: it invited a change of frame
mid-calibration, and produced a grid that claimed to be aligned when nobody had
looked at it. It now locks while the grid is enabled and resets the alignment
if changed beforehand.

**De-rotating micro-image tiles is off by default, and that is a physical
statement.** An apparent rotation of the lattice has two possible causes. If
the *sensor* is rotated relative to the optical assembly, each micro-image is
rotated too and de-rotating restores the true field. If the *MLA* is rotated
relative to the sensor with the objective square to it, only the lattice of
centres rotates — each lenslet is rotationally symmetric and re-images an
intermediate image that has not moved, so the tile content is **not** rotated
and de-rotating adds a rotation that was never there. This rig is the second
case. Measured cost of the unnecessary resampling: ~0.07 px RMS added corner
noise against a ~0.15 px baseline.

**A capture is not "saved" until it is verifiably on the device.** `close()`
does not write to a disk; it hands bytes to the page cache and returns.
Metadata is journalled and data is not, so a power cut produces correctly named
zero-byte files — which is exactly what happened to a whole recording session.
Writes are now fsync'd, the directory is fsync'd, the size is verified, and the
byte count is displayed next to every capture. Cost: about 20 ms per frame.

**Offline reading exists in two languages, and they are checked against each
other.** `scripts/read_capture.py` and `matlab/` read the same files. They are
not assumed equivalent: the MATLAB extraction was compared pixel for pixel
against the Python one on the same file, maximum difference 0. That comparison
is how a real bug was found — MATLAB's `meshgrid` varies its first output along
columns and NumPy's `indexing='ij'` varies its first along rows, so the
resampling grid was transposed in a way no shape check could see. It showed as
a mean difference of 83.7 of 255, and nothing else would have caught it.

---

## 5. Verification approach

For a software reader this is probably the most transferable part.

**320 tests, all of which run with no hardware.** Two synthetic camera
backends drive the entire stack. One renders a lenslet array where every
micro-image holds a whole checkerboard, built so that a *correct* detector
finds one complete pattern per tile and an incorrectly scaled or offset one
finds none. That distinction is the value of it: with the real rig, "no
corners" could mean the crop is wrong, the board is the wrong size, the grid is
misaligned, or the lens cap is on.

**Tests are mutation-checked, not just written.** Every non-trivial claim gets
a deliberate break introduced to confirm the test fails for the stated reason.
Recent examples, all caught: swapping the 90° and 270° rotation matrices;
applying mirrors before the rotation; flipping a sign in the quarter-turn
count; composing an orientation matrix in the wrong order; removing a
validation lock; dropping a state-file dirty flag. One mutation was *not*
caught, and the code now says so — a safety margin in a search radius that is
genuinely slack, so that a future reader does not assume a test covers it.

**Where two components must agree, there is a test on the seam.** The rotation
feature has two halves — the code that moves pixels, and the matrix that claims
to say where they went — and the grid arithmetic trusts the second completely.
A sign error in each would cancel in every test that checked only one. There is
now a test that follows a marked pixel through the real orientation code and
checks it against the matrix, for all sixteen settings.

**The page is checked too.** Two bugs in the dashboard each cost an exchange
and were invisible to a Python test suite: a rotate control that existed but
was not on screen (the browser was serving a cached page — the index now sends
`Cache-Control: no-cache` and carries a build hash it compares against the
server), and a `<span id="live-actions"` missing its closing bracket, which
made the browser read two buttons as *attributes* of the span. The page is now
parsed in the test suite: every tag closes, and the twelve element IDs the boot
script resolves are real elements in the expected nesting.

---

## 6. Status

| area | state |
|---|---|
| Dual-camera preview, MJPEG to the browser | working |
| Sensor controls (exposure, gain, auto-exposure), saved and restored | working |
| Declarative processing pipeline, live reconfiguration | working |
| MLA grid overlay and sub-aperture crops | working |
| Full-resolution still capture, `.npy` + JSON sidecar, verified writes | working |
| Hot-pluggable output storage, live retargeting | working |
| Quick-record: space bar saves a raw stereo set | working |
| Offline reading in Python and MATLAB, cross-verified | working |
| Live checkerboard presence map | working — ~2 % of a core |
| Hands-free pose capture and recording | working, **parked** — see below |
| **Corner detection and the fit** | **not built** |
| Video recording | not built |
| Hardware sync between the two sensors | not built — needs XVS wiring |
| Full 4D light-field resampling (`lenslet_extract`) | placeholder |

**On-rig calibration is parked rather than removed.** The hands-free loop
works — the rig watches, gates, captures and speaks. It is not the supported
path today: quick-record plus offline processing is, and it is not settled that
real-time on-board detection earns its cost on a Pi. The machinery underneath
it (readiness checks, the coverage model, the pose manifest) is what any
calibration needs whether detection runs on the rig or on the desk, which is
why it stays.

**The fit is the largest remaining piece**, and it is specified rather than
speculative: `docs/calibration-spec.md` §4 sets out the residual, the
objective, the sparsity structure and a staged fit. §6 sets out the
verification, including the number that is the actual deliverable —
cross-view consistency on **held-out** poses, target < 0.2 px equivalent.

Both remaining pieces should be built against the synthetic backend first,
rendering a known lattice with planted parameters. Recovering planted ground
truth is the only way to separate an estimator bug from a rig problem.

---

## 7. Open decisions

Two are physical rather than software, and both block calibration work. They
are yours to have an opinion on.

### 7.1 Target design

A micro-image sees roughly a 4 × 4 corner fragment of a 9 × 6 board.
OpenCV's `findChessboardCornersSB` needs a *complete* rectangular pattern of
the size you specify, so a fragment of a larger board is not directly
detectable, and ChArUco markers do not survive at 20 px squares. Three routes:

| | approach | cost |
|---|---|---|
| (a) | small board, whole inside one micro-image | simplest detector; covers little field per pose, so more poses |
| (b) | an array of small boards on one target, each sized to a micro-image and identified by position | covers the field in one pose, detector stays trivial; needs a custom printed target |
| (c) | one large board, corner identity assigned by predicting each tile's field from the current parameter estimate | no special target, best coverage; bootstraps, and a mis-assignment is a structured outlier — the worst kind |

The detector as built assumes (a) or (b), because that is the only thing that
can exist before the target does. Choosing (c) later replaces one function and
nothing else: the geometry, the acceptance rule and the display are all
indifferent to how a tile's corners were found.

My inclination is **(b)** — it makes the detector a solved problem and moves
the difficulty to a printing job done once.

### 7.2 Lenslet aperture shape: square or circular

This decides whether grid rotation costs any usable crop area, and the two
cases differ in kind rather than degree.

| rotation | square apertures | circular apertures |
|---|---|---|
| 0° | 100 % of pitch | 70.7 % |
| 2° | 96.7 % | 70.7 % |
| 10° | 86.3 % | 70.7 % |

Square apertures rotate with the lattice, so the usable axis-aligned crop is
`1 / (|cos θ| + |sin θ|)`. **A circle has no orientation**, so with circular
lenslets rotation costs nothing at all — the bound is the inscribed square,
`1/√2`, whatever the angle. With circular apertures there is no reason to
minimise the mounting angle; with square ones, keeping it under ~2° costs under
4 %.

### 7.3 Smaller, and measurable rather than decidable

- The presence threshold (`min_corners`) and the target pose count per tile are
  both currently guesses. The first session's coverage map should set them.
- Whether one focal length across the array is justified, or whether MLA tilt
  demands a linear variation across it. This is a two-parameter extension,
  physically motivated, to be added **on evidence** from a diagnostic fit
  rather than assumed.

---

## 8. What the failures have taught, and why they are recorded

`docs/cleanup-log.md` is a running record of every behavioural change with its
reasoning and its rollback. It is long, and it is the most useful document in
the repository, because the failures on this rig share a shape:

> **The dangerous failures here are silent and confident.** They produce a
> plausible number, an image with obvious structure, or a file with the right
> name and size.

Four that cost real time:

1. **A whole recording session in a compressed format.** On a Pi 5, libcamera's
   default raw format for this sensor is `MONO_PISP_COMP1` — the imaging
   pipeline's *compressed* transport, not sensor counts. `make_array` hands
   those bytes back as a plain image with the right shape and obvious
   structure, so it looks like a picture that has gone slightly wrong rather
   than a decode failure. The same code on a Pi 4 got `R10` and worked. The
   format is now chosen explicitly and recorded in every sidecar.
2. **Row-stride padding, twice.** A raw buffer's rows are padded to a 64-byte
   stride and the array is shaped by that stride *in bytes*, delivered as uint8
   whatever the real pixel size is. A 10-bit 1456 px frame arrives 2944 wide —
   which is 1472 uint16 pixels, not 1456 plus padding. Cropping the width to
   1456 keeps the first 728 pixels and half of the next: structure, at the
   wrong scale.
3. **Correctly named zero-byte files** (see §4).
4. **A transposed resampling grid** in the MATLAB reader (see §4).

None of these raised an exception. Each was found by a comparison against
something independent — a second implementation, a closed form, a byte count.
That is the pattern the test strategy is built around.

---

## 9. Practicalities

```bash
python -m trilobite --config config/desktop.yaml   # no hardware needed
python -m trilobite --config config/pi.yaml        # on the rig
```

Then `http://<pi>:8000/`. Deployment to the Pi is `git pull`.

| where to look | for |
|---|---|
| `README.md` | install from a blank SD card, the dashboard, the API, the traps |
| `docs/calibration-spec.md` | the imaging model, parameters, the fitting problem, verification |
| `docs/calibration-ui-spec.md` | the acquisition workflow and the open target question |
| `docs/cleanup-log.md` | every behavioural change, why, and how to undo it |
| `docs/pi-troubleshooting.md` | recovery for a rig that will not answer |
| `matlab/README.md` | the offline reading path, and four things that will bite you |

Configuration is one heavily commented YAML file per rig; nothing about the
hardware is hardcoded in Python. Runtime parameters dialled in through the UI
are saved as a JSON overlay beside it, so the commented YAML stays the readable
record of intent.

---

## 10. What I would like from a supervisor

In rough order of value:

1. **A decision, or a decision process, on the target (§7.1).** It blocks the
   detector, and the detector blocks the fit.
2. **The aperture shape (§7.2)**, which may be a five-minute answer from the
   MLA datasheet and closes a live uncertainty about mounting tolerance.
3. **A view on whether on-rig real-time calibration is worth completing**
   (§6), or whether offline-only is the right permanent answer.
4. **Review of the fitting plan** in `docs/calibration-spec.md` §4 before it is
   implemented, since it is the part where an error is most expensive to find
   later.
