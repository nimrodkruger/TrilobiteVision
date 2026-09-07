# Research-rig implementation plan — finish still metadata, then enable video

6 September 2026. **This is an exploratory instrument, not a product release.**
Prefer small changes that prevent lost recordings, misleading data or an
unresponsive rig. Keep the current stack and avoid frameworks built for
hypothetical future requirements.

This plan supersedes the earlier ten-stage plan and `stage-3-contract.md` for
remaining work. The complete previous plan, including its latest Stage 3 notes,
is preserved in [the historical plan](implementation-plan-before-video-rescope.md).
Stage numbers below are the current ones: **Stage 5 now means video activation**.
The old storage/transaction/lifecycle stages are not separate prerequisites;
only the protections video actually needs are included here.

## Current position

Stages 0–2 provide the existing test, status and raw-format foundations.
Stage 3 routes stills and controls through the capture thread and reports a
failed stop without closing a busy camera. Keep the reduced design.

[The Stage 3 review](stage-3-review.md) identifies three local corrections:
prevent a second worker after failed-stop/repeated-start; synchronise command
completion/timeout and stop admission; clean up failed camera startup. Local
suite: 456 passed, 2 skipped. Pi, browser and MATLAB checks are not established
by that result. No automatic camera reconnection is promised.

## Stage 4 — small corrections and trustworthy saved settings

**Outcome:** a saved image describes the settings that produced it, and a
failed camera cannot accidentally acquire a second worker. No new dashboard
workflow or general metadata framework is needed.

### Instructions to the implementation agent

1. Make the three local corrections in the Stage 3 review. Show `failed-stop`
   in plain language in the existing camera/status display, with the supported
   recovery action. Do not add automatic reconnect or a new process model.
2. Attach a copied record of processing settings and applied orientation to
   each processed frame. Make processing and parameter updates agree on one
   set of values for that frame. Use the existing objects and locks or a small
   per-frame parameter copy; do not build an immutable pipeline framework.
3. Save that attached record instead of reading current settings at save time.
   Raw images should retain their existing source/format/sensor metadata and
   clearly say that display processing was not applied. Keep requested sensor
   settings separate from sensor-reported values; unknown means unknown.
   Retain existing validity/reservation information without inventing evidence.
4. Preserve existing JSON fields where practical; add only the small fields
   needed above and a format version if structure changes. No full archive
   migration, replay reconstruction, command audit database or reader rewrite.

### Enough verification

- A blocked old worker prevents another start; normal stop/start still works.
  Test startup cleanup and one controlled completion/timeout race.
- Save the same retained preview before and after editing a parameter: its
  pixels and recorded processing settings stay the same. A newly processed
  frame carries the new settings. Exercise an edit during processing too.
- Run the existing suite and a short dual-camera still/control session on the
  Pi. Check the real service-stop behaviour. Document any remaining reboot
  requirement rather than claiming it has been fixed.

Complete these corrections, then proceed to short video trials. No additional
formal architecture approval or full lifecycle soak is required.

## Stage 5 — video recording: operator overview

**Set each camera up in its image tab, then use Video to record.** Video shows
a read-only summary of those settings; it does not duplicate exposure, gain,
orientation or alignment controls. Before Start, the summary follows current
settings. At Start, the recording uses a fixed copy. Settings affecting the
recorded picture are locked until the recording has finished closing its files,
including changes attempted from another browser tab.

