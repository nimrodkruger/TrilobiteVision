"""Runtime state persistence.

The YAML config is the *baseline*: hand-written, commented, version-controlled.
It is never rewritten. What gets saved here is the delta -- the parameter values
you arrived at during a session -- in a separate JSON file that overlays the
config at startup.

Keeping them separate matters:

  * Rewriting the config would destroy its comments, which are most of its
    value, and would make every session show up as a diff in git.
  * "Go back to the documented defaults" becomes `rm` on one file, rather than
    a git checkout that also reverts deliberate edits.
  * A state file that no longer matches the config (a stage renamed, a camera
    removed) degrades to partial application with a warning, instead of
    refusing to start.

Saving happens both on shutdown and, debounced, whenever something changes. The
shutdown save alone is not enough: a Pi that loses power, or a process killed
with SIGKILL, would lose an alignment that took twenty minutes to dial in.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def default_state_path(config_path: str | Path) -> Path:
    """Sit beside the config, named after it: config/pi.yaml -> config/pi.state.json."""
    p = Path(config_path)
    return p.with_suffix("").with_suffix(".state.json") if p.suffix else p / "state.json"


class StateStore:
    """Holds the state file and an autosave thread."""

    def __init__(self, path: Path, snapshot, autosave_interval: float = 3.0) -> None:
        self.path = Path(path)
        self._snapshot = snapshot          # callable -> dict
        self._interval = autosave_interval
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- reading --------------------------------------------------------

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            log.info("no saved state at %s; starting from the config as written", self.path)
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt state file must never stop the rig from starting. The
            # config alone is always a valid starting point.
            log.exception("could not read %s; ignoring it", self.path)
            return {}
        if data.get("schema") != SCHEMA_VERSION:
            log.warning(
                "state file %s has schema %s, expected %s; ignoring it",
                self.path, data.get("schema"), SCHEMA_VERSION,
            )
            return {}
        log.info("restoring state saved %s from %s", data.get("saved", "?"), self.path)
        return data

    # -- writing --------------------------------------------------------

    def mark_dirty(self) -> None:
        self._dirty.set()

    def save(self) -> bool:
        with self._lock:
            try:
                payload = {
                    "schema": SCHEMA_VERSION,
                    "saved": datetime.now().isoformat(timespec="seconds"),
                    **self._snapshot(),
                }
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Write-then-rename: a power cut mid-write leaves the previous
                # good state intact rather than a truncated file.
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty.clear()
                return True
            except Exception:
                log.exception("could not save state to %s", self.path)
                return False

    def start_autosave(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="state-autosave", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._dirty.is_set():
                self.save()

    def stop(self, final_save: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if final_save:
            self.save()
            log.info("state saved to %s", self.path)
