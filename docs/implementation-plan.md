# Implementation plan — responding to the 0.1 architecture review

**Responds to:** `docs/software-stack-review.md` (6 September 2026).
**Shape:** ten stages with explicit dependency and acceptance gates. Each has
a coherent deliverable and its own evidence; implementation is not acceptance.
**Amended 6 September 2026:** following code review through `ce1d92c` in
[the Stage 2 supervisory review](stage-2-supervisory-review.md). The amendment
below and rewritten Stages 3/4 supersede their original scope and tests.

**How to use it.** Do one stage. Deploy it. Run its bench test on the rig. If
it passes, say so and the next stage starts; if it fails, the stage is reworked
before anything else moves. No stage leaves the rig in a state where the
previous stage's test would no longer pass.

## Amendment: explicit changes to Stages 3 and 4

| Item | Original plan | Required amended plan |
|---|---|---|
| Entry | Stages 0–2 treated as complete foundations | Close R1–R6 below; distinguish implemented, desktop-tested and rig-accepted. Supervisor reviews entry evidence before Stage 3. |
| Stage 3 ownership | Queue stills and controls on the capture thread | Own open/configure/start/request/release/stop/close too; migrate every caller. |
| Stage 3 workload | Drain commands against the current request | Bounded queue/work budget, explicit rejection, fairness, deadline/cancellation races and future-request control semantics. Minimum admission moves from Stage 8. |
| Stage 3 lifecycle | Stop/recovery mainly in Stage 7 | Partial-open cleanup, failed-stop, generation isolation and prevention of a second owner move into Stage 3. Durable state and broader soak stay in Stage 7. |
| Stage 3 evidence | Matching software sequence proves same exposure | Use actual request lineage and copied SDK metadata; distinguish software delivery IDs, SDK identity and clock domains. No inter-head synchronisation claim. |
| Stage 3 → 4 interface | Frame metadata to be addressed in Stage 4 | Stage 3 supplies immutable acquisition evidence, orientation applied and control acknowledgement identity; Stage 4 adds processing execution and persisted schema. |
| Stage 4 validity/replay | Reader enforcement and replay dealt with later | Close diagnostic/default/legacy promotion before Stage 3; implement complete replay lineage and schema enforcement in Stage 4. |
| Stage 4 execution | Snapshot configuration | Freeze nested values and stage topology for the actual execution; separate acquisition, processing and save-event records across all active writers. |
| Stage 4 test | Same retained preview saved twice must have different sidecars | Acquisition/processing evidence must stay identical; save events may differ. A newly processed frame is the separate new-revision test. |
| Supervision | Bench before moving on | Review entry contract before Stage 3 and ownership/rig evidence before Stage 4; review G4 results at completion. Do not wait until Stage 4 to intervene. |

### Prerequisite closure before Stage 3 — **IMPLEMENTED 6 Sep**

All six are implemented and verified on the desktop suite; rig acceptance is
still outstanding and is a precondition for Stage 3, not for its contract
review. Full record in `docs/cleanup-log.md` 2026-09-06 (q), including the
bench-test steps. Summary of what each turned into:

| | closed by |
|---|---|
| R1 | both readers require an explicitly recognised validity **plus** the admission record it rests on. `require_science` (values) and `require_geometry` (positions) are separate questions, so ISP poses still work with no flag; `--allow-diagnostic` and `--allow-legacy` are separate explicit paths. |
| R2 | a third validity, `unvalidated`, as the **default**, plus a `source_kind` field. Closed on the `Frame` default, ISP main, the served full frame, previews, both synthetic paths, and replay — which now reads the sidecar and promotes nothing. |
| R3 | unsigned dtype and native byte order enforced; positive geometry; and sample depth, container width and **alignment** separated, with `raw_alignment` declared in config, `raw_bits_nominal` named as nominal, and the shift recorded rather than applied to the pixels. |
| R4 | a runtime admission failure fails the science request. `/api/capture/{id}/raw` returns 422 and no file unless `allow_unvalidated_raw` is set. |
| R5 | `_read_back_raw` takes format, size and stride from `camera_configuration()` after `configure`. Driver substitutions are caught; the negotiated stride must match exactly; `raw_stride_source` records which rule applied. |
| R6 | a 4 s `AbortController` bound on the status fetch **and** an independent 1 s watchdog that renders staleness from elapsed time, so a permanently pending response cannot hide. |

24 mutants across R1–R5, all caught. Suite 433 passed / 13 skipped, 13 browser
scenarios. **Still outstanding:** rig acceptance of Stages 1–2 and of these
corrections, and the Stage 0 residuals (legacy tree and service file; the CI
workflow is still a draft outside `.github/workflows/`).

The original statement of the six follows.

The detailed evidence and probe results are in the supervisory review. Retain
existing tests and add regressions for these missing contracts:

- **R1 — readers fail closed:** unknown/missing validity is not measurement
  admission. Explicit science status needs admission evidence; retain an
  explicit legacy/inspection path in both Python and MATLAB.
- **R2 — no validity promotion:** defaults, replay, ISP main and derived-frame
  paths cannot turn unvalidated/diagnostic data into admitted sensor data.
  Prevent replay promotion now; full replay fidelity remains Stage 4.
- **R3 — validate representation:** reject signed/floating samples and invalid
  geometry; establish unsigned dtype, byte order, negotiated stride, sensor
  bit depth versus container width and alignment, not merely item size/max.
- **R4 — enforce diagnostic opt-in:** runtime admission failure fails the
  science request. Diagnostic saving is explicit and distinguishable, governed
  by `allow_unvalidated_raw`, rather than an unconditional fallback.
- **R5 — validate actual negotiation:** use post-configuration raw size,
  format/stride and sensor mode, not requested format or main resolution. Test
  driver substitutions and unequal main/raw sizes. Preserve real rig fixtures
  and configuration/version evidence; synthetic fixtures alone do not close G1.
- **R6 — status hangs age out:** bound fetch waits and make freshness visible
  independently of a poll completing. Test a permanently pending response.

Record Stage 0 residuals: legacy tree/service removal and deployed-unit check;
the workflow currently at `Claude outputs/tests.yml` must be installed as an
active CI workflow before claiming CI. Record Linux/browser results separately.
These housekeeping items may be tracked explicitly, but R1–R6 and required
rig evidence are not silently deferred into Stage 3. G1/G5 remain pending until
their applicable acceptance checks pass. Local review result: 374 passed,
2 skipped; no Pi or MATLAB run, and Playwright was unavailable.

Before implementation, document finite queue capacity and numeric provisional
latency/freshness/shutdown thresholds. Supervision approves the contract and
evidence, not an unspecified future design. Estimates for Stages 3/4 must be
reassessed because this amendment adds work.

---

## 0. Findings verified before planning

Every finding below was checked against the current working tree, not taken on
trust. The review holds up. Three checks worth recording:

**F2 is confirmed and is the worst of them**, because the code asserts the
protection it does not provide. `devices.is_mounted` says in its docstring
*"Comparing device ids catches that"* and its body walks up to any existing
ancestor and tests writability — no device comparison anywhere. Meanwhile
`mount_of`, twenty lines below, already does the `st_dev` walk. The fix is
almost in the file already.

**F1 is confirmed.** `CameraRuntime.capture_still` calls `source.capture_full()`
on the API worker thread; `Picamera2Source.capture_full` calls
`capture_request()` itself. Two mutexes serialise it. There is no single owner.

**`capture_all` is worse than my report claimed.** It completes each camera's
*disk write* before requesting the next frame, so pair skew includes storage
latency. My progress report says "tens of milliseconds"; that is the
free-running sensor skew alone and ignores the write.

`src/flyeye/` and `systemd/flyeye.service` remain. Stage 0 now restricts package
discovery to `trilobite*`, so the original double-packaging observation is
resolved; legacy-source/service retirement remains pending.

### Where I disagree with nothing, but would sequence differently

The review orders its six work packages by severity. This plan orders by
**testability**, and the difference is one preparatory day at the front:

> You cannot bench-test the acquisition rework if the status display can report
> a stale frame rate as live and a failed stage as success (F5). The
> observability work is not just a P2 finding — it is the instrument every
> later stage is measured with.

