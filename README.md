# TrilobiteVision

Dual-camera capture, configurable processing and browser preview for a
Raspberry Pi 5 with two IMX296 global shutter cameras. Built as a skeleton for
plenoptic camera calibration work.

### Names used here

| thing | name | why |
|---|---|---|
| repo / project folder | `TrilobiteVision` | the project |
| distribution (pyproject `name`) | `trilobitevision` | matches the repo |
| Python import package | `trilobite` | what you type: `python -m trilobite` |
| venv | `~/.venvs/trilobite` | created by `install_pi.sh`; does not exist until then |
| systemd unit | `trilobite.service` | `systemctl status trilobite` |
| data directory | `~/trilobite-data` | change via `storage.root` in the config |
| the Pi itself | `flyeye` | hostname and login user, unrelated to the code |
| the cameras | `cam0`, `cam1` | `cam_id` in the config; rename them freely |

The import package is shortened to `trilobite` so commands stay typeable. To
use the full name instead, rename `src/trilobite/` and replace `trilobite`
with `trilobitevision` in `pyproject.toml` and in the imports.


---

## The two paths

The single most important idea in this codebase. Every frame goes down one of
two paths and they must not be confused:

| | **view path** | **science path** |
|---|---|---|
| source | `lores` stream, ISP-processed | `raw` stream, ISP bypassed |
| resolution | 728×544 | 1456×1088 native |
| processing | gamma, gain, overlays | none |
| destination | JPEG → browser | `.npy` + JSON sidecar → disk |
| purpose | is it in focus? is it aligned? | measurement |

The view path is allowed to lie — stretch contrast, draw grids, decimate. The
science path is not allowed to touch a pixel. picamera2 produces both streams
from the same sensor read, so the view path is nearly free and the two are
always in temporal agreement.

Conflating them is the classic way to lose a week: you calibrate against
gamma-corrected preview frames and every radiometric result is wrong.

---

## Architecture

```
                    ┌──────────────┐
   sensor ─────────▶│ CameraSource │  picamera2 │ synthetic │ replay
                    └──────┬───────┘
                           │ Frame (pixels + sensor metadata + timestamps)
              ┌────────────┴────────────┐
              ▼                         ▼
       ┌─────────────┐          ┌──────────────┐
       │  Pipeline   │          │ capture_full │  ─── raw, unprocessed
       │  (view)     │          └──────┬───────┘
       └──────┬──────┘                 │
              ▼                        ▼
       ┌─────────────┐          ┌──────────────┐
       │ LatestFrame │          │SessionWriter │  .npy + .json sidecar
       │    (bus)    │          └──────────────┘
       └──────┬──────┘
              ▼
       ┌─────────────┐
       │ JPEG → HTTP │  MJPEG to the browser
       └─────────────┘
```

Four seams, each placed where a future change is expected:

**`CameraSource`** (`cameras/base.py`) — the whole stack runs with no hardware
via the `synthetic` and `replay` backends. You develop the web UI, the
parameter plumbing and the storage layout on Windows, and an event camera or a
machine-vision USB3 camera later is a subclass, not a refactor.

**`Stage`** (`processing/base.py`) — a stage *declares* its parameters as a
pydantic model. From that declaration the system derives validation, the
browser controls, and the settings record saved with every image. Adding a
stage is one file; the UI updates itself. This is the thing that stops the
codebase needing a rebuild as it grows.

**`LatestFrame` / `FrameQueue`** (`bus.py`) — the capture thread only reads,
processes and publishes. It never encodes, writes or waits on the network, so
a slow browser cannot perturb capture timing.

**`create_app`** (`web/server.py`) — MJPEG is the starting transport because
it works everywhere with no client library. Replacing it with WebRTC later
touches this module and the static page, nothing else.

### Files

