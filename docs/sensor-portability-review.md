# Sensor portability — whole-stack review process

**Status:** revised review plan; not a portability certification or an application
patch. **Updated:** 11 October 2026. **Source check:** `c3aa989`.

This revision corrects the 10 October proposal after targeted source inspection.
It defines how to conduct the full sweep, what evidence to collect and what
counts as support. The full code review, test matrix and hardware trials have
not yet been performed. Keep this proportionate to a research rig: extend the
existing tests and metadata, fix demonstrated gaps, and avoid a new framework
unless a concrete limitation requires it.

## 1. Purpose and scope

The user is considering replacement sensors because the current sensor format
is unsuitable. Row noise is a separate unresolved investigation, not an assumed
cause or a prerequisite for portability work. Record candidate optical-format,
resolution and sampling requirements without making sensor selection dependent
on resolving noise first.

**The scope is the entire stack:** installation and camera identification,
configuration, acquisition, processing, calibration interfaces, preview/UI/API,
stills, burst and continuous recording, storage, export, Python and MATLAB
readers, saved-state restoration and diagnostics. The UI cannot be excluded:
incorrect options, coordinate mapping or old settings can invalidate an
otherwise correct backend. This calls for functional checks, not a UI redesign.

The initial integration target is **Raspberry Pi 5 CSI cameras exposed through
the supported Picamera2/libcamera stack on a recorded Raspberry Pi OS build**.
A compatible connector or vendor claim does not establish application support.
Include vendor drivers/overlays where a candidate needs them, but test and
identify that software combination separately. USB/UVC cameras, bridges and
vendor-only APIs are not automatically covered by this adapter; list such a
candidate as requiring a separate backend review rather than quietly excluding
it or calling it supported.

Do not narrow the review to mono sensors. Cover mono and Bayer acquisition and
storage; assess colour-specific scientific processing separately. Likewise,
rolling shutter does not make a camera software-incompatible. Record its
measurement limitations without presenting global shutter as a universal rule.

## 2. Replace the universal claim with a verifiable one

The goal is that a sensor or mode change within a supported capability set
requires configuration and renewed calibration, not sensor-specific application
code. The whole application need not know a model name, but it **must** know
what its streams and samples mean. A new format or vendor backend can require
an implementation extension; discovering that is a review result.

A useful support record is one row per **camera module + driver/software build
+ sensor mode + output format + single/dual-head configuration**. Record preview,
stills, burst, continuous recording, export, Python, MATLAB and relevant
scientific operations separately. Use these evidence labels:

- **Verified:** the claimed operation passed its automated and applicable real
  Pi tests for this configuration.
- **Desktop-tested / hardware pending:** simulated or fixture evidence only.
- **Limited:** supported operations and restrictions are explicitly named.
- **Unsupported:** the operation is unavailable with a clear reason.

A safe refusal is a successful negative test, **not interoperability for that
operation**. A camera that previews but cannot record is not fully supported.
For a candidate to be called fully interoperable, every agreed required
operation must be verified on its intended configuration. No finite review can
certify every present or future Pi-compatible sensor from fake-camera tests.

## 3. Corrections to the original inventory

These are source observations or review targets, not a completed defect list.
Each must be confirmed with a focused test during the sweep.

