# Stage 3 contract — one acquisition owner and a bounded lifecycle

**For supervisory review before implementation.** The amended
`docs/implementation-plan.md` requires that "finite queue capacity and numeric
provisional latency/freshness/shutdown thresholds" be documented and approved
first, on the grounds that "supervision approves the contract and evidence, not
an unspecified future design". This is that document. No Stage 3 code has been
written.

Closes review finding **F1**, gate **G2**, and absorbs the minimum lifecycle
work moved here from Stage 7 and the minimum command admission moved here from
Stage 8.

**Entry status.** R1–R6 are implemented and verified on the desktop suite
(see `docs/cleanup-log.md` 2026-09-06 (q)). Rig acceptance of R1–R6 and of
Stages 1–2 has not yet been recorded, and the plan makes that a precondition
for this stage rather than for its approval. The Stage 0 residuals — the legacy
tree, the service file, the CI workflow that is still a draft outside
`.github/workflows/` — remain outstanding and are tracked, not silently folded
in here.

---

## 1. What the problem actually is

`CameraRuntime.capture_still` calls `source.capture_full()` on whichever thread
the web framework happened to dispatch the request on. `Picamera2Source`
`capture_request()`s from there. The capture loop is doing the same thing on
its own thread. Two mutexes serialise the calls, which is why the rig usually
works.

Serialisation is not ownership, and the difference is the whole stage. A mutex
makes two callers take turns; it does not make one of them responsible. Nothing
in the current design answers:

- who releases a request when the thread that took it raises;
- which thread is allowed to call `stop()` while another is inside
  `capture_request()`;
- what a caller's timeout means when the SDK call it is waiting on cannot be
  interrupted;
- whether a control submitted a millisecond ago applies to the request now in
  hand.

An earlier design had a second thread pulling full frames for corner detection.
Two threads on a four-deep request pool, one at 30 Hz and one at 1 Hz, took the
Pi down repeatedly — while a four-core CPU stress test did not, which is how
the camera path rather than the load was identified. The handshake in
`cameras/base.py` was the fix, and it is a fix for one caller. Every other
caller still reaches in.

**A queue is not the deliverable.** Moving `capture_still` onto a queue
serialises it slightly differently and satisfies none of the questions above.
What is being built is an owner with a lifecycle.

---

## 2. The owner

One `AcquisitionOwner` per camera head. It owns the `Picamera2` object from
construction to close, and it is the only object in the application permitted
to touch it.

```
                 submit(command) ──► bounded queue ──► owner thread
                        │                                   │
                        │                              capture_request()
                        ▼                                   │
                   Future/result ◄──── envelope ◄───── copy, admit, release
                                                            │
                        publish ─────────────────────────────┘
                        (bus, JPEG, pipeline, disk — all off-thread)
```

**Owned operations**, none of which may be called from anywhere else:
`Picamera2(...)`, `configure`, `camera_configuration`, `set_controls`,
`camera_controls`, `start`, `capture_request`, `request.make_array`,
`request.get_metadata`, `request.release`, `stop`, `close`.

**Not owned:** JPEG encoding, pipeline stages, disk writes, HTTP. Those run on
their callers' threads from data the owner has already copied. Nothing the
owner does may block on any of them.

SDK-internal threads are libcamera's business and are not additional
application owners. The claim being made is about *this* code.

### 2.1 Callers to migrate

Every one of these currently reaches the camera directly or through the
handshake, and every one moves to `submit()`:

| Caller | Today | Command |
|---|---|---|
| `CameraRuntime._run` preview loop | `read_preview()` / `skip_preview()` | the owner's own loop; not a submitted command |
| `capture_still` (API, `/api/capture/{id}/raw`) | `source.capture_full()` on the request thread | `StillCommand(raw=True)` |
| `capture_preview` | reads the bus | unchanged — it does not touch the camera |
| `capture_all` | sequential `capture_still` per head | one `StillCommand` per head, submitted before any completes |
| calibration poses | `request_full_frame()` / `wait_full_frame()` handshake | `StillCommand(stream="main")` |
| `set_controls` (API) | `source.set_controls()` on the request thread | `ControlCommand` |
| `control_spec` / `get_controls` | reads `picam.camera_controls` | `QueryCommand`, or a snapshot cached by the owner at open |
| shutdown | `source.close()` from `CameraRuntime.stop` | `stop()` on the owner |

