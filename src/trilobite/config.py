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