| ID | Observation in the current tree | Correction to the review process |
|---|---|---|
| S1 | `calibration/settings.py` defines `PIXEL_PITCH_MM = 3.45e-3`, used in focal-length/baseline calculations and inverse conversion. | Search physical constants and units as well as image dimensions. A new sensor must not inherit IMX296 physical pitch. Require a per-head value with a source, or report the derived metric as unavailable. |
| S2 | `_full_resolution()` in that file returns `(1456, 1088)` after missing information or an exception. | Test unopened/failed cameras. A placeholder may support layout, but must never enable calibration or be persisted as measured geometry. |
| S3 | The class is `Picamera2Source`. `_configure_and_start()` requests main/raw/lores; `_read_back_raw()` reads actual raw configuration. `describe()` reports `_full_res` and configured preview size. `CameraRuntime.start()` uses that description to register recording geometry. | There is not yet a demonstrated single actual-geometry source for all streams. Trace raw, main, lores, oriented display and stored recording sizes separately, including driver-adjusted requests. |
| S4 | `KNOWN_FORMATS` includes mono `R16`, but Bayer entries are only 10/12-bit names and their packed forms. | The old inventory omitted R16. Test actual Pi 5 negotiation, including Bayer 16-bit containers. Adding an allowlist name alone is not proof of decoding, sample depth, alignment or reader support. |
| S5 | `_admit_raw()` supplies a format, size, stride and configured alignment to `admit()`; nominal depth comes from the format table. | Verify the connection from selected sensor-mode bit depth to container interpretation and downstream reduction. A 16-bit container must not be mistaken for a 16-bit sensor sample. |
| S6 | `recording/manager.py:common_geometry()` rejects unequal head sizes; a common plan and `_store_shift` are shared. | Test equal-size heads with different depths/alignment as well as unequal sizes. Mixed-head support is a real gap to assess, not already satisfied by a refusal. Trace bytes, reduction and metadata per head. |
| S7 | `recording/ladder.py` defaults exact storage to uint16; its 8-bit option is explicitly a lossy reduction. `reduce_to_store()` performs that reduction in Python despite older module comments. | Audit native 8-bit exact recording separately from reducing deeper samples. Native 8-bit input is not inherently diagnostic. Verify actual output dtype and file size, not the option label or comments. |
| S8 | `app.py` restores state by logical `cam_id`; capabilities are captured at open. | Check replacement under the same name, reordered camera indices and mode changes. Stale controls, geometry, capabilities and calibration must not silently transfer. |
| S9 | `scripts/probe_cameras.py` includes IMX296-specific overlay advice. | Include diagnostics, deployment configuration and operator instructions in the sweep. Advice for one sensor must not configure a replacement as an IMX296. |
| S10 | Recording round-trip fixtures already use 128 × 96, and other tests use small synthetic frames. | The suite does not prove only one geometry. Inventory actual coverage before adding tests; a small synthetic frame still does not exercise real raw-byte negotiation. |

The original bandwidth example was low by a factor of two: two 4056 × 3040
heads at 30 fps stored as uint16 require
`4056 × 3040 × 2 bytes × 30 × 2 = 1,479,628,800 bytes/s`, about **1.48 GB/s**
before overhead; about 740 MB/s is one head. This is an illustrative workload,
not evidence that a particular module offers full resolution at 30 fps. Use
actual mode/rate combinations and measured storage performance.

## 4. What the reviewer must trace

For every advertised operation, follow one identifiable frame from the driver
to its final reader/display. At each boundary record the producer, consumer,
geometry, representation, transformations, metadata and failure outcome. Review
all callers, not only the preferred capture route.

### 4.1 Discovery, modes and camera identity

Inventory `config.py`, camera adapters/registry, `app.py`, `state.py`, deployed
YAML/service/overlay configuration and camera diagnostic scripts. Collect:

- Logical head name, enumerated camera ID/model and available stable device or
  port identity. Indices can change; a model name alone cannot distinguish two
  identical modules. Explain identity limits where serial numbers are absent.
- OS/kernel, firmware, Picamera2, libcamera, rpicam-apps and vendor driver/tuning
  versions. Record requested settings and post-configuration results separately.
- Mode list, selected sensor mode, crop/readout region, bit depth, frame-duration
  range and actual raw/main/lores formats, sizes, strides and buffer layout.
- Optional features: autofocus, automatic exposure/white balance, HDR and
  sensor-specific controls. These are capabilities, not obligations to implement
  every feature. Unknown advanced modes must not silently alter raw meaning.

