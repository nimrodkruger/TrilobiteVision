# TrilobiteVision

Capture, processing and browser control for a Raspberry Pi 5 optics rig with
two IMX296 mono global-shutter cameras. Built for **focused plenoptic
(Plenoptic 2.0) camera calibration**: a microlens array sits behind the main
objective, and the deliverable is pixel-to-pixel registration across
sub-aperture views, valid at any object distance.

Runs with no hardware attached. Two synthetic camera backends drive the entire
stack — web UI, pipeline, storage, corner detection — so development and tests
happen on a desktop and only the parts that need photons happen on the Pi.

```
python -m trilobite --config config/desktop.yaml     # no camera needed
python -m trilobite --config config/pi.yaml          # on the rig
```

Then open `http://localhost:8000/` (or `http://<pi-address>:8000/`).

---

## Status

| area | state |
|---|---|
| Dual-camera preview, MJPEG to the browser | working |
| Sensor controls (exposure, gain, auto-exposure) | working |
| Declarative processing pipeline, live reconfiguration | working |
| MLA grid overlay and sub-aperture crops | working |
| Runtime parameters saved on exit, restored on start | working |
| Full-resolution still capture, `.npy` + JSON sidecar | working |
| Hot-pluggable output storage | working |
| Calibration: live checkerboard presence map | working — 2% of a core |
| Calibration: hands-free pose capture and recording | working |
| Calibration: the fit | not built (offline, by design) |
| Video recording | not built |
| Hardware sync between the two sensors | not built (needs XVS wiring) |
| `lenslet_extract` — full 4D light-field resampling | placeholder; see its docstring |

Two open decisions are blocking further calibration work, both physical rather
than software: the **target design** (see `docs/calibration-ui-spec.md` §10.1)
and whether the **lenslet apertures are square or circular**, which determines
whether grid rotation costs any usable crop area.

---

## Names

| thing | name |
|---|---|
| repo and project folder | `TrilobiteVision` |
| distribution (`pyproject.toml` name) | `trilobitevision` |
| Python import package | `trilobite` |
| virtualenv on the Pi | `~/.venvs/trilobite` |
| systemd unit | `trilobite.service` |
| default data directory | `~/trilobite-data` |
| the Pi's hostname and login user | `flyeye` (unrelated to the code) |
| the cameras | `left`, `right` (`cam_id` in the config) |

The import package is short so commands stay typeable.

---

## Install

### Raspberry Pi 5 — from a blank SD card

Nine steps. Each ends with something to check, because the failures here are
mostly silent and the checks are how you find out at the right moment rather
than three steps later.

Budget about 40 minutes, most of it `apt` downloading.

#### 1. Flash the card

Use **Raspberry Pi Imager** (2.0 or later) on the desktop.

| setting | value | why |
|---|---|---|
| Device | Raspberry Pi 5 | filters the OS list to what will boot |
| OS | **Raspberry Pi OS Lite (64-bit)** | Lite: the rig is headless, and a desktop session costs RAM and CPU the cameras want. 64-bit: picamera2 and numpy are built for it. |
| Storage | your card | — |

Then **Next → Edit Settings** and fill in the customisation, all of it:

| field | value |
|---|---|
| Hostname | `flyeye` — this becomes `flyeye.local`, which is how you will reach the rig |
| Username / password | `flyeye` and something you will not lose |
| Wireless LAN | your network, if the Pi will use wifi; leave off for wired |
| Locale / time zone | yours — wrong time zone makes every capture timestamp wrong |
| **Services → Enable SSH** | **yes**, with password authentication (or paste a public key) |

SSH is the one that cannot be fixed afterwards without a monitor and keyboard.
If you forget it, reflash; it is faster.

Write, wait for verification, eject.

> **Which OS version.** Raspberry Pi OS **Trixie** (Debian 13) has been the
> current release since 2 October 2025 and is what to use. Bookworm still
> works and is still supported, but Trixie is where new Pi packages land —
> including `rpi-usb-gadget`, which matters if you want the USB-C addressing
> option below.

#### 2. First boot, through a router

**Put the Pi on a network that has a DHCP server** — any home or travel router,
or the lab network. Do not start with a cable straight to your PC: there is no
DHCP server on that link, NetworkManager does not fall back to an IPv4
link-local address, and the Pi ends up with no IPv4 address at all. It looks
exactly like a dead board. `docs/pi-troubleshooting.md` has the way out if you
are ever stuck there.

Card in, both camera ribbons already seated (see step 4 before powering on if
they are not), Ethernet in, power last. The first boot resizes the filesystem
and reboots itself, so give it two minutes.

```bash
ssh flyeye@flyeye.local
```

**Check:** you get a prompt. If the name does not resolve, open the router's
admin page — its attached-devices or DHCP-clients list shows the Pi by hostname
with its address. Use that.

Home routers do not block mDNS, so `flyeye.local` usually just works. Windows
resolves `.local` only partially; installing Apple's **Bonjour Print Services**
makes it reliable, and a Mac or Linux box is a useful second opinion.

#### 3. Update, then reboot

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

**Check:** `uname -a` and `cat /etc/os-release` after it comes back. A kernel
update that has not been rebooted into is a common cause of "the camera worked
yesterday".

#### 4. Cameras: cable and overlays