| Control | What the researcher sees |
|---|---|
| Cameras | Left, right or both; both selected by default. An unavailable selected camera prevents Start rather than being silently omitted. |
| Recording length | A duration in seconds and an always-available Stop button. Begin with short trials; proposed initial default 10 seconds and maximum 60 seconds, extended only after a successful rig test. |
| Recording frame rate | Requested frames per second, up to the inherited sensor rate and the tested recording limit. Lower rates reduce data volume without changing the sensor setup. Show actual recorded rate as well. |
| Compression | Off / **lossless**. Off makes larger files; lossless aims to make them smaller without changing the saved pixel values. No quality slider or lossy mode in the first version. |
| MLA on/off | Proposed interpretation: show/hide the alignment grid on the Video preview only. Saved pixels stay free of drawn grid lines; alignment parameters are recorded in JSON. This is an explicit assumption pending clarification, not a requirement for extracted sub-aperture movies. |
| Destination | External disk name, output folder, free space and an approximate clip size. Start is unavailable unless a writable external disk is selected and has enough room. |
| Internal-storage override | Checkbox: **“Allow this recording on internal storage.”** Unchecked by default and reset after every recording and page/application restart. It applies only to a deliberately selected internal destination, never to fallback after a disk disappears. |
| Start / Stop | Ready → Recording → Finishing → Saved, or a clear Stopped early / Failed message. Keep recording when switching tabs or losing the browser connection; the duration limit still applies. |

The rate limit and default compression mode should be the ones demonstrated
to work on this Pi, not a promised 30 fps. The controls keep the requested
uncompressed option, but unsupported combinations are visibly unavailable
rather than silently changing pixel depth or recording format.

### Useful information, without a crowded dashboard

Always show elapsed/remaining time, actual saved FPS and missed-frame count
for each selected camera, bytes recorded, free disk space and the current
recording state. Label intentional lower-rate sampling separately from missed
frames. Show a short reason when Start is unavailable or recording stops early.

Keep one small read-only summary per head: exposure/gain (and whether automatic),
sensor resolution, output resolution/bit depth and orientation. An optional
clip label is useful for identifying experiments. Detailed queue, CPU and
encoder statistics belong in logs or a collapsed diagnostic section, not in
the normal control panel. No extra codec, buffer-size or thread-count knobs.

After finishing, show the actual duration, saved frames, any losses and the
output filenames. A two-camera recording produces separate matching pairs:

```text
experiment_001_left.avi     experiment_001_left.json
experiment_001_right.avi    experiment_001_right.json
```

Both pairs share a recording ID. A joint Start is convenient but does not make
the camera exposures simultaneous. Do not combine the two images into one
side-by-side AVI for the first version.

### Behaviour on problems

- If storage is missing, read-only or too full, do not start. Keep a reserved
  amount of free space, including when internal storage is explicitly allowed.
- If the selected disk disappears or writing cannot keep up, stop the recording
  and explain why. Never continue AVI output onto the internal card. Close and
  retain any usable partial files on the original disk where possible.
- If one selected camera fails during a dual-camera clip, stop the clip for
  both and identify the failed head. This keeps the first version predictable.
- Disable disk Release/Change while recording or finishing. “Saved” means files
  have been closed successfully, not merely that the timer ended. An interrupted
  AVI may be unusable; do not promise power-loss recovery in this version.

## Stage 5 — technical guidelines for good practice

These guide implementation choices; they are not a prescribed class design or
a list of mandatory new subsystems.

**Reuse the owner, not repeated still requests.** Video should receive copied
frames from the existing camera acquisition path before preview throttling.
The current `skip_preview()` intentionally discards requests; enabling video
needs a recording branch there. A separate encoder/writer can consume a small,
bounded queue after SDK buffers are released. It should not call the camera
itself, nor should video be reconstructed from MJPEG/browser frames. If the
writer falls behind, explicit early termination is a simple initial policy.

**One active recording and fixed settings are enough.** Enforce these on the
server, including settings, still/burst/calibration capture and storage actions
from other tabs. Preview viewing may continue; stop or temporarily refuse
competing capture jobs. Snapshot current camera settings and video options at
Start. If automatic exposure remains enabled, values may vary even though
configuration is locked: record actual per-frame values where available.

**Decide the saved pixels before choosing the encoder.** The recommended
research default is full-resolution sensor data with the image-tab orientation,
without display contrast enhancement or burned-in grid lines. State the actual
stream, transformations and bit depth in the UI/JSON. “Uncompressed” does not
mean raw sensor data if an ISP stream or an 8-bit conversion was used. Keep any
unvalidated data labelled as such; do not promote it because encoding worked.