Full physical sensor resolution, sensor-mode output, main-stream resolution
and preview resolution are different concepts. Test negotiation where all
three streams differ. Requesting native size is not proof of the actual mode;
selecting the widest advertised bit depth must not pick an incompatible size
or rate. Re-query mode-dependent capabilities after reconfiguration.

Check camera loss/start failure and clean stop/restart with a different sensor.
A failed candidate must not strand the other head or resurrect old capabilities.
Reuse the owner model and existing failure tests; do not start concurrent SDK
access merely to probe capabilities.

### 4.2 Raw interpretation and numerical meaning

Review `cameras/rawformat.py`, still and `read_raw()` paths, `Frame`/`RawRead`,
recording conversion, stored metadata and both readers as one contract.

Keep distinct: sensor sample depth, container bits, dtype, byte order, packing,
compression, alignment, valid image width, byte stride and Bayer order. Preserve
source evidence and any reservations across every save/read/export path.

The Picamera2 guide documents Pi 5 unpacked output in left-shifted 16-bit words,
including Bayer names such as `SBGGR16`; sensor depth is obtained separately
from sensor configuration. It also documents configuration-dependent control
limits. Check the deployed versions rather than inferring either from the
requested format or old IMX296 fixtures. [Picamera2 guide, raw stream configuration and controls](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)

Require exact expected values for supported lossless paths. Test zero, saturation,
low-bit patterns and byte-order markers, not just natural-looking pictures or
matching shapes. For deliberate 8-bit reduction verify the defined shift,
error bound, dtype and recorded loss. Missing depth/alignment must not become
an unexplained cast. Distinguish unchanged native-8 data from reduced data.

Audit display normalisation, clipping, histogram/statistics and calibration
thresholds for fixed count ranges. Carry black/white-level information and its
scale when established; missing levels are not a reason to invent IMX296
defaults. Raw counts are not automatically comparable radiometry across sensors.
Check ISP RGB/BGR/YUV channel ordering separately from raw, and record any
tuning, HDR or other processing that changes output meaning. Keep native
metadata types/units intact through JSON conversion, including optional fields
and enum-valued controls.

Known packing/compression is not automatically decodable: exercise advertised
but unsupported modes and the diagnostic escape path. Require an explicit
supported conversion or a visible restriction; diagnostic bytes must never be
promoted to measurements. An attractive image is not proof that every row or
sample has the right meaning.

Do not call stride handling generic solely because it uses arithmetic. Test
negotiated padding beyond the current 256-byte bound, missing stride, byte views,
uint16 views, non-contiguous arrays and malformed rows. The core admission
fallback currently uses a bounded padding allowance; it is not a universal
64-byte rule. Audit separate legacy-reader heuristics too.

### 4.3 Geometry, physical units and saved calibration

Trace `orientation.py`, `optics/mla.py`, processing stages, calibration settings/
sessions, state restoration and the readers. Use coordinates in explicitly
named spaces: physical array, active mode/crop, raw pixels, oriented pixels,
main/preview and display coordinates. Width-only scaling cannot establish an
arbitrary crop, aspect-ratio change, binning or transform.

A crop changes origin/field of view, not physical pixel pitch. Binning or
subsampling changes sampling; main-stream resizing is a separate operation.
Record the relevant mapping rather than calling all three a pitch change.

Where available, the guide describes `UnitCellSize` as an x/y size in nanometres
and `ScalerCrop` in full-sensor coordinates. Allow a documented module-specific
physical value when the driver lacks it; preserve units and origin of evidence.
Do not invent a value from image width. [Picamera2 guide, camera properties](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)

On a module, mode, crop or relevant orientation change, old MLA/calibration
settings are candidates for reuse, not accepted calibration. Test a new sensor
with **the same dimensions but different physical pitch**. Preserve old data,
mark incompatible settings as needing alignment/calibration and avoid silently
rescaling them into a claim of validity. A harmless image-only resize may have
an established transform; verify that specific case rather than migrating all
calibrations or discarding all settings indiscriminately.

