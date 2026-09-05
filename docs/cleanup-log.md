# Cleanup log

Removals and behavioural changes, and how to undo them. Newest first.

Everything here is recoverable from git, because nothing is removed that was
not already committed. The general rollback is:

```powershell
git checkout -- <path>      # one file or directory back to HEAD
git checkout .              # everything back to HEAD
```

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