Stages 0–2 establish test, observability and admission foundations. Stage 2
changes capture eligibility and is not merely additive. Retain the sequence,
subject to the prerequisite corrections above; Stage 3 is the first ownership
refactor.

---

## Stage summary

| # | Stage | Closes | Size | Hardware test |
|---|---|---|---|---|
| 0 | Test hygiene and legacy retirement | §6, §4 | ½ day originally | Partial: legacy retirement, active CI and platform evidence outstanding |
| 1 | Truthful status and liveness | F5, G5 | 1 day originally | Implemented; R6 correction and rig acceptance pending |
| 2 | Validated raw-frame admission | F7, G1 | 1 day originally | Implemented; R1–R5 corrections and rig acceptance pending |
| — | R1–R6 prerequisite closure | G1, G5 | 1 day | Implemented 6 Sep; rig acceptance pending |
| 3 | **Single owner, bounded commands and minimum lifecycle** | F1, G2 | Re-estimate | Implemented 6 Sep in reduced scope; rig acceptance pending |
| 4 | Immutable execution provenance and reader schema | F4, G4 | Re-estimate | Retained-frame invariance, revision changes and reader round trips |
| 5a | Storage identity, reserve and sustained-rate record | F2, G3a | **IMPLEMENTED 8 Oct** | pull the USB disk in four ways |
| 5b | **Bounded burst recorder** | new | **IMPLEMENTED 8 Oct** | record to the buffer limit; pull the disk before flushing |
| 5c | **Continuous recorder with declared degradation** | G9 | **IMPLEMENTED 8 Oct** | a five-minute pair recording onto the USB 3 SSD |
| 6 | Transactional capture sets | F3, G3b | 3 days | cut power mid-capture |
| 7 | Lifecycle states and durable state file | F6 | 2 days | 100 stop/start cycles; kill during save |
| 8 | Server admission, trust boundary, deployment manifest | §4, G6–G8 | 3 days | three browsers, slow disk, restart |
| 9 | Unattended long-session operation | — | gated | superseded in part by 5c; see that section |

Sizes are engineering days for one person, excluding bench time.

---

## Stage 0 — make the evidence trustworthy — **IMPLEMENTED 6 Sep**

This stage improves test evidence and retires the legacy implementation.
The implemented durability metadata also changes writer output and needs
platform verification; it is not proof of per-write durable completion.

### Changes

1. **Two Windows `fsync` failures.** The tests assert that
   `os.fsync` was *called* on the directory. On Windows `fsync_dir` returns
   early because a directory cannot be opened, by design. Rewrite both to
   assert the *outcome* — the file is durable, and the directory entry is
   durable on platforms that support it — and have the writer report a
   `durability` capability (`strict` / `file-only`) that the test reads. The
   review's point stands generally: a test should check the externally
   meaningful outcome, not that an internal helper was called.
2. **Tests that write to the real user data directory.** `tests/test_rotation.py`
   builds `AppConfig` without a storage root, so it lands in `~/trilobite-data`.
   That is mine and it is wrong. Add a session-scoped autouse fixture that
   fails any test constructing a writer outside `tmp_path`, so the class cannot
   come back.
3. **`httpx` as an explicit test dependency**, not incidentally installed.
4. **Delete `src/flyeye/` and `systemd/flyeye.service`.** Check first that no
   deployed unit references the old entry point. Separate commit, easy to
   revert.
5. **Retain the browser scenarios.** The Playwright checks used during
   development are currently ad-hoc scripts. Move them into
   `tests/browser/`, skipped when Playwright is absent, so they are executable
   rather than described.
6. **A CI workflow** running the suite on Linux and Windows.
7. **Correct the progress report.** Four overclaims, all mine:
   - §3.2 "Only that thread touches the camera" — not true until Stage 3.
   - §3.2 "the full frame is the **same exposure** as the preview" — true of
     the handshake path, false of `capture_full`, which is what stills use.
   - §6.3 implies device disappearance is detected — F2.
   - §10 "tens of milliseconds apart" — omits the disk write.

### Bench test

Verify changed writer metadata on the supported platforms; retain test-only
changes separately from runtime changes. Do not infer Linux results from Windows.

### Acceptance

Suite green on Windows and Linux with no writes outside `tmp_path`; one package
in `src/`.

---

## Stage 1 — truthful status and liveness *(F5, gate G5)* — **IMPLEMENTED 6 Sep**

Status and UI behaviour change here. This is the instrument for testing
Stages 3–8, which is why it is early; R6 remains an acceptance correction.

### Changes

- **Measured, not configured, rates.** `status.sensor_fps` currently reports
  `cfg.fps`, which is what was asked for, not what is happening. Measure the
  acquisition cadence at the point a request is taken.
- **`RateMeter` must age out.** It computes from the last N ticks with no time
  bound, so it keeps reporting a healthy rate after frames stop. Return `0.0`
  when the newest sample is older than a few periods.
- **Frame age and last-acquired / last-published timestamps** per camera, and
  **last successful write** per writer.
- **Stage failures become visible.** `Pipeline.__call__` catches a stage
  exception and passes the frame on — keep that, it is the right behaviour for
  a preview, but attach the failure to the frame's metadata and count it per
  stage. `CameraRuntime.errors` does not currently see these at all.
- **Stale frames are marked.** The MJPEG generator can retransmit the last
  frame after a wait timeout; a viewer cannot tell. Mark it, and show the age
  in the header past a freshness deadline.
- **The dashboard stops swallowing status failures.** It currently catches and
  ignores; a status endpoint that has stopped answering should be visible.
- **Document `Frame.seq` and `Frame.now` accurately.** `seq` counts software
  deliveries, not sensor exposures — and the Pi skip path does not increment it
  while the synthetic default skip does, which is an inconsistency to fix.
  `now` timestamps construction, after the copy. Neither is drop evidence or
  synchronisation evidence; preserve the driver's `SensorTimestamp` separately,
  with its clock domain recorded.

### Bench test

1. With both cameras running, inject a source stall or controlled acquisition
   failure. Within the freshness deadline the UI must show it as stale.
   For physical absent-camera testing, power off before changing CSI cables.
2. Inject an overlay-stage exception in the test harness. The stage's error
   counter must rise and the panel must show it; the preview may continue.
3. Make a status response remain pending indefinitely, then separately return
   errors. The header must show stale status within the declared deadline.

### Acceptance

G5. No path where a frozen source, a throwing stage or a dead status poll
continues to read as healthy.

### Rollback

Revert independently, but do not use reverted observability as acceptance
evidence for later stages.

---

## Stage 2 — validated raw-frame admission *(F7, gate G1)* — **IMPLEMENTED 6 Sep**

**Review status: not accepted.** The historical implementation description
below records intent and delivered mechanisms; R1–R5 above qualify its claims
and are mandatory corrections before Stage 3.

Closes the failure class that cost a whole recording session.

### Changes

Introduce an explicit admission boundary between the SDK buffer and anything
that calls itself a science frame:

- A raw buffer is admitted only if the negotiated format is a **known,
  uncompressed, unpacked** one, and its shape, stride and bit depth reconcile
  with the sensor geometry. Anything else — compressed, packed-but-undecoded,
  unknown, or a stride that does not resolve to a whole bytes-per-pixel — is
  **refused**, not saved with a warning flag.
- `Frame` carries a `validity` field: `science` or `diagnostic`. A refused
  buffer can still be captured, but only as `diagnostic`, named as such, and
  the offline readers refuse it for measurement.
- `_choose_raw_format` currently logs an error and proceeds when an explicitly
  configured format looks compressed, and accepts an unchecked driver default.
  Both become refusals at open time unless the config sets an explicit
  `allow_unvalidated_raw: true` escape hatch, which is recorded in every
  sidecar it touches.
- **Golden fixtures.** Byte-level buffers for each supported mode — R8, R10
  unpacked, R10 padded to a 2944-byte stride, and a `MONO_PISP_COMP1` sample —
  exercising the adapter with no camera, including every rejection path.

### Bench test

1. Normal capture on both cameras: unchanged files, `validity: science` in the
   sidecar, same byte counts as before.