Power off before touching a ribbon. Both IMX296 ribbons go into the two CSI
connectors, **contacts facing the board**, latch pushed home square. A ribbon
in the wrong way round shows up as "no cameras detected", not as an error.

```bash
git clone <your-repo> ~/TrilobiteVision
cd ~/TrilobiteVision
bash scripts/install_pi.sh
sudo reboot                      # /boot/firmware/config.txt changed
```

The script installs everything in `apt-packages.txt`, writes the overlays, and
builds the virtualenv. Three things it does that are not optional:

- **apt, not pip, for the camera stack.** `python3-picamera2` is a C++
  extension built against the system libcamera; pip versions drift out of step
  and produce import errors or silent format mismatches. `python3-numpy`,
  `python3-opencv` and `python3-simplejpeg` come from apt for ABI compatibility
  with picamera2's buffers.
- **`python3 -m venv --system-site-packages`.** Raspberry Pi OS is an
  externally-managed environment (PEP 668), so pip will not install into the
  system Python, and picamera2 lives in the system site-packages. Without the
  flag every camera import fails with a `ModuleNotFoundError` that looks like a
  broken install.
- **Two explicit camera overlays** in `/boot/firmware/config.txt`. Auto-detect
  is reliable for one camera and not for two.

  ```
  camera_auto_detect=0
  dtoverlay=imx296,cam0
  dtoverlay=imx296,cam1
  ```

**Check:**

```bash
rpicam-hello --list-cameras
```

Two IMX296 entries, at `/base/axi/pcie@120000/rp1/i2c@88000/imx296@1a` and the
matching `i2c@80000` node. One entry means one ribbon is not seated. Zero means
the overlays did not take, or you have not rebooted: run
`bash scripts/diagnose_cameras.sh`, which separates a kernel/cable problem from
a software one.

**Do not pip-install OpenCV on the Pi.** The apt build shares numpy's ABI with
picamera2; a pip wheel alongside it gives two `cv2` modules and an import order
that decides which one you get.

#### 5. Prove the cameras from Python

```bash
source ~/.venvs/trilobite/bin/activate
python scripts/probe_cameras.py --grab
```

**Check:** both cameras report their sensor modes and a frame is grabbed from
each. This is the boundary between "libcamera can see it" and "our stack can
use it", and it is worth crossing deliberately.

#### 6. Run it

```bash
python -m trilobite --config config/pi.yaml
```

**Check:** the startup log now lists every address the rig answers on, not the
bind address:

```
serving on 0.0.0.0:8000 -- reachable at:
    http://flyeye.local:8000/   <- survives a DHCP change, if mDNS is not blocked
    http://10.44.12.87:8000/
    http://127.0.0.1:8000/   <- this Pi only
```

Open one from the desktop. Two live previews, both at their configured frame
rate.

#### 7. Give it a stable address

DHCP changes its mind — on a lease expiry, a port change, a reboot after a
power cut — and a rig that has moved is a rig you cannot reach.

**On a router you control, use its address reservation.** Bind the Pi's MAC to
a fixed IP in the router's own DHCP settings. One checkbox, permanent, nothing
installed on the Pi. This is the right answer whenever it is available.

On a network you do not control, `bash scripts/setup_network.sh` is the
fallback: it sets up mDNS, advertises the UI so the rig appears in network
browsers, adds a fixed second address alongside the DHCP lease, and prints the
MAC addresses to send to whoever runs the network. Safe to re-run, and it does
not touch the lease.

**Check:** `http://flyeye.local:8000/` opens, and the address you got is one
that will still be there tomorrow.

#### 8. Storage

Plug in the USB SSD. On a headless Pi nothing auto-mounts it, which is normal
and is what the **Mount** button in the Storage panel is for. Then press
**Verify**: it writes 4 MB, flushes it, reads it back and compares.

**Check:** Verify reports a sensible rate. Do not start a session on a disk
that has not passed this — see **Output storage**.

#### 9. The service, last

Only once everything above works by hand:

```bash
sudo cp systemd/trilobite.service /etc/systemd/system/
sudo sed -i "s/%USER%/$USER/g" /etc/systemd/system/trilobite.service
sudo systemctl daemon-reload && sudo systemctl enable --now trilobite
journalctl -u trilobite -f
```

**During active development leave this disabled.** A service holding the
cameras makes manual runs fail with a confusing "device busy". Enable it when
the rig is doing unattended work, not while you are developing against it.

---

### Desktop (Windows, macOS, Linux)

```powershell
git clone <your-repo>
cd TrilobiteVision
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[desktop,dev]"
python -m trilobite --config config/desktop.yaml
pytest
```

No Pi, no cameras, no libcamera — `picamera2` is imported lazily inside
`Picamera2Source.open()` so this works. The `desktop` extra brings numpy,
Pillow and `opencv-python-headless` (needed by the corner detector).

### Deploy loop

```powershell
git add -A; git commit -m "..."; git push          # desktop
```

```bash
ssh flyeye@flyeye.local 'cd ~/TrilobiteVision && git pull && sudo systemctl restart trilobite'
```

Use the name, not an address — see **Addressing** above. If mDNS is blocked on
your network, put the fixed address in `~/.ssh/config` once and use that:

```
Host trilobite
    HostName 192.168.50.10
    User flyeye
```

