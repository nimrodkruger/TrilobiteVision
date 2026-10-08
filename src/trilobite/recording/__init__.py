"""Recording: a bounded RAM burst (5b) and a continuous recorder (5c).

The two share everything that matters -- the frame buffer, the chunk format,
the index, the storage admission -- and differ in one respect only: where the
frames are held between the sensor and the disk.

    burst        sensor -> prefaulted RAM -> (one bulk write) -> disk
    continuous   sensor -> bounded queue  -> (streamed blocks) -> disk

That difference is why burst came first. A burst's size is arithmetic before a
single byte is written, so "will this fit, on this target, with the reserve
intact" can be *answered* rather than monitored. A continuous recording can
only be watched while it runs, and the honest response to a disk that cannot
keep up is to lose frames on purpose and count them.
"""

from __future__ import annotations

from .buffer import FrameBuffer, FrameMeta, PrefaultFailed
from .chunks import RECORDING_SCHEMA, ChunkWriter, Gap, Journal
from .ladder import FORMATS, RAW8, RAW10P, RAW16, Plan, RecordingFormat, enumerate_plans

__all__ = [
    "FORMATS",
    "RAW8",
    "RAW10P",
    "RAW16",
    "RECORDING_SCHEMA",
    "ChunkWriter",
    "FrameBuffer",
    "FrameMeta",
    "Gap",
    "Journal",
    "Plan",
    "PrefaultFailed",
    "RecordingFormat",
    "enumerate_plans",
]
