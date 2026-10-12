# Sensor portability — change report

**Status:** code review executed and corrections applied. **Hardware
verification not performed and not claimed.**
**Date:** 11 October 2026.
**Against:** `docs/sensor-portability-review.md`, revision of 11 October.
**Evidence state for every row below: desktop-tested / hardware pending.**

This reports what was inspected, what was found, what was changed and what each
change is held in place by. It is written to be checked rather than believed:
every claim names the test that would fail if the claim were false, and §7 lists
what was *not* done.

---

## 1. What this pass was, and what it was not

The review asks for a trace of one identifiable frame from the driver to its
reader at every boundary, with the producer, consumer, geometry,
representation, metadata and failure outcome recorded. That trace was run on
the acquisition and recording paths. It produced ten findings, of which **four
were silent misinterpretations** — the class the review ranks first — and all
ten are now either corrected or explicitly bounded.

It is **not** a portability certification. No sensor was swapped, no second
module was attached, and nothing here establishes driver support, optical
correctness or sustained dual-camera performance for any candidate. The
support table the review asks for cannot be filled in from a desktop and is
not filled in here.

**The one thing to check first:** everything below was verified against a
**fake SDK** (`tests/fakesdk.py`), which puts a fake `picamera2` into
`sys.modules` so the real `Picamera2Source` runs against it. That is a
deliberate choice the review calls for in Layer B, and it has a boundary: it
proves what the adapter does *given* driver behaviour, and proves nothing about
what any real driver does. Where a claim depends on real driver behaviour, it
is marked **assumption** below and listed again in §7.

---

## 2. Findings, in the order the review ranks them

### 2.1 Silent misinterpretation

| ID | Finding | Status |
|---|---|---|
| **F1** | `describe()` reported the **requested** stream sizes, not the negotiated ones | fixed |
| **F2** | The sample depth came from the format **name**, so a 12-bit sensor negotiated as `R16` was read as 16-bit | fixed |
| **F3** | The pixel pitch was a module constant holding the IMX296's 3.45 µm, read directly by the focal-length arithmetic | fixed |
| **F4** | Two heads of equal size but different depth or alignment shared one plan, one storage dtype and **one reduction shift locked by whichever head delivered first** | fixed |
| **F5** | A frame offered while a recording was closing was counted as exposed and never stored or dropped | fixed |

**F1 is the most consequential.** `_configure_and_start` requested `main` and
`lores` sizes and never read back what the driver negotiated, while
`_read_back_raw` did exactly that for the raw stream and had done since
supervisory review R5. So a driver that adjusted a size — for stream
alignment, a mode's output limits, or a format's own rules — left
`full_resolution` and `preview_resolution` describing a frame that does not
exist, and with them the MLA reference frame, the readiness arithmetic, the
recording plan's geometry, the session manifest and the click-to-sensor
mapping. On the IMX296 the request is honoured, which is precisely why this
has never shown.

**F5 was found by the acceptance criterion rather than by inspection**, and is
worth reporting as such. The portability matrix failed intermittently — about
one run in four — on `every_frame_accounted`. The cause: a capture thread
could pass the state check in `HeadRecorder.offer`, increment `exposed`, and be
pre-empted before the queue put; `stop` then signalled the writer, which found
the queue momentarily empty and finished; the frame was queued behind it and
nobody took it. One frame, exposed and unaccounted. That is the discrepancy
the Stage 5c criterion exists to detect, detecting something.

### 2.2 Explicit restrictions and bounded behaviour

| ID | Finding | Status |
|---|---|---|
| **F6** | Bayer 16-bit container names were absent from the allowlist while mono `R16` was present, so a driver negotiating `SBGGR16` could not open at all | fixed |
| **F7** | `_full_resolution()` returned `(1456, 1088)` after any failure, as though measured | fixed |
| **F8** | `raw8` was labelled lossy and diagnostic unconditionally, including on a sensor whose samples are 8 bits | fixed |
| **F9** | Saved state is keyed on the logical head name, so a replacement module inherited the old one's controls and MLA alignment | fixed |
| **F10** | The row-padding allowance was a flat 256 bytes, chosen against this sensor's 2912-byte row | fixed |
| **F11** | `probe_cameras.py` printed IMX296 overlay lines as the fix for "no cameras" | fixed |

