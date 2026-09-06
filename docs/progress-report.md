# TrilobiteVision — software stack, orientation and progress

**For:** an incoming supervisor with a software and systems background.
**Purpose:** what the software is, why it is built this way, and where it
stands.
**Date:** 5 September 2026.

**Scope.** This edition covers the software: acquisition, streaming,
networking, recording, and the user interface. The optics and the calibration
model are deferred to a second report — §11 says where they are documented in
the meantime, and nothing below depends on understanding them.

---

## 1. What the software has to do

Two cameras on one Raspberry Pi 5, driven from a browser, producing recorded
image data for offline analysis. Concretely:

- run two 1456 × 1088 mono global-shutter sensors at 30 fps;
- show both live in a browser over the lab network, with enough responsiveness
  to align optical hardware by eye against the image;
- expose every processing parameter as a live control, and record the value of
  every one of them alongside every image;
- write full-resolution, unprocessed, lossless captures to a removable disk,
  and be able to prove they arrived;
- do all of that on four ARM cores that also have to encode JPEG in software,
  because the Pi 5 has no hardware encoder.

One constraint shapes most of the decisions below: **the Pi captures and
decides; the desk measures.** Anything expensive runs offline from recorded
files. The Pi runs only what has to be live.

---

## 2. The stack, and what is deliberately not in it

| layer | choice | why this one |
|---|---|---|
| language | Python 3.11+ | picamera2 is Python-first; nothing here is compute-bound in Python — the heavy work is inside numpy, libcamera and the JPEG encoder |
| camera | **picamera2** / libcamera | the only supported path to a CSI sensor on a Pi 5 |
| web | **FastAPI** + **uvicorn[standard]** | one dependency for routing, validation and OpenAPI; uvicorn's threadpool handling matters (§4.2) |
| schema | **pydantic v2** | the load-bearing choice — see §7 |
| config | **PyYAML** | comments survive, which is most of the config file's value |
| arrays | **numpy** | from apt on the Pi, not pip |
| JPEG | simplejpeg → OpenCV → Pillow | tried in that order at import (§4.3) |
| front end | one hand-written HTML file, no framework | ~2,000 lines including CSS. A build step on a Pi deployed by `git pull` would be one more thing to be broken at the bench |
| offline | Python (`scripts/read_capture.py`) **and** MATLAB (`matlab/`) | the analysis is done in MATLAB; both readers exist and are checked against each other (§8) |

15,900 lines: 10,600 application, 3,900 tests, 2,200 scripts, 1,300 MATLAB.
Four runtime dependencies in `pyproject.toml`.

### 2.1 apt versus pip on the Pi, which is not a style question

`pyproject.toml` deliberately **excludes** picamera2, numpy and OpenCV. On the
Pi they come from apt:

- `python3-picamera2` is a C++ extension built against the *system* libcamera.
  There is no pip build that tracks it. Installing one produces import errors,
  or worse, a silent format mismatch.
- numpy and OpenCV come from apt so they stay ABI-compatible with picamera2's
  buffers. A pip OpenCV wheel alongside the apt one gives two `cv2` modules and
  an import order that silently decides which you get. It also avoids a
  40-minute source build on the Pi.
- Raspberry Pi OS is an externally-managed environment (PEP 668), so the venv
  is created `--system-site-packages`. Without that flag every camera import
  fails with a `ModuleNotFoundError` that reads like a broken install.

The desktop extra (`pip install -e '.[desktop]'`) pulls numpy, Pillow and
`opencv-python-headless` — headless because the GUI build drags in Qt and a
display stack nothing here uses. **That extra must not be installed on the
Pi.**

`apt-packages.txt` is the full list with a reason on every line, including the
diagnostics you want the first time something is wrong (`v4l-utils`,
`i2c-tools`, `libcamera-tools`) and the ones the storage panel depends on
(`udisks2`, `exfatprogs`, `ntfs-3g`).

---

## 3. Acquisition

### 3.1 One camera configuration, three streams