For Bayer, preserve native and stored mosaic order through crop parity, flips
and 90-degree rotations. Test all four common Bayer patterns with recognisable
colour samples. An ISP luma preview can work while raw Bayer is still mishandled.
Keep exact raw acquisition distinct from colour rendering and MLA scientific
processing; define which research operations are valid. Do not silently
average, demosaic or treat a mosaic as mono sensor counts.

### 4.4 Controls, timing and browser/API behaviour

Review control discovery, validation, application and readback, including
`raw_capabilities()`, `request_frame_rate()`, the recording rate choices and
web endpoints. Test absent controls, unsupported enum values, numeric bounds,
invalid requests, accepted-but-clamped settings and delayed effective values.
Optional unsupported controls can be disabled with explanation; a required
recording-rate request cannot be silently dropped.

Use the current mode's capability ranges and exposure constraints for proposed
rates, then verify observed timestamps/rate. For dual heads, test the common
feasible rate and rollback if only one accepts a change. Lowering FPS does not
by itself guarantee longer exposure, uniform delivery or less read noise.

Preserve original timestamps, units, documented event/clock interpretation and
host timing separately. Test absent or repeated metadata and distinguish
application sequence counts from driver exposure sequence. Global versus
rolling shutter belongs in the data record; readout timing may remain unknown.
Do not infer synchronisation or correct rolling-shutter motion from two similar
frame timestamps. Missing metadata limits a measurement, not necessarily viewing.

Browser checks include aspect ratio, orientation, overlays/click coordinates,
per-camera control ranges, actual recording options, byte/duration estimates,
unsupported-operation messages and stale controls after a swap. No new dashboard
architecture is required. Include API calls that bypass the visible controls.

### 4.5 Recording, storage, export and readers

Trace `recording/{manager,ladder,buffer,burst,continuous,chunks,preflight}.py`,
application handoffs, storage and every currently offered export. The working
chunk-based recording path is part of scope; do not assume everything is AVI
because it was requested in an earlier plan. Review AVI/FFV1 only where offered,
or label it unimplemented rather than silently adding a feature to this sweep.

For each head use its actual delivered shape/dtype, sample interpretation,
orientation convention, rate and source identity. Budget dual-camera RAM and
throughput as the **sum of each head's costs**. Exercise different shapes,
equal shapes with different depths, different orientations, different rate
limits and mono/Bayer pairs. A common-plan restriction is a finding to remove
for required mixed-head operation, or an explicit limitation preventing that
claim. Verify any suggested single-head workaround is actually selectable.

Test burst sizing and flush/retry, continuous queue/drop policy, chunk boundaries,
partial final chunks, start/stop and reset of capabilities after a swap. Re-run
slow/full/lost-storage protection with larger frames; external-only policy and
its explicit internal-storage override remain unchanged. Neither an allocation
that returns nor an empty preflight buffer proves the real workload fits.

Verify `scripts/read_capture.py`, `tv_read_capture.m`, `tv_read_recording.m`,
`tv_recording_frame.m` and any remaining public loaders/exports against the same
small golden files. Check pixels, dtype, channel/mosaic layout, row/column order,
orientation exactly once, numerical scale, per-head metadata, validity, timestamps
and frame accounting. Missing/unknown schema or capability fields must produce
an explicit inspection/unsupported result, not new sensor defaults. Opening a
file without an exception is not a round-trip test.

## 5. Test design — layers, not one large Cartesian product

### Layer A: current coverage and contract tests

First map existing tests to the contracts above. Search for model names,
dimensions, physical constants, count ceilings, bit shifts, byte alignment,
mono/channel assumptions, control names and fixed rate limits. Classify hits as
runtime assumptions, intentional defaults, fixtures or accurate examples.

**Replace the original 'grep must be empty' rule.** Legitimate IMX296 regression
fixtures and labelled examples should remain. Test that changing capabilities
changes behaviour correctly; use an allowlisted/static check only for a specific
regression if it adds value. A keyword scan cannot establish portability.

