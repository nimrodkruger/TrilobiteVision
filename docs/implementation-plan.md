# Implementation plan — responding to the 0.1 architecture review

**Responds to:** `docs/software-stack-review.md` (6 September 2026).
**Shape:** ten stages. Each is a single coherent change, independently
deployable, with a bench test that can pass or fail on its own. Nothing in a
later stage is needed to evaluate an earlier one.

**How to use it.** Do one stage. Deploy it. Run its bench test on the rig. If
it passes, say so and the next stage starts; if it fails, the stage is reworked
before anything else moves. No stage leaves the rig in a state where the
previous stage's test would no longer pass.

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

`src/flyeye/` and `systemd/flyeye.service` are still present on the working
tree, and `[tool.setuptools.packages.find] where = ["src"]` discovers both
packages.

### Where I disagree with nothing, but would sequence differently

The review orders its six work packages by severity. This plan orders by
**testability**, and the difference is one preparatory day at the front:

> You cannot bench-test the acquisition rework if the status display can report
> a stale frame rate as live and a failed stage as success (F5). The
> observability work is not just a P2 finding — it is the instrument every
> later stage is measured with.

So Stages 0–2 are small, additive and low-risk, and Stage 3 is the first
structural change. If you would rather take the biggest change first, the
order becomes 3 → 5 → 6 → 4 → 2 → 1 → 7 → 8, and Stage 3's bench test gets
weaker. Say which you want; the default below is the recommended one.

---

## Stage summary

| # | Stage | Closes | Size | Hardware test |
|---|---|---|---|---|
| 0 | Test hygiene, delete `src/flyeye`, correct the report | §6, §4 | ½ day | **DONE** — bar the `src/flyeye` deletion |
| 1 | Truthful status and liveness | F5, G5 | 1 day | **DONE** — awaiting the bench test below |
| 2 | Validated raw-frame admission | F7, G1 | 1 day | capture works; a compressed format is refused |
| 3 | **Single acquisition owner per head** | F1, G2 | 3–4 days | the crash regression, sustained |
| 4 | Provenance bound to the frame | F4, G4 | 2 days | change parameters mid-burst, read the sidecars |
| 5 | Storage identity and drainable release | F2, G3a | 2 days | pull the USB disk in four ways |
| 6 | Transactional capture sets | F3, G3b | 3 days | cut power mid-capture |
| 7 | Lifecycle states and durable state file | F6 | 2 days | 100 stop/start cycles; kill during save |
| 8 | Server admission, trust boundary, deployment manifest | §4, G6–G8 | 3 days | three browsers, slow disk, restart |
| 9 | Continuous recorder | G9 | gated | requirements must be frozen first |

Sizes are engineering days for one person, excluding bench time.

---

## Stage 0 — make the evidence trustworthy — **IMPLEMENTED 6 Sep**

**No behaviour changes. Nothing to deploy to the Pi.** This exists so that "the
suite is green" means something on your machine, and so there is one capture
implementation in the tree rather than two.

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

None. Deploy nothing.

### Acceptance

Suite green on Windows and Linux with no writes outside `tmp_path`; one package
in `src/`.

---

## Stage 1 — truthful status and liveness *(F5, gate G5)* — **IMPLEMENTED 6 Sep**

**Purely additive.** No acquisition, storage or UI behaviour changes. This is
the instrument for testing Stages 3–8, which is why it is early.

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

1. With both cameras running, pull a ribbon cable. Within the freshness
   deadline the UI must say that camera has stopped — not keep showing ~12 fps.
2. Set an MLA pitch that makes the overlay stage throw. The stage's error
   counter must rise and the panel must show it; the preview may continue.
3. Stop the server's status polling (block the endpoint in devtools). The
   header must show that status is stale.

### Acceptance

G5. No path where a frozen source, a throwing stage or a dead status poll
continues to read as healthy.

### Rollback

Revert; nothing else depends on it.

---