picamera2 is configured once per camera with three streams out of one request:

```python
picam.create_video_configuration(
    main  = {"size": full_resolution},                    # science path
    lores = {"size": preview_resolution, "format": "YUV420"},  # browser
    raw   = raw_stream,                                   # ISP bypassed
    buffer_count = 4,
)
```

- **`lores`** is what the browser sees. It is produced in parallel by the ISP,
  so previewing costs almost nothing on the camera side — the cost is the JPEG
  encode afterwards. It arrives as YUV420, and for a mono sensor the first
  `height` rows *are* the image, so the preview is a slice of the luma plane
  rather than a colour conversion.
- **`main`** is the full-resolution processed frame.
- **`raw`** is sensor data with the ISP bypassed, and it is what calibration
  needs. Its format is chosen explicitly rather than left to libcamera, for
  reasons in §9.

### 3.2 One thread per camera, and exactly one consumer

Each camera gets a capture thread that does three things and nothing else:
read a frame, run the processing pipeline, publish to a one-slot bus. It never
encodes, never writes to disk, never waits on the network. A slow browser or a
stalled disk cannot perturb capture timing.

**One thread should own the camera. Today, not quite.** An earlier design had
a detection worker calling `capture_request()` on its own schedule while the
preview loop did the same. Two threads pulling from a four-deep buffer pool,
one at 30 Hz and one at 1 Hz, took the rig down repeatedly — and a four-core
CPU stress test did *not*, which is how the camera path rather than the load
was identified.

The mitigation was a handshake: anything wanting a full-resolution frame raises
a flag, and the capture loop — already holding a request containing every
stream — pulls `main` out of *that same request* before releasing it. On that
path the full frame is the same exposure as the preview it arrived with, and a
request made while the camera is stopped never completes rather than raising
inside a thread with no way to report.

> **Correction (6 September 2026).** An earlier draft of this report said
> flatly that only the capture thread touches the camera. It does not.
> `CameraRuntime.capture_still` calls `source.capture_full()` on the API worker
> thread, and that calls `capture_request()` itself; sensor-control writes take
> the same route. Two mutexes serialise them, which may well prevent the
> original crash, but serialised access is not single ownership: request
> identity, cancellation and shutdown still span two paths, and the raw stills
> that calibration uses are **not** the same exposure as any preview. This is
> finding F1 of the external review, and Stage 3 of
> `docs/implementation-plan.md` replaces the flag with a command queue owned by
> the capture thread.

The loop also has an exponential backoff on exceptions (0.1 s doubling to 5 s),
so a persistent hardware fault does not spin a core while a transient one still
recovers immediately.

### 3.3 Three frame rates, which are not the same number

| rate | set by | governs |
|---|---|---|
| **sensor** | `cameras[].fps` = 30 | how often an exposure is produced. Must be drained at this rate whatever else happens, or the four-deep request pool starves and capture stalls. |
| **pipeline** | `cameras[].process_fps`, default = browser rate | how often the processing stages actually run. |
| **browser** | `server.preview_fps` = 12 | how often a JPEG is encoded and pushed. |

The reported symptom was "everything lags, including updating parameters". The
browser stream was *already* capped at 12 Hz. What was uncapped was the
pipeline, running the full stage chain on every sensor frame: 60 passes a
second across two cameras, on cores also encoding JPEG and answering the API.
The web thread lost.

Frames arriving early are now released without being decoded — the request is
taken and returned, metadata kept, nothing converted. `/api/status` reports all
three rates plus a `skipped` count per camera, so the cap can be seen working
rather than assumed.

One detail worth stealing: the deadline **accumulates** (`due = max(now, due +
interval)`) rather than restarting from now. 12 does not divide 30, so a
"now + interval" deadline always lands just after a frame and quantises down to
10 Hz. Accumulating makes the gaps alternate 100/67 ms and the mean come out at
the rate that was asked for; the `max` drops the arrears after a stall so
recovery is not a burst.

### 3.4 The processing pipeline