2. Set `raw_format: MONO_PISP_COMP1` in the config deliberately. The camera
   must refuse to open for science capture and say why, rather than recording
   1,400 files of compressed transport.
3. Remove `raw_format` entirely and confirm the auto-chosen format is validated
   and named in the log.

### Acceptance

G1. No path admits a buffer whose meaning is not established.

### As implemented

`src/trilobite/cameras/rawformat.py` is the boundary, deliberately with no
camera dependency so every rejection path is reachable from a byte array.
`Frame.validity` is a first-class field (`science` / `diagnostic`) carried
through `derive`; its constants live in `types.py` to keep the import direction
right. `_choose_raw_format` raises `RawFormatError` at open time and releases
the device for that refusal path. Runtime buffer failures currently return
diagnostic data without requiring the hatch; R4 corrects this discrepancy.
Diagnostic captures are named `diagnostic_…` ahead of the tag and carry
`validity` in the sidecar. Saved previews are diagnostic too, which was not in
the plan and follows from the same argument. `scripts/read_capture.py --detect`
and the new `matlab/tv_require_science.m` refuse exact diagnostic labels, but
currently accept missing/unknown labels; R1 closes that gap.

The substantive change beyond what the plan asked for is the **direction** of
the stride reconciliation: the old code inferred bytes-per-pixel from the row
length, which cannot distinguish "10-bit padded" from "8-bit wide". The format
now states it and the shape confirms it.

15 mutants, all caught — one only after adding a test for the invariant rather
than the table. See `docs/cleanup-log.md` 2026-09-06 (p) for the full record
and the bench-test steps.

---

## Stage 3 — one acquisition owner and a bounded lifecycle *(F1, gate G2)* — **IMPLEMENTED 6 Sep**

> **Implemented in reduced scope. `docs/stage-3-contract.md` remains the
> specification; `src/trilobite/acquisition.py` implements the part of it the
> finding requires and its module docstring names what it leaves out.**
>
> Built: one owner provable by thread id, a bounded queue with explicit
> rejection, deadlines, expiry checked at execution rather than submission,
> exactly-once resolution, a stop that will not close a device out from under a
> running thread, and separated capture/save so disk latency is never inside a
> request.
>
> Deliberately not built: control coalescing (`superseded`), per-kind fairness,
> generation isolation across restart, and the `cancelled` outcome. Five
> outcomes, not seven. Each of those guards a failure mode this rig has not
> exhibited, and each is another interacting state to get wrong — the same
> judgement that had to be applied in reverse to the Stage 2 corrections, where
> uniform strictness cost more than it bought. They stay specified and unbuilt.
>
> **Evidence:** 17 tests in `tests/test_acquisition.py`, including a 200-capture
> four-thread soak asserting every file on disk with a parsing sidecar and a
> matching byte count. 10 mutants, all caught. Rig acceptance outstanding.

> **Contract written and reviewed: `docs/stage-3-contract.md`.** This plan
> requires the queue capacity and the numeric thresholds to be documented and
> approved before implementation, on the grounds that supervision approves the
> contract rather than an unspecified future design. That document exists and
> no Stage 3 code has been written. It covers the owner and every caller to
> migrate, command admission and the provisional capacities, deadline and
> cancellation semantics with the seven outcome states, the control-to-request
> binding policy, the acquisition envelope, the lifecycle and generation rules,
> the four separate counters, the fake-SDK test plan and the rig run, the
> commit sequence, and the three questions it cannot answer alone.

**Amended 6 September after implementation review through Stage 2.** Enter
only after the prerequisite gate above is closed. This stage delivers the
acquisition envelope and command semantics that Stage 4 consumes; it does not
claim complete processing provenance or transactional storage.

### Difference from the original Stage 3

The original queue sketch served preview and drained commands against an
already acquired request. It omitted admission limits, control timing and
ownership during startup/shutdown. Replace that sketch with the following
contract. Moving a still call onto a queue alone does not meet G2.

### Required design

1. **One application owner per head for the whole SDK lifecycle.** Create/open,
   configure, start, control submission, request acquisition, buffer access,
   release, stop and close run through the owner. SDK-internal threads are not
   additional application owners. Audit every entry path: API stills, preview,
   calibration poses, burst/capture-all, controls and shutdown. Remove the
   pending-frame handshake when all callers use the replacement. Remove locks
   only when ownership makes their particular protection unnecessary.
2. **Bounded command admission.** Commands carry ID, kind, camera generation,
   submission time, monotonic deadline and one terminal outcome. Define queue
   capacity, rejection response, FIFO/fairness policy and bounded work per loop.
   Control coalescing is permitted only with an explicit superseded outcome.
   Status must expose depth, oldest age, rejections, timeouts and owner state.
   Keep this basic bound here; broader client/resource policy remains Stage 8.
3. **Precisely defined deadlines and cancellation.** Distinguish queued,
   executing and terminal commands. Expired queued work must never execute
   later. A caller timeout does not interrupt the SDK or prove that an
   executing command had no effect. Suppress late delivery/persistence for
   cancelled capture requests, record late control effects when unavoidable,
   and resolve completion/timeout races exactly once. Bound the API wait even
   if SDK acquisition hangs; surface the owner's degraded/failed state.
4. **Controls apply to future requests.** Dispatch controls at the documented
   SDK boundary and record their acknowledgement separately. A control change
   cannot alter the exposure of a request already obtained. Effective values
   come from that request's sensor metadata; absent evidence remains unknown.
   Specify whether a still uses the next delivered request or must follow a
   particular acknowledged control revision; test the chosen policy.
5. **One request produces an acquisition envelope.** Copy admitted raw data,
   required preview/main data and metadata while the request is held. Give
   them the same application request identity, camera/run generation and
   source identity. Preserve SDK sequence/timestamp when supplied, units and
   documented clock domain; otherwise record unknown. Keep host monotonic
   timing separate. Copy orientation/configuration values actually applied at
   acquisition, admission evidence and validity. Do not reconstruct them from
   mutable settings later. Stage 4 adds the processing execution record.
6. **Release before downstream work.** Every request is released exactly once,
   including copy/admission/processing failures. No SDK-backed view or mutable
   SDK metadata escapes. JPEG, pipeline processing, disk IO and waits on
   downstream workers stay outside the owner; downstream handoffs are bounded.
   Raw/preview siblings may be traceable without both being displayed. Do not
   label an unrelated latest browser preview as the still's exposure.
7. **Minimum lifecycle correctness moves here from Stage 7.** Stop rejects new
   work, terminates pending commands and drains or reports executing work.
   Only the owner releases/closes its device. Clean up partial opens on every
   failure path. A join timeout means failed-stop, not stopped: do not close
   concurrently or start a replacement owner over a surviving one. Generation
   checks reject late results after restart. If bounded recovery cannot be
   demonstrated in-process, document the supported recovery boundary and
   reassess process isolation before accepting G2.
8. **Truthful accounting.** Count actual SDK deliveries, intentional preview
   suppression, published frames and admission failures separately. Software
   sequence gaps or configured FPS are not sensor-drop or synchronisation
   evidence. State what can and cannot be measured on this driver.

### Automated acceptance

Use a deterministic fake SDK with thread IDs, unique request tokens and
buffers invalidated/reused on release. Exercise concurrent stills, controls,
preview and all capture callers. Assert:

- All application SDK operations use the declared owner; each request is
  released once and copied output remains unchanged after SDK reuse.
- Queue overload is bounded and rejected explicitly; command ordering,
  coalescing and deadlines meet the declared policy. No expired queued command
  executes, and no command resolves twice under completion/cancellation races.
- Delayed controls do not become fictitious effective values on an earlier
  request. Raw and preview siblings have the same SDK request token and copied
  metadata, not merely equal locally assigned sequence numbers.
- Exceptions at open/configure/start/copy/admit/release/stop, an indefinitely
  blocked acquisition and a late completion all produce observable outcomes.
  No concurrent close, leaked partial-open handle, false stopped state or
  second owner is permitted. Test stop followed by attempted restart.
- Slow processing/writes cannot hold an SDK request or grow an unbounded queue.
  Existing Stage 1/2 invariants still pass through the unified path.