### 2.3 Observations that did not become changes

- **`_mechanisms` compared a string to a dataclass** (`fmt.key == RAW10P`), so
  the packed-10-bit branch was dead. Already corrected on 10 October; noted
  here because two faults were hiding one another — the rung is not offered
  either.
- **S10 was right.** The suite does *not* only prove one geometry: the
  recording round-trip already used 128 × 96 and several modules use small
  synthetic frames. What it did not have was any test of **raw-byte
  negotiation**, which is what §4 of this report adds. The review's instruction
  to drop the "grep must be empty" rule was followed: no such test was written,
  and the IMX296 fixtures were left in place.
- **The 1.48 GB/s correction is accepted.** Two 4056 × 3040 heads at 30 fps as
  uint16 is 1,479,628,800 B/s; the earlier document halved it by dropping a
  head. The figure is illustrative in both documents and no code depends on it.

---

## 3. What changed, file by file

### `src/trilobite/cameras/picam.py`

- **`_read_back_streams()`** (new). Reads `main` and `lores` from
  `camera_configuration()` after `configure`, warns when either differs from
  the request, records the difference as a note, and makes the negotiated main
  size the authority for `_full_res`. **F1.**
- **`_mode_sample_bits()`** (new). Finds the selected sensor mode by matching
  the negotiated raw format against each mode's `format` and `unpacked` names,
  disambiguating by the negotiated raw size, and returns its `bit_depth`.
  Returns `None` — labelled `format-name` — when no mode matches or when
  matching modes disagree, because a guess here sets the scale of every value
  in the archive. **F2.**
- **`geometry()`** (new). The five sizes as five quantities: physical array,
  raw stream, main, lores, and what a consumer holds after orientation, plus
  `quarter_turns`, `raw_matches_main`, the sample depth with its source, and
  the adjustment notes.
- **`pixel_pitch()`** (new). Config → driver `UnitCellSize` (nanometres, per
  the Picamera2 guide) → per-model table → **not established**. Warns on a
  non-square cell and uses the x pitch, which is the assumption every formula
  downstream makes, now visible. **F3.**
- **`_delivered_bits()`** (new) feeds `raw_capabilities`, so the recording
  ladder's 8-bit gate uses the sensor's depth rather than the container width.
- `KNOWN_PIXEL_PITCH_MM`, keyed on the reported model, with one entry and a
  datasheet reference in the comment. Keyed on the model **so that an unlisted
  sensor gets no pitch rather than inheriting one.**
- `describe()` now reports the negotiated lores size, the geometry block and
  the pitch with its source.

### `src/trilobite/cameras/rawformat.py`

- **`admit(..., sample_bits=, sample_bits_source=)`.** The sensor mode's depth
  overrides the format name's. Metadata gains `raw_bits_source`,
  `raw_bits_from_format_name`, `raw_sample_max` and, when the two disagree,
  `raw_bits_note`. A depth **wider than its container is refused**, not
  reserved: one of the two statements is wrong and guessing which decides every
  value's scale. **F2.**
- `_value_evidence(..., bits=)`. The ceiling and the msb shift follow the depth
  in force. Before this, a 12-bit sensor as `R16` had a ceiling of 65535 — a
  ceiling nothing can exceed is not a check.
- **Bayer 16** (`SRGGB16`, `SBGGR16`, `SGRBG16`, `SGBRG16`) added, with the
  container-is-not-the-sample caveat beside them. **F6.**
- **`stride_pad_allowance(row_bytes)`** replaces the flat bound:
  `max(256, row_bytes - 1)`. Padding of a whole row or more is still reserved,
  because that is the shape a pixel-size disagreement takes — a 10-bit buffer
  read as 8-bit has a row exactly twice as long. `MAX_STRIDE_PAD_BYTES`
  survives as the floor's name. **F10.**

