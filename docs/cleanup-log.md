# Cleanup log

Removals and behavioural changes, and how to undo them. Newest first.

Everything here is recoverable from git, because nothing is removed that was
not already committed. The general rollback is:

```powershell
git checkout -- <path>      # one file or directory back to HEAD
git checkout .              # everything back to HEAD
```

---

## 2026-09-06 (s) — stage 3: one thread owns the camera

Review finding **F1**, gate **G2**. `src/trilobite/acquisition.py` is new.

### What was actually wrong

`capture_still` called `source.capture_full()` on whichever thread the web
framework dispatched the request on, while the capture loop called
`read_preview()` on its own. Two mutexes made them take turns.

**Serialisation is not ownership.** A mutex does not say who releases a request
when the holder raises, which thread may `close()` while another is inside
`capture_request()`, or what a caller's timeout means for an SDK call that
cannot be interrupted. The rig usually worked, and an earlier version of the
same shape — a second thread pulling full frames at 1 Hz against a preview loop
at 30 — took the Pi down repeatedly.

### The change

`CameraOwner` is a bounded queue serviced by the capture loop that already
exists. It is not a new thread: adding one would only move the question of
which of two loops is authoritative. Callers submit and block on a deadline;
the capture thread is the only thread that reaches the source.

| moved | from | to |
| --- | --- | --- |
| `capture_full` | web worker thread, under a mutex | `still` command |
| `set_controls` | web worker thread | `control` command |
| `control_spec` | live SDK read per page load | snapshot taken at open |
| `open` | unchanged — the capture thread does not exist yet, so the invariant holds by construction | |
| `close` | after a join that might have timed out | only after a join that succeeded |

**Capture and save are now separate calls.** `grab_still()` runs on the owner;
`save_frame()` runs on the caller. A write to a slow USB stick must never be
holding an SDK request, and must never be what the next capture queues behind.
`capture_all` uses the halves separately, so both heads are asked before either
is written — the old loop completed each head's disk write before requesting
the next frame, which put storage latency inside the pair skew.

**A join that times out is `failed-stop`, not `stopped`.** The old code joined
with a timeout and closed the source regardless, which is a `close()` racing a
thread that may be inside `capture_request()` — a segfault rather than an error
message. The camera now stays held, `lifecycle` says `failed-stop`, and status
says why. A held device an operator can see beats a crash they cannot diagnose,
and it is the honest report: the thread really is still running.

**A submit from the owner thread runs inline** rather than queueing behind a
loop that is currently inside the call. Not a convenience — it is the deadlock
that would appear the first time a stage or a command submitted work.

New API status codes: **429** when the queue is full or the camera is stopped
(nothing was attempted; retrying is reasonable) and **504** when a deadline
passes, with a message saying the capture may still have happened, because
nothing here can interrupt an SDK call.

### Scope, deliberately

`docs/stage-3-contract.md` specifies seven command outcomes, control
coalescing, per-kind fairness and generation isolation across restarts. This
implements **five outcomes and none of the rest**. Each omitted piece guards a
failure this rig has not exhibited, and each is another interacting state to
get wrong — which is the same judgement the Stage 2 corrections needed applied
in the other direction. They stay specified in the contract and unbuilt.

The one bound that is currently slack is `WORK_PER_LOOP`: at capacity 8 and a
budget of 4 it costs at most one extra frame period, so no end-to-end test can
distinguish it from an unbounded drain. Said so in the test rather than
pretending otherwise. It stops being slack the moment the capacity rises.

### Evidence

`tests/test_acquisition.py`, 17 tests. The load is deliberately concurrent and
the recording happens INSIDE the source, below anything a test could route
around:

- **one thread, proved by id** — eight threads issuing stills and controls
  against a running preview loop; the recorded thread set must have one member
  and the peak concurrency inside the source must be 1. A version that merely
  serialised would show nine threads and still pass a "captures work" test.
- **the right thread** — the set must equal the capture thread's id, so a
  dedicated command thread cannot satisfy it.
- **200 captures, four threads**: every one on disk, sidecar parsing, byte
  count matching, 200 distinct sequence numbers, no two writing one path,
  queue empty at the end, zero errors.
- preview keeps publishing through a 40-capture burst; a slow save does not
  hold the camera; `capture-all` asks both heads before writing either.
- expired queued work never executes; an abandoned command resolves once; a
  full queue refuses; `retire` refuses work already queued rather than
  stranding a caller on a 30-second wait; failed-stop leaves the device open.

**10 mutants, all caught.** Two survived first: `retire` not refusing queued
work, and the default `service()` budget — both were gaps in the tests, not in
the code, and both are now covered.

Suite: **457 passed, 13 skipped**, 13 browser scenarios. Ruff clean.

### Bench test

1. Both previews running. Take stills from two browser tabs at once for a few
   minutes. `commands.max_depth` and `commands.max_age_s` in `/api/status` say
   whether the queue was ever near its bound; `rejected`, `expired` and
   `abandoned` should all be zero.
2. Capture-all repeatedly with the output on a slow USB stick. Files land; the
   preview does not stall.
3. Stop the service during a capture burst. It must stop cleanly — check for
   `failed-stop` in the log, which would mean a thread would not join.
4. `journalctl -u trilobite | grep -i "does not own this camera"` after a
   session. Any hit is a caller that escaped the owner and is a bug.

---

## 2026-09-06 (r) — the boundary grades instead of refusing

**Reported from the bench: every raw frame reads as white noise with row
artefacts in MATLAB, and looking at a diagnostic capture errors out.** Both are
my regression from (q), and both come from one bad decision.

### The bug

When admission refused a buffer, `_admit_raw` returned it **untouched** — not
re-viewed as uint16, not trimmed. So a 10-bit frame whose stride or values did
not reconcile reached disk as a 2944-wide **uint8** array. Displayed, that is
pairs of bytes shown as pixels: white noise with row structure. Exactly the
report.

I wrote the justification into the code — *"there is no correct interpretation
to apply, so applying none is the honest answer"* — and it was wrong. It
conflates **we cannot vouch for these values** with **we will not interpret
these bytes**. The first is worth saying. The second just makes the rig
undiagnosable at the moment somebody is trying to diagnose it.

### The change

The split now follows what a person can SEE.

| | outcome | why |
| --- | --- | --- |
| compressed, packed or unknown FORMAT | **refused** — `RawFormatError`, needs `allow_unvalidated_raw`, tagged `diagnostic` | no reading to produce, and the failure is invisible: a PiSP buffer looks like a slightly damaged photograph. This is the one with a body count |
| row count, stride, values, dtype | **graded** — `unvalidated`, reasons in `raw_reservations`, frame returned re-viewed and trimmed | all visible on screen. Wrong alignment is 64× too bright; wrong stride skews the aspect ratio. Refusing the frame buys no protection and costs the diagnosis |

`admit()` returns `Admitted(array, meta, validity, reservations)`. Every check
still runs and every failure is still recorded — the verdict just stops being
binary. The pixels are always the best reading available.

Both readers loosen to match. `require_science` (VALUES) stays strict, because
reading a value as a photon count with the alignment unresolved is silently
wrong by 64× and nothing downstream catches it. `require_geometry`
(POSITIONS) now refuses only `diagnostic` and warns on everything else, naming
the reservations. Archive files with no recorded validity are readable again;
`--allow-legacy` is gone because there is nothing left for it to unlock.

### On the wider question

The bench question was "are we not over-complicating this — a raw frame just
needs to be a frame". Partly yes, and the part that was over-complicated is
exactly the part removed here: I applied the review's fail-closed principle
uniformly instead of asking which failures are silent. One is. The rest
announce themselves on screen, and for those a recorded reservation does the
same job as a refusal without stopping the work.

What is kept, and why it is worth the machinery: refusing a compressed or
packed format at open, and recording what the format, stride, geometry and
alignment actually were. That is one hard stop and a handful of fields.

### Testing

`tests/test_rawformat.py` 63, with the former refusal cases rewritten as
grading cases — the checks are the same, the verdicts changed. Two new
properties: only the format stops a capture, and a graded frame is still a
frame (re-viewed at the right pixel size, trimmed to the sensor width, values
intact). `tests/test_reader_gate.py` covers the loosened geometry gate and that
reservations reach the reader and the report.

Suite: **440 passed, 13 skipped**. Ruff clean.

### For the bench

Old `diagnostic_*` files were written by the broken path and their .npy really
is a byte buffer — re-capture rather than trying to rescue them. New captures
that do not reconcile land as `unvalidated_raw_…`, open normally, and
`python scripts/read_capture.py <file>` prints every reservation, which is the
fastest way to see what the rig is actually negotiating.

---

## 2026-09-06 (q) — supervisory review R1–R6: the boundary was bypassable

`docs/stage-2-supervisory-review.md` reviewed the Stage 2 work and did not
accept it against gate G1. Six findings, all of the same shape: **the admission
boundary was correct in isolation and the claim it produced did not survive
contact with the rest of the software.** Probes against the running code, not
inspection — which is why they found things the mutation pass did not.

This entry closes R1–R6. Stage 3 is gated on it.

### R2 — validity was being laundered

The worst of the six, because it made the other five academic. `Frame.validity`
defaulted to `science`, so every construction that never considered the
question asserted the strongest claim in the system by omission. And
`ReplaySource` read pixels and ignored the sidecar beside them. Put together:
a capture the rig had **refused**, written to disk as `diagnostic`, replayed
through the replay backend, came back out labelled `science`. The stack
laundering a refusal into an admission by round-tripping through a file.

**Three validities now, and the third is the default.**

| | means |
| --- | --- |
| `science` | established: these are sensor counts |
| `diagnostic` | established NOT to be — refused, or processed for viewing |
| `unvalidated` | **the default.** Nothing was established either way |

`unvalidated` is not a weaker `diagnostic` and collapsing them would lose the
distinction that matters: `diagnostic` is a positive statement that the values
are wrong, `unvalidated` is the absence of a statement. An ISP frame and a
compressed buffer are different situations and the record now says which.

A new `source_kind` field (`raw` / `isp_main` / `isp_lores` / `synthetic` /
`replay` / `unknown`) sits beside it, because **measurement eligibility is not
one predicate**. Corner geometry off an ISP mono frame is defensible;
radiometry off the same frame is not. Without the source recorded a reader
cannot tell those apart, and every calibration pose is exactly that case.

Closed: the `Frame` default, `capture_full(raw=False)`, the served full frame,
the preview, both synthetic paths, and replay — which now reads the sidecar and
carries what it finds, promoting nothing.

### R1 — both readers failed open

`scripts/read_capture.py` asked one question: is the validity string exactly
`diagnostic`? Everything else passed. The review's probes returned true for
`unrecorded` and for `typo`. So a missing field, a misspelling and a value from
a newer schema all read as measurable — failing open on the one field whose
job is to fail closed. `tv_require_science.m` had the same policy.

Both now require **an explicitly recognised claim plus the evidence it rests
on**. A sidecar saying `science` with no `raw_admitted` record is refused: a
label anyone can edit is not evidence, and treating it as such makes the whole
boundary bypassable with a text editor.

And both now distinguish the two questions:

- `require_science` — reading VALUES as sensor counts. Needs admitted `science`.
- `require_geometry` — reading POSITIONS. Accepts admitted `science` and
  `unvalidated` output from an ISP source.

That distinction is what keeps `--detect` working on calibration poses without
a flag, which it must: it is the one documented use of the script. Refusing
them would have trained the operator to pass `--allow-everything` and thrown
the guard away. `--allow-diagnostic` and `--allow-legacy` are separate flags
because they permit different things, and the archive is not silently promoted
— a pre-boundary file reads as `unknown` and needs saying out loud.

### R3 — the representation was never checked

