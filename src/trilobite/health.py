"""Is the host itself coping?

Added after the rig went down during a detection run. When a Pi stops
responding, the question "was that a bug or was that the hardware" cannot be
answered from the application log, and by the time anyone looks the evidence is
gone. Three numbers answer it, and all three are cheap:

  **Under-voltage.** `vcgencmd get_throttled` carries sticky bits: bit 16 means
  the supply has dropped below 4.63 V at some point since boot, whatever it is
  doing now. A Pi 5 with two cameras and four busy cores draws more than many
  "5 V 3 A" supplies deliver, and the failure is a hard reset with nothing in
  the log. If that bit is set, no amount of software tuning is the fix.

  **Temperature.** Above about 80 °C the firmware caps the clock; above 85 it
  caps it hard. Sustained detection is the first workload this project has that
  can get there without an active cooler. Throttling looks like "the software
  got slower for no reason".

  **Free memory.** Two cameras with three streams each already hold tens of
  megabytes of buffers, and detection allocates a full-resolution frame per
  pass. A kill by the OOM killer leaves a dead process and an intact machine,
  which is a different diagnosis from a reset.

Everything degrades quietly off a Pi: absent files and a missing `vcgencmd`
give `None`, not an exception, so the same status endpoint serves the desktop.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Bit meanings from `vcgencmd get_throttled`. The low nibble is "right now",
# the high nibble is "has happened since boot" and does not clear -- which is
# the half that matters after a crash.
THROTTLE_BITS = {
    0: ("under_voltage_now", "under-voltage RIGHT NOW"),
    1: ("freq_capped_now", "ARM frequency capped now"),
    2: ("throttled_now", "throttled now"),
    3: ("soft_temp_limit_now", "soft temperature limit active"),
    16: ("under_voltage_since_boot", "under-voltage HAS OCCURRED since boot"),
    17: ("freq_capped_since_boot", "frequency capping has occurred"),
    18: ("throttled_since_boot", "throttling has occurred"),
    19: ("soft_temp_limit_since_boot", "soft temperature limit has been hit"),
}

# vcgencmd is a subprocess, and /api/status is polled once a second by every
# open browser tab. Cache it.
_THROTTLE_TTL = 5.0
_lock = threading.Lock()
_cached: tuple[float, dict[str, Any] | None] = (0.0, None)


def cpu_temperature_c() -> float | None:
    for path in (
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        # Millidegrees on every kernel that exposes this, but guard anyway.
        return round(value / 1000.0 if value > 200 else value, 1)
    return None


def _read_throttled() -> dict[str, Any] | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True,
            timeout=5.0, check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "=" not in out:
        return None
    try:
        value = int(out.split("=", 1)[1], 0)
    except ValueError:
        return None
    flags = {name: bool(value & (1 << bit)) for bit, (name, _) in THROTTLE_BITS.items()}
    problems = [text for bit, (_, text) in THROTTLE_BITS.items() if value & (1 << bit)]
    return {"raw": hex(value), **flags, "problems": problems}


def throttled() -> dict[str, Any] | None:
    global _cached
    now = time.monotonic()
    with _lock:
        stamp, value = _cached
        if now - stamp < _THROTTLE_TTL:
            return value
    value = _read_throttled()
    with _lock:
        _cached = (now, value)
    return value


def memory_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def load_average() -> list[float] | None:
    try:
        fields = Path("/proc/loadavg").read_text().split()[:3]
        return [round(float(v), 2) for v in fields]
    except (OSError, ValueError, IndexError):
        return None


def host_health() -> dict[str, Any]:
    """One dict for the status endpoint. Never raises."""
    t = throttled()
    temp = cpu_temperature_c()
    problems: list[str] = list((t or {}).get("problems", []))
    if temp is not None and temp >= 80.0:
        problems.append(f"CPU at {temp:.0f} °C -- the firmware caps the clock above 80")
    mem = memory_available_mb()
    if mem is not None and mem < 200:
        problems.append(f"only {mem:.0f} MB of memory available")
    return {
        "cpu_temp_c": temp,
        "throttled": t,
        "mem_available_mb": mem,
        "load": load_average(),
        "problems": problems,
        "ok": not problems,
    }