An ordered list of stages, configured in YAML, reconfigurable live. A stage
declares its parameters as a pydantic model and implements `apply(frame)`.
Current stages: `stats`, `levels`, `crop`, `downsample`, `mla_grid_overlay`,
`checkerboard_presence`.

Frames carry their metadata: a `Frame` is pixels plus `cam_id`, sequence
number, monotonic and wall-clock timestamps, a colour-space tag, and a metadata
dict that every stage can add to. Stages use `frame.derive(...)` rather than
mutating, because two consumers may hold the same frame and one of them is
often writing it to disk.

Per-stage timing is measured and shown in the UI beside each panel, so "which
stage is costing me" is a readable number rather than a guess.

---

## 4. Streaming to the browser

### 4.1 MJPEG, and the connection budget

The preview transport is MJPEG — `multipart/x-mixed-replace`, one JPEG per
part. It is bandwidth-hungry and carries no timestamps, and it was chosen
because it works in every browser with no client library and no negotiation.
Replacing it with WebRTC touches `web/server.py` and the page and nothing else;
that seam is deliberate.

The hard constraint it imposes is the **connection budget**. A browser allows
about six concurrent HTTP/1.1 connections per origin, and an MJPEG stream holds
one open forever. Two cameras with a main preview and three sub-aperture tiles
each is eight permanent connections — over the limit, at which point every
other request on the page, *including every button press*, queues behind them
and never completes. The symptom is a UI whose controls silently do nothing.

So: exactly one persistent stream per camera, everything else served as
single-shot JPEGs that the page polls. This is a constraint, not a tuning
parameter. It is also why the dashboard streams only the tab you are looking at
(§7.1).

### 4.2 Sync generators, on purpose

The streaming endpoints are plain `def`, not `async def`. Starlette runs sync
generators on a threadpool, which is what is wanted here: JPEG encoding is
CPU-bound and an `async def` generator would block the event loop and stall
every other request on the server.

The stream body waits on the frame bus for a version newer than the one it last
sent, then applies the rate gate, then encodes. `wait_newer` has a timeout so a
camera that goes quiet produces a keepalive rather than a dead connection.

### 4.3 The bus

`LatestFrame` — one slot, newest wins, guarded by a `threading.Condition` with
a monotonically increasing version counter. Writers overwrite; readers either
take what is there or block for something newer. A reader that falls behind
silently skips ahead, which is the correct behaviour for a viewfinder: a stale
frame is worthless.

Recording will need the other shape — a bounded FIFO with an explicit drop
counter, so an overflow gives you a number you can report rather than a
mystery. It is deliberately not written yet. An untested queue that nothing
calls is worse than no queue, because it reads as a decision already made.

### 4.4 JPEG encoding

The Pi 5's VideoCore VII dropped the hardware JPEG and H.264 encoders, so every
preview frame is compressed on the CPU. Two consequences are designed in:
encode the small `lores` stream rather than the full frame, and cap the preview
rate well below the sensor rate.

The encoder is chosen once at first use and cached: **simplejpeg** first
(already a picamera2 dependency, and the fastest of the three), then OpenCV,
then Pillow. 16-bit input is scaled for display by the frame's actual maximum
rather than the dtype maximum, so dim scenes stay visible — which means preview
brightness is not comparable between frames. That is fine for a viewfinder and
unacceptable for measurement, which is exactly why the measurement path never
goes through here.

### 4.5 The API surface

About forty routes on one FastAPI app: the streams and single-shot JPEGs;
`/api/cameras` and `/api/status`; the pipeline (list stages, patch parameters,
add, remove); sensor controls; orientation; storage (list, retarget, mount,
unmount, verify, diagnostics, release); capture; and the calibration session.

Parameter updates are validated by the stage's own pydantic model, and a
`ValidationError` becomes a 422 whose body names the real limit — rather than a
crashed capture thread, or a silent clamp that makes a control look broken.

---

## 5. Networking and addressing