## Stage 2 — validated raw-frame admission *(F7, gate G1)* — **IMPLEMENTED 6 Sep**

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
the device; `allow_unvalidated_raw` per camera is the only way past.
Diagnostic captures are named `diagnostic_…` ahead of the tag and carry
`validity` in the sidecar. Saved previews are diagnostic too, which was not in
the plan and follows from the same argument. `scripts/read_capture.py --detect`
and the new `matlab/tv_require_science.m` refuse them.

The substantive change beyond what the plan asked for is the **direction** of
the stride reconciliation: the old code inferred bytes-per-pixel from the row
length, which cannot distinguish "10-bit padded" from "8-bit wide". The format
now states it and the shape confirms it.

15 mutants, all caught — one only after adding a test for the invariant rather
than the table. See `docs/cleanup-log.md` 2026-09-06 (p) for the full record
and the bench-test steps.

---

## Stage 3 — one acquisition owner per head *(F1, gate G2)*

**The structural change.** Everything after it is easier; nothing before it is
required, except that Stage 1 makes its bench test worth running.

### Design

Today: the capture thread calls `capture_request()`; so does `capture_full()`
from the API worker; so do control writes. Two mutexes serialise them. The
review is right that this is not a single owner — request identity,
cancellation and shutdown span two paths, and the failure class that took the
rig down has been mitigated rather than removed.

Replace with a command queue owned by the capture thread:

```
API worker                      capture thread (sole SDK owner)
  submit(Command) -> Future       loop:
  future.result(deadline)           take request  (all three streams)
                                    serve preview
                                    drain command queue against THIS request
                                    release request
```

- A `Command` carries an id, a kind (`still_raw`, `still_main`, `set_controls`),
  a deadline and a `Future`. Every command reaches a terminal outcome —
  fulfilled, failed, or cancelled by deadline. No uncorrelated shared pending
  flag.
- **`capture_full` and `set_controls` become owner-private.** Nothing outside
  the thread calls into picamera2. `_capture_lock` and the picam `_lock`
  disappear, because there is no second entrant to lock against.
- **The raw still comes out of the request the loop is already holding.** The
  three-stream configuration already carries `raw`, so a second
  `capture_request()` is unnecessary — and this makes the raw still the same
  exposure as the preview it arrived with, which today is only true of the
  `main` handshake.
- SDK buffers are copied or released according to their documented lifetime
  before anything else sees them, including on a decode failure.
- Disk and JPEG work stay outside the owner, as now.

The existing `request_full_frame` / `take_full_frame` handshake is subsumed by
the queue and removed, so there is one mechanism rather than two.

### Bench test

This is the regression that matters. On the rig, both cameras running:

1. **Sustained contention, 30 minutes.** Preview on both, a still every two
   seconds, and a slider being dragged continuously. Previously this class of
   load took the rig down. No crash, no stall, no rising skipped count beyond
   the configured cap.
2. **Deadlines are honoured.** Stop a camera while a still is in flight — the
   request must come back as a failed or cancelled outcome within its deadline,
   not hang the HTTP handler.
3. **Same exposure.** A raw still and the preview frame it accompanies must
   carry the same sequence number.
4. Check `dmesg` for CSI errors before and after.

### Acceptance

G2, with the fake-SDK audit the review specifies: exactly one thread ever takes
a request per head, every command terminates, every buffer is released even on
a decode failure.

### Rollback

Largest revert of the plan. Keep it a single commit against a known-good tree
and run the bench test before starting Stage 4.

---

## Stage 4 — provenance bound to the frame *(F4, gate G4)*

The review's probe is unanswerable: render a preview at gain 1, change the
stage to gain 2, save the preview — the pixel is 20 and the sidecar says gain 2.
No timing trick required.

### Changes

- **A pipeline execution is an immutable revision.** `Pipeline.__call__`
  snapshots stage *references* today, and the overlay re-reads `self.params`
  during an invocation. Instead, build a frozen configuration record at the
  frame boundary and run the whole pipeline against it.
- Each derived frame carries: the ordered stage configuration actually used,
  per-stage outcome (`ok` / `skipped` / `failed`), the orientation applied, and
  the sensor metadata the driver reported.
- **The writer persists that record**, and never calls
  `pipeline.settings_snapshot()` at save time. Saving an old preview stores the
  old processing record.