During active development leave the systemd service **disabled** and run the
app by hand over SSH. A service holding the cameras makes manual runs fail with
a confusing "device busy".

---

## Running

```
python -m trilobite [options]

  -c, --config PATH    rig config YAML (default: config/pi.yaml)
      --host HOST      override server host
      --port PORT      override server port
      --log-level L    override the config's log level
      --state PATH     where runtime parameters are saved
                       (default: <config-name>.state.json beside the config)
      --no-restore     start from the config as written, ignoring saved state
                       (the state file is still written on exit)
```

Parameters dialled in through the UI are saved on exit and restored on start.
The state file is a JSON overlay on the config, kept separate so the commented
YAML stays the readable record of intent. It is gitignored.

Use Ctrl-C, not `kill -9`: libcamera does not always recover from a process
that dies holding a sensor, and the fix is a reboot.

### Configurations

| file | cameras | for |
|---|---|---|
| `config/pi.yaml` | two IMX296 via picamera2 | the rig |
| `config/desktop.yaml` | two synthetic, drifting gratings | UI and pipeline work |
| `config/desktop-plenoptic.yaml` | two synthetic lenslet arrays, a whole checkerboard per micro-image | calibration work off-rig |

---

## The two paths

Every frame goes down one of two paths, and they must not be confused.

| | **view path** | **science path** |
|---|---|---|
| source | `lores` stream, ISP-processed | `raw` stream, ISP bypassed |
| resolution | 728 × 544 | 1456 × 1088 native |
| processing | gamma, gain, overlays | none |
| destination | JPEG → browser | `.npy` + JSON sidecar → disk |
| purpose | is it in focus? is it aligned? | measurement |

The view path may stretch contrast, draw grids and decimate. The science path
does not touch a pixel. picamera2 produces both from the same sensor read, so
the view path is nearly free and the two are always in temporal agreement.

Corner detection is a third case and sits between them: full sensor
resolution, because the preview would halve corner precision, but taken from
the ISP output, because the ISP's companding moves no corner. It comes out of
the **same request** as the preview frame — `CameraRuntime.grab_full()` raises a
flag and the capture thread serves it — so it is the same exposure, and no
second consumer of the camera exists.

---

## The dashboard

Two modes, switched in the header. They are separate because alignment and
calibration are different jobs: during alignment you stare at the image,
during calibration you stare at coverage.

### Imaging mode

Per camera: the live preview with the MLA grid drawn on it, three sub-aperture
tiles below it, and a scrolling column of controls generated from the stage
schemas. Save raw or save view, per camera or both at once.

The MLA parameters — pitch, rotation, offsets, crop scale — are what the
sub-aperture crops use, so what you see aligned is what gets extracted. They
are in **full-resolution sensor pixels** — 1456 × 1088 here, not the 728 × 544
preview you align against. The overlay scales them down to draw; everything
that measures uses them as they are. The sensor frame they are expressed in is
recorded with them, and anything holding a different-sized frame converts
through `MLAGridOverlay.geometry_for()`.

The Storage panel lives here — see below.

### Calibration mode

**Hands-free.** The assumption behind every default: you are holding the board
with both hands, you cannot press anything, and you are looking at the board
rather than at the screen. So the rig watches and decides, and tells you out
loud what it did.

    SHOW THE BOARD  →  HOLD STILL  →  CHECKING  →  CAPTURED  →  MOVE THE BOARD

You move, pause, move, pause. A rising two-tone beep means a pose was kept; a
low buzz means it was rejected. **Space** forces a shot through every gate — for
the extreme angles that never arm on their own, or with a presenter clicker as
a shutter release. After each capture the display freezes for a couple of
seconds on the frame that was actually recorded, with its corners drawn;
**Escape** within that window discards it. A session ends when you press Finish
and at no other time.

Two detectors do this, and the split is what makes it fit on a Pi:

- a **presence map** that counts saddle points per micro-image over the whole
  preview frame — three convolutions and a `bincount`, about 3 ms, and its cost
  does not depend on the number of tiles. It drives the state machine and tints
  the live view so aiming needs no second display.
- a **five-tile cross** put through `findChessboardCornersSB` at full
  resolution, once per pose, about 45 ms. That is the acceptance check.

Full-field corner detection over all ~130 micro-images happens offline, on the
desktop, from the recorded frames. See `docs/calibration-ui-spec.md` §4 for the
measurements behind that split, and §11 for the desktop head this is heading
towards.

Each pose writes both cameras' full frames, the cross corners in sensor
coordinates, and a frozen copy of the MLA geometry. About 3 MB a pose. Nothing
is ever deleted: a discarded pose is marked in the index and its files stay.

To rehearse the whole loop with no camera, run
`config/desktop-plenoptic.yaml` and set the board to **4 × 3 inner corners,
7 mm**.

**If nothing is detected**, the answer is on the page and not in a log. The
Preconditions panel refuses to start a session at all when the presence stage
is missing, switched off, or not reading the MLA grid — the three ways a rig
can run a preview and silently count nothing. Once running, the line under each
preview reports the best tile against the threshold, the median, the total
peaks in the frame, and what your configured board *should* give; **show what
the detector sees** replaces the preview with every saddle peak marked and each
micro-image's count written in its box. Between them:

| what you see | what it is |
|---|---|
| peaks everywhere, boxes in the wrong place | the MLA grid, not the detector |
| a handful of peaks per box where ~30 are expected | the board's squares are too large for a micro-image |
| no peaks at all, best contrast near zero | lens cap, no light, or wildly wrong exposure |
| peaks present, best contrast below the floor | too soft or too dim — focus and light, not the threshold |
| 117/117 seeing but `0/5 cross tiles` | the **board size** in the settings is not the board in your hand |

### Why the tiles are polled and not streamed

A browser allows about six concurrent HTTP/1.1 connections per origin, and an
MJPEG stream holds one open forever. Two cameras with a preview and three tiles
each is eight — over the limit, at which point every other request on the page,
including every button press, queues behind them and never completes. The
symptom is a UI whose controls silently do nothing.

So: exactly one persistent stream per camera, everything else polled as
single-shot JPEGs. This is a constraint, not a tuning parameter.

### Three frame rates, and which one to change

They are separate on purpose, and the symptom of confusing them is a page where
the preview looks fine but every slider and button responds a second late.

| rate | set by | what it governs |
|---|---|---|
| **sensor** | `cameras[].fps` | how often the camera produces an exposure. Must be drained at this rate whatever else is happening: an unreleased picamera2 request starves a four-deep pool and stalls capture. |
| **pipeline** | `cameras[].process_fps`, defaulting to `server.preview_fps` | how often stats, levels, the grid overlay and the presence map actually run. Frames arriving faster are released without being decoded. |
| **browser** | `server.preview_fps` | how often an MJPEG frame is encoded and pushed. |

The pipeline rate is the one that was missing. Running the full pipeline on
every sensor frame — a ~3 ms presence map among them, twice over for two
cameras — is sixty passes a second on four cores that also have to encode JPEG
and answer the API, and the web thread loses. Capping it to the browser rate
cuts that by 60% and costs nothing, because the browser is the only thing that
reads the result. `GET /api/status` reports `fps` (pipeline), `sensor_fps` and
`skipped` per camera, so you can see the cap working rather than assume it.

`process_fps: 0` restores the old behaviour of processing every frame. Raise it
above the browser rate only if something other than the browser starts reading
the preview bus.

---

## Output storage

The SD card is the wrong place for a session — slow, and sustained writes wear
it out — and the right USB SSD is usually not plugged in when the application
starts. The output directory is therefore movable while the rig is running,
from the Storage panel in imaging mode.

- **A plugged-in disk is not necessarily a mounted disk**, and this is the part
  that catches people out. A headless Pi runs no desktop session, so nothing
  auto-mounts removable media: a USB SSD plugged into a running rig is visible
  to `lsblk` and reachable by nothing at all. Such a disk is listed here anyway,
  marked *not mounted*, with a **Mount** button that shells out to `udisksctl`
  (hence `udisks2` in `apt-packages.txt`). If the mount fails, the panel shows
  the tool's own words — "unknown filesystem type 'exfat'" means install
  `exfatprogs`.
- **Devices are polled**, so something plugged in after startup appears without
  a reload. Listing never writes a probe file; read-only mounts are read out of
  `/proc/mounts`, and a filesystem `lsblk` cannot name is probed with `blkid`
  rather than dropped.
- **Choosing a device creates a new session directory on it**, under
  `trilobite-data/`. Nothing already written is moved, mirrored or deleted, and
  a note records where the earlier part of the session went.
- **Pulling the disk is survivable.** A watcher notices within two seconds and
  falls back to `storage.root`; a write that fails mid-capture recovers and
  completes rather than losing the frame. Unplugging a USB stick leaves the
  mount point behind as an ordinary empty directory, so writes otherwise keep
  *succeeding* — onto the SD card, under a path that says otherwise.

- **A capture is on the device before it is called saved.** `close()` does not
  write to a disk — it hands the bytes to the page cache and returns, and
  writeback flushes them thirty seconds later or never. Metadata is journalled
  promptly, so a disk pulled or a Pi powered off in between leaves correctly
  named, correctly placed, **zero-byte** files. That cost a session here. Every
  file is now fsync'd, its directory is fsync'd so the name is durable too, and
  the size is read back and checked against what was written before the capture
  is reported. A write that lands short or empty raises and falls back to the
  internal disk rather than reporting success. Cost: about 20 ms per frame.
- **Check a disk before trusting it.** **Verify** on the storage panel writes
  4 MB to the live session directory, flushes it, reads every byte back and
  compares. It catches a mount that accepts writes and stores nothing, a full or
  read-only filesystem, and a device too slow for the capture rate. It cannot
  prove the disk survives being unplugged; nothing short of unplugging it does.

The size on disk is shown beside every capture in the UI. If it ever reads
`0 BYTES — NOT SAVED`, stop and press Verify.

To remove a disk: press **Release** so captures go back to `storage.root`, then
**Unmount**. Unmount refuses while anything holds the filesystem open, which
includes this application, so the order matters. `storage.root` is the fallback
and must be somewhere always mounted.

If a disk you plugged in does not appear at all, `GET /api/storage/diagnostics`
returns the raw `lsblk`, `/proc/mounts` and udisks2 version beside what the
enumerator made of them, which localises it in one look: in `lsblk` but not in
the list is a bug here, in neither is a kernel or cable problem, in both but
unmountable is a missing filesystem driver. `bash scripts/diagnose_host.sh` on
the Pi prints the same thing with more context.