AVI is a container, not a guarantee of pixel fidelity. FFV1 is a lossless codec
candidate with AVI mapping in its specification, but the chosen AVI/codec/pixel
format must work in the actual Pi encoder **and** the intended offline reader.
The first feasibility check is a short encode/decode comparison, including
known nonzero low bits in 16-bit samples: dimensions and every pixel should
round-trip exactly in lossless/uncompressed modes. Do not silently convert
10/16-bit data to 8-bit to make a writer open. If a mode cannot pass, disable
that combination and state the limitation. Lossless compression may consume
more CPU and may save little space on noisy frames; measure it on this rig.
Sources: [FFV1 specification](https://github.com/FFmpeg/FFV1/blob/master/ffv1.md),
[FFmpeg AVI documentation](https://www.ffmpeg.org/ffmpeg-formats.html#avi).

**External-only is a server rule, not just a checkbox.** Verify the actual
mounted device backing the destination, excluding the system-storage device;
a different folder or a `/media` prefix is not evidence of an external disk.
USB SSDs may not advertise themselves as removable. Keep the selected mount/
device identity fixed for the clip and recheck it on output creation and error
handling. Do not create a replacement directory under a vanished mount when
opening AVI parts or writing the final JSON. Keep video away from the still
writer's automatic internal fallback. An override defaults false server-side,
is explicit for one recording, and does not authorise mid-clip fallback.

**Protect free space and finish honestly.** Estimate uncompressed demand from
output dimensions, bytes per pixel, FPS, duration and selected heads; treat
compression savings as uncertain. As a scale example, two 1456 × 1088 streams
at 30 fps stored as 16-bit pixels are about 190 MB/s before overhead, or 11.4 GB
per minute. Free space does not prove sustained disk speed. Use a conservative
reserve and a tested rate/duration limit. Close the encoder before reporting
success; handle disk-full, write/encoder failure and Stop explicitly. Locking
the destination is simpler than supporting a mid-clip disk swap.

**Keep JSON useful and modest.** A matching sidecar can contain recording/head
IDs, copied settings, raw-layout/validity information, output codec/pixel format,
compression and MLA choice, requested/actual duration and FPS, frame count,
known losses and completion/stop reason. Include an ordered mapping from AVI
frame index to available source sequence/timestamp, with units/clock source;
include actual exposure/gain when available. This is especially useful for
active vision: fixed-rate AVI playback does not prove uniform acquisition
timing. Missing sensor timing or losses that cannot be observed stay unknown.

For initially short, capped clips a per-frame list in JSON is sufficient; no
database or separate event service is needed. Write a small “recording” sidecar
at the beginning and update it after successful close or a handled failure.
If the disk is gone, retain the error in normal application logs/status rather
than claiming the final sidecar was saved there. No per-frame file sync,
transaction framework or promise to repair arbitrary power cuts is required.

**A small acceptance set is enough.** Demonstrate one short clip per available
encoding mode, one dual-camera clip at the intended maximum load/duration,
normal Stop and automatic duration stop. Verify pixel round-trip and timing
metadata offline. Exercise absent/full/lost storage, a slow writer, camera
failure and an unchecked/explicit internal override through the server API.
Confirm no AVI or replacement sidecar reaches internal storage on external
failure, settings cannot change mid-clip, and the UI remains responsive while
files finish closing. If AVI size limits are reached during these trials,
cap duration or use simple numbered AVI/JSON parts; do not silently truncate.

## Explicitly deferred

Full transactional still capture, general storage refactoring, automatic
camera reconnect, process isolation without a demonstrated need, replay
reconstruction, multi-user ownership, authentication redesign and long-duration
recording infrastructure. Legacy packaging cleanup and CI activation remain
housekeeping, not reasons to block a controlled bench trial. Optical/noise
investigation remains separate; recording must not quietly denoise sensor data.