The `_full_pending` / `_full_ready` handshake in `cameras/base.py` is **removed**
once all of these are migrated, not left as a second path. Its docstring, which
records why a second consumer is not allowed to exist, moves to the owner.

Existing locks are removed only where ownership makes the particular thing they
protected impossible. `CameraSource._full_lock` goes with the handshake;
`Picamera2Source._lock` goes because there is one thread; `_capture_lock` in
`CameraRuntime` goes because the queue orders stills. Any lock whose purpose is
not covered by ownership stays and gets a comment saying what it still guards.

---

## 3. Commands

```python
@dataclass(frozen=True)
class Command:
    id: str                 # unique per process
    kind: str               # "still" | "control" | "query"
    generation: int         # camera run generation; see §7
    submitted_mono: float   # time.monotonic() at submit()
    deadline_mono: float    # absolute, not a duration
```

Exactly one terminal outcome per command, resolved exactly once:

| Outcome | Meaning |
|---|---|
| `completed` | executed; result attached |
| `failed` | executed; raised. The exception is attached |
| `rejected` | never queued — full, or the owner is not running |
| `expired` | queued, deadline passed before execution started. **Never executes** |
| `superseded` | a later control of the same kind replaced it before execution |
| `cancelled` | withdrawn by the caller before execution started |
| `abandoned` | execution started, the caller's wait expired. **The command may still have had an effect** — see §5 |

`abandoned` exists because the alternative is a lie. A caller timing out does
not interrupt a blocking SDK call, and reporting `failed` would assert that
nothing happened.

### 3.1 Admission

| Parameter | Provisional value | Why |
|---|---|---|
| `QUEUE_CAPACITY` | 8 per head | Two operators, a burst of stills and a control sweep. A queue deeper than this is latency nobody wants; the rejection is the useful signal. |
| `STILL_CAPACITY` | 4 of those 8 | A control change must not be starved by a burst of stills. |
| `WORK_PER_LOOP` | 4 commands, or until the next frame deadline | Bounds the delay a command backlog can add to the preview cadence. |
| `MAX_COMMAND_BYTES` | n/a | Commands carry no payload; results do, and results are copies the owner already makes. |

Order is FIFO within a kind. Between kinds, controls go first: a control is
microseconds of SDK call and a still is a frame period plus a copy, so putting
stills first would make the control latency the still latency. That is a
deliberate fairness choice and the status endpoint exposes enough to see if it
is wrong.

Rejection is explicit, immediate and carries the reason and the current depth.
The API returns **429** with the depth and the oldest queued age, not a 500 and
not a silent wait.

**Control coalescing** is permitted only when a later `ControlCommand` sets a
superset of the same control names, and only while the earlier one is still
queued. The earlier one resolves `superseded` with the id of the one that
replaced it. A coalesced control is never reported as `completed`.

---

## 4. Deadlines and cancellation

Three states before terminal: `queued`, `executing`, `resolved`. The
transitions are the contract.

1. **A queued command whose deadline has passed never executes.** Checked when
   it reaches the head of the queue, not only when it was submitted.
2. **A caller's wait expiring does not interrupt execution.** There is no
   mechanism to interrupt `capture_request()`, and pretending otherwise is how
   a "cancelled" capture ends up in a session directory.
3. **Cancelling a still suppresses its delivery and its persistence.** The
   frame is dropped after release; nothing is written. This is the one place
   cancellation has teeth, and it is a suppression, not an interruption.
4. **Cancelling a control cannot un-apply it.** If the SDK call has been made,
   the outcome is `abandoned` and the effect is recorded, because the sensor's
   subsequent frames will show it whatever the command record says.
5. **Completion and timeout race exactly once.** A single compare-and-set on
   the outcome. Whichever arrives first wins and the other is a no-op.