### `src/trilobite/calibration/settings.py`

- `PIXEL_PITCH_MM` is no longer read by any computation. `DEFAULT_PITCH_MM`
  remains for a caller with no camera at all, and such a caller gets
  `pitch_source="default:imx296"` so the figure cannot be mistaken for a
  measurement. **F3.**
- `DerivedOptics.compute(..., pitch_mm=, pitch_source=)` and
  `invert_derived(..., pitch_mm=)`. With no pitch, the result carries
  `available=False`, zeroed metric fields and a note naming what to set.
  `kappa_px` and `alpha` are unaffected, which is the other half of the
  contract: **detection and alignment work in pixels and must not depend on a
  physical constant.**
- `_full_resolution()` returns `None` instead of `(1456, 1088)`, and the
  readiness report emits a **blocking** `<cam>.geometry` check. **F7.**
- `camera_pitch(cam)` (new, exported) asks the camera, and a camera that cannot
  say produces a non-blocking `<cam>.pixel_pitch` check stating that the
  millimetre figures are unavailable and that detection is unaffected.

### `src/trilobite/recording/`

- `manager.register(..., sample_bits=, alignment=, mono=)`, and
  `common_geometry()` refuses heads that differ in **depth**, **alignment** or
  **mono/colour**, each with its own reason. **F4.**
- `_store_shift` is **per head**, locked from that head's first frame and
  checked on every later one. It was one shift for the pair.
- `reduction_report()` reports `per_head` shifts and scales; the top-level
  `shift` is populated only when the heads agree.
- `manager.delivered_bits()` (new) feeds the plan and the option list.
- `ladder.Plan.delivered_bits`, `Plan.exact` and `Plan.validity_ceiling`:
  **an 8-bit store of 8-bit samples is exact and can be science**; a reduction
  from deeper samples is not; an unknown depth is treated as a reduction,
  because calling a lossy store exact cannot be undone from the file. **F8.**
- `continuous.HeadRecorder` gains `_gate`/`_closing`: `offer` holds the gate
  for the counter bump, the slot acquire and the queue put, and `close()` shuts
  the gate before signalling the writer. `offer` now has three outcomes, with
  `None` for "the recording was already closing". **F5.**

### `src/trilobite/app.py`

- `CameraRuntime.sensor_identity()` (new): model, `frame_shape` (sorted, so a
  quarter turn is not mistaken for a swap), mono, negotiated raw format, sample
  depth and pitch. **Not a serial number** — CSI modules do not generally
  expose one, so this detects a change of type, not of unit, and that limit is
  stated in the docstring.
- `state_snapshot()` records it; `apply_state()` compares and, on a difference,
  **restores the orientation and withholds the MLA alignment and the controls**,
  with a note saying so and nothing deleted. `_identity_differences()` treats
  absent or unknown fields as "no difference", so an old state file still
  restores. Surfaced as `sensor_changed` in `status()`. **F9.**
- `_request_frame_rate` and the derived-optics endpoint pass the camera's pitch.

### `src/trilobite/types.py`, `config.py`, `cameras/base.py`, `cameras/offline.py`

- `CameraInfo` gains `geometry`, `pixel_pitch_mm`, `pixel_pitch_source`.
- `CameraConfig.pixel_pitch_mm` (new, default `None`), documented with its
  precedence and the reason it exists.
- `CameraSource.pixel_pitch()` and `simple_geometry()` defaults, so the offline
  backends answer honestly rather than implying a negotiation.

### `scripts/probe_cameras.py`

- The "no cameras" advice names the IMX296 overlay **as the example for that
  module**, says a different module needs a different overlay, points at
  `ls /boot/firmware/overlays`, and warns that an overlay for the wrong module
  is indistinguishable from a loose ribbon. **F11.**

---

## 4. What holds it in place

### 4.1 The fake SDK — `tests/fakesdk.py`

The review's Layer B, and the reason this pass could find anything. Six named
sensors plus a parameterised builder. It separates **requested** from
**negotiated** configuration, produces real-shaped padded byte buffers, carries
a per-mode `bit_depth` distinct from the format name, and can simulate a format
substitution, an adjusted stream size, a missing `UnitCellSize`, an absent
stride and an oversized pad.