`admit()` checked `dtype.itemsize`, which is not a check on the dtype. `int16`,
`float16` and `uint16` are all two bytes wide, and the review's probes admitted
`int16(-1)` and `float16(0.5)` as 10-bit sensor counts. A negative count and a
fractional count are both impossible, so the invariant the module advertised
was simply false. Now: unsigned integers only, native byte order only, positive
geometry only.

**And the harder half.** The review cites the Picamera2 manual (raw stream
configuration, pp. 21–22): Pi 5 uncompressed samples are **left-shifted within
their 16-bit word**, and the manual explicitly warns against deriving the
sensor bit depth from the format name. `R10` names a ten-bit sample and says
nothing about whether those ten bits are 0–9 or 6–15. **Those two readings
differ by a factor of 64 in every pixel.**

So three quantities are now kept apart where one `bits` field used to stand:

| field | is |
| --- | --- |
| `raw_bits_nominal` (+ `raw_bits_source`) | what the format NAME implies. Evidence of nothing on its own, and now named so |
| `raw_container_bits` | the width of the word the sample arrives in |
| `raw_alignment` / `raw_sample_shift` | where the one sits inside the other |

Alignment is **declared in the config, not guessed**, because a dark
left-aligned frame and a bright right-aligned one have indistinguishable
histograms and a guess would enter the record with the same confidence as a
measurement. Getting it wrong one way is loud: `lsb` declared against
left-aligned data puts values far past the ten-bit ceiling and admission
refuses the buffer, naming the setting in the refusal. The other way is quiet
— the frame just reads dark — so `raw_observed_max` is in every sidecar for
that check, and `config/pi.yaml` says to confirm it once against a bright
target.

**The pixels on disk are never shifted.** `raw_sample_shift` is recorded and
the readers apply it. Silently rescaling every value on the way to storage is
precisely the act this boundary exists to prevent, and doing it "helpfully"
would be worse than not having the boundary.

### R5 — the request was being validated, not the negotiation

`_choose_raw_format` decided what to ask for and the code then treated that
decision as established. But the driver answers for itself: it may substitute a
format, and — separately — the raw stream's geometry is not the main stream's.
`full_resolution` sizes `main`; raw is configured at the sensor's native size.
Admission was checking raw buffers against the main resolution, which happens
to work only while the two are equal.

`_read_back_raw` now reads `camera_configuration()["raw"]` after `configure`
and takes **format, size and stride** from it. That is the only thing admission
checks against. A substituted compressed format is caught here and nowhere
else, because from that point on every buffer is self-consistent with the
substitution and looks perfectly correct. A substitution to a different but
still admissible format (R10 requested, R8 delivered) is allowed and named
loudly — two bits per pixel have gone and nothing downstream would otherwise
say so.

With the driver's stride in hand the row length must match it **exactly**;
the bounded-pad rule survives only as the fallback when the driver does not
report one, and `raw_stride_source` records which was used. Two different
strengths of evidence, and a sidecar must not present them as the same thing.

### R4 — the opt-in was advertised and not consulted

`_admit_raw` caught every admission failure and returned diagnostic data
regardless of `allow_unvalidated_raw`. So a **science capture request could be
answered — and reported as a success — with pixels the code had just
established were not sensor counts.** A 65535-valued R10 probe confirmed it.

The runtime failure now fails the request. `/api/capture/{id}/raw` returns
**422 and no file**, naming the setting that would permit a diagnostic capture
instead. There is no unconditional fallback left.

### R6 — an unbounded await is not a request, it is a hostage

The status poll awaited `fetch` with no timeout and scheduled the next poll
only after that await returned. A response that never completes — a half-open
TCP connection surviving a Pi that has gone away, which is the ordinary way a
bench rig disappears — suspended the poll loop indefinitely. And the poll loop
was what set the stale indicator. **The page went quiet in exactly the case it
exists to shout in**, while MJPEG carried on displaying the last frame it
received.

Two independent fixes, because either alone is a single point of failure: a
4-second `AbortController` bound on the status fetch, and a 1-second watchdog
timer that renders the banner from **elapsed time since the last good status**
regardless of whether any poll has completed. The banner counts up, so it reads
as a live measurement of how long the rig has been away rather than as a stuck
warning.

### On disk

`validity` and `source_kind` are in every sidecar, including poses. Filenames
carry the validity as a prefix ahead of the tag — `diagnostic_raw_…`,
`unvalidated_still_…`, and nothing for `science` — so `ls`, a glob and a drag
into MATLAB all separate measurement from everything else. Three validities,
three names: calling an ISP frame `diagnostic_` would be as inaccurate in its
own direction as calling it `science`.

### Testing

New: `tests/test_validity.py` (15) proves nothing anywhere can produce a
science frame without establishing one — defaults, replay, ISP, synthetic,
pipeline. `tests/test_reader_gate.py` (14) loads `read_capture.py` by path and
asserts every case that used to pass. `tests/test_rawformat.py` grew to 57 with
the representation and alignment cases. Three browser scenarios cover R6,
including a route that swallows `/api/status` entirely — never fulfilled, never
aborted, because an error response is the easy case.

**Mutation testing: 24 mutants across R1–R5, all caught.** Including the ones
that matter most — `Frame` defaulting to `science` again, the reader trusting a
label without its evidence, admission using the main resolution, and the hatch
being ignored at runtime.

Suite: **433 passed, 13 skipped**, plus 13 browser scenarios. Ruff clean.

### Not closed here

Rig acceptance of Stages 1, 2 and these corrections. The Stage 0 residuals:
`src/flyeye` and its service file still need removing (PowerShell, since this
session cannot delete on Windows), and `Claude outputs/tests.yml` is still a
draft outside `.github/workflows/` so there is no active CI. Recorded as
outstanding rather than folded into this entry's claims.

### Bench test for these corrections

1. Normal capture: `"validity": "science"`, `"source_kind": "raw"`,
   `raw_admitted: true`, `raw_stride_source: "negotiated"`, and
   `raw_observed_max` present. Check that last one against 1023 — if it is
   much larger, the pipeline is left-aligning and `raw_alignment` must be `msb`.
2. A capture with `raw_format: MONO_PISP_COMP1` and the hatch shut: the camera
   refuses to open. With the hatch open: it opens, and the capture lands as
   `diagnostic_still_…` with the refusal quoted.
3. `python scripts/read_capture.py <any pose> --detect --board 4x3` still works
   with no flag. The same command on a `diagnostic_` file refuses.
4. Take an old pre-boundary capture off the archive and run `--detect` on it:
   it must refuse and name `--allow-legacy`.
5. Unplug the Pi's network mid-session with the dashboard open. Within five
   seconds the header must show a stale-status banner whose age counts up.
   Plug back in; it must clear.

---

## 2026-09-06 (p) — review stage 2: a raw buffer must earn the word "science"

Stage 2 of `docs/implementation-plan.md`, closing review finding **F7** and
gate **G1**. This is the first stage that changes what the rig acquires and
what it puts on disk.

### The finding, restated

The review's generalisation across the whole failure log:

> The common pattern is **accepting plausible structure as proof of correct
> meaning**: shaped arrays accepted as sensor counts, named files accepted as
> complete data, writable paths accepted as the selected disk.

A raw buffer is the first of those. It arrives with the right shape and, at a
glance, obvious structure, and that says nothing about whether its values are
sensor counts.

### What changed

**A new module with no camera dependency: `src/trilobite/cameras/rawformat.py`.**
That independence is the point — every rejection path is reachable from a byte
array in a test, with no Pi and no libcamera, so the code that decides what
counts as measurement data is as testable as the code that fits models to it.

| Piece | What it decides |
| --- | --- |
| `KNOWN_FORMATS` / `classify()` | an **allowlist**. An unrecognised name is refused, not assumed ordinary. Compressed names are matched by the stable PISP/COMP pattern and returned as a `RawFormat` rather than `None`, so a refusal can say "this is compressed" instead of "unknown" |
| `RawFormat.admissible` | known, **not** compressed, **not** packed, whole bytes per pixel |
| `best_format()` | widest bit depth among the unpacked candidates |
| `admit()` | the boundary itself: four checks, then the pixels plus the evidence |
| `describe_refusal()` | why, in terms that say what to do about it |

**`admit()` reverses the direction of the stride reconciliation, and that is
the substantive fix.** The old `_trim_stride` *inferred* the bytes per pixel
from the row length: it tried 1, then 2, and took the first that fitted inside
a plausible pad. That cannot distinguish "10-bit, 1456 wide, padded" from
"8-bit, 2944 wide" — it is the same mistake one level down, deducing meaning
from shape. Now the negotiated format **states** the pixel size and the shape
**confirms** it; disagreement is the finding, not something to resolve by
picking whichever reading fits. The four checks, each against something
independent of the buffer's own shape:

1. the format is known, uncompressed and unpacked;
2. the row **count** equals the sensor height — padding is a per-row
   phenomenon, so a row count that disagrees means this is not the frame it
   claims to be, and no reshaping of it is legitimate;
3. the row length in **bytes** equals width × bytes-per-pixel plus a bounded
   stride pad (256 bytes: generous because the alignment is not ours to
   promise, bounded because an unbounded allowance would accept a buffer of an
   entirely different format whose row happens to be longer);
4. no value exceeds the declared bit depth. This is the one with teeth against
   a driver that hands back something other than what it negotiated: R10 that
   is really R12 has the right stride, shape and dtype and differs only in its
   values. About 1 ms on a 1.6 Mpx frame, so it is on the still path and off
   the preview path — and whether it ran is recorded, so a sidecar never
   implies a check that did not happen.

**`Frame.validity` — `science` or `diagnostic`.** A first-class field rather
than a metadata key, because it is a claim about admissibility and a claim like
that should be impossible to lose by forgetting to copy a dictionary entry.
`derive` carries it automatically, so it survives the pipeline. The two
constants live in `types.py` rather than in `rawformat.py`, where they are used
most: `cameras.base` already imports `types`, so the other direction would be a
latent import cycle waiting for the day `cameras/__init__.py` stops being
empty.

**`_choose_raw_format` refuses instead of warning.** Both of its old
carry-on-anyway paths — a configured format that looks compressed, and no
uncompressed format advertised at all — now raise at open time and the camera
does not start. A log line is not a control: nobody reads the journal of a rig
that appears to be working, which is precisely how 1,400 files of compressed
transport came to be recorded. The device is released before the exception
leaves, or the next attempt finds libcamera reporting no cameras and sends the
operator to the ribbon cables.

**`allow_unvalidated_raw`** is the one way past, per camera, in the config. It
turns each refusal into a warning and marks the source diagnostic for the rest
of its life. Bringing up a new sensor needs to be possible; what must not be
possible is producing something that calls itself science. Setting it is
permission, not a mode: a camera that *can* be validated still is.

**On disk.** A diagnostic capture is named `diagnostic_` **first**, ahead of
the tag, and carries `validity` in its sidecar. The prefix leads because the
failure being guarded against is a directory of `.npy` files picked up by glob
and fitted to — exactly how the compressed session was used — and a leading
prefix is the one piece of provenance that survives `ls`, a glob and a drag
into MATLAB.

**Saved previews are now diagnostic too.** A processed preview is gamma-shaped,
downsampled and may have a grid drawn on it. That it is not measurement data
was a sentence in a docstring plus two fields a reader had to think to check.
It is now the same closed claim as everything else.

**Both offline readers refuse it.** `scripts/read_capture.py --detect` exits
with the recorded reason unless `--allow-diagnostic` is passed; the new
`matlab/tv_require_science.m` does the same and `tv_micro_images` calls it.
Both readers also skip their legacy stride-trim when the sidecar says
`raw_admitted`, since re-deriving it could only disagree with the boundary that
already ran against the negotiated format.

