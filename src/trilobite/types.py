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

# The values `Frame.validity` may take. Defined here rather than in
# cameras/rawformat.py, where they are used most, because `types` is the layer
# everything else sits on: the other direction would have `types` importing
# from `cameras`, and `cameras.base` already imports `types`. That cycle is
# latent today only because `cameras/__init__.py` happens to be empty.
#
# THREE values, not two, and the third is the default. Supervisory review R2:
# `validity` defaulted to `science`, so every construction that did not think
# about the question asserted the strongest possible claim about its pixels.
# A replayed array whose own sidecar said `diagnostic` came back out marked
# `science`. The default now asserts nothing, and `science` has to be put there
# by something that established it.
SCIENCE = "science"            # established: these are sensor counts
DIAGNOSTIC = "diagnostic"      # established NOT to be: refused, or made for viewing
UNVALIDATED = "unvalidated"    # nothing was established either way
VALIDITIES = (SCIENCE, DIAGNOSTIC, UNVALIDATED)

# What produced the pixels. Separate from `validity` because the two answer
# different questions -- "where did this come from" and "what is known about
# it" -- and because measurement eligibility is not one predicate. Corner
# GEOMETRY off an ISP mono frame is defensible; radiometry off the same frame
# is not. Without the source recorded, a reader has no way to tell those two
# cases apart, and Stage 4 has to define the distinction properly.
SRC_RAW = "raw"                # sensor buffer, ISP bypassed, through admission
SRC_ISP_MAIN = "isp_main"      # full-resolution ISP output
SRC_ISP_PREVIEW = "isp_lores"  # the low-resolution ISP preview stream
SRC_SYNTHETIC = "synthetic"    # rendered; no sensor involved
SRC_REPLAY = "replay"          # read back from a file
SRC_UNKNOWN = "unknown"
SOURCE_KINDS = (SRC_RAW, SRC_ISP_MAIN, SRC_ISP_PREVIEW, SRC_SYNTHETIC,
                SRC_REPLAY, SRC_UNKNOWN)


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

    seq:    monotonically increasing per camera, counting frames this software
            **delivered**. It is not an exposure counter, and a gap in it is
            not evidence of a dropped exposure: frames released by the rate cap
            never reach here at all, and neither does anything the driver lost.
            Do not use it for drop accounting or for pairing two cameras. The
            docstring used to say "gaps mean dropped frames", which was wrong
            in the direction that matters -- it invited exactly that use.

    t_mono: `time.monotonic()` at Frame CONSTRUCTION, which is after the buffer
            has been taken, converted and copied. Fine for intervals between
            deliveries. It is not the moment light hit the sensor, and it is
            several milliseconds after it.

    t_wall: `time.time()` at construction. Filenames and logs only -- it is
            subject to NTP steps, so an interval computed from it can come out
            negative.

    meta:   sensor metadata from the driver, plus anything stages record. The
            driver's own exposure timestamp arrives here as `SensorTimestamp`,
            in the kernel's monotonic clock domain, in nanoseconds. That is the
            only timestamp in this object with a defined relationship to the
            exposure, and it is the one that stereo pairing and drop accounting
            will need.

    **Four times, and this object carries two of them.** Exposure, receipt,
    processing completion, write completion. `t_mono` is receipt. Treating it
    as exposure is the mistake that makes a synchronisation measurement quietly
    wrong, which is why the distinction is written down here rather than
    assumed.
    """

    data: np.ndarray
    cam_id: str
    seq: int
    t_mono: float
    t_wall: float
    space: str = "mono8"
    # What is known about whether these pixels are sensor counts. A
    # first-class field rather than a metadata key because it is a claim about
    # the data's admissibility, and a claim like that should be impossible to
    # lose by forgetting to copy a dictionary entry. `derive` carries it.
    #
    #   'science'      the format was negotiated, known, uncompressed and
    #                  unpacked, the samples are unsigned integers in native
    #                  byte order, the geometry and stride reconcile with what
    #                  the driver negotiated, and the values fit the depth and
    #                  alignment claimed. See cameras/rawformat.py.
    #   'diagnostic'   established NOT to be: a refused buffer captured under
    #                  `allow_unvalidated_raw`, or a preview deliberately
    #                  processed for viewing.
    #   'unvalidated'  nothing was established. THE DEFAULT, so that a producer
    #                  which never considered the question cannot assert the
    #                  strongest claim by omission.
    #
    # `unvalidated` is not a weaker `diagnostic`. Diagnostic is a positive
    # statement that the values are wrong; unvalidated is the absence of a
    # statement, which is what an ISP frame or a sidecar-less replay honestly
    # has. Readers treat both as inadmissible and say which they hit.
    validity: str = UNVALIDATED
    # Where the pixels came from. See the SRC_* constants above.
    source_kind: str = SRC_UNKNOWN
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(
        cls, data: np.ndarray, cam_id: str, seq: int, space: str = "mono8",
        validity: str = UNVALIDATED, source_kind: str = SRC_UNKNOWN, **meta: Any
    ) -> Frame:
        return cls(
            data=data,
            cam_id=cam_id,
            seq=seq,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            space=space,
            validity=validity,
            source_kind=source_kind,
            meta=dict(meta),
        )

    @property
    def is_science(self) -> bool:
        return self.validity == SCIENCE

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