### Rig acceptance and supervisor checkpoint before Stage 4

Record the deployed commit, Pi/SDK versions, both negotiated stream layouts,
queue capacities and numeric thresholds for command latency, frame freshness
and shutdown **before** the run. Use provisional engineering thresholds if
research requirements are unresolved, and identify them as provisional.

Run both previews, a still every two seconds and sustained control changes
for 30 minutes. Record latency distribution/maxima, maximum queue age/depth,
admission failures, rejection/timeout counts, memory trend and CSI logs.
Require no crash, unexplained stall, ownership violation or unbounded growth;
explicit preview suppression must follow the declared policy. Exercise stop
during capture and injected acquisition delay. Use same-request identity and
sensor metadata to demonstrate raw/preview lineage; software sequence equality
alone is insufficient and this test establishes no inter-head synchronisation.

The supervisor reviews this evidence and unresolved driver limitations before
Stage 4. Keep the refactor in independently reviewable commits and verify
rollback on the rig. Stage 7 retains durable state and broader lifecycle soak.

---

## Stage 4 — immutable frame provenance and reader enforcement *(F4, gate G4)* — **IMPLEMENTED 8 Oct**

> Three items, all three built. Plus the three Stage 3 corrections from
> `docs/stage-3-review.md`, which were reproduced bugs and did not wait for a
> stage boundary: the restart guard, the unlocked outcome transition, and
> partial-open cleanup. `assert_owner` is now wired into the source, which the
> review correctly called an overclaim before — and it immediately found two
> real off-owner calls in the test suite.
>
> 18 mutants, all caught. Suite 483 passed / 1 skipped, 13 browser scenarios.
> Full record in `docs/cleanup-log.md` 2026-10-08 (t). Rig acceptance pending.

**Amended 6 September.** Requires accepted Stage 3 acquisition envelopes and
the corrected Stage 2 validity policy. This stage establishes which acquisition
and processing execution produced saved pixels. Capture-set atomicity remains
Stage 6; deployment inventory remains Stage 8.

### Compacted 7 September, after Stages 2r and 3

Six required changes became three. Not by dropping requirements -- by noticing
that three of them were closed by work done since the amendment was written:

| was | status |
| --- | --- |
| 5. versioned reader contract | **built** in R1: both readers require an explicitly recognised validity plus its admission evidence, and refuse what they cannot establish. Only the schema *version field* is outstanding, folded into item 2 below. |
| 6. replay lineage | **built** in R2: `ReplaySource` carries the sidecar's validity and evidence and promotes nothing. What the amendment additionally asked for -- original timing, orientation history -- is not needed by anything and is deferred rather than written now. |
| 4. measurement eligibility for derived outputs | **built** in R1 as the `require_science` / `require_geometry` split. |

Also corrected: the amendment assumed Stage 3 would deliver a formal
acquisition Envelope. It did not -- Stage 3 shipped the owner and left the
envelope in the contract, so the acquisition half of the record is written
here, from what the frame already carries.

### Required changes

1. **Freeze the processing record at execution, not at save.** The live bug,
   concretely: `CameraRuntime.save_frame` calls `pipeline.settings_snapshot()`
   *at save time*. Edit a gain between capture and save and the sidecar
   describes the value that was never applied. Worse, a `capture_full` frame
   never enters the pipeline at all and still gets a full `pipeline` block --
   a record of processing that did not happen, indistinguishable from one that
   did.

   The pipeline attaches its own frozen record to the frame it produced; the
   writer serialises what the frame carries and reads nothing live. A raw
   capture's record says `bypassed`, naming the stages it did not go through.

2. **Three blocks in one sidecar, and a version field.** `acquisition`,
   `processing`, `saved` -- not three separately persisted records, which was
   more structure than the problem needs and would have to be rejoined by every
   reader. Acquisition: source kind, validity, admission evidence, sensor
   metadata with its clock domain named, orientation as applied. Processing:
   the frozen parameters, ordered topology, and per-stage `ok` / `skipped` /
   `failed` with reason. Saved: the ids and times that may legitimately differ
   between two saves of one frame. `schema: 1` at the top level, and both
   readers refuse a version they do not know.

3. **Requested is not effective.** Keep the control values that were asked for
   separate from what a frame's own metadata reports, and record `unknown`
   where the driver said nothing. Never backfill the requested value into the
   effective slot -- the same rule the raw boundary applies to pixel values,
   applied to control values.

### Automated acceptance

- Retain a processed frame, edit gain and topology, save that exact frame
  twice. Pixels, acquisition and processing evidence identical; only the
  `saved` block may differ. Then process a new frame and verify its record
  reflects the edit.
- Force a parameter update between two stages with a barrier. One execution
  uses one frozen revision, never a mixture.
- A raw capture's `processing` block says `bypassed` and names no parameters it
  did not use. A frame with a failed stage says so even though the preview
  passed through.
- Round-trip valid, malformed and unknown-version sidecars through both
  readers.

### Rig acceptance

Use the existing still/burst path for 20 frames, with controlled changes to
display gain and orientation midway. Verify each frame's pixels, acquisition
identity and processing record offline; raw outputs must correctly identify
display-only processing as bypassed. Separately run the retained-frame test
above and prove that live edits cannot rewrite old provenance.

Accept G4 only with schema/fixture review and recorded automated and rig
results. Missing MATLAB or rig execution is a pending check, not a pass.
Supervisor sign-off here closes frame provenance; Stages 5–8 still gate broader
field/release claims and Stage 9 remains separately gated.

---

## Stage 5 — recording *(5a storage, 5b burst, 5c continuous)*

**Restructured 8 October**, after the requirement was stated plainly: Stage 5
must deliver **continuous recording to disk**, degrading by dropping frames or
other declared lossy means when the write path cannot keep up. The continuous
recorder was Stage 9; it is now Stage 5c, and Stage 9 keeps only what is
genuinely later.

Build order is **5a → 5b → 5c**, and the sections appear below in the order they
were written rather than that order, because the supervisory review, the cleanup
log and the Stage 3 contract all cite stage numbers and churning them buys
nothing. Read 5a (p. "storage identity") first.

Requirements as they now stand:

| | |
|---|---|
| pixels | raw sensor counts |
| heads | both, free-running, **no synchronisation claimed** |
| shape | burst (RAM) **and** continuous (disk) |
| duration | continuous: **a few minutes** — 1–5 min, so 11–57 GB at full rate |
| target | **USB 3 SSD** |
| under-rate behaviour | **explicit loss is permitted** — frame dropping, or another declared lossy configuration. Silence is not. |

That last row also closes open question 2 in the decisions table.

---

## Stage 5b — bounded burst recorder *(new, 7 September)* — **IMPLEMENTED 8 Oct**

Requirements settled 7 September: **raw sensor counts, both heads, no
synchronisation claim, burst first and continuous later.**

### The constraint that reframes this

**There is no hardware H.264 encoder on a Pi 5.** The legacy VideoCore encoder
was dropped; `rpicam-apps` reports "Unable to find an appropriate H.264 codec"
and the v4l2 encode devices are absent. Compressing 2 x 1456 x 1088 at 30 fps
would be software encoding on the same four cores already running two capture
loops, the JPEG preview and the disk path. It will not hold rate.

So on this rig "video" means **an uncompressed frame sequence**, not a video
file, and the constraint is the data path rather than the codec:

| stored | per frame | pair at 30 fps |
|---|---:|---:|
| 10-bit as uint16 | 3.02 MiB | **190 MB/s** |
| 8-bit | 1.51 MiB | 95 MB/s |

Raw was chosen, so 190 MB/s is the number.

### Why burst first — corrected 28 September

The first draft of this section claimed the burst recorder "needs neither half"
of Stage 5 and that a failed flush could "retry against the internal disk".
**Both were wrong, and the second was the exact failure Stage 5a exists to
prevent** — a 2–4 GB write diverted onto the SD card. Corrected below.

What survives of the argument is narrower but still decisive: a burst's size is
**known before a single byte is written**. `frames × 3.02 MiB` is arithmetic,
not a forecast. So "will this fit, on this target, with the reserve intact" is a
question that can be *answered* before the flush starts, where a continuous
recording can only be monitored while it runs. That is the real asymmetry.

