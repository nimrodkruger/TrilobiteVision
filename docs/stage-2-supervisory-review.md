# Supervisory review through Stage 2

6 September 2026. Reviewed tree: `ce1d92c`, including the Stage 0/1 changes
in `64b4c3d`. Scope: software architecture, implementation contracts and
verification; no optical or calibration-method assessment, and no application
code changes in this review.

## Decision

**The staged approach remains valid. Stage 2 is implemented but is not yet
accepted against G1. Supervision is needed before Stage 3, not only after
Stage 4.** Close the admission and observability gaps below, then review the
Stage 3 command/lifecycle contract before implementation. Review Stage 3's
test and rig evidence before beginning Stage 4. These are evidence checkpoints,
not a recommendation to replace the software stack.

The revised `implementation-plan.md` is authoritative for remaining work. Its
change matrix distinguishes the original Stage 3/4 scope from this amendment.
The ten-stage sequence is retained; completion of Stage 4 is not release
acceptance for storage integrity, transactional capture or continuous recording.

## Evidence and limits

The existing suite ran locally with `PYTHONPATH=src` using
`.venv-review/Scripts/python.exe -m pytest -q`: **374 passed, 2 skipped**, with
two dependency deprecation warnings, in 49.47 seconds. Playwright is absent
from this environment, so this is not browser acceptance. No attached Pi,
physical-camera, Linux CI or MATLAB execution was available in this review.
The earlier cleanup log's test count is historical, not this run's result.

Additional temporary Python probes exercised the actual admission function,
reader class, Picamera2 adapter admission method and replay backend. They
confirmed the results below. MATLAB conclusions are from source inspection.
Synthetic byte fixtures and mutation tests are valuable, but do not establish
the rig's negotiated format or driver behaviour.

## What is sound

- The raw-format allowlist and geometry reconciliation are a substantially
  better boundary than inferring pixel width from an array's row length.
  Admission before orientation is the correct order for trimming row padding.
- Explicit diagnostic sidecars, diagnostic filenames and diagnostic saved
  previews improve the visibility of data that must not be used as sensor counts.
- Stage 1 distinguishes configured rates from observed activity, ages out
  rate estimates, attaches processing failures and avoids continuous stale
  MJPEG retransmission. These are useful foundations for the owner refactor.
- Packaging is restricted to `trilobite*`; reader/test dependencies and
  test isolation have improved. The new tests should be retained.

## Findings requiring closure before Stage 3

| ID | Evidence in the implementation | Consequence and required correction |
|---|---|---|
| R1 | `scripts/read_capture.py:Capture.is_science` accepts every value except exact `diagnostic`; `matlab/tv_require_science.m` has the same policy. Probes returned true for `unrecorded` and `typo`. | Missing, corrupt or misspelled validity bypasses the measurement guard. Require explicit recognised science validity plus admission evidence appropriate to the schema. Preserve archive inspection through an explicit legacy/inspection path; never infer that all archived files were manually validated. |
| R2 | `types.py` defaults frames to `science`. `cameras/offline.py:ReplaySource` ignores sidecars. Replaying an array whose sidecar says `diagnostic` produced a frame marked `science`. | Validity is not preserved across the software stack. Make eligibility explicit at trusted producers; prevent default construction, replay, ISP main capture and derived frames from promoting unvalidated input. Full session replay is Stage 4, but this promotion must close now. |
| R3 | `cameras/rawformat.py:admit` checks item size rather than unsigned dtype and checks only the maximum value. R10 probes accepted `int16(-1)` and `float16(0.5)`. | The claimed sensor-count invariant is false. Define supported unsigned sample/byte-buffer representations and byte order, positive geometry, exact negotiated stride and value alignment; reject incompatible representations. Add negative and fractional fixtures as regressions. |
| R4 | `cameras/picam.py:_admit_raw` catches admission failure and returns diagnostic data even when `allow_unvalidated_raw` is false. A 65535-valued R10 probe confirmed this. | Runtime failure is not governed by the advertised opt-in contract. Refuse the science request explicitly; diagnostic persistence requires explicit authorisation through the configured policy and a distinguishable result. Never report a diagnostic fallback as successful science capture. |
| R5 | `picam.py:open` selects a format before configuration; raw is configured at `sensor_res`, while `_admit_raw` validates against `_full_res`, also used for main output. | Requested configuration and main geometry are not independent evidence of the actual raw layout. Validate the post-configuration raw stream and sensor mode, record actual format/size/stride, and use these for each buffer. Test unequal main/raw sizes and driver substitutions. |
| R6 | Dashboard status polling awaits `fetch` without a timeout; the next poll is scheduled after that await. | A request that never completes can prevent the stale-status indication. Add a request timeout and an independent elapsed-time freshness watchdog; verify hung responses as well as HTTP errors. |