### Layer B: fake Picamera2/driver at the adapter boundary

A richer synthetic source alone bypasses `_configure_and_start()` and decoding.
Use a fake SDK exercising the **real adapter**, with separate requested and
negotiated configurations, real-shaped byte buffers, per-mode controls and
metadata. Independently construct the expected output; do not generate it using
the same trimming/shift helper being tested. Simulate driver substitutions,
missing properties and buffers invalidated/reused after release. Test startup
cleanup and configuration changes without hardware.

### Layer C: representative end-to-end matrix

Use small equivalent images for most unit tests and selected realistic sizes
for integration/resource tests. These are capability fixtures, not promises
that physical cameras expose every combination:

| Case | Deliberate difference | Required evidence |
|---|---|---|
| A | Current IMX296 mode and real recorded layout | Baseline still + both recording paths + readers remain unchanged. |
| B | Wide versus tall/non-default main/raw/preview sizes | Correct mappings, no hidden crop/upscale or wrong recording buffer. |
| C | 10/12-bit samples in LSB/MSB uint16, plus true 16-bit fixtures | Exact values; sensor depth distinguished from container depth; correct reduction. |
| D | Native uint8, e.g. 640 × 480 | Exact recording/readback; no mandatory lossy conversion or incorrect memory estimate. |
| E | Odd image size, e.g. 1457 × 1089; boundary padding | Array/reader mapping exact; formats such as YUV420 may reject or negotiate an even size explicitly, never silently truncate. |
| F | Bayer RGGB/BGGR/GRBG/GBRG including negotiated `S*16` | Adapter acceptance where supported, exact mosaic storage and correct crop/rotation/channel semantics. |
| G | Same output size, changed crop/binning/pixel pitch | Saved calibration is not silently accepted; physical quantities and coordinate origin stay correct. |
| H | Two unlike heads: size, depth/alignment, mono/Bayer and rates varied selectively | Per-head plan/accounting and interpretation, or a documented support gap; no cross-head leakage. |
| I | Missing/unsupported controls and formats; negotiated substitutions | Useful refusal or explicitly limited operation; no false science or recording-rate claim. |
| J | Swap/reorder/restart, old state present, one camera absent | Correct head identity, settings invalidation, refreshed UI and surviving-head behaviour. |

Add byte-order, padding sentinels, distinct corner markers, per-head tags and
known low-bit patterns to catch errors a photograph hides. Do not force physically
invalid odd YUV modes to pass merely to satisfy case E. A deliberate rejection
there is correct; loss of a valid raw edge column is not.

### Layer D: independent readers and targeted fault injection

Run Python and MATLAB against the same golden outputs, including stored sensor
orientation versus already-oriented frames, legacy files and reduced-depth
recordings. If MATLAB/browser/hardware execution is unavailable, record **not
run**, not passed or equivalent to a Python test.

Use a handful of targeted fault injections or mutations only where they add
confidence: wrong dtype/shift, leaked row padding, x/y transposition, wrong head
metadata, stale calibration and silent FPS substitution. Do not remove an MLA
'rebase' and require a failure indiscriminately: the right new-sensor behaviour
may be to invalidate that calibration rather than rescale it.

## 6. Hardware verification and support evidence

Desktop tests demonstrate contracts, not driver support, optical correctness
or sustainable dual-camera performance. Verify **each candidate and each mode
claimed for use**, first alone and then in the intended pair. Sampling a family
can guide implementation but does not certify an untested module.