### Orientation

**Rotate**, **Flip horizontal** and **Flip vertical** in each camera's Sensor
panel turn and mirror the image. They are applied at **acquisition**, before
anything else sees the pixels, so the preview, the saved `.npy`, the
sub-aperture crops and the recorded calibration corners all agree — there is no
way for the display and the data to differ, because there is only one
orientation and it happens first. Every sidecar records all three.

They describe how the camera is *mounted* — a fold mirror, an inverted bracket,
a sensor turned on its side to put the long axis across the bench — rather than
a display preference, which is why they sit with the sensor controls and not in
the pipeline.

Rotation is **clockwise as you look at the image**, in quarter turns only, and
is applied **before** the two mirrors, so the flip boxes always mean "mirror
what I am looking at" whatever the rotation. Anything other than a quarter turn
would be a resample rather than a relabelling of pixels, and would cost
resolution on every frame; the endpoint refuses it.

**90° and 270° swap width and height**, and that swap runs the length of the
stack. Nothing downstream may assume a landscape frame: `describe()` is the
single place the post-rotation size is decided, and the MLA reference frame,
the readiness arithmetic, the session manifest and the offline readers all take
their geometry from there. The one thing that deliberately does *not* see the
rotation is the raw stride trim — row padding is a property of the buffer as
the sensor delivers it, so it is removed against the native sensor width first
and the frame is turned afterwards.

An MLA alignment **survives** a change of orientation. Turning or mirroring the
camera moves no lenslets; it relabels the pixels. So the grid is carried across
rather than invalidated: the offsets are transformed, the reference frame swaps
if the turn was odd, the lattice tilt negates under a mirror, and pitch is
untouched, because all eight orientations are isometries. The arithmetic is in
`src/trilobite/optics/orientation.py` — the eight being the dihedral group of a
rectangle — and an alignment records which orientation it was made under, so
the change is computed rather than guessed. The UI prints what became of the
offsets when you change it.

Re-check the overlay by eye afterwards all the same. The transform is exact,
but "nothing else moved when I turned the camera" is a claim about the bench,
not about the software.

Alignments made before the orientation was recorded are read as having been
made at 0° with no mirrors, which is what they were.

### Quick-record

The manual counterpart to the hands-free calibration loop, in imaging mode, for
the same ergonomic reason — both hands are on the board — but with the decision
removed rather than automated. Tick **quick-record** in the header and the
space bar saves a raw set from both cameras. Nothing is detected, nothing is
judged, nothing is rejected: every press is written, and whether a pose was any
good is a question for the desk.

A rising two-tone beep means the write completed; wait for it before moving the
board, because it is the only confirmation there is. A press arriving while the
previous set is still being written is queued rather than dropped, at most one
deep. The header keeps a running count.

This is the recommended path while the on-board detector is still being trusted:
it produces exactly the same files the automatic loop does, minus the pose
manifest, and every frame is analysable offline with `scripts/read_capture.py`
or the MATLAB functions in `matlab/`.

### Reading a capture back

Every capture is a `.npy` array plus a `.json` sidecar carrying the frame's
metadata, the pipeline's parameters and the MLA geometry that was in force.
`scripts/read_capture.py` opens the pair anywhere Python and OpenCV are
installed, left or right camera, view or raw mode:

```bash
python scripts/read_capture.py path/to/left_000123.npy --show
python scripts/read_capture.py ... --grid --corners             # overlay
python scripts/read_capture.py ... --tile 3,-2 --zoom 6         # one micro-image
python scripts/read_capture.py ... --detect --board 4x3 --csv corners.csv
```

The sidecar's pitch and offsets are quoted against whichever frame the stage
was configured on, so the reader converts them to the array it is actually
holding — a raw frame and a preview of the same pose need no different
arguments.

`--detect` is the part the rig deliberately does not do: `findChessboardCornersSB`
over **every** micro-image, at full resolution, with the corners returned in
frame coordinates. On the Pi that costs about a second per camera and never
fitted in the live loop; here it costs whatever the desktop takes and is the
input to the model fit. `--csv` writes one row per corner, `--json` the whole
per-tile structure.

Ten to twenty seconds of arithmetic on a laptop replaces a design that took the
rig down. See §4.3 of `docs/calibration-ui-spec.md` for where each detector runs
and why.

### Lines across the image

Horizontal lines — "variation on the row value, like a broken screen" — have
four causes that look identical on screen and want completely different
responses, and swapping the cable tests only one of them.
`scripts/diagnose_lines.py` measures all four:

```bash
python scripts/diagnose_lines.py path/to/session/left/     # several frames
```

| what it finds | what it means | what to do |
|---|---|---|
| kernel reports CSI/FIFO errors | the link is failing, counted by the driver | reseat both FFC ends, shorter cable, and try one camera at a time — two share the CSI bandwidth |
| rows match their neighbours better when **shifted sideways** | bytes are being dropped: the pixels are not wrong, they are *moved* | same as above; a sensor cannot do this |
| row offsets **repeat** frame to frame | fixed-pattern row noise | subtract a dark frame taken at the same exposure and gain |
| row offsets **do not repeat**, and are small | ordinary read noise with the contrast stretched | nothing — judge the frame at its real contrast first |

