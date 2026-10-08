"""What may be recorded, and what each choice costs.

Five mechanisms can close a gap between what the sensors produce and what the
disk can absorb. They are not interchangeable, and the distinction that governs
this whole module is **which are chosen before Start and which may act during
the recording**:

    1  lower the requested frame rate   exact, UNIFORMLY sampled   configuration
    2  packed 10-bit raw from the ISP   exact                      configuration
    3  8-bit raw (R8) from the ISP      LOSSY -> diagnostic        configuration
    4  drop whole frames at the queue   exact per frame, gaps      runtime
    5  FFV1 lossless compression        exact, ~10 MB/s per core   post-capture

**The configuration is fixed before Start and the only thing that may change
during a recording is whether a frame is kept.** Nothing alters bit depth,
geometry or requested rate mid-stream. Two reasons, and the second is the
stronger one:

  * a file whose frames are not all the same shape and depth breaks every
    reader downstream, and this project has two of them plus a MATLAB suite;
  * a recording that silently changed its own fidelity half way through is
    worse than one that lost frames, because the loss is at least countable.
    A gap in the sequence numbers is evidence. A change of meaning in the
    pixel values is not recoverable from the file.

Two notes on the ladder itself, both of which are easy to get backwards:

  **Rate reduction beats dropping when the deficit is known in advance.**
  Dropping discards exposures that were already taken and leaves irregular
  sampling. Asking the sensor for 15 fps instead gives uniform sampling AND a
  longer integration time, so less read noise per frame. Dropping is the safety
  valve for a stall; it is not the plan for a deficit.

  **8-bit and packed formats are requested from the sensor, not computed.**
  Truncating uint16 to uint8 in Python costs a full-rate pass over every frame
  on cores that do not have it spare. `R8` costs nothing, because the ISP
  already produces it. The same goes for packed 10-bit: if libcamera offers a
  packed mode for this sensor it is free bandwidth, and if it does not it is
  not worth doing in software. Which is the case is a measurement, not an
  assumption -- see preflight.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..types import DIAGNOSTIC, SCIENCE

# Mechanism identifiers, used in manifests and in the UI.
RATE = "rate"
PACKED = "packed"
EIGHT_BIT = "eight-bit"
DROP = "drop"

CONFIGURATION = "configuration"       # chosen before Start
RUNTIME = "runtime"                   # may act during the recording
POST = "post-capture"


@dataclass(frozen=True)
class RecordingFormat:
    """One way of storing a frame, and what it does to the claim about it.

    `validity` is the ceiling, not the verdict: a frame stored in an exact
    format is `science` only if admission also passed for that frame. A frame
    stored in a lossy one can never be better than `diagnostic` however clean
    the sensor data was, because two bits of every pixel are gone.
    """

    key: str
    raw_format: str | None        # what to ask libcamera for; None = whatever is configured
    bytes_per_pixel: float
    exact: bool
    validity: str
    label: str
    note: str

    @property
    def lossy(self) -> bool:
        return not self.exact


# uint16 is the default and the only one that needs no capability check: every
# uncompressed mono format picamera2 can decode arrives as uint16 already.
RAW16 = RecordingFormat(
    key="raw16", raw_format=None, bytes_per_pixel=2.0, exact=True,
    validity=SCIENCE, label="10-bit in a 16-bit word",
    note="Exact. What a still capture already stores, so the readers need no "
         "change. Costs 2 bytes per pixel for 10 bits of information, which is "
         "the whole of the bandwidth problem.",
)

# Packed 10-bit. 4 pixels in 5 bytes, as the MIPI RAW10 layout. Offered ONLY
# when the pre-flight found that libcamera will deliver it for this sensor:
# packing in software would cost a full-rate pass per frame to save bandwidth
# this rig may not need to save.
RAW10P = RecordingFormat(
    key="raw10p", raw_format="R10_CSI2P", bytes_per_pixel=1.25, exact=True,
    validity=SCIENCE, label="packed 10-bit",
    note="Exact, and 0.625x the bytes. Free if the ISP delivers it; not worth "
         "doing in software. Readers must unpack, which is why it is not the "
         "default.",
)

RAW8 = RecordingFormat(
    key="raw8", raw_format="R8", bytes_per_pixel=1.0, exact=False,
    validity=DIAGNOSTIC, label="8-bit",
    note="LOSSY: the low 2 bits of every pixel are gone, so this can never be "
         "better than diagnostic however good the sensor data was. Half the "
         "bytes. Produced by the ISP, so it costs no CPU -- never truncate "
         "uint16 in software to get here.",
)

FORMATS = (RAW16, RAW10P, RAW8)
BY_KEY = {f.key: f for f in FORMATS}


@dataclass(frozen=True)
class Plan:
    """A complete recording configuration, with its arithmetic.

    Frozen, and that is not decoration: this object is what is checked against
    the measured throughput, shown to the operator, written into the manifest
    and compared against afterwards. If it could change during a recording the
    manifest would describe something other than what happened.
    """

    fmt: RecordingFormat
    fps: float
    heads: tuple[str, ...]
    width: int
    height: int
    duration_s: float | None = None        # None -> until stopped or full
    # Per head: the rotation and flips that were NOT applied to the stored
    # pixels. A recording stores sensor-orientation data -- see
    # cameras/base.RawRead for why a quarter turn is not affordable at
    # 190 MB/s -- so the transform has to travel with the file or the reader
    # cannot reproduce what the operator saw. Part of the plan rather than
    # per-frame metadata because it is fixed before Start, like everything else
    # here, and because eighteen thousand copies of the same four fields is not
    # a record, it is a waste of an index.
    orientation: dict[str, dict[str, Any]] | None = None
    # True when the pixels reaching the recorder are ALREADY oriented, in which
    # case `orientation` records what was applied rather than what remains to
    # be. The two must agree with the backend (see
    # cameras/base.CameraSource.records_oriented), because the plan's width and
    # height have to be the frame the backend will actually deliver -- a plan
    # built for the sensor frame against a backend that delivers a turned one
    # is a shape mismatch on every frame.
    oriented: bool = False

    # -- per-frame arithmetic -------------------------------------------

    @property
    def pixels(self) -> int:
        return int(self.width) * int(self.height)

    @property
    def bytes_per_frame(self) -> int:
        return int(round(self.pixels * self.fmt.bytes_per_pixel))

    @property
    def heads_count(self) -> int:
        return max(1, len(self.heads))

    @property
    def bytes_per_second(self) -> float:
        """What the whole rig produces. The number the disk has to absorb."""
        return self.bytes_per_frame * self.fps * self.heads_count

    @property
    def mb_s(self) -> float:
        return self.bytes_per_second / 1e6

    def total_bytes(self, seconds: float | None = None) -> int:
        s = self.duration_s if seconds is None else seconds
        if s is None:
            return 0
        return int(self.bytes_per_second * s)

    def frames_per_head(self, seconds: float | None = None) -> int:
        s = self.duration_s if seconds is None else seconds
        if s is None:
            return 0
        return int(round(self.fps * s))

    # -- feasibility -----------------------------------------------------

    def max_fps(self, sustained_mb_s: float) -> float:
        """The highest rate this format can be recorded at on that disk."""
        per_frame_all_heads = self.bytes_per_frame * self.heads_count
        if per_frame_all_heads <= 0:
            return 0.0
        return (sustained_mb_s * 1e6) / per_frame_all_heads

    def predicted_drop_fraction(self, sustained_mb_s: float | None) -> float | None:
        """The fraction of frames the disk cannot take, from measurement alone.

        None when nothing has been measured -- which is reported as "unknown"
        and not as zero. Claiming no drops because no measurement exists is the
        one answer this must never give.

        It is a *prediction*, and a conservative one: it assumes the shortfall
        is spread evenly, where a real drive stalls in bursts. The recording
        reports what actually happened; this is only for deciding whether to
        press Start.
        """
        if sustained_mb_s is None or sustained_mb_s <= 0:
            return None
        required = self.mb_s
        if required <= sustained_mb_s:
            return 0.0
        return max(0.0, min(1.0, 1.0 - sustained_mb_s / required))

    def max_duration_s(self, usable_bytes: int) -> float:
        """How long this plan can run before the free-space reserve is reached."""
        if self.bytes_per_second <= 0:
            return 0.0
        return max(0.0, usable_bytes / self.bytes_per_second)

    # -- reporting -------------------------------------------------------

    def as_dict(self, sustained_mb_s: float | None = None,
                usable_bytes: int | None = None) -> dict[str, Any]:
        drop = self.predicted_drop_fraction(sustained_mb_s)
        out: dict[str, Any] = {
            "format": self.fmt.key,
            "format_label": self.fmt.label,
            "format_note": self.fmt.note,
            "exact": self.fmt.exact,
            "validity_ceiling": self.fmt.validity,
            "fps": round(self.fps, 3),
            "heads": list(self.heads),
            "width": self.width,
            "height": self.height,
            "bytes_per_frame": self.bytes_per_frame,
            "mb_s": round(self.mb_s, 1),
            "duration_s": self.duration_s,
            "predicted_drop_fraction": (
                None if drop is None else round(drop, 4)),
            "predicted_drop_percent": (
                None if drop is None else round(drop * 100, 1)),
            # Stated explicitly, including when it is a no-op, so a reader
            # never has to decide whether an absent key means "no rotation" or
            # "not recorded".
            "pixels_oriented": self.oriented,
            "orientation": self.orientation or {},
        }
        if self.duration_s:
            out["total_bytes"] = self.total_bytes()
            out["total_gb"] = round(self.total_bytes() / 1e9, 2)
            out["frames_per_head"] = self.frames_per_head()
        if sustained_mb_s:
            out["max_fps"] = round(self.max_fps(sustained_mb_s), 2)
            out["sustained_mb_s"] = round(sustained_mb_s, 1)
        if usable_bytes is not None:
            out["max_duration_s"] = round(self.max_duration_s(usable_bytes), 1)
        return out


def enumerate_plans(
    heads: tuple[str, ...],
    width: int,
    height: int,
    sensor_fps: float,
    available_formats: set[str] | None = None,
    sustained_mb_s: float | None = None,
    rates: tuple[float, ...] = (),
) -> list[dict[str, Any]]:
    """Every configuration this rig can actually offer, with its consequences.

    `available_formats` is the set of format keys the pre-flight established
    libcamera will deliver here. **A format that was not established is not
    offered**, which is the rule that keeps packed 10-bit from appearing in the
    UI on a rig whose sensor does not provide it. None means "nothing has been
    measured", and then only `raw16` is offered, because it is the one whose
    availability follows from the camera having opened at all.

    `rates` lets a caller ask about specific frame rates; by default the sensor
    rate and the halves below it are enumerated, because halving is the
    reduction that stays on a clean divisor of the sensor clock.
    """
    keys = {RAW16.key} if available_formats is None else set(available_formats)
    candidates = [f for f in FORMATS if f.key in keys] or [RAW16]

    if rates:
        rate_list = tuple(rates)
    else:
        rate_list = tuple(
            r for r in (sensor_fps, sensor_fps / 2, sensor_fps / 4)
            if r >= 1.0
        )

    out: list[dict[str, Any]] = []
    for fmt in candidates:
        for fps in rate_list:
            plan = Plan(fmt=fmt, fps=float(fps), heads=heads,
                        width=width, height=height)
            row = plan.as_dict(sustained_mb_s=sustained_mb_s)
            row["mechanisms"] = _mechanisms(fmt, fps, sensor_fps)
            row["feasible"] = (
                sustained_mb_s is None or plan.mb_s <= sustained_mb_s)
            out.append(row)
    # Exact formats first, then by rate descending: the operator should see the
    # best available science configuration at the top, not the fastest one.
    out.sort(key=lambda r: (not r["exact"], -r["fps"]))
    return out


def _mechanisms(fmt: RecordingFormat, fps: float, sensor_fps: float) -> list[dict[str, str]]:
    """Which ladder rungs a configuration uses, and when each one acts."""
    used: list[dict[str, str]] = []
    if fps < sensor_fps * 0.99:
        used.append({
            "mechanism": RATE, "when": CONFIGURATION,
            "effect": f"{fps:.0f} fps instead of {sensor_fps:.0f}: exact, "
                      f"uniformly sampled, and a longer exposure per frame",
        })
    if fmt.key == RAW10P:
        used.append({
            "mechanism": PACKED, "when": CONFIGURATION,
            "effect": "0.625x the bytes, exact; the readers unpack",
        })
    if fmt.key == RAW8.key:
        used.append({
            "mechanism": EIGHT_BIT, "when": CONFIGURATION,
            "effect": "half the bytes, LOSSY -- diagnostic, never science",
        })
    used.append({
        "mechanism": DROP, "when": RUNTIME,
        "effect": "the only runtime response: whole frames are dropped, "
                  "counted, and recorded as explicit gaps",
    })
    return used