### Required design

**1. Memory: conservative, and actually reserved.**

`np.empty` on Linux does not commit pages — the kernel hands back a mapping and
faults pages in on first touch. **A successful allocation therefore proves
nothing**; the shortfall arrives as an OOM kill at frame 400, mid-recording.
So arming must **prefault** the buffer by writing to every page, and only then
report itself armed. Writing 3 GB costs a few seconds once, at arm time, which
is the right place to spend it.

Sizing comes from measured `MemAvailable` with an explicit reserve, never from
a config constant that will be wrong on a 4 GB board:

| | |
|---|---|
| cap | ≤ 50% of `MemAvailable` at arm time |
| reserved for | the application, libcamera's own buffer pools, and the flush working set |
| flush headroom | dirty page cache counts against available memory until writeback completes, so a large single write shrinks free RAM while the buffer is still held |

Indicative: ~18 s on an 8 GB board, ~8 s on 4 GB. **The armed duration is
displayed before Start**, computed from the buffer that was actually
prefaulted, not from the size that was requested.

**2. Storage protection applies to the flush, in full.**

- **External target only, by default.** Saving to internal storage requires an
  explicit override that is **unchecked every time** — not a remembered
  preference, because the whole point is that it is a deliberate act.
- **The free-space reserve is enforced before the flush begins**, against the
  known exact size. A flush that would breach the reserve is refused while the
  data is still in RAM and can go somewhere else.
- Storage identity (Stage 5a) applies: the target is verified to be the volume
  that was selected, not a mount point left behind by a pulled stick.

**3. RAM is temporary, and the state machine says so.**

`idle → armed → recording → captured (unsaved) → saved`

- **"Captured — not saved"** is surfaced in the UI and in `/api/status`, and it
  says plainly that the recording is lost on a process restart or a power cut.
  It is an exception state to be cleared, not a resting place.
- **Arming is refused while an unsaved burst exists.** Discarding one is an
  explicit act. Nothing may overwrite an unsaved recording.
- The default path flushes promptly and automatically to the verified external
  target; the unsaved state exists for when that fails, not as the normal flow.

**4. The recorder runs inside the owner loop**, not as a command: it needs every
frame, not one. Recording is a state of the loop.

**5. The preview rate cap is suspended while armed.** `skip_preview` releases
frames without decoding, which is exactly wrong here. Every delivered frame is
stored; the preview publishes from the buffer at its own rate.

**6. A full buffer stops the recording and says so.** Not a wrapped ring, not a
silent drop. The result names the duration captured.

**7. Per-frame `SensorTimestamp`, per head, in its own clock domain.** The only
timestamp with a defined relation to exposure, and what makes two free-running
heads reconcilable afterwards. **No synchronisation is claimed**: the manifest
records the measured inter-head offset distribution and states that it is
measured, not enforced.

**8. Admission runs per frame.** A recording where frame 300 stopped being
admissible is a finding. Per-frame validity in the manifest; the top level says
whether every frame was science.

### Output format: why `.npy`, and where AVI does belong

The "no hardware encoder" argument does **not** settle this, and using it to
was sloppy — recording into RAM decouples encoding from the capture rate
entirely, so a post-capture encode can take as long as it likes.

The argument that does settle it is fidelity. The frames are 10-bit sensor
counts and the entire Stage 2 boundary exists so that `validity: science` means
exactly that. **H.264 is 8-bit and lossy**, so an H.264 science path is a
contradiction in terms — it would be `diagnostic` by construction.

But "AVI" is a container, not a codec, and one option inside it is real:

Measured on an **x86 desktop** on *synthetic* noise, 60 frames, 1456 × 1088
uint16. **Not a Pi — expect roughly 4–6× slower on a Cortex-A76 — and not this
sensor**, which is why the figures below are indicative only and the real
measurement moved into Stage 5c's pre-flight:

| format | MiB/s | ratio (σ=12) | ratio (σ=40) | fidelity |
|---|---:|---:|---:|---|
| `.npy` | 1930 | 1.00× | 1.00× | exact |
| FFV1 16-bit | 47 | 2.68× | 1.98× | **exact** |
| x264 8-bit | 212 | 14.7× | 6.7× | lossy, 8-bit |

Read off it:

- **FFV1 is a genuine lossless option** and compresses better than expected.
  But the ratio is governed by sensor noise — it falls from 2.7× to 2.0× when
  σ goes from 12 to 40 counts, and real data will be at the worse end. On a Pi,
  a 20 s burst would take **several minutes** to encode. That is a real trade,
  not an obvious win: minutes of post-capture wait to halve the file.
- **`.npy` stays the science default.** Exact, no dependency, one line to load
  in numpy and in MATLAB via `tv_read_npy`, and the flush is disk-bound rather
  than CPU-bound. FFV1 becomes a config option once the Pi numbers are known.
- **An 8-bit H.264 proxy is worth adding**, and this is the part of the AVI
  question that lands. It is ~15× smaller, encodes in about a minute on a Pi,
  and is the right thing to scrub through when deciding whether a burst is
  worth keeping. Written **alongside** the science data, named
  `diagnostic_…`, tagged `validity: diagnostic`, never instead of it.

### Bench test

1. Arm; check the reported duration against `free -m`, and confirm the buffer
   was prefaulted (RSS rises by the full buffer size, not by nothing).
2. Record to the limit. It stops cleanly, names the duration, flushes every
   frame to the external target.
3. Pull the USB stick before flushing. The flush is **refused**, the state reads
   "Captured — not saved", and saving to internal storage requires ticking the
   override. Arming again is refused until the burst is saved or discarded.
4. Fill the external target to within the reserve. The flush is refused before
   it writes anything, while the data is still in RAM.
5. Record 10 s, read the pair back, plot the inter-head `SensorTimestamp`
   difference. That number is the honest statement of this rig's stereo timing.
6. Encode one burst to FFV1 and to the 8-bit proxy, post-capture, and time
   both. **Superseded 8 October:** this was `python scripts/bench_encode.py`,
   which measured synthetic noise and therefore measured an assumption about
   the sensor. The timing now happens on real frames inside Stage 5c's
   pre-flight; `scripts/bench_encode.py` is to be deleted.

### Acceptance

Every frame the SDK delivered during the armed window is in the output or
explicitly accounted for. No unsaved burst is ever silently lost or overwritten.
No flush reaches internal storage without a deliberate, per-save override.

---

## Stage 5a — storage identity and drainable release *(F2, gate G3a)* — **IMPLEMENTED 8 Oct**

**Re-scoped 7 September.** Split, because only half of it gates recording:

- **5a, and it gates continuous recording absolutely:** identity at selection
  time (`st_dev` recorded and compared), the storage generation counter, and
  `EmptyWriteError` quarantining the device. A pulled USB stick leaves a
  writable mount-point directory behind, so without this a 190 MB/s recording
  silently lands on the SD card and destroys it.
- **5b, bench ergonomics, can follow:** drainable release, the four-way
  absent/read-only/full/slow distinction, the reserved internal-space floor.

**The burst recorder needs 5a.** An earlier draft of this section said it
needed neither half; that was wrong. Flushing 2-4 GB can fill internal storage,
and a flush that falls back to internal storage unasked is the SD-card failure
5a exists to prevent. What the burst does NOT need is 5b -- drainable release
matters to a session writing continuously, not to one bulk write that either
succeeds or is refused before it starts.

The reserved internal-space floor listed under 5b is therefore promoted to 5a:
a burst knows its exact size in advance, so the reserve can be enforced before
the first byte rather than discovered during.



### Changes

- **Identity at selection time.** Record the target's `st_dev` and mount point
  when it is chosen. `is_mounted` becomes: the path itself exists, and its
  `st_dev` equals the recorded one. `mount_of` already contains most of this.
  This closes the case where a pulled USB stick leaves a writable mount-point
  directory and writes silently go to the SD card.
- **A storage generation counter.** Every write is tagged with the generation
  it was admitted under. `retarget()` and `release()` increment it.
