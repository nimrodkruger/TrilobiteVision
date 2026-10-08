"""Measure THIS rig, on this disk, with these sensors, before offering to record.

The replacement for `scripts/bench_encode.py`, and the reason that script was
dropped rather than fixed: it generated synthetic Gaussian noise and measured
how fast that compressed. Compression ratio is governed almost entirely by
sensor noise, so what it measured was an assumption about the sensor rather
than the sensor -- and it reported the answer with the same confidence either
way. A benchmark whose input is a guess is worse than no benchmark, because its
output enters the record as a measurement.

So this runs inside the application, uses real frames from the real cameras and
writes them to the real target through the real chunk writer. Four questions,
in order of how badly a wrong answer hurts:

  1. **What sustained write rate does this target hold?** Reported as the
     final-quarter figure, because a consumer SSD's first few gigabytes land in
     an SLC cache at the headline rate and a five-minute recording spends
     almost all of itself past that. The mean is reported beside it and the
     ratio is the diagnostic.
  2. **Which raw formats will these sensors actually deliver?** Decides whether
     the packed and 8-bit rungs of the degradation ladder exist here at all.
  3. **What is the maximum sustainable frame rate for each configuration?**
     Arithmetic, once (1) is known, and the thing the operator actually needs.
  4. **How long can a recording run before the free-space reserve?** Also
     arithmetic, and also the thing that should be on screen before Start
     rather than discovered 90 seconds in.

The result is persisted **against the storage generation counter**. Change the
disk and the measurement is invalidated rather than carried over, which is the
one way a stale number could otherwise be used to size a recording on a
different drive.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..storage import devices
from ..storage.writer import SessionWriter, measure_sustained
from .ladder import BY_KEY, RAW8, RAW16, Plan, enumerate_plans

log = logging.getLogger(__name__)


@dataclass
class Preflight:
    """What was measured, and which target it applies to."""

    generation: int = 0
    target: str = ""
    t_wall: float = 0.0
    storage: dict[str, Any] = field(default_factory=dict)
    cameras: dict[str, Any] = field(default_factory=dict)
    transport: dict[str, Any] = field(default_factory=dict)
    formats: list[str] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def sustained_mb_s(self) -> float | None:
        value = self.storage.get("sustained_mb_s")
        return float(value) if value else None

    def valid_for(self, writer: SessionWriter) -> bool:
        """Does this measurement still describe where output is going?

        Generation AND path. The generation alone would be enough for a
        retarget, but a release-and-reselect of the same disk bumps the
        generation twice and the operator would reasonably expect the
        measurement to still apply -- so the path is checked too and the
        measurement is reported as stale rather than silently reused.
        """
        return (self.generation == writer.generation
                and self.target == str(writer.root))

    def as_dict(self, writer: SessionWriter | None = None) -> dict[str, Any]:
        out = {
            "measured": bool(self.t_wall),
            "generation": self.generation,
            "target": self.target,
            "t_wall": self.t_wall,
            "age_s": round(time.time() - self.t_wall, 1) if self.t_wall else None,
            "storage": self.storage,
            "cameras": self.cameras,
            "transport": self.transport,
            "formats": self.formats,
            "plans": self.plans,
            "notes": self.notes,
            "sustained_mb_s": self.sustained_mb_s,
        }
        if writer is not None:
            out["stale"] = not self.valid_for(writer)
            out["stale_reason"] = (
                "" if self.valid_for(writer) else
                f"measured on generation {self.generation} at {self.target}; "
                f"output is now generation {writer.generation} at "
                f"{writer.root}. Measure again.")
        return out


def available_format_keys(camera_caps: dict[str, Any]) -> set[str]:
    """Which ladder rungs these cameras can actually supply.

    `raw16` is always present: it follows from the camera having opened with an
    admissible raw format at all. The others have to be established, and a rung
    that was not established is **not offered** -- which is the rule that keeps
    packed 10-bit out of the UI on a rig whose sensor does not provide it, and
    keeps the operator from choosing a configuration that cannot be delivered.
    """
    keys = {RAW16.key}
    caps = [c for c in camera_caps.values() if isinstance(c, dict)]
    if caps and all(c.get("eight_bit_available") for c in caps):
        keys.add(RAW8.key)
    if caps and all(c.get("packed_available") for c in caps):
        keys.add("raw10p")
    return keys


def run(
    writer: SessionWriter,
    heads: dict[str, Any],
    width: int,
    height: int,
    sensor_fps: float,
    budget_bytes: int = 8 << 30,
    budget_seconds: float = 30.0,
    block_bytes: int = 8 << 20,
) -> Preflight:
    """Measure, then enumerate what may be recorded.

    `heads` maps cam_id -> that camera's `raw_capabilities()` dict, snapshotted
    at open on the owner thread. Passed in rather than fetched here so this
    module needs no camera access and can be tested with a dict.

    The write measurement is admitted through the storage layer like any other
    write, so a target that is absent, read-only, quarantined or already below
    its reserve refuses the measurement instead of filling the last of it.
    `internal_ok=True`, because measuring the internal disk is a legitimate
    thing to want and the probe file is removed either way.
    """
    pf = Preflight(
        generation=writer.generation,
        target=str(writer.root),
        t_wall=time.time(),
        cameras=dict(heads),
    )

    with writer.writing(budget_bytes, internal_ok=True):
        pf.storage = measure_sustained(
            writer.session_dir, budget_bytes=budget_bytes,
            budget_seconds=budget_seconds, block_bytes=block_bytes)

    state = writer.state()
    pf.storage["target_state"] = state.get("state")
    pf.storage["filesystem"] = (
        (state.get("identity") or {}).get("fstype")
        or devices.describe_mount(state.get("mount", "")).get("fstype", ""))
    pf.storage["usable_bytes"] = state.get("usable_bytes", 0)
    pf.transport = devices.usb_transport()

    keys = available_format_keys(pf.cameras)
    pf.formats = sorted(keys)
    pf.plans = enumerate_plans(
        heads=tuple(heads), width=width, height=height,
        sensor_fps=sensor_fps, available_formats=keys,
        sustained_mb_s=pf.sustained_mb_s,
    )
    pf.notes = _notes(pf, width, height, sensor_fps, tuple(heads))
    log.info("pre-flight: %s", pf.storage.get("message"))
    return pf


def _notes(pf: Preflight, width: int, height: int, sensor_fps: float,
           heads: tuple[str, ...]) -> list[str]:
    """The findings worth putting in front of a person, in words.

    Each one exists because it is a conclusion a number does not announce on
    its own, and because the operator is the one who has to act on it.
    """
    out: list[str] = []
    st = pf.storage
    if not st.get("ok"):
        out.append(f"The write measurement failed: {st.get('message')}")
        return out

    mean = float(st.get("mean_mb_s") or 0.0)
    tail = float(st.get("sustained_mb_s") or 0.0)
    if tail and mean > tail * 1.5:
        out.append(
            f"This drive has a write cache: the mean was {mean:.0f} MB/s and "
            f"the final quarter {tail:.0f} MB/s, a factor of "
            f"{mean / tail:.1f}. Plan with {tail:.0f} MB/s -- a multi-minute "
            f"recording spends almost all of itself past the cache.")
    elif tail:
        out.append(
            f"Sustained {tail:.0f} MB/s, with no cache cliff (mean "
            f"{mean:.0f} MB/s). This drive holds its rate.")

    fs = str(st.get("filesystem") or "")
    if fs.lower() in {"exfat", "vfat", "fuseblk", "msdos"}:
        out.append(
            f"The target is {fs}. That is what lets the disk mount on a "
            f"Windows desktop without a driver, and it costs throughput and "
            f"journalling on Linux. ext4 is faster and safer and needs a "
            f"reader on the desktop side. The measurement above is of {fs} as "
            f"it stands, so the trade can be made on numbers.")
    if fs.lower() == "vfat":
        out.append(
            "FAT32 caps a single file at 4 GB. Chunk files are well under "
            "that, but nothing else on this disk should assume otherwise.")

    tr = pf.transport
    if tr.get("available"):
        if tr.get("bot_only"):
            out.append(
                "The USB storage driver is usb-storage, not uas: this "
                "enclosure's bridge does not support UASP, which commonly "
                "halves sustained write rate on an otherwise fast drive. "
                "Nothing in software can change it -- a different enclosure "
                "can.")
        elif tr.get("uas"):
            out.append("UASP is active (uas driver), so the bridge is not "
                       "the bottleneck.")

    # The headline arithmetic, stated once in full so the ladder is legible.
    plan16 = Plan(fmt=RAW16, fps=sensor_fps, heads=heads,
                  width=width, height=height)
    if tail:
        out.append(
            f"{len(heads)} head(s) at {sensor_fps:.0f} fps in "
            f"{RAW16.label} needs {plan16.mb_s:.0f} MB/s. This target holds "
            f"{tail:.0f} MB/s, so the highest exact full-depth rate is "
            f"{plan16.max_fps(tail):.1f} fps.")
        drop = plan16.predicted_drop_fraction(tail)
        if drop:
            out.append(
                f"At the full {sensor_fps:.0f} fps, expect roughly "
                f"{drop * 100:.0f}% of frames to be dropped. Lowering the "
                f"requested rate instead gives uniform sampling and a longer "
                f"exposure, and is the better trade when the deficit is known "
                f"in advance.")

    if RAW8.key in pf.formats:
        out.append(
            "8-bit (R8) is available and halves the bandwidth, but it is "
            "LOSSY: those recordings are diagnostic, never science.")
    else:
        out.append(
            "No admissible 8-bit format was advertised, so that rung of the "
            "ladder is not offered here.")
    if "raw10p" not in pf.formats:
        packed = sorted({
            p for c in pf.cameras.values() if isinstance(c, dict)
            for p in (c.get("packed_advertised") or [])})
        out.append(
            "Packed 10-bit is not offered: "
            + (f"advertised ({', '.join(packed)}) but the readers cannot "
               f"unpack it yet." if packed
               else "no packed format is advertised for this sensor."))
    return out


def plan_from(
    key: str,
    fps: float,
    heads: tuple[str, ...],
    width: int,
    height: int,
    duration_s: float | None = None,
    orientation: dict[str, Any] | None = None,
    oriented: bool = False,
) -> Plan:
    """Build a Plan from a UI selection, refusing a format that is not a rung."""
    fmt = BY_KEY.get(key)
    if fmt is None:
        raise ValueError(
            f"unknown recording format {key!r}; known: "
            f"{', '.join(sorted(BY_KEY))}")
    if fps <= 0:
        raise ValueError("a recording needs a positive frame rate")
    return Plan(fmt=fmt, fps=float(fps), heads=tuple(heads),
                width=int(width), height=int(height), duration_s=duration_s,
                orientation=orientation, oriented=oriented)