```
src/trilobite/
  types.py                 Frame, CameraInfo. Metadata travels with pixels.
  config.py                pydantic schema for the rig YAML
  bus.py                   LatestFrame (newest wins), FrameQueue (counts drops)
  app.py                   CameraRuntime (thread per camera), Application
  __main__.py              CLI entry point
  cameras/
    base.py                CameraSource ABC — the hardware seam
    picam.py               Picamera2 backend, main+lores+raw stream config
    offline.py             synthetic and replay backends (no hardware)
    registry.py            backend lookup, libcamera discovery
  processing/
    base.py                Stage ABC + StageParams — the parameter contract
    registry.py            @register decorator, stage catalogue
    pipeline.py            ordered runner, live reconfiguration, timing
    stages/basic.py        passthrough, levels, crop, downsample, stats
    stages/plenoptic.py    MLA grid overlay; lenslet_extract stub
  sinks/jpeg.py            JPEG encode (simplejpeg → cv2 → PIL)
  storage/writer.py        session dirs, .npy + JSON sidecar
  web/server.py            FastAPI: stream, params, controls, capture
  web/static/index.html    UI generated from the stage schemas
scripts/probe_cameras.py   run this first, on the Pi
scripts/install_pi.sh      apt + config.txt + venv, idempotent
systemd/trilobite.service     run as a service
tests/test_pipeline.py     runs anywhere, no camera needed
```

---

## Environment

### On the Pi

Nothing below exists on a freshly flashed Pi -- no venv, no Python packages,
no camera overlays. `install_pi.sh` creates all of it and is safe to re-run.

```bash
git clone <your-repo> ~/TrilobiteVision
cd ~/TrilobiteVision
bash scripts/install_pi.sh      # apt packages, config.txt, venv, editable install
sudo reboot                     # required: config.txt changed
```

Then:

```bash
source ~/.venvs/trilobite/bin/activate
python scripts/probe_cameras.py --grab
python -m trilobite --config config/pi.yaml
```

Open `http://<pi-address>:8000/`.

**What the install script does, and why each piece matters:**

*apt, not pip, for the camera stack.* `python3-picamera2` is a C++ extension
built against the system libcamera. The pip version drifts out of step and
produces import errors or, worse, silent format mismatches. `python3-numpy`,
`python3-opencv` and `python3-simplejpeg` come from apt too — for ABI
compatibility with picamera2's buffers, and to avoid a 40-minute OpenCV source
build on the Pi.

*The venv must be created with `--system-site-packages`.* Raspberry Pi OS is
an externally-managed environment (PEP 668), so pip refuses to install into
the system Python; but picamera2 lives in the system site-packages. Without
that flag, the venv cannot see the camera library and every import fails with
a `ModuleNotFoundError` that looks like a broken install. This one flag causes
more wasted hours on Pi camera projects than anything else.

*Two explicit camera overlays.* In `/boot/firmware/config.txt`:

```
camera_auto_detect=0
dtoverlay=imx296,cam0
dtoverlay=imx296,cam1
```

Auto-detection is reliable for one camera and not for two. `cam0` and `cam1`
name the Pi 5's two CSI connectors. A reboot is required.

Verify with `rpicam-hello --list-cameras` before blaming the Python.

### On the Windows desktop

```powershell
git clone <your-repo>
cd TrilobiteVision
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[desktop,dev]"
python -m trilobite --config config/desktop.yaml
pytest
```

No Pi, no cameras, no libcamera. Two synthetic cameras produce a drifting
grating and a fixed grid; the full web UI, the whole pipeline and the storage
layer all run. `picamera2` is imported lazily inside `Picamera2Source.open()`
precisely so this works.

Develop here. Deploy to the Pi to test the parts that actually need photons.

### Deploy loop

```powershell
# Windows
git add -A; git commit -m "..."; git push
```

```bash
# Pi
ssh flyeye@<address> 'cd ~/TrilobiteVision && git pull && sudo systemctl restart trilobite'
```