Give it several frames. Fixed-pattern and random row noise are the same picture
in a single frame and are only separable across a few, and more frames also
stop a scene with genuine horizontal structure from being read as a fault.

The displacement test is the one worth understanding: a dropped byte does not
corrupt a row's values, it shifts everything after the loss one or more pixels
early, so the row still looks like the scene and simply sits in the wrong
place. That is invisible to any measure of row brightness — the same pixels are
still in the row — and obvious to a cross-correlation against the rows above
and below. It is also something no sensor does, which is what makes it
decisive.

### In MATLAB

`matlab/` holds the same reading path for MATLAB, base install only — no
toolboxes, and it runs unmodified under Octave:

```matlab
addpath('matlab');
cap   = tv_read_capture('raw_left_000001.npy');   % pixels + all metadata
tiles = tv_micro_images(cap);                     % M×N cell of X×Y, de-rotated
subs  = tv_sub_apertures(tiles);                  % X×Y cell of M×N
demo_read_capture                                 % the whole chain, with figures
tv_selftest('raw_left_000001.npy')                % 19 assertions, headless
```

`tv_read_capture` rescales the recorded MLA geometry from the preview frame it
was aligned on to the frame you are actually holding, which is the step that
silently ruins everything if it is skipped. `matlab/README.md` has the details,
including the micro-image / sub-aperture distinction and the two array-ordering
traps between NumPy and MATLAB.

---

## HTTP API

| method | path | purpose |
|---|---|---|
| GET | `/` | the dashboard |
| GET | `/api/status` | uptime, per-camera fps and errors, storage state, host health |
| GET | `/api/cameras` | camera list and hardware description |
| GET | `/api/stage-types` | every registered stage type and its schema |
| GET | `/stream/{cam}.mjpg` | MJPEG preview (one per camera; see the budget above) |
| GET | `/snapshot/{cam}.jpg` | single preview frame |
| GET | `/subaperture/{cam}/{view}.jpg` | one sub-aperture tile, single shot |
| GET | `/api/subapertures/{cam}` | which lenslet indices the named views resolve to |
| GET | `/api/pipeline/{cam}` | stages, schemas, current values, per-stage ms |
| POST | `/api/pipeline/{cam}/{stage}` | `{"values": {...}}` — validated, 422 on bad input |
| POST | `/api/pipeline/{cam}/_add` | insert a stage at runtime |
| DELETE | `/api/pipeline/{cam}/{stage}` | remove a stage |
| GET | `/api/controls/{cam}` | advertised sensor controls with the sensor's own limits |
| POST | `/api/controls/{cam}` | `{"controls": {"ExposureTime": 8000, ...}}` |
| POST | `/api/capture/{cam}/raw` | full-resolution capture, ISP bypassed |
| POST | `/api/capture/{cam}/view` | the processed preview as displayed |
| POST | `/api/capture-all/{raw\|view}` | trigger every camera |
| GET, PUT | `/api/calibration/settings` | board, optics, acceptance, detection, capture |
| GET | `/api/calibration/readiness` | preconditions, split blocking vs advisory |
| POST | `/api/calibration/start` | open a hands-free capture session |
| POST | `/api/calibration/stop` | finish it |
| GET | `/api/calibration/session` | phase, banner, coverage, poses, event log |
| POST | `/api/calibration/force` | shoot now, through every gate |
| POST | `/api/calibration/discard` | mark the last pose discarded |
| GET | `/calibration/shot/{cam}.jpg` | the last pose, cross and corners drawn |
| GET | `/api/storage` | filesystems on offer, mounted or not, and where output is going |
| POST | `/api/storage/target` | `{"path": "/media/stick"}` — move output there |
| POST | `/api/storage/mount` | `{"device": "/dev/sda1"}` — mount a plugged-in disk |
| POST | `/api/storage/unmount` | `{"device": "/dev/sda1"}` — so it can be pulled safely |
| POST | `/api/storage/release` | back to `storage.root` |
| POST | `/api/storage/verify` | write 4 MB, flush, read it back and compare |
| GET | `/api/storage/diagnostics` | raw `lsblk`, `/proc/mounts`, udisks2 — why is my disk missing |

Sensor controls and pipeline parameters are separate endpoints on purpose. One
changes what the sensor does, the other what happens to the numbers afterwards;
conflating them makes it impossible to tell whether an observed change was
optical or computational.

`capture-all` is **not** synchronised capture — the requests go out
sequentially and the sensors free-run, so frames land tens of milliseconds
apart. Real simultaneity needs the IMX296 XVS pins wired together.

---

## Layout