- Distinguish **requested** controls, **acknowledged** requests and
  **effective** exposure in the sidecar. They are three different things today
  reported as one.
- Offline readers refuse a measurement frame with missing mandatory provenance,
  and allow it in an explicitly labelled inspection mode.
- **Replay fidelity** (review §4, last item). `ReplaySource` loops image files,
  invents timestamps and sequence numbers and ignores the original sidecars. It
  is image playback, not session replay. Either make it preserve recorded
  identity, or rename it so nobody cites it as evidence of recorded timing.

### Bench test

1. Start a 20-frame quick-record burst; halfway through, change display gain
   and toggle an orientation flip. Every sidecar must describe exactly the
   configuration its own pixels went through. This is checkable offline with
   `scripts/read_capture.py`.
2. Save a preview, then change a stage parameter, then save the *same* preview
   again from the bus. The two sidecars must differ, and the first must match
   the pixels.

### Acceptance

G4.

---

## Stage 5 — storage identity and drainable release *(F2, gate G3a)*

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
- **Record the durability capability** actually achieved (`strict` on Linux,
  `file-only` where directory sync is unavailable), rather than swallowing the
  directory-sync failure.
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

## Stage 7 — lifecycle states and durable state file *(F6)*

### Changes

- Explicit `starting → running / degraded → stopping → stopped / failed` per
  camera and for the application.
- **`CameraRuntime.stop` must not lie.** It joins for five seconds then closes
  the source regardless; a blocking SDK call outlives the join. A timeout
  becomes a visible `failed-stop`, not an assumption that ownership ended.
- Guaranteed cleanup of a partially opened camera: `Picamera2Source.open`
  assigns `self._picam` only after successful start-up, so an exception midway
  leaves an unowned handle.
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
4. Unplug a camera during start-up. The other must come up `running` and the
   missing one must be `failed`, not silently absent.
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
  are browser-local and protect one tab. Add server-side admission, an operator
  lease or revision conflict policy, and capture-by-ID reconciliation.
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

## Stage 9 — continuous recorder *(G9)* — gated

**Not started until the requirements are frozen.** The review's arithmetic for
two 1456 × 1088 heads at 30 fps:

| stored samples | rate | one minute | ten minutes |
|---|---:|---:|---:|
| 8-bit | 95 MB/s | 5.7 GB | 57 GB |
| 10-bit as uint16 | 190 MB/s | 11.4 GB | 114 GB |

A 512 MiB queue absorbs about **2.8 seconds** of the uint16 pair stream. No
finite queue makes an unavailable disk survivable.

Shape: acquisition owner → validated frame packet → bounded queue with an
explicit drop counter → dedicated chunk writer → completion journal. Preview
takes an independently throttled branch. Default to **stopping with an explicit
incomplete result** when the data path cannot keep up, and never silently
divert a 190 MB/s recording to the SD card.

This is why Stages 3, 5 and 6 come first: the recorder needs a single owner, a
verified target and a transaction, and building it before them means building
it twice.

---

## Decisions needed from you, and which stage each blocks

| # | Question | Blocks |
|---|---|---|
| 1 | Is 0.1 a still/burst instrument, or must it record continuous raw data? At what cadence and duration? | Stage 9 entirely |
| 2 | Must every exposed frame be retained, or are explicit losses acceptable? What should happen when the disk fills or vanishes? | Stage 5 fallback policy; Stage 9 |
| 3 | How many simultaneous viewers/operators, and is the rig on an isolated network? | Stage 8 — both the concurrency cap and whether authentication is needed |
| 4 | Acceptable command latency, preview age, stop/recovery time? | Stage 1 freshness deadline; Stage 8 acceptance thresholds |
| 5 | Supported OS / picamera2 / libcamera / filesystem baseline, and who owns rollback? | Stage 8 manifest |
| 6 | Should runtime pipeline add/remove persist? | Stage 7 |

**Stages 0–4 need none of these** and can start immediately. Question 2 is the
one that most changes the design, and it is worth answering before Stage 5.

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