This got more attention than expected because it cost more time than expected.

### 5.1 The problem

A headless Pi on a university network gets its address from DHCP, and DHCP
changes its mind — on a lease expiry, a switch port change, a reboot after a
power cut. The rig then answers on an address nobody knows. Finding it means
asking IT or scanning the subnet. That happened, and cost an afternoon.

It is made worse by the log line that a naive server prints: `serving on
http://0.0.0.0:8000`. That is the **bind** address, which is not somewhere you
can point a browser, and it is actively unhelpful.

### 5.2 Three answers, installed together because they fail independently

`scripts/setup_network.sh` sets up all three:

1. **mDNS** — `flyeye.local`. Free, already on Raspberry Pi OS via avahi,
   works from macOS, Windows 10+ and Linux, needs nobody's permission. Many
   enterprise networks drop multicast between VLANs, so it is the everyday path
   and not the guarantee. (Windows resolves `.local` only partially; Apple's
   Bonjour Print Services makes it reliable.)
2. **A fixed second address on the wired interface, alongside DHCP.** The Pi
   keeps its lease for internet access *and* always answers on a private
   address of your choosing. One Ethernet cable to a laptop with an address on
   the same little subnet reaches the rig with no DHCP server, no router and no
   administrator. This is the one that always works.
3. **A DHCP reservation from IT.** The correct answer on a managed network and
   the only one that needs a ticket. The script prints the MAC addresses so you
   can send them.

A fourth — USB-C gadget mode, one cable for power and network at a fixed
address — is documented but not installed: on a Pi 5 driving two cameras, a
laptop's USB-C port is usually not up to powering the board, and the failure
mode is a reboot mid-capture.

### 5.3 `net.py`: the rig answers "where am I?"

Rather than assume, the application enumerates every address it can actually be
reached at, and puts them in the startup banner *and* in `/api/status.network`.
So after a DHCP change, `journalctl -u trilobite | grep -A6 'serving on'` says
where it went, and a script can ask the rig itself:

```bash
curl -s http://flyeye.local:8000/api/status | jq -r .network.urls[]
```

Implementation is dependency-free: `ip -j -4 addr show` parsed as JSON where it
exists, and a UDP-socket routing trick as the fallback everywhere else — the
fallback matters because the tests for this run on a Windows desktop. Loopback
and link-local addresses are filtered out, because they are real and useless to
hand to someone.

### 5.4 The direct-cable trap

Worth stating because it looks exactly like a dead board. A fresh Pi on an
Ethernet cable straight to a PC has **no IPv4 address at all**: there is no
DHCP server on that link, and NetworkManager — unlike the older dhcpcd — does
not fall back to an IPv4 link-local address. Nothing answers, nothing pings,
and there is no indication why.

The fix in every case is to start on a network with a DHCP server (any home
router will do) and then install a fixed second address for later. This is
documented at the top of `docs/pi-troubleshooting.md`, along with the IPv6
link-local recovery route for when you are already stuck, and
`scripts/boot_report.sh` — a two-stage flight recorder you copy to the boot
partition to read a Pi's state off the SD card when it will not answer at all.

### 5.5 Deployment

`systemd/trilobite.service` runs it as a service. Deployment to the Pi is `git
pull` with the browser left open on the dashboard, which caused its own bug —
see §7.3.

---

## 6. Recording

### 6.1 The format, and what travels with the pixels

Every capture is an `.npy` array plus a `.json` sidecar. Two rules:

**Never save pixels without their metadata.** The sidecar carries the sensor
settings libcamera reported, the *complete* parameter set of every pipeline
stage, the camera description, the orientation applied at acquisition, shape,
dtype, byte count and timestamps. An uncalibrated image with no record of how
it was taken is not data.

**Default to lossless and unprocessed.** `.npy` holds the native dtype with no
compression artefacts and loads in one line at the other end. PNG and TIFF are
offered for interchange. JPEG is deliberately not an option on the science
path.

Captures are grouped into a session directory created once per run, so a day's
work is one folder and one `scp`.