```
src/trilobite/
  types.py                 Frame, CameraInfo — metadata travels with the pixels
  config.py                pydantic schema for the rig YAML
  bus.py                   LatestFrame: one slot, newest wins
  state.py                 runtime parameters saved on exit, restored on start
  app.py                   CameraRuntime (thread per camera), Application
  __main__.py              CLI entry point
  cameras/
    base.py                CameraSource ABC — the hardware seam
    picam.py               picamera2 backend: main + lores + raw streams
    offline.py             synthetic and replay backends, no hardware
    registry.py            backend lookup, libcamera discovery
  processing/
    base.py                Stage ABC + StageParams — the parameter contract
    registry.py            @register decorator, stage catalogue
    pipeline.py            ordered runner, live reconfiguration, timing
    stages/basic.py        passthrough, levels, crop, downsample, stats
    stages/plenoptic.py    MLA grid overlay; lenslet_extract placeholder
  health.py                CPU temperature, under-voltage, memory — is the host coping
  optics/mla.py            lenslet geometry, shared by the overlay and the crops
  calibration/
    settings.py            board, optics, acceptance, capture gates, readiness
    presence.py            saddle counting: the cheap live detector
    detect.py              corner detection — no threads, no camera, no files
    session.py             the hands-free capture loop and the recorder
  sinks/jpeg.py            JPEG encode: simplejpeg → cv2 → Pillow
  net.py                   every address the rig is reachable at
  storage/writer.py        session directories, .npy + sidecar, live retargeting
  storage/devices.py       which filesystems can hold a session, right now
  web/server.py            FastAPI: streams, parameters, controls, capture
  web/static/index.html    the dashboard, generated from the stage schemas
config/                    rig configurations, heavily commented
docs/                      the calibration model and the acquisition workflow
scripts/
  probe_cameras.py         run this first on the Pi
  install_pi.sh            apt, config.txt, venv — idempotent
  setup_network.sh         mDNS, a fixed second address, the MACs to give IT
  boot_report.sh           a flight recorder for a Pi you cannot reach
  diagnose_cameras.sh      when libcamera reports zero cameras
  diagnose_host.sh         after a crash, or when a disk does not appear
  boot_report.sh           read a Pi's state off the SD card when it will not answer
  benchmark_detectors.py   what each detector costs, on your machine
  measure_derotation_cost.py   what tile de-rotation actually costs in precision
  read_capture.py          open a recorded .npy + .json off the Pi, and detect
matlab/                    the same reading path in MATLAB, base install only
  tv_read_npy.m            .npy -> MATLAB array, native class, right way round
  tv_read_capture.m        image + metadata, MLA geometry rescaled to the frame
  tv_micro_images.m        M×N cell of X×Y micro-images, de-rotated
  tv_sub_apertures.m       the permute: X×Y sub-aperture images, each M×N
  demo_read_capture.m      the whole chain, with figures
  tv_selftest.m            19 assertions, headless
systemd/trilobite.service  run as a service
tests/                     all of it runs anywhere, no camera needed
```

Four seams, each where a change is expected:

- **`CameraSource`** — the hardware boundary. A new sensor is a subclass.
- **`Stage`** — a stage *declares* its parameters as a pydantic model, and
  validation, the browser controls and the settings record saved with every
  image all follow from that declaration. Adding a stage is one file; the UI
  updates itself.
- **`LatestFrame`** — the capture thread only reads, processes and publishes.
  It never encodes, writes or waits on the network, so a slow browser cannot
  perturb capture timing. It is also the **only** consumer of the camera: a
  full-resolution frame is requested by raising a flag, and the capture loop
  serves it out of the request it is already holding.
- **`create_app`** — MJPEG is the starting transport because it needs no client
  library. Replacing it with WebRTC touches this module and the page, nothing
  else.

---

## Adding a processing stage

```python
# src/trilobite/processing/stages/my_stage.py
from pydantic import Field
from ...types import Frame
from ..base import Stage, StageParams
from ..registry import register

@register("dark_subtract")
class DarkSubtract(Stage):
    accepts = ("raw", "mono8", "mono16")

    class Params(StageParams):
        level: float = Field(0.0, ge=0.0, le=1024.0, description="Dark level, DN")

    def apply(self, frame: Frame) -> Frame:
        return frame.derive((frame.data.astype("f4") - self.params.level)
                            .clip(0).astype(frame.data.dtype))
```

Add `- {type: dark_subtract, name: dark}` to the camera's pipeline in the YAML.
That is all: the control appears in the browser with the right range and
tooltip, the value is validated on the way in, and it is recorded in the
sidecar of every image captured afterwards.

Field conventions the UI honours:

- `json_schema_extra={"widget": "box"}` — a number box, no slider. Use it where
  a slider is all travel and no precision.
- `json_schema_extra={"widget": "hidden"}` — not shown. For parameters that are
  units rather than knobs, such as the MLA reference resolution.
- An unbounded field, or one whose range spans more than ~500×, gets a box
  automatically. A bar with no upper bound is not a bar.

If a stage builds anything frame-independent, cache it keyed on the parameters
and the frame shape — see `MLAGridOverlay._masks`. Rebuilding the grid
coordinates every frame cost 22 ms; caching them cost 8 ms.

---

## Development

```bash
pytest                       # 83 tests, no hardware
ruff check src/ tests/
python -m trilobite --config config/desktop-plenoptic.yaml
```

The tests cover the MLA geometry (including the preview-to-sensor scaling that
calibration depends on), the pipeline and parameter plumbing, corner detection
end to end against the synthetic lenslet array, and storage device selection
including a simulated hot-unplug.

For UI changes, drive the page headlessly with Playwright against a running
instance rather than eyeballing it — several of the bugs in this project's
history (dead buttons from the connection budget, a slider that rejected valid
values, a build race between the two modes) were visible only in a real
browser.