6. **The API wait is bounded even if the SDK is not.** A still request that has
   not resolved within its deadline returns 504 with the command id, and the
   owner transitions to `degraded`. The command id is queryable afterwards,
   which is what makes an abandoned capture findable rather than lost.

### 4.1 Provisional numeric thresholds

Marked provisional because the research requirements behind plan questions 1–4
are unresolved. They are engineering values chosen to be testable now, and they
are what the rig acceptance run measures against. They are not a claim about
what the science needs.

| Threshold | Provisional | Basis |
|---|---|---|
| Control command, submit → SDK call | p95 ≤ 100 ms, max ≤ 400 ms | One 30 Hz frame period is 33 ms; three of them is the point a slider stops feeling connected. |
| Still command, submit → envelope | p95 ≤ 250 ms, max ≤ 1000 ms | One frame period plus a full-resolution copy, with margin for a preview pass in between. |
| Still API deadline | 3000 ms | Well past the max above; reaching it means something is wrong, not slow. |
| Control API deadline | 1000 ms | |
| Preview frame freshness | stale at 2.0 s | Already implemented and shipped in Stage 1 (`STALE_PREVIEW_S`); restated so this stage does not contradict it. |
| Status freshness | stale at 5.0 s | Already implemented in R6. |
| Owner stop, normal | ≤ 1.5 s | |
| Owner stop, hard deadline | 5.0 s, then `failed-stop` | systemd's budget is 15 s and two heads plus the server must fit inside it. |
| Queue oldest-age alarm | 1.0 s | Surfaced in status; not an error by itself. |

Every one of these is recorded in the run log before the acceptance run, per
the amended plan, and any that proves wrong is changed with a reason rather
than quietly widened.

---

## 5. Controls apply to future requests

The only honest statement about a control is when it was submitted and when the
SDK acknowledged it. What a given exposure actually used comes from **that
request's own metadata** and from nowhere else.

- `set_controls` is dispatched at the SDK boundary and its acknowledgement is
  recorded with its own identity and monotonic time.
- A control submitted while a request is already in hand **cannot** have
  affected it. The envelope records the last acknowledgement that preceded the
  request being obtained, and that is a bound, not a claim of effect.
- Effective values come from `ExposureTime` / `AnalogueGain` in the request
  metadata. Absent metadata is recorded as `unknown` — never backfilled from
  what was requested. This is the same rule the raw admission boundary applies
  to pixel values, applied to control values.

**Still-to-control binding policy.** A `StillCommand` takes the next request
the owner obtains after the command begins executing. It does *not* wait for a
particular control revision to be observable in the metadata. That is the
simpler policy and it is the one being specified, because the alternative —
"wait until the sensor confirms exposure = X" — needs a convergence criterion,
a timeout, and a decision about what to do when the sensor never reports the
value, and none of that is justified before the exposure-sweep workflow exists.

A caller that needs a specific exposure therefore does: submit control, await
its acknowledgement, submit still, and read the effective values out of the
result. The test asserts that a still submitted immediately after a control
does *not* claim the new value unless the metadata says so.

---

## 6. The acquisition envelope

One request yields one envelope. Everything in it is copied while the request
is held, and the request is released before anything downstream runs.

```python
@dataclass(frozen=True)
class Envelope:
    # identity
    request_token: object       # the SDK's own handle identity, for lineage
    app_request_id: str         # ours, unique per process
    head: str                   # cam_id
    generation: int             # see §7

    # timing, three clocks kept apart
    sensor_timestamp_ns: int | None   # SDK, kernel monotonic. None = unknown
    sensor_sequence: int | None       # SDK frame counter, if supplied
    received_mono: float              # host monotonic, at copy
    received_wall: float              # host wall clock, filenames only

    # pixels, already copied
    raw: Admitted | None
    main: np.ndarray | None
    lores: np.ndarray | None

    # provenance
    sensor_meta: dict           # a copy, not the SDK's dict
    orientation: dict           # as APPLIED, not as configured now
    raw_negotiated: dict        # format, size, stride — from §R5
    validity: str
    source_kind: str
    control_ack: dict | None    # the last acknowledgement preceding this request
```

Rules:

- **Nothing SDK-backed escapes.** `make_array` returns a view into a buffer the
  SDK will reuse. Every array in the envelope is `np.ascontiguousarray`'d, and
  `get_metadata()`'s dict is copied, before release. The fake-SDK test
  invalidates buffers on release specifically to catch this.
- **Raw and preview siblings share `request_token` and `app_request_id`.** That
  is what makes "same exposure" a claim with evidence behind it. Equal
  locally-assigned sequence numbers are not evidence and the review is right to
  say so; `Frame.seq` stays what its docstring now says it is — a count of
  software deliveries.
- **`sensor_timestamp_ns` is preserved with its clock domain named, or recorded
  as unknown.** No conversion to wall time, no substitution of `received_mono`.
- **Orientation is recorded as applied.** Not read back from `self.cfg` at save
  time, which is how a live edit can rewrite the provenance of a frame captured
  before it.
- **No inter-head synchronisation is claimed.** Two envelopes from two heads
  are two exposures. The sensors free-run; software cannot fix that and this
  stage does not pretend to.

Stage 4 adds the processing execution record. Stage 3 supplies the acquisition
half and freezes it.

---

## 7. Lifecycle

States, per head: `created → opening → running → degraded → stopping →
stopped | failed`. `degraded` is running-but-something-is-wrong (acquisition
timeouts, admission failures, queue at capacity) and is reachable from and back
to `running`.

**Generation** increments on every successful open. Every command and every
envelope carries the generation it was created under, and a result arriving
under a stale generation is discarded with a counter, not published. This is
what stops a late completion from a previous run appearing as current data
after a restart.

Required behaviours, all of which are currently absent:

1. **Partial-open cleanup on every failure path.** `Picamera2(index)` succeeds
   and `configure` raises: the device is held and the next open reports no
   cameras at all, which reads as absent hardware and sends the operator to the
   ribbon cables. Already fixed for the two raw-format refusal paths; this
   generalises it to every step between construction and `start()`.
2. **Only the owner closes its device.** No path from `CameraRuntime.stop`,
   from a signal handler, or from an error handler may call `close()`.
3. **Stop is a sequence, not a flag.** Reject new work → resolve queued
   commands `rejected` → allow the executing command to finish or time out →
   release → stop → close.
4. **A join timeout is `failed-stop`, not `stopped`.** The current code joins
   with a timeout and then closes the source regardless, which is a close
   racing a thread that may be inside `capture_request()`. On failed-stop:
   do **not** close, do **not** start a replacement owner, mark the head
   `failed`, and say so in status. A held camera the operator can see beats a
   segfault they cannot.
5. **A second owner over a surviving one is impossible.** Construction of an
   owner for a head that already has a non-`stopped` one raises.

If bounded in-process recovery from a hung `capture_request()` cannot be
demonstrated, the supported recovery boundary is documented as "restart the
service" and process isolation is reassessed before G2 is accepted. That is an
outcome of this stage's evidence, not a decision taken in advance.

---

## 8. Accounting

Four counters per head, and they are four different things:

| Counter | Counts |
|---|---|
| `sdk_deliveries` | requests actually obtained from the SDK |
| `preview_suppressed` | requests released without decoding, by the rate cap. Intentional |
| `frames_published` | frames that reached the bus |
| `admission_failures` | raw buffers refused |

Plus, on the queue: `depth`, `oldest_age_s`, `rejected`, `expired`,
`superseded`, `abandoned`, and the owner's `state`.

What must **not** be reported: a gap in `Frame.seq` as a dropped exposure, and
`cfg.fps` as an observed rate. Both were corrected in Stage 1 and the unified
path must not reintroduce them.

Where the driver does not supply `SensorTimestamp` or a frame counter, status
says the drop accounting is **unavailable on this driver** rather than
computing something from software timing and labelling it drops. The rig
acceptance run records which of the two this Pi and this libcamera give.

---

## 9. How it will be tested

### 9.1 Deterministic fake SDK