### 6.2 A capture is not saved until it is on the device, and that is checked

`close()` does not write to a disk. It returns as soon as the bytes are in the
kernel's page cache, and writeback flushes them at its leisure — thirty seconds
later, or never if the power goes or the disk is pulled. File *metadata* takes
a different route: on a journalling filesystem the directory entry is durable
long before the data is.

The two together produce a failure that looks like nothing else: a session
directory full of correctly named, correctly placed, **zero-byte** files. It
cost a field session. Every capture reported "saved", `session.json` was intact
— it was written at startup, so writeback had had minutes — and every file from
the run itself was empty.

So now: each file is `fsync`'d, its directory is `fsync`'d so the *name* is
durable too, the size on disk is read back and compared with what was written,
and the byte count is displayed in the UI next to every capture. About 20 ms
per frame, against losing an afternoon.

`EmptyWriteError` subclasses `OSError` deliberately, so a device that silently
discarded a frame takes the same recovery path as one that refused the write —
because the rest of the session must not be written to it either.

### 6.3 Hot-pluggable output

The SD card is the wrong place for a session (slow, and sustained writes wear
it out) and the right USB SSD is usually not plugged in when the application
starts. So the writer's root is not fixed at construction: `retarget()` moves
it, `release()` puts it back, and a watcher thread notices within two seconds
when the active device has been pulled and falls back to the configured root
rather than letting every subsequent capture raise. Nothing already written is
moved or deleted; a note in the session log records where the earlier part
went.

The subtle failure this is *meant* to avoid: unplugging a USB stick leaves the
mount point behind as an ordinary empty directory, so writes keep *succeeding*
— onto the SD card, under a path that says otherwise.

> **Correction (6 September 2026).** It does not currently avoid it.
> `devices.is_mounted` says in its own docstring that it compares device ids,
> and its body does not: it walks up to any existing ancestor and tests
> whether that is writable, which a leftover mount-point directory on the SD
> card satisfies. So the detection this paragraph describes is not implemented,
> and `release()` returns while writes to the old target may still be in
> flight, which makes the UI's "release, then remove the disk" advice unsafe.
> Review finding F2; Stage 5 of `docs/implementation-plan.md`. `mount_of`,
> twenty lines below in the same file, already contains the `st_dev` walk the
> fix needs.

### 6.4 Enumerating devices on a headless machine

`storage/devices.py` exists as a module rather than a config field because of
one fact: **a plugged-in disk is not necessarily a mounted disk.** With a
desktop session, udisks2 auto-mounts removable media. On a headless Pi nothing
mounts anything, so a USB SSD plugged into a running system is present in
`/sys/block`, visible to `lsblk`, and completely absent from `/proc/mounts`. A
listing built only from `/proc/mounts` shows nothing, and gives no reason why.

So it enumerates from both and merges: `/proc/mounts` for what can be written
to now, `lsblk` for every block device the kernel can see, including unmounted
partitions — which are then offered with a **Mount** button that shells out to
`udisksctl` (mounts as the invoking user, no sudo). Failures are surfaced
verbatim, because the interesting ones announce themselves clearly and are
useless if swallowed: "unknown filesystem type 'exfat'" means install
`exfatprogs`.

Listing never writes a probe file. A filesystem `lsblk` cannot name is probed
with `blkid` rather than dropped. Non-Linux hosts get a degraded but working
answer, so the UI is developable on Windows.

There is also a **Verify** button: write a few MB to the active target, flush
it, read it back, compare. A pre-flight, because the failure it looks for is
silent and total.

---

## 7. The user interface

One HTML file, no framework, no build step. Served by the same FastAPI app.

### 7.1 Six tabs, split by job

System (storage, host health, addresses, camera rates, paths) · one tab per
camera (that sensor's orientation and optical alignment) · Imaging (both
sensors, capture) · Video (placeholder) · Calibration (parked).

