# Sensor portability — review strategy

**Status:** proposal for review. **No code changes are proposed by this
document.** It sets out what must be true before the sensors are swapped, how
each claim can be checked, and what the supervisor is being asked to decide.

**Date:** 10 October 2026
**Scope:** backend only. The user interface is explicitly out of scope: the
dashboard takes its geometry from the cameras at run time and must continue to,
so "the UI works with the new sensor" is a consequence to verify, not work to
do. Candidate sensors are restricted to those supported by libcamera on
Raspberry Pi OS.

---

## 1. What is being proposed, and why

The current heads are two mono global-shutter **IMX296** (1456 × 1088,
10-bit). The reason to change is **row noise** — the horizontal banding that
has been visible since the earliest captures and that survived a cable swap, a
different SD card and the Stage 2 format work. The proposal is to move to a
sensor with better row-noise behaviour.

Two things follow immediately, and they are the reason this document exists
rather than a patch:

- **A sensor swap is not a configuration change.** It changes the frame size,
  very likely the sample depth, possibly the colour filter, certainly the
  frame-rate limits, and possibly the shutter type. Every one of those is
  carried through the software, the stored files and both readers.
- **The row noise has not been attributed.** Changing sensors to fix an
  artefact whose cause is unestablished is a plausible move and an expensive
  one, and if the cause is in the CSI-2 link, the supply, or the PiSP
  pipeline rather than the sensor, a new sensor will show it too. §8 treats
  this as the first question, before any of the portability work.

---

## 2. The claim to be reviewed

The claim this document asks the supervisor to test is:

> **Nothing in the software stack needs to know which sensor is attached.**
> Geometry, sample depth, raw format, control set and frame-rate limits are all
> discovered from the camera at open time and carried with the data; a
> different Pi-compatible sensor requires a configuration file change and no
> source change.

That claim is **mostly true today and not entirely true**, and the value of
reviewing it before the hardware changes is that the exceptions are cheap to
find now and expensive to find with a half-built rig on the bench.

The claim decomposes into five propositions, each of which can be tested:

| # | Proposition | How it is tested |
|---|---|---|
| P1 | Frame geometry comes from the camera, never from a constant | §4 inventory + the matrix in §6 |
| P2 | Sample depth and raw format are established per sensor, not assumed | §5.2, existing Stage 2 admission tests |
| P3 | Nothing in the processing or storage path breaks on a different frame shape | §6 matrix |
| P4 | Both readers open files from the new sensor with no edit | §6 matrix, round-trip |
| P5 | The dashboard renders and controls the new sensor with no edit | §6 matrix, browser scenarios |

---

## 3. Where geometry comes from today

There is a single source of truth, and it is worth stating because most of the
portability argument rests on it.

```
picamera2 → PiCameraSource._configure_and_start()
              full  = cfg.full_resolution or picam.sensor_resolution
              raw   = picam.sensor_resolution, format from _choose_raw_format()
              → read back from picam.camera_configuration()["raw"]
          → CameraInfo (describe())  ← every consumer asks here
```

`CameraInfo.full_resolution` and `.preview_resolution` are what the pipeline,
the MLA stage, the calibration settings, the recording plan and the web layer
all use. `config.full_resolution` is an **override**, not a declaration: left
unset, the sensor's own resolution is used. That is already the right design
for portability.

Three properties of the existing design that a swap depends on, all of which
were built for other reasons and happen to carry the weight here:

- **The raw format is read back, not assumed** (Stage 2, review item R5). What
  the driver negotiated is what admission checks against. A new sensor
  advertising a format this code has not seen is refused with a message naming
  the format, rather than silently misread — which is the behaviour that
  matters most during a swap.
- **Controls are intersected with what the sensor advertises.**
  `_split_controls` drops unknown controls with a warning instead of aborting,
  which is what lets one config file serve a mono and a colour rig.
