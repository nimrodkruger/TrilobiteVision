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

import numpy as np

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
    bytes_per_pixel: float
    exact: bool
    validity: str
    label: str
    note: str
    # How the stored samples are produced from what the camera delivers.
    # `"as-delivered"` stores the uint16 frame unchanged. `"shift-to-8"` takes
    # the top 8 bits of each sample, by a shift derived from that frame's own
    # recorded alignment -- never a blind cast, which wraps modulo 256 and
    # turns a bright frame into noise.
    reduction: str = "as-delivered"
    # False for a rung that is described here but **not wired up end to end**.
    # It is not offered to the operator, because a configuration that cannot
    # be delivered is worse than one that is absent: it gets chosen.
    offered: bool = True

    @property
    def lossy(self) -> bool:
        return not self.exact


# uint16 is the default and the only one that needs no capability check: every
# uncompressed mono format picamera2 can decode arrives as uint16 already.
RAW16 = RecordingFormat(
    key="raw16", bytes_per_pixel=2.0, exact=True,
    validity=SCIENCE, label="full depth",
    note="Exact. The frame as the camera delivered it, which is what a still "
         "already stores, so both readers need no special case. Two bytes per "
         "pixel for ten bits of information -- the whole bandwidth problem.",
    reduction="as-delivered",
)

RAW8 = RecordingFormat(
    key="raw8", bytes_per_pixel=1.0, exact=False,
    validity=DIAGNOSTIC, label="8-bit",
    note="Half the bytes, and LOSSY: the low bits of every sample are gone, "
         "so this is diagnostic however clean the data was. The top 8 bits "
         "are taken by a shift derived from the frame's own recorded "
         "alignment; multiply by the journal's scale_to_counts to get back to "
         "approximate sensor counts.",
    reduction="shift-to-8",
)

# Packed 10-bit: 4 pixels in 5 bytes, the MIPI RAW10 layout. Described because
# it is the one rung that would buy bandwidth with no loss at all -- and NOT
# OFFERED, because nothing asks the ISP for it and neither reader unpacks it.
# Three things have to be true before `offered=True` here: the pre-flight shows
# libcamera delivers a packed mode for this sensor, the recorder requests that
# format rather than converting, and `read_capture.py` plus the MATLAB readers
# unpack it. Until then it would be an option that silently cannot be served.
RAW10P = RecordingFormat(
    key="raw10p", bytes_per_pixel=1.25, exact=True,
    validity=SCIENCE, label="packed 10-bit",
    note="Exact, 0.625x the bytes. NOT AVAILABLE: no ISP request and no "
         "unpacking in the readers yet.",
    reduction="packed-10",
    offered=False,
)

FORMATS = (RAW16, RAW8, RAW10P)
OFFERED = tuple(f for f in FORMATS if f.offered)
BY_KEY = {f.key: f for f in FORMATS}

# The frame rates offered, highest first, filtered to what the sensor can
# actually deliver. A conventional ladder rather than halves of whatever the
# sensor happens to be configured for: an operator picking a rate wants 10 fps,
# not 7.5, and the frame duration is set in microseconds so there is no divisor
# to respect. Each is only offered when it is at or below the configured sensor
# rate AND the backend can actually apply it -- see
# `CameraSource.request_frame_rate`.
RATE_LADDER = (120.0, 90.0, 60.0, 50.0, 30.0, 25.0, 20.0, 15.0,
               10.0, 5.0, 2.0, 1.0)


def rates_for(sensor_fps: float) -> tuple[float, ...]:
    """The rate ladder, capped at the sensor rate, with that rate included.

    The sensor's own rate is always first even when it is not on the ladder --
    it is the only one that needs no rate request at all, so it must never be
    missing from the list.
    """
    cap = float(sensor_fps)
    rates = [cap]
    rates.extend(r for r in RATE_LADDER if r < cap * 0.99 and r >= 1.0)
    return tuple(rates)


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
    def store_dtype(self) -> str:
        return "uint8" if self.fmt.bytes_per_pixel <= 1.0 else "uint16"

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
    # `offered` is applied here as well as at the capability stage, so a rung
    # that is not wired up cannot reach the operator even if something upstream
    # claims it is available.
    candidates = [f for f in OFFERED if f.key in keys] or [RAW16]
    rate_list = tuple(rates) if rates else rates_for(sensor_fps)

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
    # `RAW10P` and not `RAW10P.key` here until 10 October, so this branch was
    # comparing a string to a dataclass and was dead. It never showed, because
    # the rung is not offered either -- two faults hiding one another.
    if fmt.key == RAW10P.key:
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


def shift_to_eight(meta: dict[str, Any] | None) -> int:
    """How far to shift a delivered sample right to keep its top 8 bits.

    Derived from the frame's **own** recorded alignment rather than assumed,
    because the two alignments differ by 64x per pixel on a 10-bit sensor and
    the wrong one is not a subtle error:

        lsb-aligned 10-bit in uint16   value in bits 0..9    shift 2
        msb-aligned 10-bit in uint16   value in bits 6..15   shift 8

    `shift = (bits - 8) + sample_shift`, where `bits` is the sample depth and
    `sample_shift` is how far the sample was left-shifted in its container.
    Both come from `rawformat`'s admission metadata, which is the only place
    either is established.

    A missing or nonsensical value returns 0 rather than a guess: storing the
    low 8 bits is wrong, but it is visibly wrong -- a noise field -- where a
    guessed shift produces a plausible picture at the wrong scale. The caller
    records the shift it used in the journal either way.
    """
    meta = meta or {}
    bits = meta.get("raw_bits_nominal")
    shift = meta.get("raw_sample_shift", 0)
    try:
        bits = int(bits)
        shift = int(shift)
    except (TypeError, ValueError):
        return 0
    if bits <= 8:
        return 0
    return max(0, (bits - 8) + shift)


def reduce_to_store(pixels: Any, dtype: Any, shift: int) -> Any:
    """Convert one delivered frame to what the plan says to store.

    For `uint16` storage this is the frame itself -- no copy, no conversion.
    For `uint8` it is `(pixels >> shift)`, which **is** the lossy step, and it
    is a shift and not a cast: `np.copyto(uint8_buffer, uint16_frame,
    casting="unsafe")` keeps the LOW byte, so a sample of 300 counts arrives as
    44 and a bright frame arrives as noise. That was the behaviour here until
    10 October and it is the same failure mode as the white-noise incident in
    Stage 2 -- a conversion that produces an array of the right shape and the
    wrong meaning.
    """
    want = np.dtype(dtype)
    if want.itemsize >= pixels.dtype.itemsize and want.kind == pixels.dtype.kind:
        return pixels
    if shift > 0:
        return (pixels >> shift).astype(want, copy=False)
    return pixels.astype(want, copy=False)