During active development, leave the systemd service **disabled** and run the
app by hand in an SSH terminal — a service holding the cameras will make your
manual runs fail with a confusing "device busy".

---

## API

| method | path | purpose |
|---|---|---|
| GET | `/` | UI |
| GET | `/api/status` | uptime, per-camera fps, error counts, session dir |
| GET | `/api/cameras` | camera list and hardware description |
| GET | `/api/stage-types` | every registered stage type and its schema |
| GET | `/stream/{cam}.mjpg` | MJPEG preview |
| GET | `/snapshot/{cam}.jpg` | single preview frame |
| GET | `/api/pipeline/{cam}` | stages, schemas, current values, per-stage ms |
| POST | `/api/pipeline/{cam}/{stage}` | `{"values": {...}}` — validated, 422 on bad input |
| POST | `/api/pipeline/{cam}/_add` | insert a stage at runtime |
| DELETE | `/api/pipeline/{cam}/{stage}` | remove a stage |
| POST | `/api/controls/{cam}` | sensor controls: `ExposureTime`, `AnalogueGain`, ... |
| POST | `/api/capture/{cam}?raw=true` | full-res capture with sidecar |
| POST | `/api/capture-all?raw=true` | trigger every camera |

Sensor controls and pipeline parameters are deliberately separate endpoints.
One changes what the sensor does, the other changes what happens to the
numbers afterwards; conflating them makes it impossible to tell whether a
change was optical or computational.

---

## Adding a processing stage

The whole procedure:

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
That is all. The slider appears in the browser with the right range and
tooltip, the value is validated on the way in, and it is recorded in the
sidecar of every image captured afterwards.

If a stage builds anything frame-independent, cache it keyed on the parameters
and the frame shape — see `MLAGridOverlay._grid_mask`. Rebuilding a coordinate
grid every frame cost 22 ms; caching it cost 8 ms.

---

## Things that will bite you

**The Pi 5 has no hardware video encoder.** VideoCore VII dropped H.264 and
JPEG encode. Every preview frame is compressed on the CPU. Hence: encode the
small `lores` stream, and cap `preview_fps` well below the sensor rate. Two
1456×1088 streams at 60 fps is 190 MB/s of raw pixels — Python is not going to
process that, and the design assumes it will not try.

**`capture-all` is not synchronised capture.** The requests go out
sequentially and the sensors free-run, so the two frames land tens of
milliseconds apart. Measured on the synthetic backend: ~70 ms. For stereo or
plenoptic work needing real simultaneity, wire the IMX296 XVS sync pins
together and drive an external trigger. Software cannot fix this.

**Auto-exposure ruins calibration.** `AeEnable: false` and `AwbEnable: false`
are set in `config/pi.yaml` on purpose. Frames taken under auto-exposure are
not comparable to each other.

**Put the data on an SSD.** Continuous capture to the SD card is slow and
wears it out. Change `storage.root` to a mounted USB SSD or an NVMe HAT.

**Release the cameras on exit.** libcamera does not always recover from a
process killed while holding a sensor; the fix is a reboot. SIGINT and SIGTERM
are handled, so use Ctrl-C rather than `kill -9`.

---

## Next steps, roughly in order

1. `probe_cameras.py --grab` on the Pi. Confirm indices, sensor modes, and
   whether your IMX296 is the mono or colour variant. Fix `config/pi.yaml`.
2. Get both previews live. Check `fps` and `errors` in `/api/status`.
3. Exposure and gain sweeps via `/api/controls`, watching
   `stat_saturated_fraction` from the `stats` stage.
4. Mount the MLA. Use `mla_grid_overlay` to align it physically.
5. Video recording — a `Recorder` consuming a `FrameQueue`, writing raw frames.
   Do not reach for H.264 on the Pi 5; store lossless and compress off-device.
6. Hardware sync between the two sensors.
7. `lenslet_extract`. Read its docstring first — there is a design decision
   about the `Frame` type that needs making before writing any of it.