- **Mono is detected, not configured** — from the raw format string and then
  firmed up from the negotiated format.

---

## 4. Inventory of sensor-specific assumptions

Graded by what happens if the new sensor differs. This is the part the
supervisor should check hardest, because it is the list that decides whether
the swap is a config edit or a sprint.

### 4.1 Would silently produce wrong results

| Where | Assumption | Consequence |
|---|---|---|
| `calibration/settings.py:_full_resolution` | falls back to `(1456, 1088)` when `describe()` raises | detection geometry computed against the wrong frame; corner coordinates wrong by the ratio, and nothing says so. The fallback exists so the UI can render before a camera opens, which is reasonable — returning the wrong numbers silently is not. |
| MLA geometry (`optics/mla.py`, `processing/stages/plenoptic.py`) | pitch and offset are stored in **sensor pixels** against a recorded `reference_width` | already rebased with a warning when the frame size changes, which covers a sensor swap — **but the rebase is a scale, and a different pixel pitch is not a scale.** See §7. |
| `config.yaml` as deployed | `full_resolution` and `preview_resolution` set explicitly | a stale explicit size silently crops or upscales the new sensor. The override is the hazard, not the mechanism. |

### 4.2 Would fail loudly (acceptable, but must be anticipated)

| Where | Assumption | Consequence |
|---|---|---|
| `cameras/rawformat.py` `KNOWN_FORMATS` | an allowlist of `R8/R10/R12`, their packed forms, and Bayer `S*10/12` | a sensor whose negotiated format is outside the list refuses to open, naming the format. Correct behaviour; a 14-bit or 16-bit sensor needs a one-line addition plus its admission tests. |
| `recording/ladder.py` | 8-bit storage requires delivered samples deeper than 8 bits | a genuinely 8-bit sensor simply stops offering the 8-bit rung. Correct. |
| `recording/manager.py:common_geometry` | both heads report the same frame | a mixed pair refuses to arm a recording, by design |
| `picam.request_frame_rate` | the driver exposes `FrameDurationLimits` and accepts the requested duration | refuses with the sensor's own accepted range. Correct, and the rate ladder in the UI will offer rates the new sensor may reject — see §6, M6. |

### 4.3 Documentation and comments that will become wrong

Roughly thirty references to `1456`, `1088`, `1472`, `2944` and "IMX296" in
docstrings and comments across `rawformat.py`, `read_capture.py`,
`tv_read_capture.m`, `tv_selftest.m`, `mla.py`, `orientation.py` and
`plenoptic.py`. **These are explanations, not logic** — the stride arithmetic
they illustrate is computed, not hardcoded. They still have to be revised,
because a worked example in the wrong numbers is how the next person
misunderstands the stride rule, and that rule is the one that already produced
a session of white-noise frames.

### 4.4 Tests

Hard-coded `1456 × 1088` appears in several test modules. Those are fixtures
rather than assumptions, but they mean **the suite currently proves the stack
works for one geometry**. §6 proposes fixing that, and doing it *before* the
hardware changes is the single highest-value piece of work in this document.

---

## 5. The classes of difference a new Pi sensor can present

What the supervisor should expect to be asked about, in rough order of how much
it costs to absorb.

### 5.1 Resolution and aspect ratio
Absorbed by `CameraInfo`, subject to §4.1. Larger frames raise the recording
bandwidth proportionally: Stage 5c's arithmetic is per-pixel, so a 4056 × 3040
sensor at 30 fps in two heads is **≈ 740 MB/s at uint16** — far beyond the
USB 3 SSD and beyond the Pi's USB controller. A larger sensor therefore
changes the *recording* answer, not just the geometry. This has to be settled
before a sensor is chosen, not after.

### 5.2 Sample depth
A 12-bit sensor already has a `KNOWN_FORMATS` entry. The things that follow
depth are the admission value ceiling, the `raw_alignment` question (lsb vs
msb in the 16-bit word — still unconfirmed on the current rig), and the 8-bit
storage shift, which is derived per frame from the recorded depth and
alignment and so needs no edit.