- **Release must drain.** Today `save_still` holds `_lock` only while
  allocating a counter, so `release()` returns while a write to the old target
  is still in flight — the UI's "release, then remove the disk" advice is not a
  safe handoff. Release stops new writes to that generation, waits for or
  explicitly fails in-flight ones, and only then reports releasable.
- **Distinguish the states** the review lists: absent, read-only, full,
  corrupt, temporarily slow. They currently collapse to one boolean.
- **`EmptyWriteError` quarantines the device.** Recovery can currently return
  false and later captures keep targeting the same disk.
- A reserved internal-space floor, so fallback cannot fill the SD card.
- **Sustained write rate, not a burst probe — added 8 October for 5c.**
  `verify_device()` currently times a single 4 MiB write. That number is the
  SLC cache, not the drive. Consumer SSDs hold a fast write cache of a few GB
  and then fall to their native rate; a DRAM-less QLC drive can drop from
  450 MB/s to under 100 MB/s. A five-minute pair recording is 11–57 GB, which
  leaves that cache well behind. So device verification gains a sustained
  measurement — write until 8 GB or 30 s, whichever comes first, and report
  **the throughput of the final quarter**, which is the only figure that
  predicts a multi-minute recording. The mean is reported too, and the gap
  between them is itself the diagnostic.
- **Filesystem is recorded, and named to the operator.** exFAT is what lets the
  SSD mount on the Windows desktop directly; it is also slower on Linux and
  unjournalled. ext4 is faster and safer and needs a reader on the desktop
  side. This is a real trade and it is the operator's, so the pre-flight
  measures whatever is actually there and states which it found.

### Bench test

Four physical tests, all on the rig with an expendable USB stick:

1. Pull the stick mid-session. Writes must fail or divert *and say so* — not
   succeed onto the SD card under a path claiming otherwise.
2. Pull it, then plug a **different** stick in so it mounts at the same path.
   The rig must notice it is not the selected volume.
3. Press Release during a capture. It must not report released until the write
   has finished or failed.
4. Fill the stick. The failure must name "full", not "write error".

### Acceptance

G3a. No success message may name a target that was not verified.

---

## Stage 5c — continuous recorder with declared degradation *(was Stage 9)* — **IMPLEMENTED 8 Oct**

**Added 8 October.** This is the "proper video recording" requirement. It was
Stage 9, gated behind everything; it moves here because it is the capability
being asked for, and because the one answer that unblocks it has now been
given: **under-rate loss is permitted provided it is declared.**

That single permission is what makes continuous recording tractable. The
previous draft defaulted to *stopping with an explicit incomplete result*,
because without permission to lose frames an under-rate disk has no legal
response. With permission, the disk no longer has to be fast enough — it has
to be **honest about what it kept**.

### The arithmetic, and where a USB 3 SSD sits in it

Two 1456 × 1088 heads, 1.51 MiB per frame per head at uint16:

| stored samples | 30 fps pair | one minute | five minutes |
|---|---:|---:|---:|
| 8-bit (R8) | 95 MB/s | 5.7 GB | 28 GB |
| 10-bit packed (R10P, if offered) | 119 MB/s | 7.1 GB | 36 GB |
| 10-bit as uint16 | 190 MB/s | 11.4 GB | 57 GB |

The Pi 5 exposes **one 5 Gbps USB 3 controller shared between both USB 3
ports** — about 400 MB/s achievable in practice, and only if the enclosure's
bridge supports **UASP**; a bridge that falls back to BOT can halve it. So
190 MB/s is inside the bus budget with roughly 2× headroom, and whether it is
inside the *drive's* budget for five continuous minutes is the question 5a's
sustained measurement answers.

This is why the design assumes degradation rather than hoping to avoid it: the
most likely failure is not a missing disk but a drive that holds 450 MB/s for
twenty seconds and then settles to 90.

### The degradation ladder, and the line through the middle of it

Five mechanisms can close a bandwidth gap. They are not interchangeable, and
the important distinction is **which are chosen before Start and which may act
during the recording**:

| | mechanism | effect on the data | cost | when |
|---|---|---|---|---|
| 1 | **lower the requested frame rate** | exact, **uniformly** sampled, longer exposure → less noise | fewer frames per second, by choice | **configuration** |
| 2 | **packed 10-bit raw from the sensor** | exact | needs the format to exist; readers must unpack | **configuration** |
| 3 | **8-bit raw (R8) from the sensor** | **lossy** → `validity: diagnostic` by the Stage 2 rules | loses 2 bits of every pixel | **configuration**, deliberate |
| 4 | **drop whole frames at the queue** | each retained frame exact; the sequence becomes **non-uniformly** sampled | temporal gaps | **runtime**, the only one |
| 5 | FFV1 lossless compression | exact | ~10 MB/s per Pi core — three orders off | **post-capture only** |

**The rule that follows:** the configuration is fixed before Start from measured
numbers, and **the only thing that may change during a recording is whether a
frame is kept.** Nothing alters bit depth, geometry or rate mid-stream. A file
whose frames are not all the same shape and depth breaks every reader
downstream, and a recording that silently changed its own fidelity halfway is
worse than one that lost frames, because the loss is at least countable.

Two notes on the ladder itself:

- **Rate reduction beats dropping when the deficit is known in advance.**
  Dropping discards exposures that were already taken and leaves irregular
  sampling; asking the sensor for 15 fps instead gives uniform sampling and a
  longer integration time. Dropping is the safety valve for a *stall*, not the
  plan for a *deficit*.
- **8-bit and packed formats are requested from the sensor, not computed.**
  Truncating uint16 to uint8 in Python costs a full-rate memory pass on cores
  that do not have it to spare; `R8` costs nothing because the ISP already
  produces it. Same for packed 10-bit — if libcamera offers it for this
  sensor, it is free, and if it does not, it is not worth doing in software.
  Whether it is offered is a measurement, item 4 of the pre-flight.

### Required design

**1. A pre-flight that measures this rig, inside the interface.**

Not a script, and not synthetic noise — a synthetic benchmark measures an
assumption about the sensor. The Recording tab gets a **Measure** action which
uses the real camera and the real target:

1. Capture a short real burst into the 5b RAM buffer.
2. Write it through the actual chunked writer to the selected target, at the
   volume 5a specifies (8 GB or 30 s), and report **last-quarter** throughput.
3. Enumerate the raw formats libcamera actually offers for the IMX296 here, and
   whether any packed 10-bit mode exists.
4. Derive, for each configuration on the ladder, **the maximum sustainable
   frame rate** and the predicted drop fraction at the requested rate.
5. Persist the result **against the storage generation counter** from 5a, so
   changing the disk invalidates it rather than carrying a stale number.

The recorder then offers **only configurations the pre-flight proved**, each
labelled with its predicted loss and its resulting `validity`. This replaces
`scripts/bench_encode.py`, which measured my guess at the sensor rather than
the sensor.

It also settles, as a by-product, the outstanding `raw_alignment` lsb/msb
question from Stage 2, since step 3 reads the negotiated format and step 1
produces a real `raw_observed_max`.

**2. Shape: owner → per-head bounded queue → chunk writer → journal.**

- The acquisition owner (Stage 3) stays the single camera consumer. Recording
  is a state of the owner loop, as in 5b, not a command.
- **One bounded queue per head.** Sized so the total is ≤ 25 % of
  `MemAvailable`, reported in frames and seconds before Start. A few hundred
  MiB is 2–3 s of pair stream — enough to ride out a stall, and no finite
  queue survives a sustained deficit, which is precisely why item 3 exists.
- A dedicated writer thread per head, writing in **4–8 MiB blocks** (a few
  frames), `fsync` at chunk boundaries only. Per-frame `fsync` at 60 writes/s
  destroys sustained throughput and buys nothing a completion journal does not.

**3. Drop policy: drop the newest, count it, and never hide it.**

- **Drop-newest, not a wrapped ring.** Overwriting the oldest frame makes the
  retained sequence depend on future events and discards data already entered
  in the index. Drop-newest keeps the retained set a prefix-consistent
  subsample: if the queue is full when a frame arrives, that frame is counted
  as dropped and its buffer released immediately.
