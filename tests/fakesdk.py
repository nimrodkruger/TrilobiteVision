"""A fake Picamera2, so the real adapter can be exercised without a sensor.

## Why this exists rather than a richer synthetic camera

`SyntheticSource` is a `CameraSource`: it produces frames and bypasses
`Picamera2Source` entirely. Everything the sensor-portability review is about
lives in the part it bypasses -- `_configure_and_start`, the negotiated
read-back, `classify`, `admit`, the control intersection, the sensor-mode
depth. A synthetic camera cannot test any of it.

So this fakes the **SDK** instead, at the one seam the adapter has: `open()`
imports `picamera2` inside the function, so putting a module with a `Picamera2`
class into `sys.modules` makes the real adapter run against it.

## What it deliberately does

  * **Separates requested from negotiated.** `configure()` records what was
    asked for and `camera_configuration()` returns what the driver decided,
    which may differ -- a substituted format, an adjusted size, a stride the
    caller did not choose. Reading back what was requested is the bug class
    this fake exists to catch (supervisory review R5).
  * **Produces real-shaped byte buffers.** A raw plane is `height` rows of
    `stride` bytes, with the padding filled by a sentinel rather than zeros, so
    a reader that leaks padding into the image produces a visibly wrong value
    instead of a slightly dark edge.
  * **Builds expected values independently.** The buffers here are constructed
    from the fake's own parameters with no use of `rawformat`'s trimming or
    shifting helpers, so a test comparing the two is not comparing a function
    with itself.
  * **Carries a sensor mode with its own bit depth**, which is the quantity
    the review separates from the container width: `R16` is a 16-bit container
    and says nothing about whether the samples are 10, 12 or 16 bits.

Nothing here is a claim about a real module. These are capability fixtures:
"if a driver behaves this way, the adapter does that".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# The byte written into row padding. Not zero, and not a plausible sample
# value: a frame whose edge columns come back as 0xA5 is one where the stride
# was mishandled, and it says so loudly.
PAD_BYTE = 0xA5


def align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


@dataclass
class FakeMode:
    """One sensor mode, as `sensor_modes` reports them."""

    size: tuple[int, int]
    format: str                       # the packed/native name
    unpacked: str                     # what it unpacks to
    bit_depth: int                    # the SAMPLE depth, not the container
    fps: float = 30.0

    def as_dict(self) -> dict[str, Any]:
        return {"size": self.size, "format": self.format,
                "unpacked": self.unpacked, "bit_depth": self.bit_depth,
                "fps": self.fps}


@dataclass
class FakeSensor:
    """Everything about the pretend module that the adapter can discover."""

    model: str = "imx296"
    sensor_resolution: tuple[int, int] = (1456, 1088)
    modes: tuple[FakeMode, ...] = ()
    # The depth of the samples actually delivered, which the adapter should
    # take from the selected mode and NOT from the negotiated format's name.
    sample_bits: int = 10
    # Where in the 16-bit word the sample sits. 0 = lsb-aligned, 6 = a 10-bit
    # sample left-shifted into the top of the word.
    sample_shift: int = 0
    mono: bool = True
    # Row padding the driver adds, in bytes, on top of the unpadded row.
    stride_pad: int = 16
    # Controls the driver advertises: name -> (min, max, default).
    controls: dict[str, Any] = field(default_factory=dict)
    # Camera properties, as `global_camera_info()` reports them. UnitCellSize
    # is in NANOMETRES per the Picamera2 guide, x then y.
    properties: dict[str, Any] = field(default_factory=dict)
    # A format the driver substitutes for whatever was requested, to model the
    # failure the read-back exists to catch. None = honour the request.
    substitute_format: str | None = None
    # A size the driver adjusts the main stream to, modelling an alignment
    # constraint the caller did not ask for.
    adjust_main_to: tuple[int, int] | None = None
    adjust_lores_to: tuple[int, int] | None = None
    # Omit `stride` from the configuration, as older picamera2 builds do.
    report_stride: bool = True

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {"Model": self.model, "Location": 2, "Rotation": 0}
        out.update(self.properties)
        return out

    def mode_dicts(self) -> list[dict[str, Any]]:
        return [m.as_dict() for m in self.modes]


def imx296_mono() -> FakeSensor:
    """The current rig, as the baseline every case is compared against."""
    return FakeSensor(
        model="imx296",
        sensor_resolution=(1456, 1088),
        modes=(
            FakeMode((1456, 1088), "MONO_PISP_COMP1", "MONO_PISP_COMP1", 10),
            FakeMode((1456, 1088), "R10_CSI2P", "R10", 10),
            FakeMode((1456, 1088), "R8", "R8", 8),
        ),
        sample_bits=10,
        sample_shift=0,
        mono=True,
        # 1456 px at 2 bytes is 2912; the pipeline pads to 2944.
        stride_pad=32,
        controls={
            "ExposureTime": (60, 1_000_000, 10_000),
            "AnalogueGain": (1.0, 16.0, 1.0),
            "FrameDurationLimits": (1_000, 1_000_000, 33_333),
            "AeEnable": (False, True, True),
        },
        # 3.45 um, as nanometres, which is where the pitch should come from.
        properties={"UnitCellSize": (3450, 3450),
                    "PixelArraySize": (1456, 1088)},
    )


def deep_mono_16() -> FakeSensor:
    """12-bit samples in a 16-bit container, left-shifted.

    The case the review separates out: the negotiated format is `R16` and the
    samples are 12 bits. Anything that reads the depth off the format name is
    wrong by 16x here, and the picture still looks plausible.
    """
    return FakeSensor(
        model="fake-deep-mono",
        sensor_resolution=(1920, 1200),
        modes=(FakeMode((1920, 1200), "R12_CSI2P", "R16", 12),),
        sample_bits=12,
        sample_shift=4,
        mono=True,
        stride_pad=0,
        controls={"ExposureTime": (10, 500_000, 5_000),
                  "FrameDurationLimits": (2_000, 200_000, 20_000)},
        properties={"UnitCellSize": (2200, 2200)},
    )


def native_eight_bit() -> FakeSensor:
    """A sensor whose samples really are 8 bits.

    Storing those as uint8 is exact, not a reduction -- which is the
    distinction S7 is about.
    """
    return FakeSensor(
        model="fake-8bit",
        sensor_resolution=(640, 480),
        modes=(FakeMode((640, 480), "R8", "R8", 8, fps=60.0),),
        sample_bits=8,
        sample_shift=0,
        mono=True,
        stride_pad=0,
        controls={"FrameDurationLimits": (8_000, 100_000, 16_666)},
        properties={"UnitCellSize": (6000, 6000)},
    )


def bayer_sensor(pattern: str = "RGGB", bits: int = 12) -> FakeSensor:
    """A colour sensor, to keep mono out of the acquisition assumptions."""
    packed = f"S{pattern}{bits}_CSI2P"
    unpacked = f"S{pattern}{bits}"
    return FakeSensor(
        model=f"fake-bayer-{pattern.lower()}",
        sensor_resolution=(2028, 1520),
        modes=(FakeMode((2028, 1520), packed, unpacked, bits),),
        sample_bits=bits,
        sample_shift=0,
        mono=False,
        stride_pad=16,
        controls={"ExposureTime": (50, 400_000, 8_000),
                  "AnalogueGain": (1.0, 22.0, 1.0),
                  "AwbEnable": (False, True, True),
                  "ColourGains": (0.0, 32.0, None),
                  "FrameDurationLimits": (5_000, 300_000, 33_333)},
        properties={"UnitCellSize": (1550, 1550)},
    )


def no_unit_cell() -> FakeSensor:
    """A driver that does not report `UnitCellSize`.

    The physical pitch is then not available from the driver, and must come
    from somewhere with a recorded source -- or be reported as unavailable.
    """
    s = imx296_mono()
    s.model = "fake-no-properties"
    s.properties = {}
    return s


class FakeRequest:
    """One completed request, as `capture_request()` hands it over."""

    def __init__(self, arrays: dict[str, np.ndarray], metadata: dict[str, Any]):
        self._arrays = arrays
        self._metadata = metadata
        self.released = False

    def make_array(self, name: str) -> np.ndarray:
        if self.released:
            raise RuntimeError("make_array after release: the buffer is gone")
        if name not in self._arrays:
            raise KeyError(name)
        return self._arrays[name]

    def get_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def release(self) -> None:
        self.released = True


class FakeCamera:
    """The `Picamera2` surface `Picamera2Source` actually touches."""

    # Set by `install()`.
    sensors: list[FakeSensor] = []
    opened: list[FakeCamera] = []

    def __init__(self, index: int = 0):
        self.index = index
        self.sensor = self.sensors[index]
        self.sensor_resolution = self.sensor.sensor_resolution
        self.sensor_modes = self.sensor.mode_dicts()
        self.sensor_format = self.sensor.modes[0].format if self.sensor.modes else ""
        self.camera_controls = dict(self.sensor.controls)
        self.requested: dict[str, Any] | None = None
        self.negotiated: dict[str, Any] | None = None
        self.applied_controls: dict[str, Any] = {}
        self.started = False
        self.stopped = False
        self.closed = False
        self.close_raises = False
        self.frame = 0
        FakeCamera.opened.append(self)

    # -- enumeration -----------------------------------------------------

    @classmethod
    def global_camera_info(cls) -> list[dict[str, Any]]:
        return [s.info() for s in cls.sensors]

    # -- configuration ---------------------------------------------------

    def create_video_configuration(self, main=None, lores=None, raw=None,
                                   buffer_count=4, **_kw) -> dict[str, Any]:
        return {"main": dict(main or {}), "lores": dict(lores or {}),
                "raw": dict(raw or {}), "buffer_count": buffer_count}

    def configure(self, config: dict[str, Any]) -> None:
        """Negotiate. What comes back out is NOT what went in."""
        self.requested = {k: dict(v) if isinstance(v, dict) else v
                          for k, v in config.items()}
        s = self.sensor

        raw_req = dict(config.get("raw") or {})
        raw_fmt = s.substitute_format or raw_req.get("format") or (
            s.modes[0].unpacked if s.modes else "")
        raw_size = tuple(raw_req.get("size") or s.sensor_resolution)
        bpp = self._bytes_per_pixel(raw_fmt)
        raw = {"format": raw_fmt, "size": raw_size}
        if s.report_stride:
            raw["stride"] = align_up(int(raw_size[0] * bpp) + s.stride_pad, 1)

        main_size = tuple(s.adjust_main_to
                          or (config.get("main") or {}).get("size")
                          or s.sensor_resolution)
        lores_size = tuple(s.adjust_lores_to
                           or (config.get("lores") or {}).get("size")
                           or (640, 480))
        self.negotiated = {
            "raw": raw,
            "main": {"format": "BGR888" if not s.mono else "YUV420",
                     "size": main_size,
                     "stride": align_up(main_size[0] * (3 if not s.mono else 1), 64)},
            "lores": {"format": (config.get("lores") or {}).get("format", "YUV420"),
                      "size": lores_size,
                      "stride": align_up(lores_size[0], 64)},
            "buffer_count": config.get("buffer_count", 4),
        }

    def camera_configuration(self) -> dict[str, Any]:
        if self.negotiated is None:
            raise RuntimeError("camera_configuration before configure")
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in self.negotiated.items()}

    @staticmethod
    def _bytes_per_pixel(name: str) -> float:
        """Container width, from the format NAME alone.

        Deliberately a separate, dumb implementation: the point of this fake is
        to feed the adapter bytes whose layout was decided here rather than by
        the code being tested.
        """
        upper = name.upper()
        if upper.endswith("_CSI2P") or upper.endswith("P"):
            return 1.25            # packed 10-bit; not used by the main paths
        for width in ("16", "12", "10"):
            if upper.endswith(width):
                return 2.0
        if upper.endswith("8"):
            return 1.0
        return 2.0

    # -- controls --------------------------------------------------------

    def set_controls(self, controls: dict[str, Any]) -> None:
        unknown = sorted(set(controls) - set(self.camera_controls))
        if unknown:
            raise RuntimeError(f"unknown controls: {unknown}")
        self.applied_controls.update(controls)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        if self.close_raises:
            raise RuntimeError("close failed")
        self.closed = True

    # -- frames ----------------------------------------------------------

    def capture_request(self) -> FakeRequest:
        """A request whose raw plane is padded rows of real sample values.

        The values are a known ramp plus a corner marker, so a transposition,
        an off-by-one row and leaked padding are all distinguishable from each
        other in the result.
        """
        if not self.started:
            raise RuntimeError("capture_request before start")
        self.frame += 1
        s = self.sensor
        raw_cfg = self.negotiated["raw"]
        width, height = raw_cfg["size"]
        bpp = self._bytes_per_pixel(raw_cfg["format"])
        stride = raw_cfg.get("stride") or align_up(int(width * bpp), 64)

        plane = np.full((height, stride), PAD_BYTE, dtype=np.uint8)
        if bpp == 2.0:
            row = np.zeros((height, width), dtype=np.uint16)
            # A ramp that exercises the full sample range, so the value
            # ceiling and the alignment both matter.
            ceiling = (1 << s.sample_bits) - 1
            for y in range(height):
                row[y] = ((np.arange(width) + y) % (ceiling + 1))
            row[0, 0] = 0                      # a known floor
            row[0, 1] = ceiling                # and a known saturation
            row <<= s.sample_shift
            packed = row.view(np.uint8).reshape(height, width * 2)
            plane[:, : width * 2] = packed
        else:
            row = np.zeros((height, width), dtype=np.uint8)
            for y in range(height):
                row[y] = (np.arange(width) + y) % 256
            row[0, 0] = 0
            row[0, 1] = 255
            plane[:, :width] = row

        main_size = self.negotiated["main"]["size"]
        main = np.zeros((main_size[1], main_size[0]), dtype=np.uint8)
        lores_size = self.negotiated["lores"]["size"]
        lores = np.zeros((lores_size[1], lores_size[0]), dtype=np.uint8)

        meta = {
            "SensorTimestamp": self.frame * 33_333_333,
            "ExposureTime": self.applied_controls.get("ExposureTime", 10_000),
            "AnalogueGain": self.applied_controls.get("AnalogueGain", 1.0),
            "FrameDuration": 33_333,
        }
        return FakeRequest({"raw": plane, "main": main, "lores": lores}, meta)


def install(*sensors: FakeSensor) -> type[FakeCamera]:
    """Put a fake `picamera2` module in `sys.modules`. Returns FakeCamera.

    The adapter imports `picamera2` inside `open()`, so this has to be in place
    before the camera is opened and removed afterwards -- `uninstall()`, or the
    `fake_sdk` fixture in conftest, which does both.
    """
    import types

    FakeCamera.sensors = list(sensors)
    FakeCamera.opened = []
    module = types.ModuleType("picamera2")
    module.Picamera2 = FakeCamera                                # type: ignore[attr-defined]
    sys.modules["picamera2"] = module
    return FakeCamera


def uninstall() -> None:
    sys.modules.pop("picamera2", None)
    FakeCamera.sensors = []
    FakeCamera.opened = []


def expected_image(sensor: FakeSensor, size: tuple[int, int]) -> np.ndarray:
    """The image the adapter should produce, built independently.

    No use of `rawformat`'s trim or shift helpers: this is the ramp written in
    `capture_request`, reconstructed from the fake's own parameters. A test
    that compared the adapter's output against a value produced by the code
    under test would pass whatever that code did.

    Returned UNSHIFTED, because admission records the shift and does not apply
    it -- the pixels stay as the sensor delivered them.
    """
    width, height = size
    if sensor.sample_bits > 8:
        ceiling = (1 << sensor.sample_bits) - 1
        out = np.zeros((height, width), dtype=np.uint16)
        for y in range(height):
            out[y] = (np.arange(width) + y) % (ceiling + 1)
        out[0, 0] = 0
        out[0, 1] = ceiling
        return out << sensor.sample_shift
    out = np.zeros((height, width), dtype=np.uint8)
    for y in range(height):
        out[y] = (np.arange(width) + y) % 256
    out[0, 0] = 0
    out[0, 1] = 255
    return out