### 5.3 Mono versus Bayer
The format layer handles both and the pipeline accepts `rgb8`. But every
calibration and MLA assumption in this project is written for a **mono**
sensor: a Bayer sensor under a microlens array means the colour filter mosaic
and the lenslet lattice interact, and demosaicing across a micro-image boundary
is meaningless. A colour sensor is a research decision, not a port.
**Recommendation: restrict candidates to mono.**

### 5.4 Shutter type
The current sensor is global shutter. Most Pi-compatible sensors are **rolling
shutter**. For a static calibration target this may be acceptable; for anything
moving, and for any claim about the two heads being comparable frame by frame,
it is not. The recording path records per-frame `SensorTimestamp` and claims no
synchronisation, so nothing in the software breaks — but the *meaning* of a
frame changes, and a rolling-shutter frame is not one exposure. This is a
decision for the supervisor and the single most consequential item here.

### 5.5 Frame-rate limits and sensor modes
Each sensor has its own duration range and its own mode list (full, binned,
cropped). Two consequences: the rate ladder must be filtered against the
driver's reported limits rather than a constant list, and a **binned or
cropped mode changes the effective pixel pitch**, which invalidates the MLA
geometry exactly as a different sensor does. The current code configures the
raw stream at `sensor_resolution` and does not choose modes, which is the safe
default and should be preserved.

### 5.6 Stride and padding
Computed from the negotiated stride where the driver reports one, and inferred
from a 64-byte alignment rule otherwise. Generic. The worked examples in the
docstrings are not.

### 5.7 Lens mount, sensor size and optical format
Not software. Named here because it blocks everything: see §7.

---

## 6. Proposed verification strategy

The principle: **prove portability against a parameterised fake sensor before
the hardware arrives**, so that what remains on the bench is confirming the one
real device rather than discovering the design.

### M1 — A parameterised sensor fixture
Extend the synthetic backend (or add a `FakeSensor` test double) taking
*width, height, bits, alignment, mono/Bayer, fps limits, stride rule* as
parameters. The synthetic source already delivers 10-bit samples in a uint16
word for this reason; this generalises it.

### M2 — A geometry matrix across the whole stack
Run the existing end-to-end paths — open, preview, pipeline, still capture and
sidecar, burst, continuous recording, both readers — over a matrix of at least:

| | width × height | bits | notes |
|---|---|---|---|
| A | 1456 × 1088 | 10 | today, the control |
| B | 1920 × 1080 | 10 | wider, 16:9 |
| C | 2028 × 1520 | 12 | larger and deeper |
| D | 640 × 480 | 8 | small and shallow; exercises the no-8-bit-rung path |
| E | 1457 × 1089 | 10 | **odd dimensions**, which is where stride, tiling and any `// 2` will break |

Row E is the one that will find things. Nothing in the project has ever seen a
frame whose width is not a multiple of 16.

### M3 — The grep that must come back empty
A test that fails if a sensor dimension appears as a literal in `src/`: no
`1456`, `1088`, `1472`, `2944` outside comments and the allowlist. Mechanical,
cheap, and it stops the next regression rather than finding this one.

### M4 — Remove the silent fallback
`calibration/settings.py:_full_resolution` must either refuse or report that it
is using a placeholder, so a geometry it guessed cannot be mistaken for one it
was told. This is the one change in §4.1 worth making before the swap
regardless of which sensor is chosen.

### M5 — Reader portability
`scripts/read_capture.py`, `tv_read_capture.m`, `tv_read_recording.m` and
`tv_recording_frame.m` opened against files generated at every geometry in M2,
including odd widths and 8-bit chunks. The round-trip test added for Stage 5
already does this for one geometry and parameterises cleanly.