**`space` and `validity` are deliberately different questions.** `space: raw`
says the ISP was bypassed — a claim about the *path* the pixels took, not about
what the values mean. A compressed PiSP buffer is `space: raw` and is not
measurable. Conflating the two is what let the session happen.

### Removed

| Removed | Was in | Replaced by |
| --- | --- | --- |
| `_trim_stride` (infers bytes per pixel from the row length) | `cameras/picam.py` | `rawformat.admit`, which is told the pixel size and checks it |
| `_unexpected` (log a warning, save the buffer untouched) | `cameras/picam.py` | a refusal with the reason recorded in the sidecar |
| `_is_compressed_raw` | `cameras/picam.py` | `rawformat.classify`, which also knows packed and unknown |
| the `_FakeRaw` trim harness and its 11 tests | `tests/test_detection.py` | `tests/test_rawformat.py`, with no camera fake at all |

Rolling back is `git checkout -- src/trilobite/cameras/ src/trilobite/types.py
src/trilobite/storage/writer.py src/trilobite/app.py src/trilobite/config.py
scripts/read_capture.py matlab/ tests/ config/pi.yaml`, but note that the
`validity` field then disappears from sidecars written afterwards while
remaining in ones written before, and `diagnostic_`-prefixed files stay named
that way.

### Testing

`tests/test_rawformat.py` (32 assertions across 26 tests) exercises the
boundary from byte arrays: golden fixtures for R8, R10 unpacked, R10 padded to
a 2944-byte stride and a `MONO_PISP_COMP1` sample built as a smooth gradient
rather than noise — a random fixture would be testing an easier problem than
the real one, since what cost the session is that a compressed buffer *looks
like an image*. `tests/test_orientation.py` covers the open-time choice and the
`capture_full` wiring with a fake picamera2; `tests/test_storage_devices.py`
covers the naming and the sidecar.

**Mutation testing, 15 mutants, all caught.** One survived first time and is
worth recording: removing `not self.compressed` from `RawFormat.admissible`
changed nothing, because every compressed entry in `KNOWN_FORMATS` happens to
carry `bytes_per_pixel = 0` and was refused by that clause instead. The
table-driven tests could not see the difference. Fixed by testing the invariant
directly — a compressed format with a perfectly ordinary pixel size is still
inadmissible, because the objection is to what the bytes *mean*, not to how
many there are.

Suite: **375 passed, 10 skipped**, plus 10 browser scenarios. Ruff clean.

### Bench test for this stage

1. Normal capture on both cameras: files unchanged in name and byte count,
   `"validity": "science"` in every sidecar, and `raw_admitted: true` with
   `raw_format`, `raw_stride_bytes` and `raw_padding_px` beside it.
2. Set `raw_format: MONO_PISP_COMP1` in `config/pi.yaml` deliberately. The
   camera must **refuse to open**, naming the format and saying what to do,
   rather than recording anything.
3. Add `allow_unvalidated_raw: true` alongside it. The camera opens, the log
   says `DIAGNOSTIC ONLY`, and a capture lands as
   `diagnostic_still_left_…​.npy` with the refusal quoted in its sidecar.
4. Remove both settings. The log must name the auto-chosen format (`R10`
   expected) at startup.
5. `python scripts/read_capture.py <the diagnostic file> --detect --board 4x3`
   must refuse and print the reason.

---

## 2026-09-06 (o) — review stages 0 and 1: trustworthy evidence, honest status

First two stages of `docs/implementation-plan.md`, responding to
`docs/software-stack-review.md`. Nothing in either changes how the rig acquires
or stores anything; that starts at Stage 2.

### Stage 0 — make the evidence trustworthy

The reviewer's suite run was **311 passed, 9 failed**, and none of the failures
were about the software. Seven were tests writing to the real
`~/trilobite-data`, two were Windows assertions about `fsync` call counts. A
suite that fails on a clean machine for reasons unrelated to the code is not
evidence of anything, and it cost the reviewer time to establish that.

**The seven.** `tests/test_rotation.py` built `AppConfig` with no storage
block, which defaults to `~/trilobite-data`. Mine, and invisible because it
works on a machine whose home directory is writable — it just scatters session
directories through it. Fixed, and `tests/conftest.py` now refuses: an autouse
fixture wraps `SessionWriter.__init__` and fails any test rooting a writer
outside its own `tmp_path`, naming the test and the path. Reintroducing the
omission reproduces exactly those seven failures with a message that says what
to do.

**The two.** They asserted `os.fsync` was *called* four times — image, sidecar,
and their two directories. On Windows `fsync_dir` cannot open a directory and
returns early **by design**, so two never happen and the capture is as durable
as that platform allows. The test was checking an internal call count rather
than an externally meaningful outcome, which the review names as a general
fault. So `fsync_dir` now returns whether it succeeded, `durability_of()`
measures the answer per directory (measured, not inferred from `sys.platform`
— a Linux host on an exotic mount can be `file-only` too, and that is exactly
where guessing gets it wrong with confidence), and every sidecar records
`durability: strict | file-only`. The test asserts the files are on disk and
the record says which guarantee was obtained; a second, POSIX-only test asserts
`strict`, so the Pi's promise is still pinned.

Also: `httpx` and `playwright` are explicit `dev` dependencies — the API tests
passed here only because something else had pulled httpx in, and failed to
import at all in a freshly resolved environment. `packages.find` gained
`include = ["trilobite*"]`, so discovery cannot ship a second application
again. A CI workflow runs the suite on Linux and Windows across Python 3.11 and
3.13, and fails the build if anything wrote to `$HOME/trilobite-data`.

**The browser scenarios are now tests.** They were ad-hoc scripts during
development; the review looked for them in the repository and found none.
`tests/browser/` runs the real application under uvicorn, drives Chromium, and
asserts ten things a Python test cannot see — which tabs stream, which stage
panels appear where, that the rotate control has a non-zero rendered size, that
enabling the grid locks orientation and a POST then returns 409, and that the
offset slider re-ranges from ±50 to ±20 when the pitch goes 100 → 40. They skip
when Playwright or a browser is absent, so an ordinary `pytest -q` stays green;
CI installs Chromium so they execute. `TRILOBITE_CHROMIUM` points at an
existing binary where the download is unavailable.

**Four overclaims in the progress report, corrected in place** with dated
notes rather than quiet edits: that only the capture thread touches the camera
(it does not — F1); that a full frame is the same exposure as its preview (true
of the handshake, false of `capture_full`, which is what stills use); that
device disappearance is detected (F2); and that stereo pair skew is "tens of
milliseconds" (it also contains a full disk write, fsync included).

`src/flyeye/` and `systemd/flyeye.service` are still to be deleted — a shell
task on the Windows tree, listed in the plan.

### Stage 1 — status that stops being true when the rig does

Finding F5. The failure class is not that something breaks; it is that
something breaks and everything goes on reporting success. Four ways that was
possible, all now closed:

| was | now |
|---|---|
| `RateMeter` computed from its last N samples with no reference to the present, so a camera stopped an hour ago still reported the rate it had when it stopped | returns `0.0` when the newest sample is older than `stale_after` (3 s) |
| `status.sensor_fps` returned `cfg.fps` — a configured number wearing the name of a measurement, and structurally unable to fall to zero | measured at the point a frame is taken, counting frames the rate cap released, alongside `configured_fps` which keeps the old meaning under an honest name |
| `Pipeline.__call__` caught a stage exception, logged it and passed the frame on. Nothing above that line could see it: not the frame, not `CameraRuntime.errors`, not the API | per-stage counters and last message in `/api/status`; the frame carries `pipeline_failed_stages`; logging is rate-limited to the 1st and every 100th so a stage failing at 12 Hz cannot bury the journal while the counter still records every one |
| the MJPEG generator re-encoded and re-sent the last frame every time the 2 s bus wait timed out | sends nothing. One frame on connect, so a new viewer is not looking at a blank pane, then silence |

Added: `acquired_age_s` and `published_age_s` (`null`, not `0`, when nothing
has happened yet), and `last_write` on the writer — filename, age, bytes and
durability of the last file that *actually landed*, recorded only after both
members are verified, so it cannot come to mean "last write attempted".

On the page: a camera with no frame for two seconds is flagged **⚠ NO FRAME FOR
*n*s** and its image is dimmed and desaturated — a number in a header is easy
not to look at while you are staring at the picture, which is precisely when a
frozen picture does damage. Stage failures appear beside the camera. And the
status poll failing three times running now shows **⚠ no reply from the rig**,
which the page previously caught and discarded.

`Frame.seq` and `Frame.t_mono` are documented accurately. The docstring said
"gaps mean dropped frames", which was wrong in the direction that matters: it
counts software *deliveries*, frames released by the cap never reach it, and it
is not drop or pairing evidence. `t_mono` is receipt, not exposure — the
driver's `SensorTimestamp` is the only timestamp here with a defined
relationship to the exposure, and synchronisation work will need it.

### Mutation results, including one that failed usefully

| mutation | caught by |
|---|---|
| `RateMeter` stops ageing out | 2 tests |
| `sensor_fps` back to the configured number | 1 |
| stage failures not recorded | 4 |
| the stream re-sends a stale frame | **initially nothing** |

The fourth is worth recording. The test passed with the behaviour mutated away,
because the helper reading the stream was built on `urllib`, whose buffered
reader raises `OSError: cannot read from timed out object` after the first
socket timeout and stays broken. It exited at the first quiet moment and
returned zero bytes — for a live stream as readily as a dead one. Rewritten
against a bare socket, it then reported 533 bytes from a *stopped* camera,
which turned out to be correct behaviour the test had mis-specified: one frame
on connect is right. The assertion is now "one frame, then nothing across two
consecutive windows on one connection", and the mutation fails it with 1074
bytes. A test that cannot fail is worth less than no test, and only the
mutation revealed it.

### Verification

```
pytest -q               → 335 passed, 10 skipped   (was 321)
pytest tests/browser/   → 10 passed against a real server and Chromium
ruff check .            → clean
```

Bench-verified in a browser: healthy shows `11.97 fps  sensor 19.89`, the cap
visible in the gap; stopping one camera turns its header red with ⚠ NO FRAME
FOR 3s and dims its image while the other is untouched; stopping the server
raises the no-reply banner.

---

## 2026-09-05 (n) — six tabs, and an audit of what the sliders may ask for

### Why collapsing the storage panel did not help

> storage minimisation didn't really help because the dashboards didn't expand
> to full width anyway.

Correct, and the fix was in the wrong place. `main` is
`repeat(auto-fit, minmax(430px, 1fr))`; removing a card from a three-card row
leaves two cards that were already at `1fr` and does not give them the freed
column. Shrinking the third card just made the row shorter.

So the dashboard is now **six tabs**, and the split is by job rather than by
what happened to be built when:

| tab | for | streams |
|---|---|---|
| System | storage, host health, addresses, camera rates and formats, paths | 0 |
| one per camera | that sensor's orientation and MLA alignment | 1 |
| Imaging | both sensors: exposure, gain, display stages, save, quick-record | 2 |
| Video | placeholder | 0 |
| Calibration | the existing hands-free loop | 0 |

Orientation and the MLA stages appear only on a camera's own tab; exposure and
gain appear on both, because those are what you adjust while looking at
whatever you are looking at. `SETUP_STAGES` is the one list that decides, and
`buildPipelinePanels` takes `only`/`skip` against it.

The tabs also pay for themselves in the connection budget, which is the
constraint the whole streaming design sits under: only the tab on screen
streams. One camera tab is one MJPEG stream plus three polled tiles; Imaging is
two streams and no tiles; System and the placeholders are none. Previously
every tab held two streams and six tile polls whether or not anything was
looking at them.

Per-camera tabs are generated from `/api/cameras`, so a third camera adds a
third tab with no edit to the page.