Two reasons, and the second is not cosmetic. Aligning one sensor's optics is a
job you do staring at one image; capturing a stereo set is a job you do with
both — sharing one screen meant half the controls were wrong at any moment.
And it is the connection budget again: only the tab on screen streams, so a
camera tab holds one MJPEG stream, Imaging holds two, System and the
placeholders hold none. Switching tabs drops the `<img>` elements, which closes
them.

Per-camera tabs are generated from `/api/cameras`, so a third camera adds a
third tab with no edit to the page.

### 7.2 The controls generate themselves

This is the choice most worth defending. A stage declares its parameters as a
pydantic model with `Field(...)` metadata:

```python
pitch_px: float = Field(20.0, ge=10.0, le=800.0,
                        description="Lenslet pitch, SENSOR pixels")
```

From that one declaration follow: server-side validation, the 422 message when
a value is refused, the browser control with the right type and range, its
label and tooltip, and the parameter block recorded in every image's sidecar.
`Params.model_json_schema()` is served over the API and the page builds the
widgets from it. **Adding a stage is one file; the UI updates itself**, and
there is no second place where a range can be declared differently.

Each row is a slider *and* a number box, kept in sync. The slider is for
sweeping and watching the image; the box is for landing on a value, because a
slider cannot express "pitch = 20.375" and that is the precision the alignment
needs. Whatever the *server* returns is written back into both, so a value that
was clamped or corrected is visible rather than silently ignored.

Three cases fall back to a box with no slider, and the reasons are recorded in
the code: an explicit `widget: "box"` hint; a parameter with no finite bound (a
0–1 slider with the value pinned off the end is not a bar, it is a lie); and a
range spanning orders of magnitude, like exposure time from 30 µs to 100 ms,
where a linear drag cannot resolve the useful band.

Where a bound depends on another parameter — the grid offsets, whose useful
travel is half the current pitch — the schema carries the absolute limit and
the page narrows the slider dynamically. A value outside is *folded* back by
the server rather than clamped, because that parameter is periodic: clamping
would stop the image moving while the number kept changing, and the control
would look dead at one end.

### 7.3 Two failures worth recording

**A cached page.** A control that existed, was styled, and was correct, was not
on screen — the browser was serving the page from its own cache. `FileResponse`
sends an ETag but no `Cache-Control`, so a browser applies heuristic freshness
and can serve a stale copy without ever revalidating. Deployment is `git pull`
with the tab left open, which is precisely that case. The index is now sent
`Cache-Control: no-cache`, and the server stamps a hash of the file into the
page, which polls it and shows **"page is out of date — reload"** in the header
when they diverge. The first fixes the cause; the second makes the symptom
self-diagnosing.

**A missing `>`.** `<span id="live-actions"` without its closing bracket made
the browser read two buttons as *attributes* of the span. The page rendered,
the script loaded, and `$("#all-raw")` was null. Invisible to a Python test
suite — the file is HTML. The page is now parsed in the tests: every tag
closes, and the element IDs the boot script resolves are real elements in the
expected nesting.

### 7.4 State

Runtime state is a JSON overlay saved beside the config: pipeline parameters,
sensor controls, camera orientation, calibration settings. Debounced at three
seconds and written again on exit, so a power cut costs seconds rather than the
afternoon's alignment. Written with write-then-rename, so a cut mid-write
leaves the previous good state rather than a truncated file. A state file that
no longer matches the config degrades to partial application with a warning
instead of refusing to start.

The **config is never rewritten.** Rewriting it would destroy the comments,
which are most of its value, and make every session a git diff. "Go back to the
documented defaults" is `rm` on one file.

Browser-local view preferences (which panel is open) use `localStorage` and
nothing else — which panels one person has open on one screen is not rig state,
and syncing it between two browsers looking at the same Pi would be worse than
not remembering.

---

## 8. Testing