- **Heads drop independently.** Coupling the drops — discarding head R's frame
  because head L's was dropped — throws away good data to impose a pairing the
  rig does not guarantee. The heads are free-running and no synchronisation is
  claimed, so the recording is **two independently sampled sequences sharing a
  recorded clock domain**, reconciled offline by `SensorTimestamp`. This is the
  same position as 5b, applied to a longer window.
- **Loss is surfaced live**, per head: retained, dropped, instantaneous and
  cumulative drop fraction, and current write throughput. Not a post-hoc
  footnote.
- The manifest records **every gap as an explicit interval** — first and last
  missing sequence number with their bracketing timestamps — so "94 % of head
  L, 97 % of head R, longest gap 340 ms" is on the record and reconstructible.
- A **drop-fraction ceiling** is set before Start. Exceeding it stops the
  recording with an explicit incomplete result, because past some point the
  honest answer is that this disk cannot do this job. Default 10 %, operator
  editable, recorded in the manifest.

**4. File layout: chunked arrays, authoritative index.**

Per-frame files at 60 files/s for five minutes is 18 000 files, which is slow
on ext4 and worse on exFAT. Instead, **one `.npy` per head per chunk**, each a
3-D `(N, H, W)` array of fixed N — numpy memory-maps it directly and the
existing `tv_read_npy` reads it in MATLAB, so no new format arrives in the
readers. Chunk size 128 frames per head (~194 MiB); the final short chunk is
written with its true N.

Alongside it, a **per-chunk index sidecar**, which is authoritative: for every
retained frame, its sequence number, `SensorTimestamp`, per-frame validity and
offset within the chunk. Sequence numbers are **as exposed, not as stored**, so
a gap in the index *is* the record of a drop.

**5. Per-frame admission still runs.** A recording where frame 9 000 stopped
being admissible is a finding. Per-frame validity in the index; the manifest's
top level says whether every retained frame was science, and names the first
frame that was not.

**6. Preview is subordinate to the recording.** The 12 Hz browser cap competes
for the same cores as the JPEG encode and the write path. While recording,
preview drops to ~2 Hz, published from the queue rather than by a separate
read, and is suspended entirely if the drop fraction rises above half the
ceiling. **A preview frame is never worth a recorded frame.**

**7. The playable file is a post-capture proxy, as in 5b.** 8-bit H.264 or
MJPEG, written after the recording closes, named `diagnostic_…`, tagged
`validity: diagnostic`, **alongside** the exact data and never instead of it.
Scrubbing through a recording to decide whether it is worth keeping is a real
need and this is the right way to meet it; H.264 is 8-bit and lossy, so it can
never be the science artefact.

**8. Space is checked against the plan, not the moment.** Before Start, the
requested duration × the configured rate is compared against free space with
5a's reserve intact, and the **maximum recordable duration on this target is
displayed**. A recording that will hit the reserve in 90 s says so before it
starts rather than failing at 90 s.

### Bench test

On the rig, with the USB 3 SSD and an expendable filesystem:

1. Run Measure. Record last-quarter vs mean throughput. **If they differ by
   more than ~1.5×, the drive has an SLC cache and the sustained figure is the
   only one that counts.** Note the filesystem and whether UASP is active
   (`lsusb -t` shows the `uas` driver; a `usb-storage` binding is BOT).
2. Five-minute pair recording at the highest configuration Measure endorsed.
   Check: frames retained vs exposed, drop intervals present in the index,
   chunk count and sizes as predicted, throughput log flat rather than
   decaying.
3. Repeat at 30 fps uint16 whether or not Measure endorsed it — the point is to
   **see the degradation work**. Drops must appear, be counted, be visible live,
   and be reconstructible from the index afterwards.
4. Pull the SSD mid-recording. Must stop with an explicit incomplete result
   naming the chunk that failed, and must not divert to the SD card.
5. Fill the SSD to within the reserve mid-recording. Must stop with "reserve
   reached", not a write error.
6. Read a recording back on the desktop: reconstruct both sequences, plot the
   inter-head `SensorTimestamp` difference and the retained-frame intervals.
   Those two plots are the honest statement of this rig's temporal behaviour.
7. Encode the proxy post-capture and time it. That number decides whether the
   proxy is automatic or on request.

### As implemented — 8 October

Code: `src/trilobite/recording/` (`buffer`, `burst`, `chunks`, `continuous`,
`ladder`, `manager`, `preflight`), `src/trilobite/storage/identity.py`, the
`/api/recording/*` endpoints and the dashboard's Video tab.

Four corrections made while finishing the stage, each of which the design above
asked for and the first implementation did not deliver:

1. **`offer` has three outcomes, not two.** It returned `False` both for a
   frame that was dropped and for one that arrived after the recorder had
   stopped. The capture loop counts the first as a loss, and the recorder never
   counted the second as exposed, so every recording ended with one phantom
   drop — and the agreement between those two independent counts is the only
   thing that would detect a frame genuinely lost between the capture thread
   and the ring. A detector that always fires by one is not a detector. `None`
   is now that third outcome, in both recorders.

2. **Start waits for the heads.** `start()` set a flag and returned. Each
   capture loop decides once per frame whether to record or to release the
   frame to the preview cap, so a loop that evaluated that check just before
   the flag flipped released one more frame undecoded — never offered, never
   counted, simply absent, which is the one kind of loss this stage exists to
   make impossible. Start now blocks until every head with a live capture loop
   has been seen in the recording branch, and a head that does not arrive is a
   failed start rather than a half recording that looks complete.

3. **The synthetic backend records unoriented pixels**, like picamera2, instead
   of going through `capture_full` and turning them. It was the one backend in
   the rig recording under the opposite convention, and it is the backend the
   dashboard, the browser scenarios and most of the tests run against.

   The first attempt at giving it a preview while recording took a second
   `read_preview()`, which advanced the sequence counter — and a gap in the
   sequence numbers is exactly how a dropped frame is recorded, so every
   preview taken during a recording read back as a lost frame. Both planes now
   come from one render at one phase under one sequence number, which is the
   contract picamera2 gets from one request.

4. **The preview stands down past half the drop ceiling**, which item 6 above
   specifies and the first implementation left out: the 2 Hz cap was there, the
   suspension was not.

Verification: 573 unit tests, 19 browser scenarios, ruff clean. **14 of 14
mutations caught** (`tools/mutate_stage5.py`) — three survived the first run,
all three on behaviour added in corrections 1 and 2 with no test asserting it.

Readers: `scripts/read_capture.py` opens a recording directory and recomputes
the accounting from the chunks and indexes alone; `matlab/tv_read_recording.m`
and `matlab/tv_recording_frame.m` do the same in MATLAB, seeking to one frame
rather than loading a chunk. The MATLAB files are **not executed by any test**
— no MATLAB or Octave on the build machine — so what is checked is the layout
they depend on: `test_the_matlab_frame_arithmetic_lands_on_the_right_bytes`
re-implements their header parse, seek and column-major reshape in Python and
compares every row against `np.load`.

Deferred, deliberately: **the 8-bit playable proxy** (item 7). It is the one
piece of the written design not built, because the requirement settled on
8 October was continuous recording with declared degradation, and a scrubable
file was explicitly not chosen. Nothing in the implementation precludes it —
it is a post-capture pass over finished chunks — and the reason to want it
stands: deciding whether a five-minute recording is worth keeping should not
require a numpy session.

### Acceptance

Every exposed frame is either in the output or counted in the manifest as a
dropped frame inside a named interval — **no frame is unaccounted for**. No
recording changes its pixel format, geometry or requested rate mid-stream. No
recording reaches a target that was not verified under the current storage
generation. The live loss figures match the manifest.

---

## Stage 6 — transactional capture sets *(F3, gate G3b)*

### Changes

- A **capture manifest**: run ID, capture-set ID, per-head frame IDs, storage
  generation, filenames, content hashes, expected members, and a state of
  `pending` / `complete` / `partial` / `failed`.
- **Write temporary members, publish completion last.** Today the image and
  sidecar are written straight to their final names and only the image write is
  inside the recovery block, so a sidecar failure leaves an orphaned image.
