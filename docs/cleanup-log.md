# Cleanup log

Removals and behavioural changes, and how to undo them. Newest first.

Everything here is recoverable from git, because nothing is removed that was
not already committed. The general rollback is:

```powershell
git checkout -- <path>      # one file or directory back to HEAD
git checkout .              # everything back to HEAD
```

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
