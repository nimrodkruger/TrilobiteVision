"""Core data types passed between layers.

Everything that moves through the system is a Frame. Keeping metadata welded to
the pixels is deliberate: calibration work needs the exposure and gain that
produced a given image, and reconstructing that after the fact is unreliable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np


@dataclass(slots=True)
class Frame:
    """One image plus everything known about how it was produced.

    Attributes
    ----------
    data:   the pixels. Layout depends on `space`.
    space:  what the pixels mean. One of:
              'raw'    - unprocessed sensor data, Bayer or mono, ISP untouched
              'mono8'  - single channel, 8 bit
              'mono16' - single channel, 16 bit
              'rgb8'   - three channel, 8 bit
            Stages declare what they accept and what they emit, so a
            mis-ordered pipeline fails loudly instead of producing garbage.
    cam_id: which camera. Stable across restarts, comes from config.
    seq:    monotonically increasing per camera. Gaps mean dropped frames.
    t_mono: time.monotonic() at capture. Use this for intervals and sync.
    t_wall: time.time() at capture. Use this for filenames and logs only.
    meta:   sensor metadata from the driver (ExposureTime, AnalogueGain,
            SensorTimestamp, ...) plus anything stages choose to record.
    """

    data: np.ndarray
    cam_id: str
    seq: int
    t_mono: float
    t_wall: float
    space: str = "mono8"
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(
        cls, data: np.ndarray, cam_id: str, seq: int, space: str = "mono8", **meta: Any
    ) -> Frame:
        return cls(
            data=data,
            cam_id=cam_id,
            seq=seq,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            space=space,
            meta=dict(meta),
        )

    def derive(self, data: np.ndarray, space: str | None = None, **meta: Any) -> Frame:
        """Return a new Frame with different pixels but the same provenance.

        Stages must use this rather than mutating in place. Two consumers may
        hold the same Frame, and one of them is often writing it to disk.
        """
        merged = dict(self.meta)
        merged.update(meta)
        return replace(self, data=data, space=space or self.space, meta=merged)

    @property
    def shape(self) -> tuple[int, ...]:
        return self.data.shape


@dataclass(slots=True)
class CameraInfo:
    """What a source advertises about itself, for the UI and for logs."""

    cam_id: str
    model: str
    backend: str
    full_resolution: tuple[int, int]
    preview_resolution: tuple[int, int]
    mono: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cam_id": self.cam_id,
            "model": self.model,
            "backend": self.backend,
            "full_resolution": list(self.full_resolution),
            "preview_resolution": list(self.preview_resolution),
            "mono": self.mono,
            "detail": self.detail,
        }