Two properties worth checking in review:

- **`expected_image()` is built from the fake's own parameters** and uses none
  of `rawformat`'s trimming or shifting helpers, so a comparison is not the code
  against itself.
- **Row padding is filled with `0xA5`**, not zeros, so a reader that leaks
  padding produces a visibly wrong column rather than a slightly dark edge.
  `test_the_padding_sentinel_never_appears_in_the_image` asserts it.

### 4.2 `tests/test_sensor_portability.py` — 48 test functions, 61 cases

| Review item | Test |
|---|---|
| baseline, exact | `test_the_current_sensor_reads_exactly_as_the_fake_wrote_it` |
| S3 / F1 | `test_a_driver_adjusted_main_size_is_reported_not_the_request`, `..._recorded_as_a_note_...`, `test_the_five_geometries_are_reported_separately`, `test_a_quarter_turn_separates_...` |
| S4, S5 / F2 | `test_a_twelve_bit_sensor_in_a_sixteen_bit_container_is_read_as_twelve`, `test_the_same_sensor_reconciles_once_the_alignment_is_declared`, `test_a_sample_depth_wider_than_its_container_is_refused`, `test_the_capabilities_report_the_mode_depth_not_the_format_name`, `test_without_mode_information_the_format_name_is_used_and_says_so` |
| S4 / F6 | `test_every_bayer_pattern_opens_and_is_marked_colour` (×4), `test_a_sixteen_bit_bayer_container_is_known` |
| 4.2 / F10 | `test_the_padding_allowance_follows_the_row_rather_than_a_constant`, `test_a_large_but_legitimate_stride_pad_still_reconciles`, `test_padding_of_a_whole_row_is_still_reserved`, `test_a_driver_that_reports_no_stride_still_admits`, `test_a_non_contiguous_buffer_is_handled_rather_than_misread` |
| 4.1 | `test_a_substituted_compressed_format_refuses_to_open` (and the device is released), `test_controls_the_sensor_does_not_advertise_are_dropped_with_a_warning`, `test_a_colour_sensor_keeps_the_colour_controls` |
| 4.4 | `test_a_frame_rate_outside_the_sensors_range_is_refused`, `test_a_driver_without_frame_duration_limits_cannot_set_a_rate` |
| S1 / F3 | `test_the_pitch_comes_from_the_driver_property`, `test_a_configured_pitch_beats_the_driver`, `test_a_known_model_without_the_property_falls_back_to_the_table`, `test_an_unknown_sensor_with_no_property_has_no_pitch`, `test_a_non_square_pixel_is_reported_with_a_warning`, `test_the_derived_optics_refuse_to_compute_without_a_pitch`, `test_a_different_pitch_gives_a_different_answer` |
| S2 / F7 | `test_an_unreported_geometry_is_none_rather_than_the_old_sensors_size`, `test_readiness_blocks_when_the_geometry_is_unknown` |
| S6 / F4 | `test_heads_of_equal_size_but_different_depth_are_refused`, `..._different_alignment_...`, `test_a_mono_and_a_colour_head_are_refused`, `test_matching_heads_are_accepted`, `test_an_unestablished_depth_does_not_block_a_recording` |
| S7 / F8 | `test_an_eight_bit_store_of_eight_bit_samples_is_exact`, `test_an_unknown_depth_treats_an_eight_bit_store_as_a_reduction`, `test_the_offered_options_state_the_right_fidelity` |
| S8 / F9 | `test_saved_state_records_which_sensor_it_was_written_for`, `test_state_from_another_sensor_keeps_orientation_and_drops_the_alignment`, `test_state_from_the_same_sensor_applies_in_full`, `test_an_old_state_file_without_an_identity_still_restores` |

### 4.3 Layer C — the geometry matrix, end to end

Six rows, each run twice: once through open → `read_raw` → admission, and once
through a real continuous recording and back out through
`scripts/read_capture.py`.