**320 tests, none of which need hardware.** Two synthetic camera backends
implement `CameraSource`, so the whole stack — UI, pipeline, storage, capture,
detection — runs on a laptop. One renders a lenslet array where every
micro-image holds a whole checkerboard, built so that a *correct* detector
finds one complete pattern per tile and an incorrectly scaled or offset one
finds none. With the real rig, "nothing detected" could mean the crop is wrong,
the target is the wrong size, the alignment is off, or the lens cap is on.

**Tests are mutation-checked.** Every non-trivial claim gets a deliberate break
introduced to confirm the test fails for the stated reason. Recent examples,
all caught: swapping two rotation matrices; applying mirrors before rotation;
flipping a sign in a quarter-turn count; composing a transform in the wrong
order; removing a validation lock; dropping a state-file dirty flag; removing
the HTML bracket above. One mutation was *not* caught, and the code now says so
— a safety margin in a search radius that is genuinely slack, so a future
reader does not assume a test covers it.

**Where two components must agree, there is a test on the seam.** A feature
with two halves — code that moves pixels, and arithmetic that claims to say
where they went — can have a sign error in each that cancels in every test
checking only one.

**Cross-language.** The MATLAB reader was compared pixel for pixel against the
Python one on the same file: maximum difference 0. That comparison found a real
bug — MATLAB's `meshgrid` varies its first output along columns and NumPy's
`indexing='ij'` varies its first along rows, so a resampling grid was
transposed in a way no shape check could see. It showed as a mean difference of
83.7 of 255.

**The dashboard is driven headlessly** through Playwright during development —
tab list, stream counts per tab, which controls appear where, dynamic slider
ranges — because the last two UI bugs were both invisible to unit tests.

---

## 9. What the failures have taught

`docs/cleanup-log.md` is a running record of every behavioural change with its
reasoning and its rollback. It is the most useful document in the repository,
because the failures here share a shape:

> **The dangerous failures are silent and confident.** They produce a plausible
> number, an image with obvious structure, or a file with the right name and
> size.

Four that cost real time, all in the acquisition and storage path:

1. **A whole session recorded in a compressed format.** On a Pi 5, libcamera's
   default raw format for this sensor is `MONO_PISP_COMP1` — the imaging
   pipeline's *compressed* transport, not sensor counts. `make_array` hands
   those bytes back as a plain image with the right shape and obvious
   structure, so it looks like a picture that has gone slightly wrong rather
   than a decode failure. The same code on a Pi 4 got `R10` and worked. The
   format is now chosen explicitly, checked, and recorded in every sidecar.
2. **Row-stride padding, twice.** A raw buffer's rows are padded to a 64-byte
   stride and the array is shaped by that stride **in bytes**, delivered as
   uint8 whatever the real pixel size is. A 10-bit 1456 px frame arrives 2944
   wide — which is 1472 uint16 pixels, not 1456 plus padding. Cropping the
   width to 1456 keeps the first 728 pixels and half of the next: structure, at
   the wrong scale. The first fix handled the padding and not the pixel size,
   which is why it appears twice.
3. **Correctly named zero-byte files** (§6.2).
4. **A transposed resampling grid** in the MATLAB reader (§8).

None raised an exception. Each was found by comparison against something
independent — a second implementation, a closed form, a byte count. That is the
pattern the test strategy is built around.

---

## 10. Status

| area | state |
|---|---|
| Dual-camera preview, MJPEG to the browser | working |
| Sensor controls (exposure, gain, AE), saved and restored | working |
| Declarative pipeline, live reconfiguration, per-stage timing | working |
| Schema-generated browser controls | working |
| Addressing: mDNS, fixed second address, self-reported URLs | working |
| Full-resolution capture, `.npy` + sidecar, verified writes | working |
| Hot-pluggable output storage, live retargeting, recovery | working |
| Quick-record: space bar saves a raw stereo set from both cameras | working |
| Offline reading in Python and MATLAB, cross-verified | working |
| Six-tab dashboard, per-tab streaming | working |
| Optical alignment overlay and sub-aperture crops | working |
| Live target-presence map | working — ~2 % of a core |
| Hands-free capture loop | working, **parked** |
| **Video recording** | **not built** — a placeholder tab marks where it goes |
| **Hardware sync between the two sensors** | **not built** — needs XVS wiring |
| Calibration fit | not built (offline, by design) |

