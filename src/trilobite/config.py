"""Configuration schema and loading.

One YAML file describes the rig: which cameras exist, how each is opened, and
what processing stages sit behind each one. Nothing about the rig is hardcoded
in the Python. Swapping IMX296 for an event camera, or adding a third stage,
is a config edit plus a class, never a rewrite of the app.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class StageConfig(BaseModel):
    """One processing stage. `type` selects the class from the stage registry."""

    type: str
    name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class CameraConfig(BaseModel):
    # Physical identity: used in filenames, sidecars, API paths and the UI.
    # Use words that mean something at the bench -- "left", "right" -- not
    # indices. The libcamera index is a wiring detail and lives in `index`
    # alone, so if you swap the ribbon cables you change one number and every
    # file written before and after still names the physical camera correctly.
    cam_id: str

    # Display label. Defaults to a title-cased cam_id.
    label: str | None = None

    backend: Literal["picamera2", "replay", "synthetic"] = "picamera2"

    # picamera2: index into the list from Picamera2.global_camera_info().
    # Port 0 (the connector nearest the USB ports on a Pi 5) is normally 0.
    index: int = 0

    # Full-resolution stream. This is the science path: what gets written to
    # disk on capture. null means "sensor native".
    full_resolution: tuple[int, int] | None = None

    # Low-resolution stream for the browser preview. picamera2 produces this
    # in parallel on the ISP, so previewing costs almost nothing. Keep it small
    # -- the Pi 5 has no hardware JPEG encoder, so every preview frame is
    # compressed on the CPU.
    preview_resolution: tuple[int, int] = (640, 480)

    # Frames per second requested from the sensor.
    fps: float = 30.0

    # Mirror the sensor image. Applied at ACQUISITION, so the preview, the raw
    # captures, the sub-aperture crops and the calibration corners all see the
    # same orientation -- there is no way for the display and the saved data to
    # disagree, because there is only one flip and it happens before anything
    # else looks at the pixels.
    #
    # This is a statement about how the camera is MOUNTED (a fold mirror, an
    # inverted bracket), not a display preference, which is why it lives with
    # the camera rather than in the pipeline.
    #
    # Changing it invalidates an MLA alignment: the grid offsets are measured
    # from the frame centre, and a flip negates the axis they are measured
    # along. Set it before aligning, and re-check the grid if you change it.
    flip_horizontal: bool = False
    flip_vertical: bool = False

    # Quarter-turn rotation of the whole frame, CLOCKWISE as you look at the
    # image, applied at acquisition BEFORE the two mirrors above -- so the
    # mirrors mean "flip what I am looking at", not "flip the sensor".
    #
    # 90 and 270 SWAP WIDTH AND HEIGHT, and that swap is the whole reason this
    # is a camera setting rather than a display one. Everything downstream --
    # the MLA reference frame, the readiness arithmetic, the session manifest,
    # the .npy on disk -- takes its geometry from CameraInfo.full_resolution
    # and CameraInfo.preview_resolution, and those report the size AFTER this
    # rotation. `describe()` is the single place the post-rotation size is
    # decided; nothing else may assume a landscape frame.
    #
    # The raw stride trim is the one thing that must NOT see the rotation: row
    # padding is a property of the buffer as the sensor delivers it, so it is
    # removed against the native sensor width first and the frame is turned
    # afterwards. See PiCamera2Source._trim_stride.
    #
    # Like the mirrors, changing this invalidates an MLA alignment: pitch is
    # unchanged by a quarter turn but the offsets swap axes and one changes
    # sign.
    rotate_deg: Literal[0, 90, 180, 270] = 0

    # Frames per second the preview PIPELINE runs at. Not the sensor rate, and
    # not the browser rate.
    #
    # The sensor is drained at `fps` because it must be -- an unreleased
    # request starves the pool -- but running stats, levels, the grid overlay
    # and the presence map on every one of those frames, twice over for two
    # cameras, is what makes parameter edits feel slow: the web thread competes
    # with 60 pipeline passes a second for the same cores. Frames arriving
    # faster than this are released without being decoded or processed.
    #
    # null means "match server.preview_fps", which is the only rate anything
    # actually consumes. Raise it only if something other than the browser
    # starts reading the preview bus.
    process_fps: float | None = None

    # Force a specific raw stream format, e.g. "R10" or "R12".
    #
    # Leave null and libcamera picks for you -- on a Pi 5 that is
    # MONO_PISP_COMP1, a *companded* encoding. It is visually lossless but it
    # is not linear sensor data, so it is wrong for radiometric work and for
    # anything that fits a model to pixel values. Set an uncompressed format
    # here once probe_cameras.py has told you which ones this sensor offers.
    raw_format: str | None = None

    # Camera controls passed straight to libcamera, e.g.
    #   {ExposureTime: 5000, AnalogueGain: 1.0, AeEnable: false}
    # For calibration you almost always want AeEnable and AwbEnable off so
    # that frames are comparable.
    controls: dict[str, Any] = Field(default_factory=dict)

    # replay backend only: directory of images to loop over.
    source_dir: str | None = None

    # synthetic backend only. "gratings" is the drifting sinusoid used for
    # checking that a processing stage does what it claims. "plenoptic_board"
    # renders a lenslet array whose every micro-image contains a complete
    # checkerboard, which is what makes the calibration corner detector
    # exercisable end to end with no camera attached -- worth having, because
    # the detector's failure modes (wrong crop, wrong scale, wrong board size)
    # all look identical from the outside.
    synthetic_pattern: Literal["gratings", "plenoptic_board"] = "gratings"
    # Micro-image pitch of the simulated array, in FULL-RESOLUTION pixels.
    synthetic_pitch_px: float = 100.0
    synthetic_rotation_deg: float = 0.0
    # Inner corners per micro-image. The calibration board settings must be set
    # to match these, or detection will correctly find nothing.
    synthetic_board: tuple[int, int] = (4, 3)
    # Slow drift of the whole array, in full-resolution pixels. Non-zero by
    # default so the capture loop's settle and movement gates are exercised
    # every time the synthetic config is run. Set to 0 when a test needs the
    # grid to sit exactly where the geometry says it does.
    synthetic_drift_px: float = 3.0

    pipeline: list[StageConfig] = Field(default_factory=list)


class StorageConfig(BaseModel):
    # Put this on a USB SSD or NVMe HAT, not the SD card. Continuous image
    # capture will wear out an SD card and the write bandwidth is a bottleneck.
    root: str = "~/trilobite-data"

    # Raw stills are written as .npy plus a JSON sidecar by default: lossless,
    # no ISP, and trivially loadable in numpy on the desktop.
    still_format: Literal["npy", "png", "tiff"] = "npy"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    # Preview frames per second pushed to the browser. Independent of sensor
    # fps. Decoupling these is what keeps a slow browser from stalling capture.
    preview_fps: float = 12.0
    jpeg_quality: int = 80


class AppConfig(BaseModel):
    cameras: list[CameraConfig] = Field(default_factory=list)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    log_level: str = "INFO"

    @property
    def storage_root(self) -> Path:
        return Path(os.path.expanduser(self.storage.root))


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a config file. Raises on anything malformed.

    Failing at startup with a clear pydantic error beats discovering a typo
    three hours into a capture session.
    """
    p = Path(os.path.expanduser(str(path)))
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return AppConfig.model_validate(raw)
