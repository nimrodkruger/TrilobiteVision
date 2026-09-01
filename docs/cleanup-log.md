# Cleanup log

Removals and behavioural changes, and how to undo them. Newest first.

Everything here is recoverable from git, because nothing is removed that was
not already committed. The general rollback is:

```powershell
git checkout -- <path>      # one file or directory back to HEAD
git checkout .              # everything back to HEAD
```

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