**Video recording** is the largest unbuilt piece of software. The encoding is
not the hard part; the Pi 5 has no hardware H.264 encoder, so the plan is to
store lossless frames and encode off-device. The hard part is that a continuous
write has to survive the storage device going away mid-recording — which the
still path already handles, and which a recorder will have to handle
differently, with the bounded-FIFO-plus-drop-counter shape §4.3 describes.

**Hardware sync** is the other real gap and it is not software. `capture-all`
issues its requests sequentially and the sensors free-run, so a "stereo pair"
is two exposures tens of milliseconds apart — fine for a static target on a
bench, wrong for anything that moves. Real simultaneity needs the IMX296 XVS
pins wired together: a hardware task with a small software follow-on.

> **Correction (6 September 2026).** "Tens of milliseconds" is the free-running
> sensor skew alone, and understates it. `capture_all` completes each camera's
> **disk write** before requesting the next camera's frame, so the pair skew
> also contains a full save — fsync included. Stage 6 of the implementation
> plan issues both requests before either write. The software side of
> synchronisation is larger than this paragraph implies in any case: exposure
> timestamp, receive time, processing completion and write completion are four
> different clocks, and a shared trigger removes none of the need to verify
> pairing and drops.

---

## 11. Deferred to a second report

The optics, the imaging model and the calibration mathematics are not covered
here. Meanwhile they are documented:

- `docs/calibration-spec.md` — the imaging model, the parameter set, the
  fitting problem and the verification plan. The one-line version: the camera
  is modelled as a planar array of ~140 identical pinhole cameras differing
  only in where their centres sit, which is what makes the parameter count
  tractable (~678 for the stereo pair against ~80,000 observations).
- `docs/calibration-ui-spec.md` — the acquisition workflow, and §10 the open
  questions about the physical target.

Two decisions there are physical rather than software, and both block progress
on the fit: the **calibration target design** and whether the **lenslet
apertures are square or circular**. Both are covered in the second report.

---

## 12. Practicalities

```bash
python -m trilobite --config config/desktop.yaml   # no hardware needed
python -m trilobite --config config/pi.yaml        # on the rig
```

Then `http://flyeye.local:8000/`. Use Ctrl-C, not `kill -9`: libcamera does not
always recover from a process that dies holding a sensor, and the fix is a
reboot.

| where to look | for |
|---|---|
| `README.md` | install from a blank SD card, the dashboard, the API, the traps |
| `docs/cleanup-log.md` | every behavioural change, why, and how to undo it |
| `docs/pi-troubleshooting.md` | recovery for a rig that will not answer |
| `matlab/README.md` | the offline reading path, and four things that will bite you |
| `apt-packages.txt` | every system package, with a reason on each line |
| `config/pi.yaml` | the rig, heavily commented — nothing is hardcoded in Python |

Four seams, each documented where it would be changed: `CameraSource` (the
hardware), `Stage` (a processing step), `LatestFrame` (the producer/consumer
boundary), `create_app` (the transport).

---

## 13. What I would like from a supervisor

On the software, in rough order of value:

1. **A view on video recording** — whether continuous capture is required, and
   at what rate and duration. It is the largest unbuilt piece and its design
   depends entirely on that answer.
2. **Hardware sync**: whether simultaneous stereo exposure is a requirement.
   If it is, the XVS wiring should be scheduled now, because it is the only
   remaining item with a hardware lead time.
3. **Review of the testing approach** (§8), particularly whether the
   mutation-checking discipline is worth its cost at this scale, or whether it
   should be reserved for the calibration arithmetic.
4. **A second reader on the failure log** (§9). Four silent-and-confident bugs
   in one subsystem suggests a class of problem rather than four accidents, and
   a fresh view on what else has that shape would be worth more than another
   feature.