---

## Documents

| file | what it is |
|---|---|
| `docs/calibration-spec.md` | the imaging model, the unknowns, the measurements, the fit. Start here. |
| `docs/calibration-ui-spec.md` | the acquisition workflow: detection, acceptance, what gets saved, what is built |
| `docs/pi-troubleshooting.md` | when the Pi will not answer at all. Not needed for a normal setup. |
| `docs/cleanup-log.md` | removals and their rollback, newest first |
| `matlab/README.md` | reading captures in MATLAB, and the traps between NumPy and MATLAB |
| `apt-packages.txt` | every Pi package, tiered and annotated |

---

## Things that will bite you

**Only the capture thread may touch a camera.** An earlier design had a
detection worker calling `capture_request()` on its own schedule, competing
with the preview loop for a four-deep picamera2 buffer pool. It took the rig
down repeatedly; a four-core CPU stress test did not, which is how the camera
path rather than the load was identified. Anything wanting a full-resolution
frame now raises a flag and the capture loop serves it. If you add a feature
that needs pixels, use `CameraRuntime.grab_full()` — do not open a second
consumer.

**A Pi 5 with two cameras still wants a real 5 V / 5 A supply.** The header
shows CPU temperature, load and free memory, and turns red if
`vcgencmd get_throttled` reports under-voltage at any point since boot — that
bit is sticky, so it survives the crash it caused. `bash
scripts/diagnose_host.sh` reads it out with everything else worth knowing.

**The Pi 5 has no hardware video encoder.** VideoCore VII dropped H.264 and
JPEG encode, so every preview frame is compressed on the CPU. Encode the small
`lores` stream and keep `preview_fps` well below the sensor rate. Two
1456 × 1088 streams at 60 fps is 190 MB/s of raw pixels.

**The mono IMX296 advertises no white-balance control.** `AwbEnable` in a
config is dropped with a warning rather than aborting startup, but it does not
belong there.

**Auto-exposure ruins calibration.** `AeEnable: false` is set in
`config/pi.yaml` on purpose: frames taken under auto-exposure are not
comparable to each other. Turning AE off pins the exposure it had converged to,
and the UI writes that number back into the box.

**MLA parameters are in SENSOR pixels.** The number on screen is the sensor
one: pitch 100 means 100 px on the 1456-wide sensor and draws as 50 px on the
728-wide preview. It was the other way round until a stored alignment turned out
to mean something different from what every consumer of it assumed. A stored
grid carrying an older preview-referenced reference is rebased once, at
start-up, with a warning in the log.

**A raw buffer is not an image until two things are undone.** Its rows are
padded to a 64-byte stride, and the array is shaped by that stride *in bytes*
and delivered as uint8 whatever the real pixel size is — so 10-bit `R10` at
1456 px arrives as 1088 × **2944** uint8, which is 1472 uint16 pixels, not 1456
pixels plus padding. The capture path works out the bytes per pixel from the
row length, re-views the buffer, then trims. Left undone, the padding makes any
rescale of the MLA grid anisotropic and moves the frame centre — which the grid
hangs off — by half the padding.

**The Pi 5's default raw format is COMPRESSED.** libcamera hands the mono
IMX296 `MONO_PISP_COMP1` unless told otherwise — the imaging pipeline's
compressed transport, one byte per pixel, not sensor counts. `make_array`
returns it as a plain uint8 image, so captures have the right shape and obvious
structure and every value wrong. It looks like a picture that has gone slightly
wrong rather than like a decode failure, which is why a whole session was
recorded that way before anyone noticed. The backend now picks an uncompressed
format itself, records which one in every sidecar, and logs an error if it
cannot find one. The same code on a Pi 4 got `R10` and was fine.

**The rig's IP address will change.** DHCP does that. Reach it by name
(`flyeye.local`), or give it a reservation in your router. When it has moved
anyway, the rig will tell you where it went: `python -m trilobite.net` on the
Pi, `journalctl -u trilobite | grep -A6 'reachable at'`, or
`curl -s http://.../api/status | jq -r '.network.urls[]'`.

**Put the data on an SSD.** See **Output storage**.

---

## Roadmap

1. Confirm both cameras enumerate on the Pi (`probe_cameras.py --grab`; if
   libcamera reports zero, `diagnose_cameras.sh`).
2. Both previews live; check `fps` and `errors` in `/api/status`.
3. Exposure and gain sweeps, watching `stat_saturated_fraction` from the
   `stats` stage.
4. Mount the MLA and align it with `mla_grid_overlay`.
5. Decide the calibration target (`docs/calibration-ui-spec.md` §10.1) and the
   lenslet aperture shape.
6. Enable the `checkerboard_presence` stage in `config/pi.yaml` and set its
   `min_corners` from what the Preconditions panel says your board implies.
7. Run a session. Set `min corners`, `target per tile` and the capture gates
   from what the rig actually does rather than from the defaults here.
8. The fit, and full-field corner detection. Offline, off-device.
9. Video recording. Store lossless and compress off-device; do not reach for
   H.264 on a Pi 5.
10. Hardware sync between the two sensors (XVS).
11. `lenslet_extract`. Read its docstring first — there is a decision about the
    `Frame` type to make before writing any of it.