### M6 — Rate ladder from the driver
`ladder.RATE_LADDER` is currently a fixed list filtered only by the configured
sensor rate. It should be filtered by the driver's reported
`FrameDurationLimits`, so the UI cannot offer a rate the new sensor will
refuse. Small, and it is the one place the new sensor's limits reach the
operator.

### M7 — Browser scenarios at a second geometry
The dashboard scenarios run against the synthetic backend; running them at one
further geometry is close to free and is what substantiates P5.

### M8 — Mutation checks on what M2 would hide
A geometry matrix that passes because every path quietly reshapes is not
evidence. The existing `tools/mutate_stage5.py` pattern applies: remove the
chunk geometry check, the MLA rebase, the stride reconciliation, and confirm
the matrix fails.

**Order:** M3 and M4 first (they are findings, not work), then M1 and M2, then
M5–M8. M1–M8 are all possible with no new hardware.

---

## 7. Outside this document, and blocking

Stated because a software portability review that ignored them would be
misleading.

- **Pixel pitch changes the optical design.** The MLA was matched to the
  IMX296's pitch and its F-number. A different pitch changes the number of
  pixels per micro-image, and therefore the angular sampling, the useful
  depth range and whether the array is still focused-plenoptic at all. The
  software rebase handles a change of *frame size*; it cannot handle a change
  of *pixel size*, and nothing in the code will notice the difference.
- **Sensor format and mount.** A different optical format changes the image
  circle the MLA and the main lens have to cover.
- **Existing calibration data does not transfer.** Every stored MLA alignment,
  every pose set and every acceptance threshold is expressed against the
  current sensor. They are not portable and should be archived rather than
  migrated.

A sensor swap is therefore an optical redesign with a software consequence,
not the reverse. The software work in §6 is worth doing first because it is
cheap, it is useful regardless of which sensor is chosen, and it stops the
software from being the thing that is uncertain while the optics are being
settled.

---

## 8. What the supervisor is asked to check or decide

1. **Has the row noise been attributed?** Before anything else. It is not yet
   established whether it is the sensor, the CSI-2 link, the supply, or the
   PiSP pipeline. `scripts/diagnose_lines.py` exists for this and its output
   should be in the record before a sensor is chosen. *A sensor swap that does
   not fix the artefact is the expensive outcome here.*
2. **Global shutter: required, or preferred?** (§5.4) This is the constraint
   that most narrows the candidates, and the answer depends on whether
   anything in the intended measurements moves.
3. **Mono only?** (§5.3) Recommended, and a supervisor should confirm it
   rather than have it assumed.
4. **What resolution is acceptable?** (§5.1) Larger is not better here: the
   recording bandwidth is linear in pixels and the current path is already
   within 2× of the USB controller's limit at 1456 × 1088.
5. **Is the §6 work approved before hardware is ordered?** M1–M8 need no new
   parts and would otherwise be done under time pressure on a bench.
6. **Who owns the optical redesign in §7**, and is it sequenced before or
   after the sensor choice?

---

## 9. Honest unknowns

- **Which sensors are actually supported** by the libcamera and kernel
  versions on this rig is a fact to be established on the rig, with
  `libcamera-hello --list-cameras` and the overlay list for the installed
  kernel — not from a datasheet or a vendor page. Third-party modules
  frequently need an out-of-tree overlay, which is a maintenance burden that
  belongs in the decision.
- **Whether a candidate's row noise is actually better** is a measurement on
  the bench, not a specification. It is worth agreeing in advance what would
  count as better: the existing diagnostic produces per-row statistics, and a
  before/after on the same target under the same illumination is the
  comparison that would settle it.
- **`raw_alignment` on the current sensor is still unconfirmed** (lsb vs msb).
  That is a loose end on the existing rig that should be closed before it is
  re-opened on a new one, because the two differ by 64× per pixel and the
  symptom is a plausible-looking image at the wrong scale.