1. Record module/port identity, hardware and software versions, overlays/tuning
   and the unmodified current-rig baseline. Use `rpicam-hello --list-cameras`
   on current Raspberry Pi OS (record any legacy command used on an older image).
   Enumeration confirms attached devices, not every driver compiled into an OS.
   [Raspberry Pi camera software documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
2. Save requested and actual sensor/raw/main/lores configurations, capabilities
   and one original raw buffer with metadata. Confirm interpretation using an
   independent supported capture/read path at the same mode and controls. Keep
   these small captures as future adapter fixtures where practical.
3. Exercise preview, settings changes, raw/processed stills, burst/flush and
   continuous recording. Run both the normal experimental workload and its
   declared maximum resolution/rate/duration, with both heads and normal previews.
   Record actual rate, losses, RAM, temperature/throttling and write throughput.
4. Reopen in Python and MATLAB and compare values/geometry to the retained
   source evidence. Check frame timing and accounting, not only playback.
5. Swap head assignments, restart with old state and test one absent camera.
   Test a controlled source failure and storage failure; power off before
   changing CSI connections. No live-ribbon reconnection requirement is implied.
6. Record results and limitations in the support matrix. An unsupported format,
   missing physical property or unattainable rate must remain visible in both
   UI and saved metadata. Retest affected rows after driver/mode/format changes.

Use numeric acceptance criteria for the claimed workload: exact pixels on
lossless routes; correct dimensions/orientation and metadata; no silent losses;
observed rate within a stated tolerance; bounded memory; no internal recording
fallback without explicit permission. If controlled frame dropping is an offered
mode, its count/gaps must reconcile; it is not a zero-loss result. Define those
thresholds before the run rather than choosing them to fit the result.

## 7. Workflow and concrete deliverables

1. **Freeze the review baseline and intended support table.** Record commit,
   local changes, current working recording settings and candidate classes.
   Identify required operations and whether mixed-head use is required; do not
   silently turn a whole-stack request into mono-only or identical-head scope.
2. **Trace and inventory.** Produce one ledger linking assumption → producer/
   consumer paths → consequence → proposed test. Include configuration, state,
   UI, tools and readers. Code comments and old reports are leads, not evidence.
3. **Reproduce before fixing.** Build the adapter fixtures and representative
   matrix. For each finding record expected versus observed values. Prioritise
   silent misinterpretation, stale physical geometry and data loss, then explicit
   restrictions, then wording. Keep successful current-IMX296 regressions.
4. **Make small fixes in a subsequent implementation pass.** Attach a targeted
   regression test to each fix. Refresh the support table and affected metadata/
   documentation. This document itself does not authorise claiming those fixes
   are done or mandate an abstract universal sensor/plugin layer.
5. **Qualify on the Pi and close the report.** Deliver test results/fixtures,
   hardware evidence, remaining limitations and a reproducible configuration for
   each supported tuple. Missing equipment is a pending result, not a reason to
   postpone useful desktop review or approve unsupported claims.

Suggested finding ledger:

| ID | Code path and assumption | Affected sensor/mode/operation | Reproduction and result | Minimal correction/test | State/evidence |
|---|---|---|---|---|---|
| S1… | File/function plus input/output contract | Named capability difference | Expected vs actual | Concrete, reviewable change | Open / fixed / desktop-tested / Pi-verified |

Required handover: completed ledger; coverage matrix including skipped checks;
small golden input/output files; recorded rig configurations and benchmark
results; explicit supported/limited/unsupported operations. No blanket sign-off
based on total passing test count or a plausible live image.

## 8. Research decisions kept separate

Optical-format selection, acceptable pixel pitch, global versus rolling shutter,
colour suitability and noise performance determine whether a supported camera
is useful for the experiment. They do not replace the software checks above.
Changing the sensor may require optical re-evaluation; it is not automatically
an entire optical redesign, and the document should not assert an unverified
MLA-to-sensor F-number match.

Continue the row-noise comparison with matched exposure, gain, illumination and
independent capture when useful. Do not require attribution before reviewing
portability, and do not claim a sensor replacement will cure it. Preserve old
calibration/data for comparison while clearly identifying their original sensor.

The remaining decisions are the candidate modules/modes, required mixed-head
operation, required scientific analyses and practical recording limits. The
review can begin without them; candidate-specific hardware qualification and
any claim of full interoperability cannot be completed without that scope.
