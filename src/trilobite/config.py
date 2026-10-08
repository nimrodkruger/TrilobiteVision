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
    # Raw admission is the one thing that must NOT see the rotation: row
    # padding is a property of the buffer as the sensor delivers it, so it is
    # removed against the native sensor width first and the frame is turned
    # afterwards. See cameras/rawformat.py:admit.
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

    # The escape hatch, and it is deliberately awkward to reach.
    #
    # By default the camera REFUSES TO OPEN if the raw format it would capture
    # in cannot be established as linear sensor counts -- compressed, packed,
    # or simply a name this code does not know. That is the opposite of the old
    # behaviour, which logged an error and carried on, and it is the change
    # that closes the failure that cost a recording session: 1,400 files of
    # MONO_PISP_COMP1 that looked like slightly damaged pictures.
    #
    # Setting this true says "open anyway, I want to look at something". What
    # comes back is then tagged `diagnostic` rather than `science`: it is named
    # `diagnostic_...` on disk, `validity: diagnostic` goes in every sidecar it
    # touches, and both offline readers refuse it for anything that fits a
    # model. It is for bringing a new sensor up, not for capturing data.
    allow_unvalidated_raw: bool = False

    # Where the sensor sample sits inside the container word it is delivered
    # in. Supervisory review R3, and the Picamera2 manual's own warning.
    #
    #   lsb   right-aligned. A 10-bit sample occupies bits 0-9 of a uint16;
    #         values run 0..1023.
    #   msb   left-aligned, which is what the Picamera2 manual (raw stream
    #         configuration, pp. 21-22) describes for the Pi 5. A 10-bit
    #         sample occupies bits 6-15; values run 0..65472 in steps of 64,
    #         and the true sample is `value >> 6`.
    #
    # THE FORMAT NAME DOES NOT SAY WHICH. `R10` names the sensor's sample
    # depth; the manual explicitly warns against deriving more from it. The two
    # readings differ by a factor of 64 in every pixel, so this is declared
    # rather than guessed: a dark left-aligned frame and a bright right-aligned
    # one have indistinguishable histograms, and a guess would enter the record
    # with the same confidence as a measurement.
    #
    # Getting it wrong one way is loud. Declaring `lsb` against left-aligned
    # data puts values far above the 10-bit ceiling, and admission refuses the
    # buffer and names this setting in the refusal. The other way is quiet --
    # `msb` against right-aligned data just reads very dark -- so confirm it
    # once against a bright target. `raw_observed_max` is in every sidecar for
    # exactly that check.
    #
    # The pixels on disk are NEVER shifted to match. `raw_sample_shift` is
    # recorded and the readers apply it; silently rescaling every value on the
    # way to storage is the thing this whole boundary exists to prevent.
    raw_alignment: Literal["lsb", "msb"] = "lsb"

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

    # Free space that must survive every write, on the SELECTED (removable)
    # target. A session that fills the disk it is writing to loses the tail of
    # itself and gives no warning on the way, because the first sign is a
    # failed write. 2 GB is about ten seconds of a full-rate pair recording,
    # which is enough to stop cleanly and write a manifest.
    reserve_mb: int = 2048

    # The same thing for the INTERNAL disk, and deliberately much larger. The
    # internal root on this rig is the SD card the operating system runs from:
    # filling it is not a lost session but a Pi that will not boot, and the
    # recovery is a reflash. Any fallback write -- and any deliberate
    # internal-storage override -- is refused below this.
    internal_reserve_mb: int = 8192

    # -- recording (Stage 5) ---------------------------------------------

    # Fraction of MemAvailable the burst buffer may claim, measured at arm
    # time rather than taken from a constant: the right number on an 8 GB
    # board is the wrong one on a 4 GB board, and `np.empty` succeeding proves
    # nothing because Linux does not commit pages until they are touched.
    # Half leaves room for the application, libcamera's own buffer pools, and
    # the flush working set -- dirty page cache counts against available
    # memory until writeback completes, so a large write shrinks free RAM
    # while the buffer is still held.
    burst_memory_fraction: float = 0.5

    # Hard ceiling on the burst buffer regardless of what is available.
    burst_max_mb: int = 4096

    # Fraction of MemAvailable the continuous recorder's queues may claim, in
    # total across all heads. Smaller than the burst fraction because the
    # queue is only there to ride out a stall: no finite queue survives a
    # sustained deficit, so making it bigger buys seconds and costs the
    # headroom the writer needs.
    queue_memory_fraction: float = 0.25

    # Frames per chunk file, per head. 128 x 1.5 MiB is about 194 MiB, which
    # is a comfortable file and keeps the file count for a five-minute
    # recording in the dozens rather than the tens of thousands.
    chunk_frames: int = 128

    # The write unit inside a chunk, in frames. Large enough that the
    # per-write overhead disappears, small enough that a stall is noticed
    # before the queue drains. fsync happens at this boundary, never per
    # frame: 60 fsyncs a second destroys sustained throughput and a completion
    # journal gives the same guarantee more cheaply.
    write_block_frames: int = 4

    # Fraction of exposed frames a continuous recording may lose before it
    # stops with an explicit incomplete result. Dropping is permitted and
    # counted; past some point the honest answer is that this target cannot do
    # this job, and continuing to shed frames produces a recording nobody will
    # trust. Recorded in every manifest.
    max_drop_fraction: float = 0.10

    # Preview rate while a recording is running, in Hz. The browser cap
    # competes for the same cores as the JPEG encode and the write path, and a
    # preview frame is never worth a recorded frame.
    recording_preview_fps: float = 2.0


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
