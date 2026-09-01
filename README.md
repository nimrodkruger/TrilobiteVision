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
| Calibration: live per-tile corner detection | working — **records nothing** |
| Calibration: pose recording and session files | not built |
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

### Raspberry Pi 5

Nothing below exists on a freshly flashed Pi. `install_pi.sh` creates all of
it and is safe to re-run.

```bash
git clone <your-repo> ~/TrilobiteVision
cd ~/TrilobiteVision
bash scripts/install_pi.sh
sudo reboot                       # required: /boot/firmware/config.txt changed
```

```bash
source ~/.venvs/trilobite/bin/activate
python scripts/probe_cameras.py --grab      # confirm indices and sensor modes
python -m trilobite --config config/pi.yaml
```

Three things the script does that are not optional:

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

Verify with `rpicam-hello --list-cameras` before blaming the Python. If
libcamera reports zero cameras, run `bash scripts/diagnose_cameras.sh`.

**Do not pip-install OpenCV on the Pi.** The apt build shares numpy's ABI with
picamera2; a pip wheel alongside it gives two `cv2` modules and an import order
that decides which one you get.

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
ssh flyeye@<address> 'cd ~/TrilobiteVision && git pull && sudo systemctl restart trilobite'
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
the ISP output, because the ISP's companding moves no corner. That is
`CameraSource.read_full_mono()`.

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
are set in **preview pixels**, and the frame size they were measured against is
recorded with them; anything reading a different-sized frame converts through
`MLAGridOverlay.geometry_for()`.

The Storage panel lives here — see below.

### Calibration mode

A left rail of settings (board, nominal optics, acceptance, detection) and a
stage showing what the nominal optics imply, the preconditions, the frozen
alignment, and the live views.

**Start detection** runs per-tile checkerboard detection at 1–2 Hz on
full-resolution frames and **writes nothing**. It answers the question that has
to be answered before pose recording is worth building: does the detector find
the board in the micro-images, and in enough of them at once?

Each camera shows the annotated frame — the same frame the numbers came from,
tiles boxed by verdict, corners drawn where they were measured — and a coverage
lattice with one cell per lattice position: *cross*, *found*, *nothing found*,
*not a whole tile*. The lattice is not redundant with the picture: a dead
column at the edge of the array is obvious as a column of cells and invisible
as a few missing boxes in a busy image.

To rehearse it with no camera, run `config/desktop-plenoptic.yaml` and set the
board to **4 × 3 inner corners, 7 mm**.

### Why the tiles are polled and not streamed

A browser allows about six concurrent HTTP/1.1 connections per origin, and an
MJPEG stream holds one open forever. Two cameras with a preview and three tiles
each is eight — over the limit, at which point every other request on the page,
including every button press, queues behind them and never completes. The
symptom is a UI whose controls silently do nothing.

So: exactly one persistent stream per camera, everything else polled as
single-shot JPEGs. This is a constraint, not a tuning parameter.

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
| GET, PUT | `/api/calibration/settings` | board, nominal optics, acceptance, detection |
| GET | `/api/calibration/readiness` | preconditions, split blocking vs advisory |
| POST | `/api/calibration/start` | begin live detection — records nothing |
| POST | `/api/calibration/stop` | stop it |
| GET | `/api/calibration/detection` | latest pass per camera, per-tile verdicts |
| GET | `/calibration/detection/{cam}.jpg` | that pass, annotated |
| GET | `/api/storage` | filesystems on offer, mounted or not, and where output is going |
| POST | `/api/storage/target` | `{"path": "/media/stick"}` — move output there |
| POST | `/api/storage/mount` | `{"device": "/dev/sda1"}` — mount a plugged-in disk |
| POST | `/api/storage/unmount` | `{"device": "/dev/sda1"}` — so it can be pulled safely |
| POST | `/api/storage/release` | back to `storage.root` |
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
    settings.py            board, nominal optics, acceptance, readiness checks
    detect.py              live per-tile corner detection (requires cv2)
  sinks/jpeg.py            JPEG encode: simplejpeg → cv2 → Pillow
  storage/writer.py        session directories, .npy + sidecar, live retargeting
  storage/devices.py       which filesystems can hold a session, right now
  web/server.py            FastAPI: streams, parameters, controls, capture
  web/static/index.html    the dashboard, generated from the stage schemas
config/                    rig configurations, heavily commented
docs/                      the calibration model and the acquisition workflow
scripts/
  probe_cameras.py         run this first on the Pi
  install_pi.sh            apt, config.txt, venv — idempotent
  diagnose_cameras.sh      when libcamera reports zero cameras
  diagnose_host.sh         after a crash, or when a disk does not appear
  measure_derotation_cost.py   what tile de-rotation actually costs in precision
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
  perturb capture timing.
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
| `docs/cleanup-log.md` | removals and their rollback, newest first |
| `apt-packages.txt` | every Pi package, tiered and annotated |

---

## Things that will bite you

**A Pi 5 with two cameras needs a real 5 V / 5 A supply.** The first
calibration run on the rig took the board down. Sustained detection is the
first workload here that can saturate all four cores, and the resulting current
spike browns out a marginal supply — a hard reset, with nothing in any log. The
header shows CPU temperature and load, and turns red if
`vcgencmd get_throttled` reports under-voltage at any point since boot; that
bit is sticky, so it survives the crash it caused. `bash
scripts/diagnose_host.sh` reads it out with everything else worth knowing.

Detection is bounded so it cannot be the software's fault either: cameras take
turns by default, a duty-cycle cap keeps each worker under half a core, the
threads run at lowered priority, and a grid that would yield more tiles than
`max_tiles` is refused before it starts rather than discovered at runtime. All
four are in the Detection panel.

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

**MLA parameters are in preview pixels.** Pitch 50 on a 728-wide preview is
pitch 100 on the sensor. The conversion is handled in one place
(`MLAGeometry.rescaled`) and the reference resolution is recorded with the
parameters, but the number on screen is the preview one.

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
6. Run live detection against the real rig and set `min corners` and
   `target per tile` from what the detector actually returns.
7. Pose recording — `calibration/session.py`, per `docs/calibration-ui-spec.md`
   §7 and §8.
8. The fit. Offline, off-device.
9. Video recording. Store lossless and compress off-device; do not reach for
   H.264 on a Pi 5.
10. Hardware sync between the two sensors (XVS).
11. `lenslet_extract`. Read its docstring first — there is a decision about the
    `Frame` type to make before writing any of it.