**On a camera tab the controls sit beside the image, not under it.** Stacked is
right when two cameras share the width; on a single sensor's alignment tab it
means the image is either large and the controls are off-screen, or the
controls are visible and the image is a strip. Side by side gives both — the
image takes the height of the window, the controls take a `clamp(300px, 26vw,
400px)` column and scroll in it — and the 1100 px single-column cap is lifted,
because that cap exists so a lone card is not stretched across a 4K monitor and
a card with a control column is not a lone card. Under 900 px of window the
stacked layout returns. Measured at 1500, 1920 and 820 px wide: the whole
control set fits without scrolling at the first two, and the third falls back.

**Calibration is parked, not deleted.** It was asked for as a placeholder, and
the tab is labelled as the unsupported path — but it is several hundred lines
of working, tested UI over machinery (readiness checks, the coverage model, the
pose manifest) that any calibration needs whether the detection runs on the rig
or on the desk. Deleting that on "not convinced" would be throwing away the
part that is not in doubt. Say so and it goes.

**What else went on System**, since it was asked: the host's temperature, load,
memory and sticky under-voltage flag; every URL the dashboard answers on, plus
the interfaces and the mDNS name (the answer to "the DHCP lease moved, where is
it now"); per camera the backend, the frame sizes, all three frame rates with
the count of frames the pipeline cap skipped, the error count and any controls
libcamera dropped; and the session and settings-file paths. All of it existed
in `/api/status` already and none of it was on screen anywhere.

### The range audit

> There is adjustments to be made for the allowed "scale" of some of the bars.
> They except sometimes values that are not allowed and break the image.

Seven, and the first is much worse than the rest.

**`pitch_px` could hang the server.** `whole_indices` enumerates the lattice
with `_search_radius` bounds and calls `is_whole` — trigonometry — on every
candidate. The radius used the FULL frame diagonal with no offset term, so the
count went as (2·diag/pitch)², and `MLAGridOverlay.apply` reaches it through
`named_indices` **on every frame**. At the default pitch of 20 px that was
34,000 tests per frame, 800,000 a second across two cameras at 12 Hz — a real
share of the CPU the web thread was losing to. At `pitch_px: 1.5`, which the
old `gt=1.0` bound permitted, it is 1.4 million tests inside a request handler:
not slow, hung, with the browser and the log both showing nothing.

Three changes, in order of how much they buy:

1. **Memoised.** `MLAGeometry` is a frozen dataclass and hashes by value, so an
   `lru_cache` on the enumeration turns a per-frame quadratic scan into a
   per-edit one even though `geometry_for` builds a fresh instance every call.
   Measured at pitch 20 on a 1456×1088 frame: 15 ms → 7 µs.
2. **A tight radius.** Half-diagonal plus the offset magnitude, which is the
   exact bound on |i|·pitch, rather than the full diagonal. Four times fewer
   candidates. Tested against a deliberately absurd reference bound across 48
   pitch/rotation/offset combinations — the sets must be *identical*, because a
   radius that is too small silently drops the outermost ring, which is exactly
   the lenslets the corner sub-apertures use.
3. **A floor and a cap.** `pitch_px` ≥ 10, and `whole_indices` refuses above
   40,000 candidates and returns an empty list, which every caller already
   handles: the overlay draws no highlights, the tile endpoints answer 204, and
   readiness reports zero whole tiles with the pitch in the message.

**The offsets are bounded by the pitch, and folded into it.** An offset says
which physical lenslet is index (0,0), so moving it a whole lattice vector
renames the lenslets and draws the identical grid: offsets a pitch apart are
one setting written two ways, and every distinct alignment lives within half a
pitch of centre. The slider range now follows the current pitch, and a value
outside is *folded* rather than clamped — the number jumps to the other end
while the grid slides on unchanged. A clamp would stop the grid while the
number kept moving, and the control would look dead at one end.

The reduction is in the **lattice basis**, not per-axis: `u` and `v` rotate
with the grid, so subtracting whole multiples of them is exact at any rotation
where folding x and y separately modulo the pitch is only right at zero. There
is a test at four angles; folding per-axis fails three of them.

The remaining five:

| parameter | was | now | the symptom |
|---|---|---|---|
| `crop_scale` | 0.1 – 4.0 | 0.25 – 2.0 | above 2 a "sub-aperture" spans four lenslets and is not one |
| `levels.gain` | **0.0** – 8.0 | 0.05 – 8.0 | gain 0 is a black frame, which is also what a dead camera, a closed shutter and a crashed capture thread look like |
| `levels.offset` | ±128 DN | ±4096 DN | an 8-bit assumption; the preview carries mono16 from a 10-bit sensor |
| `stats.saturation_level` | ≥ 0, no maximum | 1 – 65535, boxed | no upper bound meant a 0–1 slider with the value pinned off the end |
| `crop.x0..y1` | any 0–1 | x1 > x0, y1 > y0 | an inverted rectangle was silently ignored by `apply`, so the numbers said one thing and the image showed another |

There is also a test asserting that **every numeric stage parameter has both
bounds or explicitly asks for a box**, so the next one added cannot quietly get
a 0–1 slider.

### Two checks the test suite did not have

The `<span id="live-actions"` bug from entry (m) — a missing `>` that made the
browser read the capture buttons as attributes of the span — was invisible to
every test, because the file is HTML and the suite is Python. `tests/test_ui_page.py`
now parses the page, asserts every tag closes, and asserts the twelve ids the
boot script resolves are real elements in the expected nesting. Re-introducing
the missing bracket fails three of them.

The page was also driven headlessly through Playwright while building this:
tab list and order, stream counts per tab, which stage panels appear where, and
the offset slider re-ranging from ±50 to ±20 when the pitch goes from 100 to 40.

### Verification

```
pytest -q                  → 320 passed  (was 236)
ruff check .               → clean
headless Chromium          → 6 tabs, streams 2/1/0/0/0, panels split as intended
```

Mutations tested: the 409 orientation lock removed, `mark_dirty` dropped, the
grid reset removed, the pitch zeroed, the reference not transposed, the
`live-actions` bracket removed, per-axis offset folding. All caught. One was
not: shrinking `_search_radius`'s `+2` margin changes nothing, because the
bound is already exact for the centre and `is_whole` is stricter — the margin
is deliberate slack and the code now says so, rather than leaving a reader to
assume a test covers it.

---

## 2026-09-05 (m) — orientation becomes a setup step, and three UI fixes

Three reports, one of which turned out to be about caching rather than code.

### "There is no rotate button"

There was. The control was in the page, styled, 240 px wide, and the server was
plainly running the new code — the same message reported a log line only the
new `plenoptic.py` emits. The page was the old one.

`FileResponse` sends an ETag and a `Last-Modified` but no `Cache-Control`, so a
browser applies **heuristic freshness** and may serve a cached copy without
revalidating at all. Deployment here is `git pull` on the Pi with the dashboard
left open in a tab, which is precisely the case that produces a UI missing
controls the server already implements — indistinguishable from the feature
being broken, and it cost an exchange to work out.

Two changes. The index is now served with `Cache-Control: no-cache`, which
means *revalidate*, not *do not store*: the browser still keeps it and still
gets a 304 when nothing changed. And the server substitutes a hash of the file
into the page, which polls `/api/ui-build` every fifteen seconds and puts
**⟳ page is out of date — reload** in the header when the two differ. The first
fixes the cause; the second makes the symptom self-diagnosing if it returns in
another form.

Separately, `.row select` had no CSS at all. It rendered, but as a native
widget on a dark theme, which is worth fixing regardless: `color-scheme:dark`
plus the same border and padding as the number boxes.

### Orientation stops being clever

Entry (l) added a D4 rebase: change the orientation and the MLA alignment was
carried across, offsets transformed, pitch preserved. The arithmetic was right
and mutation-tested. The feature was wrong, and the user's framing is the
argument:

> we only do the flip and rotate once at startup, and continue to work with
> these fixed throughout all the following procedures.

A grid alignment is a measurement made by eye against a particular image.
Transforming it and calling it aligned asserts something nobody checked — and
the alignment is the *least* of it: every recorded pose and the fit that
follows assume a fixed frame, and no amount of correct arithmetic about the
grid repairs those. Carrying it across therefore invited exactly the operation
that cannot be made safe.

So:

- **locked while the grid is on.** The controls grey out (greyed, not hidden —
  the operator should see the decision exists and that it is settled) and
  `POST /api/orientation` returns 409. Asking for the orientation it already
  has is not a change and is not refused, so a page re-rendering its own state
  still works.
- **changing it with the grid off resets the alignment**: offsets and lattice
  rotation to zero.
- **`pitch_px` survives.** It is `pitch_um / pixel_pitch_um` — hardware, not
  alignment — and it is the tedious number to re-enter.
- **the frame size carries forward**, which is what a quarter turn actually
  changes, and was already handled by `describe()`.

`optics/orientation.py` lost the group arithmetic and is now a value type: what
an orientation is, whether two of them transpose the frame, and a phrase for a
log line. The module docstring records why the deleted version was deleted,
since it was correct code and someone will otherwise write it again.

`tests/test_rotation.py` was rewritten to match — the closed-form grid tests and
the D4 round trips went with the feature; the lock, the reset, and the pitch
survival replace them. Five mutations, all caught:

| mutation | caught by |
|---|---|
| the 409 lock removed | the lock test |
| `mark_dirty` dropped from the orientation endpoint | the state-file test |
| `bind_sensor` keeps the offsets | 4 tests |
| `bind_sensor` also zeroes the pitch | 4 tests |
| the reference frame not transposed on an odd turn | the compose-order test |

Verified in a real browser as well as in pytest: the page driven headlessly
through Playwright, the dropdown measured on screen at 240×23 px, the three
rows going locked when the grid checkbox is ticked, and a `POST` while locked
answering 409.

### "Sensor parameters are not saved"

Two separate causes behind one symptom.

**Orientation was never marked dirty.** Autosave writes only when something
asks it to, and `POST /api/orientation` was the one mutating endpoint that
never called `application.mark_dirty()`. A flip set in the UI therefore lived
until the next restart and then reverted to the YAML. It is also now in
`state_snapshot` (added in (l)) — it describes how the camera is bolted down,
which does not change when the process does.

**Exposure and gain were saved and restored, but displayed from the config.**
`GET /api/controls/{cam}` returned `cfg.controls` — the value the YAML shipped
with — rather than `source.requested_controls()`, which is everything asked for
since, restored values included. So the camera really was at the restored
exposure and the box really did say the old one, and the first nudge of the
slider sent the sensor back to the config. The endpoint now returns both, named
`requested` and `config`.

### The storage rail collapses

It is a panel you touch once, when a disk goes in, and expanded it costs a
whole camera-sized column of a two-column layout. Collapsed it is a full-width
strip carrying the two facts worth having on screen — where output is going and
how much room is left — and it turns accent-coloured, naming the count, when a
removable disk is present that is not the current target. Collapsed by default;
the choice is remembered in `localStorage`, because which panels one person has
open on one screen is not rig state and syncing it between two browsers looking
at the same Pi would be worse than not remembering.

### Verification

```
pytest -q                  → 236 passed
ruff check .               → clean
headless Chromium          → dropdown visible, lock applies, 409 on a locked POST
```

---

## 2026-09-05 (l) — a quarter turn, a rate cap, and a line diagnostic

Three requests, of which the first was much the largest because it was asked in
the right way: *"the effect will trickle down the entire chain with width and
height being swapped. If at any stage there is an assumption on width and
height being non parametric, this we will be fixing many bugs in the future."*
So the audit came before the code.

### The audit, and what it found

A read-only sweep of `src/`, `scripts/`, `matlab/` and `tests/` for places
assuming a landscape frame. The framing finding was not any individual line:

> `CameraInfo.full_resolution` and `preview_resolution` are echoed verbatim
> from the config and never measured from a delivered array.

Every consumer wants the post-rotation size, and every consumer was reading a
figure that could not know about rotation. Nine call sites would have been
**silently wrong** — no exception, a plausible number, and a grid landing
between micro-images. The worst was `MLAGridOverlay.bind_sensor`, whose handler
for an anisotropic rescale caught the `ValueError`, stamped the new frame size
and left pitch and both offsets verbatim. A 1456×1088 alignment meeting a
1088×1456 frame took exactly that path.

### What was done instead of patching nine sites

**One place decides the size.** `CameraSource.oriented_size()` is the only
function that knows a quarter turn swaps the axes, and every backend's
`describe()` runs its resolutions through it. The nine consumers then need no
change at all, because they were already reading from `describe()`. The
sensor-native size stays where it is genuinely needed and nowhere else: the
stream configuration, and `_trim_stride`, which must see the buffer as the
sensor delivers it — padding is on the right pre-rotation and along an edge
cropping would not touch after, so trimming a turned frame would remove real
image.

**An alignment survives the turn.** A quarter turn moves no lenslets, so
invalidating the grid would be wrong; but carrying it across needs to know
*which way* it turned, and a swapped reference frame does not say. So
`MLAParams` records the orientation an alignment was made under alongside the
frame size, and `optics/orientation.py` computes the change as an element of
D4 — offsets transform as the vector they are, the reference swaps on an odd
turn, the lattice tilt negates under a reflection and is otherwise untouched
because a square lattice cannot tell quarter turns apart, and pitch is
invariant because every element of the group is an isometry. `bind_sensor`
handles orientation *before* size, so the anisotropic path is now reached only
by a genuine resample.

**The two width-only ratios in `settings.py` are gone**, replaced by
`geometry_for`, which checks both axes and raises. They were correct until the
day the two frames stopped sharing an aspect ratio, which is the day this
change arrives.

`ReplaySource.describe()` now reports the size of the frame it last produced
rather than the config's guess — the one backend where the config figure is not
authoritative, since the files on disk have whatever size they have.

### Verifying it rather than asserting it

The rotation has two halves — the pixels, and the matrix that claims to say
where they went — and the grid rebase trusts the second completely. Nothing
else compares them, so a sign error in each would cancel in every other test.
There is now one that follows a marked pixel through the real `_orient` and
checks it against the matrix acting on its centred coordinates, for all sixteen
settings. Mutation-tested; all four caught:

| mutation | caught by |
|---|---|
| 90° and 270° matrices swapped | 3 tests |
| mirrors applied before the rotation | the order test |
| `quarter_turns` sign flipped | 4 tests |
| matrix composed as `R·F` instead of `F·R` | the seam test, 4 orientations |

End to end: a synthesised 90° capture reads 20/20 under Octave, and the tiles
`tv_micro_images` extracts from it were compared pixel for pixel against
`MLAGeometry.crop` in Python on the same file — **max difference 0**, with the
tile centres agreeing to 1e-13 px.

### The frame rate (request 2)

The browser stream was **already** capped at 12 Hz; `server.preview_fps` has
been 12 in `config/pi.yaml` throughout. What was not capped was the *pipeline*,
which ran on every sensor frame: 30 Hz × two cameras × (stats, levels, overlay,
~3 ms presence map) on four cores that also encode JPEG and answer the API. The
web thread lost, and that is what "updating parameters lags" was.

`CameraConfig.process_fps` (default: follow `server.preview_fps`) now gates the
pipeline, and `CameraSource.skip_preview()` releases an early frame without
decoding it — the sensor must still be drained at its own rate or the four-deep
request pool starves, so the cap could not be applied by reading more slowly.
`GET /api/status` reports `fps`, `sensor_fps` and `skipped`, so the cap can be
seen working rather than assumed. The deadline accumulates rather than
restarting from now: 12 does not divide 30, and a restarting deadline quantises
down to 10 Hz.

### The lines (request 3)

`scripts/diagnose_lines.py`. The symptom has four causes that look identical on
screen and want different responses, and changing the cable tests one of them.
The measurement that separates the link from the sensor is that a dropped byte
does not corrupt a row's values, it **moves** them — so a bad row still looks
like the scene and simply sits a few pixels to one side. That is invisible to
any measure of row brightness, obvious to a cross-correlation against the rows
above and below, and something no sensor does.

Two probes, because one is not enough: row *offset* finds noise and misses
displacement (a displaced row has very nearly the right mean); row *roughness*
finds displacement and misses offsets (each row's mean is removed first). A
test asserts that the offset probe does **not** find the displaced rows, so the
reason for having two cannot quietly stop being true.

The verdict is ordered by how decisive the evidence is rather than by how
common the cause is: kernel-counted CSI errors first, then a measured
displacement, then a statistic about offsets — and a statement about offsets is
only made when they are large enough to see.

### Verification

```
pytest -q                  → 247 passed  (was 178)
ruff check .               → clean
octave tv_selftest         → 20/20 on a synthesised 90° capture
```

Nothing is committed. To undo any part: `git checkout -- <path>`. The new files
are `src/trilobite/optics/orientation.py`, `scripts/diagnose_lines.py`,
`tests/test_rotation.py` and `tests/test_line_artifacts.py`; deleting those four
and reverting the rest leaves the tree as it was.

---

## 2026-09-05 (k) — the second half of the raw buffer story

Fixing the compressed format in (j) moved the symptom rather than removing it.
The reader now reported **2944 x 1088**, a 2.0220 : 1.0000 aspect ratio, raw
only, both cameras, MLA on or off. View captures were fine throughout, which
localised it to the raw buffer and nothing else.

`2944 = 2 x 1472`, and `1472 = 1456 + 16`. So (j) worked: the format is now
10-bit `R10`, **two bytes per pixel**. What did not change is `_trim_stride`,
which assumed one byte per pixel throughout.

A raw buffer's rows are padded to a 64-byte stride and `make_array` shapes the
array by that stride **in bytes**, handing it over as uint8 whatever the pixel
size really is:

| format | image | row bytes | stride | delivered as |
|---|---|---|---|---|
| R8 | 1456 px | 1456 | 1472 | 1088 x 1472 uint8 |
| R10 | 1456 px | 2912 | 2944 | 1088 x 2944 uint8 |

The second is 1472 *uint16* pixels laid out as 2944 bytes. The old trim tested
`1456 < w <= 1584`, 2944 matched nothing, and it correctly refused and flagged
`raw_unexpected_shape` — so the frame was saved untouched rather than mangled.
Which was the right failure to have: had the window been wider, it would have
cropped to 1456 *bytes* — the first 728 pixels and half of the 729th, which is
structure at the wrong scale and much harder to recognise.

| change | file |
|---|---|
| `_trim_stride` infers bytes per pixel from the row length, re-views a 2-byte buffer as uint16, then trims | `cameras/picam.py` |
| `_unexpected()` split out; its message now names packed formats as the likely cause | `cameras/picam.py` |
| `raw_stride_bytes` and `raw_bytes_per_pixel` added to the metadata | `cameras/picam.py` |
| the same inference in both offline readers, for files already on disk | `scripts/read_capture.py`, `matlab/tv_read_capture.m` |
| self-test checks rewritten: the sidecar records the FILE's shape and dtype, and the image legitimately differs from both | `matlab/tv_selftest.m` |

The re-view is a re-view, not a conversion: no copy, no arithmetic, and the
values become the 10-bit counts the sensor produced rather than their low
bytes.

### Verification

```
pytest -q                       → 177 passed  (5 new)
ruff check src/ tests/ scripts/ → clean
tv_selftest, 8-bit capture      → 20 passed
tv_selftest, 10-bit repro       → 22 passed
```

Reproduced the reported file exactly — a known 10-bit image laid out as
1472 uint16 per row with 16 pixels of pad, delivered as 1088 x 2944 uint8 —
and both readers recover it **bit for bit**: `np.array_equal` against the
source image in Python, and matching values in Octave. The Python test asserts
the same on the capture-side path, plus that packed formats are still refused
and that the 8-bit branch is unchanged.

Still not verified on the rig. The next capture's sidecar should read
`raw_bytes_per_pixel: 2`, `raw_stride_bytes: 2944`, `shape: [1088, 1456]`.

### A flake fixed on the way past

`tests/test_detection.py` failed about one run in thirty on
`test_every_whole_tile_yields_the_full_pattern`. Its synthetic source never
pinned `synthetic_drift_px`, which defaults to 3.0, so the board wandered with
the **wall clock** and at some phases an edge micro-image lost a corner. The
same flake was found and fixed in `test_presence.py` earlier and never
propagated here.

Pinning the drift then exposed two things it had been hiding, both real and
neither a bug in the code under test:

* At 4 degrees rotation the outermost ring of tiles is whole for the
  axis-aligned predicate and reaches off-sensor for the rotated one, where the
  bilinear sampler clamps at the border. A clamped edge moves corners by a few
  pixels. `test_derotated_and_plain_crops_agree_on_corner_positions` now
  compares only tiles whole under **both** predicates, which is the set where
  its precision claim means anything.
* At a crop scale of 0.85 the crop cuts into the synthetic board's one-square
  quiet margin, and `findChessboardCornersSB` can lock onto a 4x3 sub-grid
  shifted by one square — the two routes then disagree by multiples of the
  square size. Measured across scales: 1.0, 0.95 and 0.9 give zero
  disagreements out of ~130 tiles, 0.85 gives eight. The test uses 0.9 and says
  why.

Both had been passing for an accidental reason. Nine consecutive full-suite
runs clean afterwards.

---

## 2026-09-05 (j) — the raw format was compressed; flip; README split

### 1. Captures were structured noise

Reported after the first successful recording session: the files contained
image data, "but it is nowhere near what the image is supposed to be".

**libcamera's default raw format on a Pi 5 for the mono IMX296 is
`MONO_PISP_COMP1`** — the Pi 5 imaging pipeline's *compressed* transport, one
byte per pixel, produced to save memory bandwidth. `make_array` hands those
bytes back as a plain uint8 image. The array has the right shape, the right
size, and obvious structure, and every value is wrong.

That is the worst available failure mode: it looks like a photograph that has
gone slightly wrong rather than like a decode failure, so it does not announce
itself. The same code on a Pi 4 received `R10` and was correct, which is why it
only appeared after the move to the 5.

The code already had a `raw_format` config field, and its own comment already
said the Pi 5 default was unsuitable — but the field defaulted to `None` and no
config ever set it. A documented hazard with no enforcement.

| change | file |
|---|---|
| `_choose_raw_format`: pick an uncompressed format from the sensor's advertised modes, preferring unpacked (`R10` over `R10_CSI2P`) and the widest bit depth | `cameras/picam.py` |
| `_is_compressed_raw`: name-match PISP/COMP | `cameras/picam.py` |
| an explicit compressed `raw_format` is honoured but logged at ERROR; no uncompressed option available is also ERROR | `cameras/picam.py` |
| `raw_format` and `raw_format_choice` recorded in every sidecar | `cameras/picam.py` |

The metadata addition matters as much as the fix. A `.npy` gave no way to tell
linear sensor counts from a compressed transport, and those are
indistinguishable by inspection — which is how a session was recorded before
anyone noticed. Files now say what their pixels are.

Rollback: set `raw_format` explicitly in the config; `_choose_raw_format`
returns it unchanged.

### 2. Flip horizontal / vertical

Requested for the saved data, not only the display — so it is applied at
**acquisition**, in `CameraSource._orient`, before anything else sees the
pixels.

| change | file |
|---|---|
| `flip_horizontal` / `flip_vertical` on `CameraConfig` | `config.py` |
| `_orient()` and `orientation` on `CameraSource` | `cameras/base.py` |
| applied on all three frame paths: preview, the full frame served to calibration, and `capture_full` | `cameras/picam.py`, `cameras/offline.py` |
| `GET`/`POST /api/orientation/{cam}` | `web/server.py` |
| checkboxes in the Sensor panel | `web/static/index.html` |

Two decisions worth recording. It lives with the **camera**, not the pipeline:
a pipeline stage would flip the preview and not `capture_full`, which bypasses
the pipeline entirely — mirroring what you look at and not what you measure,
discovered when a calibration comes back mirrored. And the raw path **trims the
stride before orienting**: the padding is on the right of the buffer as it
leaves the sensor, so flipping first would move it to the left and the crop
would then remove real image.

Changing the flip invalidates an MLA alignment (the grid offsets are measured
from the frame centre, and a flip negates the axis they run along). The
endpoint returns a warning saying so when a grid is enabled, and the UI shows
it.

### 3. README split

The install section had accumulated a running account of one week's network
debugging — IPv6 link-local recovery, the SD-card flight recorder, USB gadget
mode, three addressing mechanisms — which is the wrong content for a document
whose job is "how to run this".

`docs/pi-troubleshooting.md` (263 lines) now holds all of it, opening with the
advice that supersedes most of it: put the Pi on a router with DHCP and the
whole class of problem disappears. The README's step 2 says the same in three
lines and points at the router's client list.

README: 1088 → 884 lines.

### Verification

```
pytest -q                       → 172 passed  (15 new)
ruff check src/ tests/ scripts/ → clean
```

The flip tests assert exact array equality against `np.flip` on all three frame
paths, including the one the calibration loop uses. The raw-format tests drive
`_choose_raw_format` against the IMX296's real advertised mode list and assert
that a compressed format is never chosen, that `R10` wins over `R10_CSI2P` and
`R8`, and that both failure branches log at ERROR.

Live, against the synthetic rig: the orientation endpoint flips, returns the
alignment warning, the UI checkboxes render, and two captures taken either side
of the toggle differ by the mirroring with the flag recorded in each sidecar.

**Not verified on the rig.** The raw-format fix is the one that matters and it
cannot be tested here — there is no PiSP hardware in this container. The next
capture on the Pi is the test: the sidecar should read `raw_format: R10` (or
whatever `probe_cameras.py` reports) rather than anything containing PISP.

---

## 2026-09-04 (i) — the first-contact gap in (h)

The reflashed Pi could not be reached at all: `flyeye.local` failed, a direct
Ethernet cable showed nothing, and the PC pinned to `192.168.50.20` still saw
nothing. ACT LED blinking, Ethernet lights on, SSH enabled at flash time,
hostname set. So the board was fine and the advice was wrong.

**Two defects in (h)'s documentation, one of them mine to own.**

1. **The fixed address is created by the script, and the script needs a shell.**
   §Addressing presented `192.168.50.10` as somewhere to point a PC, without
   saying it does not exist until `setup_network.sh` has run once. Telling
   someone to set their PC to `192.168.50.20` on a freshly flashed card puts
   the two ends on different subnets with nothing at the far end. Step 2 also
   said "step 7 fixes it properly", which is useless when the failure is that
   you cannot get to step 7.

2. **NetworkManager does not do IPv4 link-local fallback.** Unlike the `dhcpcd`
   it replaced in Bookworm, it does not hand an interface a `169.254.x.x` when
   DHCP times out. On a direct cable a fresh Pi therefore has **no IPv4 address
   at all** on eth0 — so every IPv4 approach fails for the same reason, and the
   symptom is indistinguishable from a dead board.

   What always exists is the IPv6 link-local `fe80::` address. That is the way
   in, and it needed to be in the document.

| change | file |
|---|---|
| new §"If you cannot reach the Pi at all": the IPv4-less explanation, IPv6 link-local recovery with the Windows PowerShell commands, and three ways to avoid it next time | `README.md` |
| §Addressing states that the fixed address exists only after the script runs | `README.md` |
| step 2 points at the recovery section instead of at step 7 | `README.md` |
| Windows mDNS is partial; Bonjour Print Services makes `.local` reliable | `README.md` |
| `ipv4.may-fail yes` asserted alongside the static address | `scripts/setup_network.sh` |

That last one is small and load-bearing. A connection whose IPv4 is allowed to
fail still activates when DHCP finds nothing, and the manual address is still
applied. Without it, the direct-cable case the fixed address exists for is
exactly the case where it would not appear. It is the default, but a default
this scenario depends on is worth writing down.

### Verification

```
pytest -q                       → 157 passed
ruff check src/ tests/ scripts/ → clean
shellcheck scripts/setup_network.sh → clean
```

The diagnosis is from documentation and from the reported symptoms, not from a
Pi: the IPv6 link-local recovery has **not** been executed against this rig.
Facts checked rather than recalled: NetworkManager needs explicit configuration
for zeroconf fallback, and Windows `.local` resolution is partial without
Bonjour.

---

## 2026-09-04 (h) — a from-SD-card Pi procedure, and a rig that stops moving

The Pi's address changed and the rig went missing. Separately, the SD card is
being reflashed, and the install documentation assumed a Pi that was already up
and reachable — it started at `git clone`.

**No code touched outside the two concerns below.** In particular nothing in
the MLA, stride or calibration path, which is untested on the rig as of this
entry.

### Addressing

`serving on http://0.0.0.0:8000` is the *bind* address. It is not somewhere a
browser can go, and on a DHCP network the number you need is precisely the one
that just changed underneath you.

| change | file |
|---|---|
| `net.py`: enumerate every reachable address, name the mDNS one, build the URL list | `src/trilobite/net.py` (new) |
| the startup banner lists every URL, annotated | `__main__.py` |
| `/api/status` carries a `network` block, recomputed per call | `app.py` |
| `python -m trilobite.net` prints the same without starting the app | `net.py` |
| `setup_network.sh`: hostname, avahi, an `_http._tcp` advert, a fixed second address, and the MACs to send IT | `scripts/setup_network.sh` (new) |
| `avahi-daemon` listed explicitly; `rpi-usb-gadget` documented as optional | `apt-packages.txt` |

Three mechanisms, installed together because they fail independently: mDNS
(free, blocked on some enterprise networks), a **fixed second address on eth0
alongside DHCP** (the one that always works), and a DHCP reservation (needs a
ticket).

The second is worth recording precisely, because the obvious version of it is
dangerous. NetworkManager applies manual addresses *in addition to* the DHCP
lease **as long as `ipv4.method` stays `auto`**. Setting the method to `manual`
instead takes the Pi off the network entirely — and over SSH that means a
monitor and a keyboard. The script sets the address and then explicitly
re-asserts `ipv4.method auto`.

A fourth option is documented but not installed: Raspberry Pi OS Trixie images
from 2025-10-20 carry `rpi-usb-gadget`, which makes the Pi 5's USB-C port a USB
Ethernet device at a fixed 10.12.194.1. One cable, no network administrator.
Not the recommendation *for this rig* because that port then becomes the only
power input, and a Pi 5 with two cameras and an SSD can outdraw a laptop USB-C
port — the failure mode being a reboot mid-capture.

### The install procedure

`README.md` §Install is rewritten as nine steps from a blank card: imager
settings (including the ones that cannot be fixed later without a monitor),
first boot, update, ribbons and overlays, proving the cameras from Python,
first run, addressing, storage, and the service last. Each step ends with
something to check, because the failures here are silent and finding out at the
wrong step costs an hour.

Verified against current sources rather than from memory: Raspberry Pi OS
Trixie has been current since 2 October 2025 (Bookworm still supported), and
Pi 5 USB gadget mode is real, is on the USB-C port, and needs an image dated
2025-10-20 or later.

### Verification

```
pytest -q                       → 157 passed  (8 new)
ruff check src/ tests/ scripts/ → clean
shellcheck scripts/*.sh         → clean for the new script
bash -n on both shell scripts   → clean
```

The address tests run against a captured `ip -j -4 addr show` sample — a Pi
with a DHCP lease, the fixed second address, wifi and a USB gadget link — so
they assert the same thing on a Windows desktop with no `ip` command as on the
rig. Live check: the banner and the `/api/status` network block both render.

`setup_network.sh` has **not** been run on a Pi. It is written to be idempotent
and non-destructive, and the DHCP-preserving `nmcli` behaviour is documented,
but that is an argument, not a test.

---

## 2026-09-03 (g) — raw stride padding, and grid parameters in sensor pixels

Reported from MATLAB:

```
the grid was aligned on a 728x544 frame and this capture is 1472x1088
(x2.0220 horizontally, x2.0000 vertically). Pitch has no single value
under an anisotropic rescale.
```

The refusal was correct. Two separate problems were behind it, and only one of
them was the one being asked about.

### 1. The raw frame is 1472 px wide and the image is 1456

**This is the cause of the error, and it is not a units question.** A raw
buffer's rows are padded out to a hardware-friendly stride, and picamera2's
`make_array` shapes the array by that stride rather than by the image width.
The IMX296 is 1456 px wide; 1456 is not a multiple of 32, the next one up is
1472, and an 8-bit raw frame therefore arrives as 1088 × **1472** with sixteen
columns on the right that are not image data.

`1472 = 46 × 32`, and `1456 = 45.5 × 32`. That arithmetic is the whole
diagnosis.

Left in, those columns do two things, both silent:

* the frame is 2.0220× the preview width against exactly 2.0000× its height,
  so any rescale of the grid onto it is anisotropic — the reported error;
* **the grid hangs off the frame centre**, and the centre of a 1472-wide array
  is 8 px right of the centre of the image. Every micro-image would land 8 px
  off — a quarter of a checkerboard square. Had the second fix below been made
  without this one, the error would have gone away and the detection would
  still have failed, for a reason nothing was reporting.

| change | file |
|---|---|
| `_trim_stride`: crop raw frames to the sensor width, record `raw_stride_px` / `raw_padding_px` / `image_width` in the metadata | `cameras/picam.py` |
| trim on load for files already on disk, using `camera.full_resolution` from the sidecar | `scripts/read_capture.py`, `matlab/tv_read_capture.m` |

Only trimmed when the height already matches and the excess is small enough to
be a stride pad. A *packed* raw format — 10-bit as 5 bytes per 4 pixels — has an
array width that is not a pixel count at all, so cropping it by pixels would be
nonsense; that case is recorded as `raw_unexpected_shape` and left alone.

Captures already on the drive read correctly without being rewritten, because
the sidecar records the true sensor size.

### 2. Grid parameters are sensor pixels now (option b)

The right call, and for a sharper reason than "that's where it matters".

Storing the grid in preview pixels puts a conversion between the stored value
and *every* consumer of it — the detector, the crops, the recorded corners, both
offline readers — so forgetting it anywhere is a silent factor of two. It has
been forgotten twice already in this project. It also means changing
`preview_resolution` silently invalidates a stored alignment.

Sensor pixels have no such dependency. The MLA pitch is a physical property of
the array, `pitch_um / pixel_pitch_um`, fixed by hardware. Everything that
*measures* works in sensor pixels, so for them the conversion disappears
entirely, and the only one left is drawing the overlay — where being wrong is
visible in the preview immediately rather than six months later in a fit.

| change | file |
|---|---|
| `note_frame_size` → `bind_sensor`: the reference is the sensor frame, declared once from the camera, never learned from a frame flowing through | `processing/stages/plenoptic.py` |
| `apply()` and `_masks()` draw from `geometry_for`, scaling **down** to the preview | `processing/stages/plenoptic.py` |
| `bind_sensor` called at `CameraRuntime.start()`, **and again after `_restore_state()`** | `app.py` |
| the tile-count check uses `geometry_for` (an identity now) | `calibration/settings.py` |
| `pitch_px` 50 → 100 in `config/desktop-plenoptic.yaml`; units documented in `config/pi.yaml` | configs |

The second `bind_sensor` call is not redundant. State is restored *after* the
cameras start, so a state file written when the parameters meant preview pixels
would otherwise overwrite what binding had just fixed — restoring a factor of
two from a file after everything upstream had been made correct. Verified
against the real `desktop-plenoptic.state.json`, which carried
`pitch_px: 50, reference_width: 728`.

**Migration is automatic and happens once.** A stored alignment carrying its own
reference is rebased onto the sensor frame with a warning:

```
mla: rebasing the grid from a 728x544 reference onto the 1456x1088 sensor
frame (pitch 50.000 -> 100.000 px). Grid parameters are sensor pixels now;
this happens once.
```

**Both readers are unchanged by this**, which is the sign the abstraction was
right and only the choice of reference was wrong: they convert from whatever
reference the sidecar records, so old and new captures both read correctly.

### Verification

```
pytest -q                       → 149 passed
ruff check src/ tests/ scripts/ → clean
tv_selftest, unpadded capture   → 19 passed
tv_selftest, padded capture     → 20 passed  (the extra one is the padding)
```

Nine new tests. The padding ones assert the failure directly: `preview.rescaled(1472, 1088)`
raises `anisotropic`, the trimmed frame gives exactly 100.00 px, and a
1472-wide geometry's origin is 8.0 px right of a 1456-wide one.

Reproduced the reported failure end to end by synthesising a 1472-wide capture
with a 728-referenced sidecar: both readers now trim 16 columns, report a clean
×2.0000, and recover 117 complete micro-images at 100 px.

Live: the rebase fires once per camera, readiness reports 117 and 130 whole
micro-images at 100 px on the sensor, and the overlay still draws one grid cell
per micro-image on the preview — the check that a units error could not survive.

---

## 2026-09-03 (f) — captures reached the disk as zero-byte files

Reported from the rig, writing to an external drive: the session directory, the
per-camera subdirectories and every filename were correct, `session.json` was
intact, and **every `.npy` and `.json` from the run was zero bytes**. Nothing
raised. Every capture had reported "saved".

### The mechanism

`close()` does not write anything to a disk. It returns as soon as the bytes are
in the kernel's page cache; writeback flushes them when it feels like it —
thirty seconds later by default (`dirty_expire_centisecs`), or never if the
power goes or the disk is pulled. File **metadata** takes a different route: on
a journalling filesystem the directory entry is durable long before the data is.

The two together produce a signature that looks like nothing else and points
straight at the cause: correct names, correct places, zero length. And it
explains the one file that survived — `session.json` is written at startup, so
writeback had had minutes to flush it, while the captures were minutes or
seconds old when the drive left.

Every write in the project was `close()`-and-hope: `np.save(path, ...)`,
`Path.write_text(...)`, in both the still writer and the calibration session.

### The fix

| change | file |
|---|---|
| `fsync_file`, `fsync_dir`, `write_durably`, `verify_size`, `EmptyWriteError` | `storage/writer.py` |
| `_write_image` serialises to memory, writes the buffer, fsyncs, verifies the exact length | `storage/writer.py` |
| the sidecar and `session.json` go through the same path | `storage/writer.py` |
| pose frames, pose sidecars, the manifest and `poses.jsonl` likewise | `calibration/session.py` |
| `bytes` in every save response, shown next to every capture in the UI | `storage/writer.py`, `web/static/index.html` |

Three properties, in order of importance:

1. **Verified, not just flushed.** The size is read back off the filesystem and
   checked against the exact number of bytes written. A device that accepts
   everything and stores nothing now raises `EmptyWriteError` at the rig instead
   of producing a directory of empty files discovered a day later. This matters
   more than the fsync: fsync prevents the loss, verification prevents the
   *silence*.
2. **Both the data and the name.** Fsyncing a file says nothing about the
   directory entry pointing at it, so the containing directory is fsync'd too.
   Windows cannot open a directory, so that failure is logged at debug and
   ignored rather than failing captures on the dev machine.
3. **An empty write takes the existing recovery path.** `EmptyWriteError`
   subclasses `OSError` deliberately: a device that just silently discarded a
   frame is one the rest of the session must not be written to either, so the
   writer falls back to the internal disk and notes it.

Cost, measured: `save_still` for a 1456×1088 uint8 frame went to a **median of
20.4 ms** (p90 22.1, max 22.6) including the fsync. At 1 Hz, or at the rate a
person presses the space bar, that is not a constraint.

### Also: a pre-flight check

`POST /api/storage/verify`, and a **Verify** button on the storage panel. Writes
4 MB to the active session directory, flushes it, reads every byte back and
compares. Catches a mount that accepts writes and stores nothing, a full or
read-only filesystem, and a device too slow to hold the capture rate — before a
session rather than after. It does not prove the disk survives being unplugged;
nothing short of unplugging it does.

Rollback: `write_durably` reverts to `path.write_bytes(payload)` and
`verify_size` to `return path.stat().st_size` — that restores the old behaviour
exactly, without touching any call site.

### Verification

```
pytest -q                       → 143 passed
ruff check src/ tests/ scripts/ → clean
```

Seven new tests in `tests/test_storage_devices.py`, and they were
**mutation-tested**: reverting `write_durably` to a plain write and disabling
the size checks — that is, restoring the exact pre-fix behaviour — fails five of
them. A test for a data-loss bug that passes against the buggy code is worth
nothing, so this was checked rather than assumed.

End to end in the browser: quick-record wrote 1.51 MB per frame, the size
appears beside every capture, and Verify reported 4 MB read back identically at
86 MB/s and survived the storage panel's re-render.

---

## 2026-09-02 (e) — quick-record, and the MATLAB reading path

Still nothing detected on the rig after (d). Rather than keep debugging the
on-board detector blind, two changes that make the question answerable and make
progress possible without answering it first.

### Quick-record in imaging mode

A checkbox in the header; the space bar then saves a raw set from both cameras.
No detection, no gate, no decision — every press is written. It produces exactly
the files the automatic loop produces, minus the pose manifest.

| change | file |
|---|---|
| `#quick-record` checkbox, `#quick-tally` counter | `web/static/index.html` |
| `quickShot()`, `QUICK_N` / `QUICK_BUSY` / `QUICK_PENDING` | `web/static/index.html` |
| the global keydown handler now branches on mode | `web/static/index.html` |

Rollback: remove the label from `#live-actions` and the `MODE === "live"` branch
from the keydown handler. Nothing server-side changed — it posts to the existing
`/api/capture-all/raw`.

Three things it had to get right, all found by testing rather than reasoning:

* **A focused checkbox eats the space bar.** After clicking the box it holds
  focus, the browser toggles it on space, and the keydown handler correctly
  ignores keys aimed at form controls — so the first press turned the feature
  back off. Every checkbox sharing a page with a space-bar shortcut now blurs
  on change (`releaseSpace`), including `#cal-sound` and `#cal-peaks`, which had
  the same latent bug.
* **Auto-repeat.** A held space bar fires keydown about thirty times a second.
  `ev.repeat` stops it.
* **Presses were being dropped.** A raw pair took 0.6–2.2 s to write in testing,
  which is inside the interval a person presses at, so roughly one press in four
  was lost — silently, and indistinguishably from a press that worked. Now
  queued one deep rather than dropped. The two-tone beep marks the completed
  write, and the log line says to wait for it.

### matlab/ — the offline path in MATLAB

Base MATLAB only, no toolboxes, and it runs unmodified under Octave (which is
what it was tested against).

| file | what |
|---|---|
| `tv_read_npy.m` | a real `.npy` parser: v1/v2/v3 headers, big- and little-endian dtypes, C and Fortran ordering |
| `tv_read_capture.m` | image + sidecar, with the MLA geometry **rescaled** to the frame in hand |
| `tv_micro_images.m` | M×N cell of X×Y micro-images, de-rotated onto the lattice axes |
| `tv_sub_apertures.m` | the permute to X×Y sub-aperture images of M×N |
| `demo_read_capture.m` | the whole chain with figures, base graphics only |
| `tv_selftest.m` | 19 assertions, headless |
| `README.md` | the distinction, the traps, the coordinate convention |

Two ordering traps, both silent, both real:

* **C vs Fortran order.** NumPy writes the last axis fastest, MATLAB's `reshape`
  fills the first dimension fastest. Reading the bytes straight into
  `reshape(v, shape)` returns the transpose with no error.
* **`meshgrid` argument order.** MATLAB's varies its first output along columns;
  NumPy's `indexing='ij'` varies its first along rows. Writing the resampling
  grid the NumPy way transposed every de-rotated tile while every dimension
  still matched — invisible to any shape check. This was a live bug in the first
  version, caught only by comparing against `MLAGeometry.crop_derotated` on the
  same file: mean difference 83.7 of 255. `tv_selftest` now pins it with a
  coordinate-ramp assertion that has a closed form, and that assertion was
  mutation-tested (reinstating the bug fails it with a 69 px error).

### Verification

```
pytest -q                       → 133 passed
ruff check src/ tests/ scripts/ → clean
tv_selftest, left  camera (0°, scale 1.0)  → 19 passed, 0 failed
tv_selftest, right camera (2°, scale 0.9)  → 19 passed, 0 failed
```

Cross-language: the de-rotated centre tile from `tv_micro_images` versus
`MLAGeometry.crop_derotated` on the same file — **max difference 0**, and the
geometry agrees exactly (pitch 100.000000, rotation 2.0000, side 90, 129 whole
lenslets, centre at (727.5, 543.5)).

Quick-record was driven headless: armed, four spaced presses recorded four sets,
a held key added none, an immediate second press queued rather than vanished,
and disarming stopped it. No console errors.

---

## 2026-09-02 (d) — "no board is being noticed", and an offline reader

Reported from the rig: calibration starts, the preview runs, and nothing is
ever detected. Three separate defects, and the first is the one that mattered.

### 1. The presence stage shipped switched off

`config/pi.yaml` carried `checkerboard_presence` with `enabled: false`. A
disabled stage is a legal no-op, so there was no error anywhere: the page
started, the preview ran, the capture loop sat in SEARCHING for ever, and
nothing said why. `config/desktop-plenoptic.yaml` had it on, which is why it
was never seen in development.

| change | file | rollback |
|---|---|---|
| `enabled: true`, and `min_contrast: 40.0`, on both cameras | `config/pi.yaml` | set `enabled: false` again |
| three new **blocking** readiness checks — `{cam}.presence` (stage missing), `{cam}.presence_enabled` (stage off), `{cam}.presence_bound` (stage not reading the MLA grid) | `calibration/settings.py` | delete the three checks |

The checks are the real fix. A configuration that cannot detect anything now
refuses to start the capture loop and names the reason, instead of running
silently. Two tests cover them:
`test_missing_presence_stage_blocks`, `test_disabled_presence_stage_blocks`.

### 2. The detector had no absolute threshold

`PresenceDetector` gated peaks only on `rel_threshold` × the frame's own
maximum. That is scale-free, so it normalises whatever is in front of the lens
up to "detected": sensor noise at σ = 2.5 grey levels produced 11 500 peaks per
frame and put ~10 micro-images in 100 over a count threshold of 20.

Added an absolute floor, quoted as a **minimum corner contrast in grey levels**
because that is a statement about the board and the light, not about the code.
The bridge is measured, exact to three figures over the 8-bit range: an ideal
step corner of contrast `C` gives `S = (1.061·C)²`, so `min_contrast = 40`
means `S > 1800`. A printed board gives 130+.

| change | file | rollback |
|---|---|---|
| `SADDLE_PER_LEVEL`, `PresenceDetector.min_contrast`, `.floor`, the second gate in `saddle()` | `calibration/presence.py` | set `min_contrast=0.0` to get the old behaviour without editing code |
| `PresenceMap.best_contrast` / `.contrast_floor`, and a `diagnose()` branch distinguishing "no structure at all" from "structure below the contrast floor" | `calibration/presence.py` | — |
| `min_contrast` stage parameter, surfaced in the UI | `processing/stages/plenoptic.py` | — |

### 3. A test that measured the wall clock

`SyntheticSource` derived its scene phase from `time.monotonic()`, so the
`gratings` negative case drifted between runs and its saddle count crossed the
threshold at some phases and not others. Two discrimination tests failed about
one run in three, and the flake looked like the detector.

`synthetic_drift_px == 0` now means a **static scene** — the phase is pinned to
zero — not merely a board that does not translate. Guarded by
`test_the_scene_is_static_with_drift_off`.

| change | file | rollback |
|---|---|---|
| `SyntheticSource._phase()`, used by `read_preview` and `capture_full` | `cameras/offline.py` | inline the old `(now - t0) * 0.4` expression |

### Also added

| what | why |
|---|---|
| `scripts/read_capture.py` | Reads a recorded `.npy` + `.json` pair from either camera, in view or raw mode, and does off-Pi what the rig deliberately does not: full-field `findChessboardCornersSB` over every micro-image. `--show --save --grid --corners --tile I,J --zoom --detect --board CxR --csv --json`. Verified on real recorded poses: 117/117 micro-images, 77 complete crosses, 15.99 px squares, 8892 corners exported. |
| `/calibration/peaks/{cam}.jpg` and `presence.peaks_overlay()` | "0 of 117 micro-images see the board" is a number; what is needed when it is wrong is a picture. Peaks marked, per-tile counts written in each box. Distinguishes an alignment error from a too-coarse board from a lens cap at a glance. |
| the diagnosis line under each preview | `PresenceMap.diagnose()` in words: best tile against the threshold, median, total peaks, and what the configured board *should* give. |

### Verification

```
ruff check src/ tests/ scripts/   → clean
pytest -q                         → 133 passed  (three consecutive runs, for the flake)
```

End to end against `config/desktop-plenoptic.yaml`, headless: preconditions
met, session armed, pose 1 recorded at 16.0 px per square, phase advanced to
MOVE, peaks view and diagnosis rendering, no console errors.

One thing that is **not** a bug and cost a confusing minute: with the board
spec left at its 9×6 default the five-tile cross check reports `0/5` on a rig
whose target is 4×3. The presence map is board-size-agnostic and reads 117/117
at the same moment. Set the board size before blaming the detector.

---

## 2026-09-01 (c) — the capture loop, and the actual cause of the crash

`stress-ng --cpu 4` ran on the rig without a crash and barely spun the fan.
That eliminated power and thermal, which had been the leading hypothesis, and
left the only other candidate: **a second consumer of the camera**.

### The cause

`CameraSource.read_full_mono()` called `picamera2.capture_request()` from a
detection worker's own thread, at 1 Hz, while the preview loop called it at
30 Hz. Two consumers of a four-deep request pool across three full-resolution
streams. The CPU limits added in (b) reduced the load and did not help, because
load was never the mechanism.

### What replaced it

| removed | replacement |
|---|---|
| `CameraSource.read_full_mono()` and its picamera2 override | `request_full_frame()` / `wait_full_frame()`. The capture loop pulls `main` out of the request it is **already holding**; nothing else ever touches the device. |
| `DetectionWorker` (thread per camera, full-field corner detection) | `calibration/presence.py` as a pipeline stage on preview frames, plus a five-tile cross check once per pose |
| `DetectionSpec.interval_s / max_tiles / max_duty / concurrent_cameras / overlay` | no longer meaningful — nothing runs on a loop of its own. The remaining `DetectionSpec` fields are the two detector flags. |
| `/api/calibration/detection`, `/calibration/detection/{cam}.jpg` | `/api/calibration/session`, `/calibration/shot/{cam}.jpg` |

A side benefit worth naming: the full frame now shares a sequence number with
the preview frame it arrived with. They are one exposure.

### New

| file | what |
|---|---|
| `calibration/presence.py` | saddle counting — the cheap live detector |
| `calibration/session.py` | the hands-free state machine and the recorder |
| `scripts/benchmark_detectors.py` | the cost table, on whatever machine runs it |
| `CaptureSpec` in `calibration/settings.py` | every gate in the capture loop |
| `checkerboard_presence` stage | must sit **after** `mla_grid_overlay` in the pipeline |

`config/pi.yaml` gains the presence stage, disabled. Enable it when the MLA is
aligned.

### Two bugs found while testing this, both worth knowing

**Counting noise read as movement.** A saddle count fluctuates by one or two per
micro-image between frames, and across 130 tiles that sums to about the size of
a real stillness threshold. The loop sat at "0/4 still" indefinitely with
nothing moving. Fixed with a dead band (`COUNT_NOISE = 2.0`) applied before the
comparison; there is a test.

**The preview must resolve the board's squares.** At an eighth scale a
micro-image is 12 px across and its squares are two, which no saddle detector
finds — and the symptom is the console asking for a board that is plainly in
shot. Now a blocking precondition (`MIN_PREVIEW_TILE_PX = 24`).

### Rollback

Not committed. `git checkout .` reverses everything. If only the capture loop is
suspect, `checkerboard_presence` can be disabled in the config and the imaging
mode is untouched — the presence stage is the only thing that runs during
normal streaming.

### Verification

`ruff` clean; 128 tests pass (41 new). A full session was driven through the
browser against the synthetic lenslet array: searching → hold → checking →
captured → move → captured, three poses written, space forcing a shot, escape
discarding one, tones firing once each, and the files on disk carrying corners
in sensor coordinates with the frozen geometry beside them.

---

## 2026-09-01 (b) — after the first run on the rig

Two field failures: starting calibration took the Pi down, and an external disk
was never offered. Both turned out to be missing bounds rather than broken
logic, so the changes are additive and each one can be turned off in the UI.

### Detection can no longer ask for unbounded work

| change | where | revert by |
|---|---|---|
| `max_tiles` (320) — a blocking precondition, re-checked inside the worker | `calibration/settings.py`, `calibration/detect.py` | raise the limit in the Detection panel |
| `concurrent_cameras` (1) — one shared semaphore, so cameras take turns | `app.py`, `calibration/detect.py` | set it to 2 |
| `max_duty` (0.5) — a worker sleeps until its busy fraction is under this | `calibration/detect.py` | set it to 1.0 |
| `nice(10)` on detection threads | `calibration/detect.py` | no switch; delete the two lines in `_run` |

The old behaviour is `max_tiles=4000, concurrent_cameras=2, max_duty=1.0`,
which reproduces exactly what ran on the rig.

### Host health

New `src/trilobite/health.py`, read-only: CPU temperature, load, free memory,
and the decoded bits of `vcgencmd get_throttled`. Surfaced in `/api/status` and
in the header. It has no effect on behaviour — remove the `"health"` key from
`Application.status()` to drop it.

`scripts/diagnose_host.sh` is new and standalone; nothing calls it.

### Storage now sees unmounted disks

The enumerator read only `/proc/mounts`, so a disk that nothing had mounted did
not exist as far as it was concerned — which is the normal state of a USB drive
on a headless Pi. It now merges `lsblk`, falls back to `blkid` when udev has
not recorded a filesystem type, and offers a Mount action via `udisksctl`.

`udisks2` and `ntfs-3g` added to `apt-packages.txt`. Without `udisks2` the disk
is still listed; only the Mount button stops working.

### Verification

`ruff` clean; 99 tests pass (16 new). The storage path was exercised against a
real loopback ext4 filesystem through the browser: discovered unmounted,
mounted, selected, captured to, released, unmounted. The tile guard was
exercised by setting a pitch that yields 6097 tiles and confirming the button
refuses with that number in the message.

---

## 2026-09-01 (a) — leftovers from the flyeye rename, and unused code

Not committed. The whole change set is in the working tree, so
`git status` lists it and `git checkout .` reverses all of it at once.

### Deleted directories and files

These are dead: nothing imports them, nothing references them, and the tests
pass with them gone.

| path | why it is dead |
|---|---|
| `src/flyeye/` (18 files) | the pre-rename copy of the package. `src/trilobite/` replaced it in full; `pyproject.toml` only packages what is under `src/`, and only `trilobite` is imported anywhere. The two trees diverged the moment the rename happened, so the flyeye copy is not a backup of anything current. |
| `systemd/flyeye.service` | superseded by `systemd/trilobite.service`, which names the right venv, module and data directory. |
| `docs/calibration-strategy.md` | the first, exploratory calibration write-up. Superseded by `docs/calibration-spec.md`, which is the same material reorganised as a process and corrected in three places (the units bug in eq. 4, the claim that λ is depth-dependent, the claim that de-rotation invalidates corner measurements). Keeping both invites reading the wrong one. |

Deleting a directory is not something this session can do on the desktop, so
these three are removed by hand:

```powershell
cd "C:\Users\30067913\OneDrive - Western Sydney University\Projects\TrilobiteVision"
Remove-Item -Recurse -Force src\flyeye
Remove-Item systemd\flyeye.service
Remove-Item docs\calibration-strategy.md
```

To restore any of them: `git checkout -- src/flyeye` (and so on).

### Removed code

| what | where | why |
|---|---|---|
| `FrameQueue`, `QueueStats` | `bus.py` | Written for a recorder that does not exist. Never instantiated, never tested. An untested queue nothing calls is worse than no queue: it reads as a decision already made. The module docstring now says what shape recording will need and that it is not here. |
| `NAMED_SUBAPERTURES` | `optics/mla.py`, `optics/__init__.py` | A tuple of the five names `named_indices()` can resolve. `named_indices()` builds its own `targets` dict, so the constant was never read by anything. The information it carried is now a comment on `UI_SUBAPERTURES`, and `optics/__init__.py` exports `UI_SUBAPERTURES` instead — which *is* used, by both the overlay and the web layer. |
| `AppConfig.camera_order` | `config.py` | Never called. `Application` iterates `cfg.cameras` directly. |
| `AppConfig.camera(cam_id)` | `config.py` | Never called. Lookup by id happens on `Application.camera()`, against the live runtimes, which is the one callers actually want. |

### Considered and deliberately kept

| what | why it stays |
|---|---|
| `lenslet_extract` / `LensletExtract` | Registered but raises `NotImplementedError`. Harmless — `Pipeline` catches it and the frame still comes out, and there is a test for that. Its docstring holds a real unmade decision (widen `Frame` to N-dimensional arrays, or introduce a `LightField` type), which is worth keeping where the code will be written. |
| `Pipeline.reset()` | Not currently called from anywhere — `MLAGridOverlay` calls its own `reset()` directly. Four lines, and the obvious aggregate of `Stage.reset()`, which *is* used. Removing it would leave the per-stage hook looking orphaned. |
| `CameraSource.get_controls()` | Called only from tests today, but it is the ABC's answer to "what is the sensor actually doing", distinct from `requested_controls()`. The AE tests depend on that distinction. |
| `scripts/measure_derotation_cost.py` | A one-off measurement, but it is the evidence behind a spec claim (`calibration-spec.md` §2.6) and behind the de-rotation readiness check being advisory rather than blocking. Keep it runnable so the number can be re-checked. |

### Verification

```
ruff check src/ tests/     → clean
pytest -q                  → 83 passed
```

The three deletions touch no import path, so the test result is the same before
and after them. If something does break, the likely culprit is the code
removal, not the file removal — `git checkout -- src/trilobite/bus.py
src/trilobite/config.py src/trilobite/optics` restores just that half.