- **Startup reconciliation.** Incomplete sets are resolved, not left as debris
  that looks like data.
- **Idempotent retries.** A capture is queryable by ID after a lost HTTP
  response, rather than blindly recaptured.
- **`capture_all` becomes one transaction** with a shared capture-set ID, and —
  separately — stops completing each head's disk write before requesting the
  next, which is what currently inflates pair skew.
- **Record achieved durability per operation.** Stage 0's cached capability
  is not proof that a later directory sync succeeded. Track actual file and
  directory sync outcomes, invalidate capabilities across target generations,
  and distinguish platform support from achieved completion. Do not infer
  `strict` solely from Linux or swallow a directory-sync failure.
- **`CaptureSession` stays disabled** until it uses this same service. It has
  its own writer, a fixed root and its own index format; it does not inherit
  retargeting merely by sharing the durable-write helpers.

### Bench test

1. Capture 50 stereo sets. Cut power at a random point during one of them.
   After reboot, a reader must classify every set as complete or partial with
   no guessing from filenames.
2. Force a sidecar write to fail. The set must be `partial`, not an orphaned
   image reported as an error.
3. Drop the network mid-`capture-all` and re-query the capture ID.

### Acceptance

G3b.

---

## Stage 7 — lifecycle consolidation and durable state file *(F6)*

### Changes

- Explicit `starting → running / degraded → stopping → stopped / failed` per
  camera and for the application.
- **Preserve Stage 3's lifecycle guarantees.** Failed-stop handling, owner-only
  close, partial-open cleanup and restart generation isolation are already
  required in Stage 3. This stage consolidates application-wide lifecycle and
  exercises the broader 100-cycle/deployment scenarios; it does not first fix
  unsafe ownership here.
- Restore state **before** publishing measurement-ready frames.
- **`StateStore` gains revisions and durability.** Write/rename is atomic but
  not durable — no file or directory fsync. And the dirty flag is cleared after
  the snapshot is taken, so a change arriving during a save is lost until the
  one after. Acknowledge a saved *revision* and clear only that.
- Decide and document whether runtime stage add/remove is meant to persist.
  `apply_state` applies values to the YAML-created pipeline and does not
  reconstruct topology.

### Bench test

1. 100 stop/start cycles via systemd. No abandoned worker, no false stop
   success, no camera left held.
2. Kill -9 during an autosave; the previous good state must survive.
3. Change a parameter during a save and confirm it is in the next one.
4. Start with one camera absent (disconnect CSI only while powered off), and
   separately inject startup failure. The other must come up `running` and
   the missing one must be `failed`, not silently absent.
5. Time the worst-case shutdown against systemd's 15-second budget.

---

## Stage 8 — server admission, trust boundary, deployment manifest *(§4, G6–G8)*

### Changes

- **Bound the workload.** Each MJPEG client currently encodes independently;
  tab-aware streaming limits one page, not the number of clients. Add one
  encoded preview cache per head per revision, cap concurrent streams and
  captures, and reserve capacity for status and control requests. Starlette's
  threadpool default is 40 tokens and is shared.
- **Server-side command semantics.** `QUICK_BUSY` and the one-pending-shot flag
  are browser-local and protect one tab. Extend Stage 3's bounded owner-command
  admission with an operator lease or revision conflict policy and broader
  multi-client resource controls. Capture-by-ID reconciliation builds on Stage 6.
- **Declare the trust boundary.** The service binds `0.0.0.0` with no
  authentication and can drive cameras, retarget paths and mount filesystems
  under the service user. Either document an isolated bench network as the
  supported deployment, or authenticate the mutating endpoints and validate
  storage targets against an allowlist. This is a decision, not a default.
- **Run provenance.** Record release commit, dirty-tree status, Python and
  package versions, OS and kernel, picamera2 and libcamera versions, negotiated
  stream formats and encoder choice. Carry a backend build identity alongside
  the HTML hash — updating a file on disk does not update already-imported
  Python in a running process, which the current staleness check cannot see.
- **Split `web/server.py` by responsibility** and the dashboard into static JS
  modules. No build step, no framework.
- A known-good Pi image and package inventory, and a tested rollback.

### Bench test

Three browsers on the dashboard, a capture burst, a deliberately slow disk;
measure p95 control latency and maximum queue age. Then the G8 release run:
eight-hour still workload, 100 lifecycle cycles, restart and rollback.

---

## Stage 9 — unattended long-session operation

**Superseded in substance on 8 October.** The continuous recorder that was this
stage is now **Stage 5c**, because continuous recording to disk is the stated
Stage 5 requirement rather than a later ambition. The arithmetic, the
owner → queue → chunk-writer → journal shape and the SD-card prohibition all
moved there, together with the correction that matters: this section defaulted
to *stopping with an explicit incomplete result* when the path could not keep
up, which was the only legal answer while every exposed frame had to be
retained. **Explicit loss is now permitted**, so dropping counted frames is the
first response and stopping is reserved for breaching the drop ceiling.

Stage 6 was never a prerequisite, contrary to the note originally at the end of
this section. Stage 6 is about capture *sets* — a stereo pair of stills sharing
an id, with per-member manifests. A recording is a different transaction shape:
one chunked stream with a completion journal. The dependency is on storage
identity, not on set atomicity.

What genuinely remains later, and is still ungated by any requirement:

- **Sessions measured in hours** rather than minutes, where total volume rather
  than instantaneous rate is the binding constraint, and the drive has to be
  managed rather than merely verified — rotation, per-session quotas, and a
  policy for what happens when a day's work exceeds the disk.
- **Unattended operation**: scheduled or triggered recording with no operator
  present to read a drop counter, which needs the Stage 7 durable state file
  and a notification path rather than a UI panel.
- **Thermal and duty-cycle behaviour** over such sessions. Two capture loops,
  two writer threads and a saturated USB 3 controller on a passively cooled Pi
  5 is an open question, and one that only a long run answers.

None of this blocks 5c, and none of it should be designed before 5c's bench
numbers exist.

---

## Decisions needed from you, and which stage each blocks

| # | Question | Blocks |
|---|---|---|
| 1 | ~~Is 0.1 a still/burst instrument, or must it record continuous raw data?~~ **Answered 7 Sep: both, burst first. Raw sensor counts, both heads, no synchronisation claim.** | closed |
| 2 | ~~Must every exposed frame be retained, or are explicit losses acceptable?~~ **Answered 8 Oct: explicit losses are acceptable, provided they are declared and counted. Frame dropping, or another declared lossy configuration, is permitted; silent degradation is not.** Disk full or vanished still stops the recording with a named reason. | closed — defines Stage 5c |
| 3 | How many simultaneous viewers/operators, and is the rig on an isolated network? | Stage 8 — both the concurrency cap and whether authentication is needed |
| 4 | Acceptable command latency, preview age, stop/recovery time? | Stages 1/3 require documented provisional thresholds; Stage 8 final operating envelope |
| 5 | Supported OS / picamera2 / libcamera / filesystem baseline, and who owns rollback? | Stage 8 manifest |
| 6 | Should runtime pipeline add/remove persist? | Stage 7 |

Stages 3/4 are subject to the entry and evidence gates above. Provisional
engineering thresholds allow development while research requirements mature,
but must be explicit before acceptance testing.

Question 5's filesystem half is now partly load-bearing for Stage 5c rather
than only for the Stage 8 manifest: exFAT buys direct mounting on the Windows
desktop and costs throughput and journalling, ext4 the reverse. Stage 5a's
pre-flight measures whichever is present, so the decision can be made on
measured numbers rather than in advance — but it does have to be made.

---

## What this plan does not do

- It does not change the stack. The review found no case for a different
  language, framework or transport, and neither do I.
- It does not touch calibration or optics.
- It does not adopt "mutation-test everything". The review is right that
  equivalent mutations and incidental changes would dominate maintenance.
  Mutation checking is kept for the high-consequence invariants it names: frame
  interpretation, ownership and release, validity flags, transaction
  completion, provenance and timestamp mapping.
- It does not split the process. A separate capture process is justified only
  if SDK hangs prove unrecoverable in-process, and that is evidence Stage 3 and
  Stage 7 will produce or fail to produce.