| Row | Geometry | Depth | Alignment | Colour |
|---|---|---|---|---|
| A | 1456 × 1088 | 10 | lsb | mono — the control |
| B | 1920 × 1080 | 10 | lsb | mono |
| C | 2028 × 1520 | 12 | **msb** | mono |
| D | 640 × 480 | **8** | lsb | mono |
| E | **1457 × 1089** | 10 | lsb | mono |
| F | 2028 × 1520 | 12 | lsb | **Bayer RGGB** |

Each row asserts: the array is bit-for-bit the independently-built expectation,
at the right shape and dtype; the depth and its source are recorded; `mono` and
`full_resolution` are reported correctly; the recording accounts for every
frame; the desktop reader agrees on the geometry; and the chunk index's
`frame_bytes` matches the array — the number the MATLAB reader seeks with.

Row **E** (odd dimensions) is the one the review predicted would find things,
and nothing in this project had ever seen a frame whose width is not a multiple
of 16. It passes. Row **D** is what established F8.

### 4.4 Mutation checks — `tools/mutate_stage5.py`, now 34

Thirteen were added for this pass. Each is a plausible edit — a flipped
comparison, a reverted fallback, a dropped guard — and a survivor means the
behaviour is asserted nowhere:

- the sample depth taken from the format name instead of the mode;
- a sample wider than its container accepted;
- the value ceiling computed from the container width;
- `describe()` reporting the requested stream sizes;
- the preview size reported as the configured one;
- a missing pitch falling back to the IMX296's;
- an unestablished frame size falling back to 1456 × 1088;
- heads of different depth, and of different alignment, sharing one plan;
- an 8-bit store called lossy on 8-bit samples, and called exact on unknown;
- saved state from a different sensor applied in full;
- a frame offered after close counted as exposed.

**34 of 34 caught.** Two pre-existing mutations had to be re-anchored after
`offer` gained its lock — they reported as survivors on text that had moved,
which is a limitation of a text-substitution harness worth knowing about: a
survivor is sometimes a stale mutation rather than a missing test, and the
output distinguishes the two.

### 4.5 Verification run

```
pytest -q --ignore=tests/browser   655 passed      (3 consecutive clean runs)
pytest -q tests/browser             22 passed      (Chromium, headless)
ruff check src tests scripts tools  clean
tools/mutate_stage5.py              34/34 caught
```

Three consecutive full runs because F5 was intermittent at roughly one in four:
a single green run would not have been evidence.

---

## 5. Do-no-harm

The user's functional test is a do-no-harm check on the current rig with no
sensor swap. What should be identical, and why:

| Behaviour | Why unchanged |
|---|---|
| Frame geometry | The IMX296's driver honours the requested sizes, so the negotiated read-back returns the same numbers `describe()` reported before. Row A asserts it. |
| Raw admission | `R10`, 10 bits, lsb. The mode depth and the format name agree, so `raw_bits_nominal` is 10 either way; `raw_bits_source` changes from `format-name` to `sensor-mode`. |
| Stride grading | 2912-byte rows, so the new floor never binds and the allowance is the same 256. |
| Calibration figures | `UnitCellSize` is published for this module, so the pitch is 3.45 µm with source `driver:UnitCellSize` and every derived number is as before. |
| Recording options | `raw16` and `raw8` at the same eight rates. `raw8` stays lossy and diagnostic, because the samples are 10 bits. |
| Saved state | The identity matches, so the MLA alignment and controls restore in full. `test_state_from_the_same_sensor_applies_in_full` asserts exactly this. |
| Dashboard | No UI change in this pass. The 22 browser scenarios are unchanged and pass. |

**Two visible differences to expect, both intended:**

1. **New sidecar and status fields.** `raw_bits_source`,
   `raw_bits_from_format_name`, `raw_sample_max`, `geometry`,
   `pixel_pitch_mm`, `pixel_pitch_source`, `sensor_changed`, and
   `pitch_mm`/`pitch_source`/`available` on the derived optics. Additive;
   `SIDECAR_SCHEMA` and `RECORDING_SCHEMA` are unchanged because no existing
   field changed meaning, and both readers ignore unknown keys.