R3/R5 must distinguish **sensor bit depth, container width and bit alignment**.
The official Picamera2 manual describes Pi 5 uncompressed samples as
left-shifted in 16-bit words and explicitly warns against deriving sensor bit
depth from the raw format name. This does not prove the current mono rig
fails: `R16` is supported by the table. It does mean R10-only fixture assumptions
and a format-name-derived `raw_bits` field are insufficient evidence of sensor
counts. Confirm the deployed Pi/software combination with real buffers and
configuration records. Unsupported negotiated layouts must fail explicitly.
Source: [Picamera2 manual, raw stream configuration, pp. 21–22](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf).

## Stage 0/1 status corrections and deferred risks

Stage 0 is not wholly closed: `src/flyeye` and its service file remain, although
package discovery now excludes them. `Claude outputs/tests.yml` is a workflow
draft outside `.github/workflows`; it is not active GitHub Actions CI. The
writer-construction guard is useful but is not a general filesystem sandbox.
Record Linux and browser results explicitly rather than claiming them from a
Windows unit run. Stage 0 also changed writer durability metadata, and Stage 1
changed UI behaviour; the plan's original descriptions understated their scope.

Observed software read cadence must not be labelled proven sensor exposure or
drop accounting. Stage 3 should count SDK deliveries, preview suppression and
published frames separately and retain sensor identity only when the SDK
supplies it. A shared software sequence alone cannot prove that a raw image
and preview came from one exposure.

The writer's cached directory-sync capability is not evidence that every
subsequent write achieved that durability. Keep actual write-result reporting
and invalidation across target generations in Stages 5/6. Storage identity,
release races and partial capture sets remain open by design; Stage 2 does not
resolve them. The parked recorder must remain disabled.

## Required differentiation for the next two stages

**Stage 3 owns acquisition correctness.** A queue alone does not establish
exclusive SDK ownership. Open, configure, controls, request acquisition,
release and close need one explicit owner lifecycle. Queue admission is bounded;
deadlines do not imply interruption of a blocking SDK call; queued cancellation
must prevent later execution; controls submitted against an already acquired
request cannot be claimed effective in that request. Copy buffers and metadata
before release and test delayed completion after stop/restart. Move this minimum
lifecycle work from Stage 7 and command admission from Stage 8 into Stage 3.
Keep broad deployment, persistent state and multi-client resource policy later.

**Stage 4 owns scientific provenance.** Use the Stage 3 acquisition envelope
and freeze the actual processing parameters, topology and orientation used by
each execution. Preserve per-stage success/failure and separate actual sensor
metadata from control requests. Version the persisted schema and enforce it in
both readers. Full replay lineage belongs here, while prevention of diagnostic
promotion is already an entry prerequisite.

Correct the old Stage 4 acceptance test: saving the **same retained frame**
twice after a live edit must preserve identical acquisition and processing
provenance. Save IDs and times may differ. A newly processed frame after the
edit should carry the new revision. These are two distinct tests.

The amended plan gives deterministic fake-SDK tests and separate rig checks
for both stages. Record test configuration, thresholds, outcomes and unresolved
limitations before declaring a stage accepted.