A `FakePicamera2` with: recorded thread ids for every call, a unique token per
request, buffers filled with a per-request pattern and **overwritten on
release**, injectable delays and injectable exceptions at each of open,
configure, start, `capture_request`, `make_array`, `get_metadata`, `release`,
`stop`, `close`.

Assertions:

- **Ownership.** Every recorded SDK call comes from the owner thread. Asserted
  by thread id across a workload of concurrent stills, controls, preview and
  `capture_all`.
- **Release exactly once**, including on copy, admission and processing
  failure. Asserted by a release counter per token.
- **Copies survive reuse.** Envelope arrays are compared against their pattern
  after the fake has overwritten the buffer.
- **Bounded queue.** Overload rejects explicitly at capacity; ordering and
  coalescing follow §3; no expired command executes; no command resolves twice
  under a completion/cancellation race driven by a barrier.
- **Control timing.** A control submitted after a request is obtained does not
  appear as that request's effective value. A still submitted immediately after
  a control does not claim the new value absent metadata.
- **Sibling lineage.** Raw and preview from one request share `request_token`,
  and a test that makes their `Frame.seq` equal *without* sharing a token must
  fail — the point being that seq equality is not the evidence.
- **Faults.** Exception at each stage; an indefinitely blocked
  `capture_request`; a late completion after stop. Each produces an observable
  outcome, no concurrent close, no leaked partial-open handle, no false
  `stopped`, no second owner. Stop-then-restart is exercised.
- **Backpressure.** A slow pipeline and a slow writer cannot hold a request or
  grow the queue without bound.
- **Regression.** Every Stage 1 and Stage 2 invariant still passes through the
  unified path — the same tests, not equivalents.

### 9.2 Rig acceptance

Recorded **before** the run: deployed commit, Pi model, OS, kernel, picamera2
and libcamera versions, both negotiated stream layouts (format, size, stride),
queue capacities and every threshold in §4.1.

The run: both previews live, a still every two seconds, sustained control
changes, 30 minutes. Then stop during a capture, and an injected acquisition
delay.

Recorded during: command latency distribution and maxima per kind, maximum
queue depth and oldest age, admission failures, rejections, timeouts,
abandonments, RSS trend, and `dmesg`/CSI errors.

Pass requires: no crash, no unexplained stall, no ownership violation, no
unbounded growth, preview suppression matching the declared cap, and
raw/preview lineage demonstrated **by shared request identity and sensor
metadata** — not by equal sequence numbers. This run establishes nothing about
inter-head synchronisation and the report will say so.

---

## 10. Sequencing and rollback

Independently reviewable commits, in this order, each leaving the suite green:

1. `AcquisitionOwner` with the fake SDK and its tests. Nothing wired.
2. Preview loop moves into the owner. One caller migrated, handshake still present.
3. Stills and `capture_all` migrate. `capture_full` becomes owner-only.
4. Controls and queries migrate.
5. Calibration poses migrate; the handshake is deleted.
6. Lifecycle: stop sequence, generations, failed-stop, partial-open cleanup.
7. Accounting and status.

Rollback is per commit and is verified on the rig, not assumed. Steps 2–5 each
leave a working rig; step 5 is the point of no return for the handshake and is
the one to review most closely.

---

## 11. What this stage does not do

- No inter-head synchronisation, and no claim of it. That needs the XVS pins
  wired and an external trigger.
- No processing provenance — Stage 4.
- No transactional storage — Stage 6.
- No durable state or 100-cycle soak — Stage 7.
- No multi-client resource policy, operator lease or authentication — Stage 8.
- No process isolation, unless §7 produces the evidence that it is needed.

---

## 12. Questions this contract does not answer

Answers change the thresholds in §4.1 and the queue shape in §3.1. Provisional
values let implementation start; they are not a substitute.

1. Is a still ever required to follow a *specific* acknowledged control
   revision, or is submit-control-then-submit-still (§5) sufficient for the
   exposure-sweep workflow?
2. Is a rejected still acceptable under burst load, or must every requested
   capture eventually happen? The first gives a bounded queue; the second
   needs a durable request log, which is Stage 6 work.
3. What command latency is actually tolerable at the bench? §4.1 is an
   engineering guess and should be replaced by an observation.
