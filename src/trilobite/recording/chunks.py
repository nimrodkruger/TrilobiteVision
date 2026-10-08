"""Writing a recording to disk: fixed-shape chunks, and an index that is the
authority on what is in them.

## Why not one file per frame

Eighteen thousand files for a five-minute pair recording. ext4 handles it
badly, exFAT -- which is what lets the SSD mount on a Windows desktop without
a driver -- handles it far worse, and every reader then has to sort a directory
listing to recover an order that was known at write time. So: one `.npy` per
head per chunk, holding a 3-D `(N, H, W)` array.

`.npy` and not a new container, deliberately. `numpy.load(..., mmap_mode='r')`
opens it without reading it, the existing MATLAB `tv_read_npy` already parses
the format, and nothing in either reader has to learn a new header. The cost is
that the frame count has to appear in the header, which is written first --
handled below.

## Why the index is authoritative and the array is not

The array holds the frames that were **kept**. The index holds, per frame, the
acquisition sequence number **as exposed**. Those differ exactly when a frame
was dropped, and that difference is the entire record of the loss:

    index seq:  1201 1202 1203 1207 1208        <- three frames lost
    array row:     0    1    2    3    4

Reading row 3 and assuming it is the frame after row 2 is wrong by 130 ms. So
the index is written beside every chunk, it carries the sequence numbers and
the per-frame `SensorTimestamp`, and the gaps are additionally summarised as
explicit intervals so that "longest gap" is a number somebody can read rather
than a diff somebody has to compute.

## Why fsync at chunk-block boundaries and not per frame

`fsync` is per call, and sixty of them a second on a USB SSD collapses
sustained throughput -- the drive cannot coalesce, and every one is a round
trip through the bridge. The guarantee per-frame fsync would buy is "the last
frame is durable", which is not worth having when the completion journal
already says which chunks are complete. So frames are written in blocks of a
few megabytes, fsync happens at the end of each block, and a recording
interrupted by a power cut loses at most the final block, which the journal
reports as incomplete rather than leaving to be discovered.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..storage.writer import fsync_dir, fsync_file, verify_size, write_durably
from .buffer import FrameMeta

log = logging.getLogger(__name__)

# Bumped when the shape of a recording directory changes in a way a reader must
# notice. Separate from the still sidecar's SIDECAR_SCHEMA: a reader may
# understand one and not the other, and conflating them would force both to
# move together.
RECORDING_SCHEMA = 1

# Total bytes of the .npy header, fixed so the frame count can be rewritten in
# place at close without moving the data. Must satisfy numpy's v1.0 rule that
# magic(6) + version(2) + header_len(2) + header is a multiple of 64.
HEADER_BYTES = 128


def npy_header(shape: tuple[int, ...], dtype: np.dtype,
               total: int = HEADER_BYTES) -> bytes:
    """A complete .npy v1.0 header of exactly `total` bytes.

    Built by hand for one reason: the final chunk of a recording is short, and
    its true frame count is not known until the recording stops. numpy's own
    writer sizes the header to the shape it is given, so a `(128, ...)` header
    rewritten as `(37, ...)` would be shorter and the pixel data would have to
    move. Padding to a fixed length instead means the count can be corrected by
    seeking to byte zero and writing 128 bytes, with every frame left where it
    is.

    The padding is spaces before the terminating newline, which is what the
    format permits and what numpy itself emits.
    """
    descr = np.lib.format.dtype_to_descr(np.dtype(dtype))
    dims = tuple(int(s) for s in shape)
    body = (f"{{'descr': {descr!r}, 'fortran_order': False, "
            f"'shape': {dims!r}, }}").encode("latin1")
    pad = total - 10 - len(body) - 1
    if pad < 0:
        raise ValueError(
            f"a {total}-byte header cannot hold {len(body) + 11} bytes for "
            f"shape {shape}")
    header = body + b" " * pad + b"\n"
    return (np.lib.format.MAGIC_PREFIX + bytes([1, 0])
            + struct.pack("<H", len(header)) + header)


@dataclass
class Gap:
    """A run of exposed-but-not-stored frames, as an interval.

    Stored as first/last sequence number plus the timestamps either side of it,
    so a reader can state how long the hole was in seconds without having to
    assume the nominal frame interval -- which is precisely the assumption that
    a dropped-frame recording invalidates.
    """

    first_seq: int
    last_seq: int
    before_t_mono: float | None = None
    after_t_mono: float | None = None

    @property
    def frames(self) -> int:
        return self.last_seq - self.first_seq + 1

    @property
    def seconds(self) -> float | None:
        if self.before_t_mono is None or self.after_t_mono is None:
            return None
        return self.after_t_mono - self.before_t_mono

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "frames": self.frames,
            "seconds": None if self.seconds is None else round(self.seconds, 4),
        }


def gaps_from(metas: list[FrameMeta]) -> list[Gap]:
    """Every missing run in a stored sequence, as intervals.

    The index already implies this; computing it once at close and writing it
    down is the difference between a reader being *able* to find the gaps and a
    person being able to read how bad the losses were.
    """
    out: list[Gap] = []
    for prev, cur in zip(metas, metas[1:], strict=False):
        if cur.seq > prev.seq + 1:
            out.append(Gap(prev.seq + 1, cur.seq - 1,
                           prev.t_mono, cur.t_mono))
    return out


@dataclass
class ChunkRecord:
    """One written chunk file, as the journal records it."""

    index: int
    path: str
    frames: int
    first_seq: int | None
    last_seq: int | None
    bytes: int
    complete: bool = False
    generation: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "file": Path(self.path).name,
            "frames": self.frames, "first_seq": self.first_seq,
            "last_seq": self.last_seq, "bytes": self.bytes,
            "complete": self.complete, "storage_generation": self.generation,
        }


class ChunkWriter:
    """Streams one head's frames into fixed-size `.npy` chunks plus indexes.

    One instance per head. Not thread-safe by design: it is driven by that
    head's single writer thread, and making it safe would invite a second
    writer, which is the thing the chunk-per-head layout exists to avoid.
    """

    def __init__(
        self,
        directory: Path,
        cam_id: str,
        shape: tuple[int, int],
        dtype: Any,
        chunk_frames: int,
        prefix: str = "rec",
        generation: int = 0,
    ) -> None:
        self.directory = Path(directory)
        self.cam_id = cam_id
        self.shape = (int(shape[0]), int(shape[1]))
        self.dtype = np.dtype(dtype)
        self.chunk_frames = int(chunk_frames)
        self.prefix = prefix
        self.generation = int(generation)
        self.directory.mkdir(parents=True, exist_ok=True)

        self.frame_bytes = int(np.prod(self.shape)) * self.dtype.itemsize
        self.chunks: list[ChunkRecord] = []
        self.bytes_written = 0

        self._fh = None
        self._current: ChunkRecord | None = None
        self._metas: list[FrameMeta] = []
        self._all_metas: list[FrameMeta] = []

    # -- chunk lifecycle --------------------------------------------------

    def _chunk_path(self, n: int) -> Path:
        return self.directory / f"{self.prefix}_{self.cam_id}_{n:05d}.npy"

    def _open_chunk(self) -> None:
        n = len(self.chunks)
        path = self._chunk_path(n)
        fh = open(path, "wb")  # noqa: SIM115 - closed in _close_chunk
        # Written for the FULL chunk size and corrected at close if short. See
        # npy_header for why the length is fixed.
        fh.write(npy_header((self.chunk_frames, *self.shape), self.dtype))
        self._fh = fh
        self._current = ChunkRecord(
            index=n, path=str(path), frames=0, first_seq=None, last_seq=None,
            bytes=0, generation=self.generation)
        self._metas = []
        self.chunks.append(self._current)

    def _close_chunk(self) -> None:
        if self._fh is None or self._current is None:
            return
        fh, rec = self._fh, self._current
        try:
            fsync_file(fh)
            if rec.frames != self.chunk_frames:
                # Correct the declared frame count in place. The header length
                # is fixed, so every frame stays exactly where it was written.
                fh.seek(0)
                fh.write(npy_header((rec.frames, *self.shape), self.dtype))
                fsync_file(fh)
        finally:
            fh.close()
            self._fh = None

        path = Path(rec.path)
        expected = HEADER_BYTES + rec.frames * self.frame_bytes
        rec.bytes = verify_size(path, expected)
        rec.complete = True
        self.bytes_written += rec.bytes
        self._write_index(rec, self._metas)
        fsync_dir(self.directory)
        self._current = None

    def _write_index(self, rec: ChunkRecord, metas: list[FrameMeta]) -> Path:
        """The authoritative record of what is in one chunk.

        Written AFTER the chunk's own fsync and before the directory fsync, so
        an index that exists always describes data that is already on the
        device. The reverse order would allow an index naming frames that a
        power cut took, which is worse than a chunk with no index: the first
        lies, the second is visibly incomplete.
        """
        payload = {
            "schema": RECORDING_SCHEMA,
            "cam_id": self.cam_id,
            "chunk": rec.index,
            "file": Path(rec.path).name,
            "shape": [rec.frames, *self.shape],
            "dtype": str(self.dtype),
            "frame_bytes": self.frame_bytes,
            "header_bytes": HEADER_BYTES,
            "storage_generation": self.generation,
            # Per frame, in stored order. `seq` is AS EXPOSED: a jump of more
            # than one between consecutive entries is a dropped frame, and that
            # is the only place the loss is recorded.
            "frames": [m.as_dict() for m in metas],
            "gaps": [g.as_dict() for g in gaps_from(metas)],
        }
        path = Path(rec.path).with_suffix(".index.json")
        body = json.dumps(payload, indent=1).encode("utf-8")
        write_durably(path, body)
        verify_size(path, len(body))
        return path

    # -- writing ----------------------------------------------------------

    def _check(self, pixels: np.ndarray) -> None:
        """Geometry and depth do not change mid-recording. Enforced per frame.

        This is the invariant stated in ladder.py, checked where it can
        actually be violated. A configuration is chosen before Start and
        nothing is supposed to alter it, but "supposed to" is not a guarantee
        and the cost of being wrong is a chunk file whose frames are not all
        the same size -- which is not merely unreadable, it is unreadable in a
        way that silently misaligns every frame after the first bad one.
        """
        if pixels.shape != self.shape:
            raise ValueError(
                f"frame is {pixels.shape}, this recording is {self.shape}. "
                f"Geometry does not change mid-recording.")
        if pixels.dtype != self.dtype:
            raise ValueError(
                f"frame dtype is {pixels.dtype}, this recording is "
                f"{self.dtype}. Sample depth does not change mid-recording.")

    def write_frame(self, pixels: np.ndarray, meta: FrameMeta) -> int:
        """Append one frame. **Does not fsync** -- call `sync()` per block.

        One `write()` per frame and one `fsync` per block, rather than
        gathering frames into a contiguous array first. The gather would be a
        second full-rate copy of every frame (the recorder has already made one
        getting the data out of the libcamera request), and at 190 MB/s that
        copy is not affordable. A `write()` syscall per frame, by contrast, is
        sixty a second and costs nothing measurable. So the block is the unit
        of durability, not the unit of transfer.
        """
        self._check(pixels)
        if self._fh is None:
            self._open_chunk()
        assert self._current is not None and self._fh is not None
        part = pixels if pixels.flags["C_CONTIGUOUS"] else np.ascontiguousarray(pixels)
        self._fh.write(memoryview(part).cast("B"))
        self._metas.append(meta)
        self._all_metas.append(meta)
        if self._current.first_seq is None:
            self._current.first_seq = meta.seq
        self._current.last_seq = meta.seq
        self._current.frames += 1
        if self._current.frames >= self.chunk_frames:
            fsync_file(self._fh)
            self._close_chunk()
        return self.frame_bytes

    def sync(self) -> None:
        """Make everything written so far durable. The block boundary."""
        if self._fh is not None:
            fsync_file(self._fh)

    def write_block(self, frames: np.ndarray, metas: list[FrameMeta]) -> int:
        """Append several frames and sync once. Returns bytes written.

        Used by the burst flush, where the frames are already contiguous in the
        RAM buffer and the whole recording is written in one pass.
        """
        if len(frames) != len(metas):
            raise ValueError(
                f"{len(frames)} frames and {len(metas)} metadata records: a "
                f"frame without its sequence number cannot be placed in time.")
        written = 0
        for pixels, meta in zip(frames, metas, strict=True):
            written += self.write_frame(pixels, meta)
        self.sync()
        return written

    def close(self) -> list[ChunkRecord]:
        """Finish the open chunk and return the journal rows."""
        if self._fh is not None:
            if self._current is not None and self._current.frames == 0:
                # An empty trailing chunk is a file that claims zero frames and
                # confuses every reader that globs the directory. Remove it.
                self._fh.close()
                self._fh = None
                path = Path(self._current.path)
                self.chunks.remove(self._current)
                self._current = None
                try:
                    path.unlink()
                except OSError as exc:                    # noqa: PERF203
                    log.warning("could not remove empty chunk %s: %s", path, exc)
            else:
                self._close_chunk()
        return list(self.chunks)

    # -- reporting --------------------------------------------------------

    @property
    def frames_written(self) -> int:
        return len(self._all_metas)

    def summary(self) -> dict[str, Any]:
        metas = self._all_metas
        gaps = gaps_from(metas)
        exposed = 0
        if metas:
            exposed = metas[-1].seq - metas[0].seq + 1
        dropped = exposed - len(metas) if exposed else 0
        return {
            "cam_id": self.cam_id,
            "chunks": [c.as_dict() for c in self.chunks],
            "frames_stored": len(metas),
            "frames_exposed": exposed,
            "frames_dropped": max(0, dropped),
            "drop_fraction": (round(dropped / exposed, 4) if exposed else 0.0),
            "first_seq": metas[0].seq if metas else None,
            "last_seq": metas[-1].seq if metas else None,
            "bytes": self.bytes_written,
            "gaps": [g.as_dict() for g in gaps],
            "longest_gap_frames": max((g.frames for g in gaps), default=0),
            "longest_gap_s": max(
                (g.seconds for g in gaps if g.seconds is not None), default=0.0),
            # True only if every stored frame was admissible as sensor counts.
            # The first one that was not is named, because "some frame stopped
            # being science" is not actionable and "frame 9,014 was" is.
            "all_science": all(m.validity == "science" for m in metas),
            "first_non_science_seq": next(
                (m.seq for m in metas if m.validity != "science"), None),
        }


@dataclass
class Journal:
    """The completion record for a whole recording, written last.

    Last, because its presence is the claim that the recording finished. A
    directory of chunks with no journal is an interrupted recording and reads
    as one; a journal written first would turn an interrupted recording into
    one that claims to be whole.
    """

    directory: Path
    payload: dict[str, Any] = field(default_factory=dict)

    def write(self) -> Path:
        path = Path(self.directory) / "recording.json"
        body = json.dumps(self.payload, indent=2).encode("utf-8")
        write_durably(path, body)
        verify_size(path, len(body))
        return path
