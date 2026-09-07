# Stage 3 review — proportionate to a research rig

Reviewed 6 September 2026: HEAD `4506165` **plus the uncommitted Stage 3
working-tree changes**, including `acquisition.py`, `app.py`, `web/server.py`
and their tests. This review does not change application code.

**Decision: keep the reduced implementation. It is a useful improvement, with
three small reliability corrections before video activation.** The previous
contract was broader than this exploratory rig needs. The current
`implementation-plan.md` supersedes that contract as the remaining work list.

## What improved

Stills and sensor controls now run through the existing capture thread. The
queue is finite, callers have time limits, and disk writes happen after the
camera operation. Capturing both heads no longer puts the first disk write
between their acquisitions. Stop no longer closes a camera while its worker
is known to be alive. Keep these changes.

Opening before the worker starts and closing after it has actually terminated
is an acceptable ownership handoff for this rig, subject to restart guards and
cleanup. A new thread/process framework, control coalescing, elaborate command
histories and full replay reconstruction are not prerequisites for short clips.
Keeping the existing full-frame handshake is also reasonable while it has one
camera consumer and a bounded wait. Do not remove it just to match the old plan.

## Small corrections that matter

| Priority | Finding | Minimum practical correction |
|---|---|---|
| Before video | `CameraRuntime.start()` does not check whether `_thread` is still alive. After `stop()` returns `failed-stop`, it clears the shared stop event and starts another worker against the same source. | Refuse start while the previous worker is alive, including repeated start calls. Keep start/stop transitions mutually exclusive. No generation framework is necessary if overlapping lifetimes are prohibited. |
| Before video | `Command._resolve()` describes its check/set as atomic, but has no lock. Completion and timeout can both observe `queued` and both report success. | Protect outcome transitions with a small lock. Distinguish work that never started from work already executing; coordinate queue admission with retirement so stopped owners cannot receive stranded work. Keep the existing small outcome vocabulary. |
| Before video | `Picamera2Source.open()` cleans up selected format errors but not failures throughout configure/control/start. The device is local until startup succeeds. | Close partially opened devices on all startup failures, and report cleanup failure honestly. Do not report a stopped/closed device merely because a close exception was logged. |

The first two are reproduced with temporary probes against the actual classes:

```text
AFTER STOP failed-stop old thread alive True
RESTART ALLOWED True both alive True lifecycle running
RESOLVE RACE [('abandoned', True), ('done', True)] final done
```

The restart probe uses a blocked synthetic source and releases/joins both
workers afterward. The resolution probe forces a thread switch after both
callers have checked the previous state: a deterministic demonstration of the
race, not an estimate of how often it happens. Startup cleanup is a source
inspection finding, not a reproduced physical-camera failure.

## Limits to describe accurately

- This reduces unsafe concurrent camera access; it **does not implement
  automatic recovery from a disconnected or permanently blocked camera**.
  `failed-stop` is exposed in API status, but the dashboard does not explicitly
  render that field. Show a plain explanation and the supported recovery action.
  Test stopping the service on the Pi; only consider process isolation if the
  remaining inability to stop is demonstrated there.
- `assert_owner()` is tested as a helper but is not called by the production
  acquisition paths. The test logs the read/still/control calls, not the entire
  SDK lifecycle. Do not describe it as an enforced SDK-wide runtime backstop.
- A raw still still takes a separate request from preview. Do not promise that
  it is the currently displayed exposure. The two heads also remain sequentially
  requested and free-running; submission can wait longer than one frame period.
- The measured acquisition rate omits still requests, so it is not full sensor
  exposure accounting. Video needs its own delivered/written-frame counts.
- Saved still/preview metadata still reads live pipeline settings at save time.
  This is the small, concrete Stage 4 task; no general provenance platform is
  required to correct it.

## Verification

Local suite: **456 passed, 2 skipped**, 63.02 seconds, with two dependency
deprecation warnings. Command: `.venv-review/Scripts/python.exe -m pytest -q`
with `PYTHONPATH=src`. Playwright is absent here; no browser, MATLAB or Pi
acceptance run is claimed. The cleanup log's different count is historical.

Some tests substantiate less than their names suggest: the slow-disk test does
not inject a slow writer, and the capture-all test checks files rather than
the acquisition/write ordering. The code does move writes after acquisition,
but video still needs one actual slow-writer test and a short dual-camera rig
run. Add focused tests for the three corrections above; no further broad
mutation campaign is needed for this stage.

## Video readiness

The Video tab is currently a placeholder. The existing `CaptureSession` is a
calibration-pose recorder, not an AVI recorder to switch on. The existing
storage writer deliberately falls back to the configured internal root, and
`devices.is_mounted()` checks writable ancestry rather than disk identity.
These are unsuitable as the permission check or recovery policy for external-
only video. A small video-specific storage guard is essential; rewriting all
still storage is not.

The cleanup log also reports a raw byte-interpretation correction related to
the row-noise complaint. That is a plausible software artefact mechanism, not
evidence that the rig's noise is now resolved. Retain the independent raw-image
comparison as a separate bench investigation rather than making it part of
the recording interface redesign.