2. **One new readiness check per camera** (`<cam>.pixel_pitch`, non-blocking)
   and one that appears only on failure (`<cam>.geometry`, blocking).

**If the pitch source reads `table:imx296` rather than
`driver:UnitCellSize`**, that is the fallback working and worth reporting: it
means this libcamera build does not publish the property, which bears on every
candidate sensor and is the kind of thing the review wants recorded.

---

## 6. Assumptions this pass makes about real driver behaviour

Each is an assumption because it was verified against a fake. Each is cheap to
confirm on the rig and §7 says how.

1. **`camera_configuration()` reports `main` and `lores` as it reports `raw`** —
   a dict with `size`. If a build omits them, `_read_back_streams` records that
   the sizes are the requested ones, unconfirmed, and carries on.
2. **`sensor_modes` carries `bit_depth`.** `probe_cameras.py` already prints
   it, so this is near-certain on the current build; a sensor whose modes lack
   it falls back to the format name and labels the depth nominal.
3. **`UnitCellSize` is in nanometres, x then y**, per the Picamera2 guide.
4. **A mode's `format`/`unpacked` names match the negotiated raw format
   string.** The match is case-insensitive and tries both names; a miss yields
   `None` and the nominal depth rather than a wrong one.
5. **`FrameDurationLimits` bounds in `camera_controls` are microseconds.**
   Already assumed by the existing rate code; this pass only reads them to
   refuse an out-of-range request.

---

## 7. What was not done

- **No hardware.** No sensor swapped, no second module, no Pi run. Every row
  above is **desktop-tested / hardware pending** in the review's own terms.
- **MATLAB was not executed.** No MATLAB or Octave on this machine. The chunk
  layout the `.m` readers depend on is asserted in Python
  (`test_the_matlab_frame_arithmetic_lands_on_the_right_bytes`, extended here
  to the matrix's geometries and dtypes), and that is a check on the layout,
  **not** on the `.m` files. Record them as **not run**.
- **The support matrix is not filled in.** It needs the candidate list and the
  hardware.
- **Mixed-head recording is refused, not supported.** The review asks whether
  mixed-head use is *required*; until that is answered, a differing pair is a
  named refusal rather than a capability. The refusal messages say "record them
  separately" and that path is selectable.
- **Packed and compressed formats remain unsupported**, by allowlist. Nothing
  changed here.
- **No Bayer scientific processing.** Acquisition and storage are covered and
  tested; demosaicing, mosaic order through crop parity and flips, and what
  colour means under an MLA are untouched and out of scope.
- **`ScalerCrop`, binning and crop-versus-resize are not implemented.** §4.3
  separates a crop (origin and field of view) from binning (sampling) from a
  main-stream resize. The adapter configures the raw stream at the sensor's
  native size and chooses no mode, which is the safe default; the geometry
  block now reports enough to tell the three apart, but nothing acts on it.
- **The row-noise investigation is untouched**, as the review directs.

---

## 8. What the supervisor is asked to check

1. **The fake-SDK boundary** (§1, §6). Is it accepted that these contracts are
   established against a fake driver, with the five assumptions in §6 to be
   confirmed on the rig?
2. **F1's blast radius.** `full_resolution` now follows the negotiation. Is
   there any consumer that wanted the *requested* size? The trace found none —
   every caller wants the frame it will actually hold.
3. **The pitch precedence** (config → driver → model table → unavailable), and
   in particular that a config value beats the driver.
4. **The conservative direction on F8.** An unknown sample depth makes an
   8-bit store lossy. The alternative mislabels a reduction as science.
5. **That mixed heads are refused rather than supported**, and whether that
   needs to change (§7).
6. **`frame_shape` instead of `full_resolution` in the identity comparison.**
   A replacement whose dimensions are the old ones transposed is not detected;
   that is the same pixel array and the alignment does transfer. Agreed?
7. **Whether the two documents should be merged.** This report answers the
   review point by point; the review remains the process definition.
