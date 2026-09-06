"""The boundary where a buffer becomes a science frame, or does not.

Review finding F7, and the generalisation the review draws from the whole
failure log:

> The common pattern is **accepting plausible structure as proof of correct
> meaning**: shaped arrays accepted as sensor counts, named files accepted as
> complete data, writable paths accepted as the selected disk.

A raw buffer arriving from libcamera is an array of the right shape and, at a
glance, obvious structure. That says nothing at all about whether its values
are sensor counts. Three ways it can fail to be, all previously admitted:

  * **Compressed.** On a Pi 5, libcamera's default raw format for the mono
    IMX296 is `MONO_PISP_COMP1` -- the imaging pipeline's compressed transport,
    one byte per pixel. `make_array` hands those bytes back as a plain uint8
    image. It has the right shape and visible structure, so it looks like a
    picture that has gone slightly wrong rather than a decode failure. A whole
    recording session was lost to it. The old code logged an error and
    continued.
  * **Packed.** 10-bit as five bytes per four pixels. Its width is not a pixel
    count at any scale, and nothing here unpacks it.
  * **Right format, wrong interpretation.** The old `_trim_stride` INFERRED the
    bytes per pixel from the row length -- it tried 1, then 2, and took the
    first that fitted within a plausible stride pad. That is the same mistake
    one level down: deducing meaning from shape. Here the negotiated format
    states the bytes per pixel and the shape must *confirm* it. When they
    disagree, that disagreement is the finding, not something to resolve by
    picking whichever reading fits.

So: nothing is admitted as `science` unless its format is known, uncompressed
and unpacked, its geometry reconciles with the sensor's, and its values fit the
bit depth it claims. A buffer that fails is not silently downgraded either --
it is refused, and only an explicit `allow_unvalidated_raw` in the config lets
it through, tagged `diagnostic`, named as such on disk, and rejected by the
offline readers for measurement.

This module has no camera dependency, which is the point: every rejection path
can be exercised from a byte array in a test, with no Pi and no libcamera.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..types import DIAGNOSTIC, SCIENCE  # noqa: F401  (re-exported for callers)

log = logging.getLogger(__name__)

# Rows are padded to a hardware-friendly stride -- 64 bytes on this pipeline.
# The allowance is generous because the alignment is not ours to promise, and
# bounded because an unbounded one would accept a buffer of the wrong format
# whose row happens to be longer.
MAX_STRIDE_PAD_BYTES = 256


class RawFormatError(ValueError):
    """A buffer cannot be admitted as sensor data, and why."""


@dataclass(frozen=True)
class RawFormat:
    """What a libcamera raw format name means, in the terms admission needs."""

    name: str
    bits: int                 # sensor bit depth
    bytes_per_pixel: int      # in the buffer AS DELIVERED, 0 if not whole bytes
    packed: bool = False
    compressed: bool = False
    mono: bool = True

    @property
    def admissible(self) -> bool:
        """Can a buffer in this format be read as pixel values by this code?

        Packed is refused rather than unsupported-by-accident: nothing here
        unpacks 5-bytes-per-4-pixels, and a packed buffer's width is not a
        pixel count at any scale, so every downstream length check would pass
        against a number that means nothing.
        """
        return not self.packed and not self.compressed and self.bytes_per_pixel > 0

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.uint8 if self.bytes_per_pixel == 1 else np.uint16)

    @property
    def max_value(self) -> int:
        return (1 << self.bits) - 1


def _mono(name: str, bits: int, *, packed: bool = False) -> RawFormat:
    return RawFormat(name=name, bits=bits,
                     bytes_per_pixel=0 if packed else (1 if bits <= 8 else 2),
                     packed=packed, mono=True)


def _bayer(name: str, bits: int, *, packed: bool = False) -> RawFormat:
    return RawFormat(name=name, bits=bits,
                     bytes_per_pixel=0 if packed else (1 if bits <= 8 else 2),
                     packed=packed, mono=False)


# Every format this code claims to understand. A name that is not here is not
# admitted -- an allowlist, not a denylist, because the failure being guarded
# against is precisely an unfamiliar format arriving and being treated as
# ordinary pixels.
KNOWN_FORMATS: dict[str, RawFormat] = {f.name: f for f in [
    _mono("R8", 8),
    _mono("R10", 10),
    _mono("R12", 12),
    _mono("R16", 16),
    _mono("R10_CSI2P", 10, packed=True),
    _mono("R12_CSI2P", 12, packed=True),
    # Bayer, for a colour sensor on the same rig. Linear sensor data, so
    # admissible; demosaicing is an offline concern.
    *[_bayer(f"S{p}{b}", b) for b in (10, 12) for p in
      ("RGGB", "BGGR", "GRBG", "GBRG")],
    *[_bayer(f"S{p}{b}_CSI2P", b, packed=True) for b in (10, 12) for p in
      ("RGGB", "BGGR", "GRBG", "GBRG")],
]}


def classify(name: str | None) -> RawFormat | None:
    """The format by name, or None if this code does not know it.

    Compressed formats are recognised by pattern rather than enumerated: the
    PiSP family is open-ended and the naming is stable -- everything compressed
    carries PISP and COMP. Returning a `RawFormat` for them rather than None is
    deliberate, so a refusal can say "this is compressed" instead of the much
    less useful "unknown format".
    """
    if not name:
        return None
    key = str(name).strip().upper()
    known = KNOWN_FORMATS.get(key)
    if known is not None:
        return known
    if "PISP" in key or "COMP" in key:
        return RawFormat(name=key, bits=0, bytes_per_pixel=0, compressed=True)
    return None


def describe_refusal(name: str | None) -> str:
    """Why this format cannot be admitted, in terms that say what to do."""
    fmt = classify(name)
    if fmt is None:
        return (f"raw format {name!r} is not one this code knows how to read. "
                f"Known: {', '.join(sorted(KNOWN_FORMATS))}. Run "
                f"scripts/probe_cameras.py to see what the sensor offers.")
    if fmt.compressed:
        return (f"raw format {name!r} is a PiSP COMPRESSED transport, not "
                f"sensor counts. Buffers in it have the right shape and "
                f"visible structure, which is why a whole session was once "
                f"recorded in it before anyone noticed. Choose an uncompressed "
                f"format (R10 on this sensor).")
    if fmt.packed:
        return (f"raw format {name!r} is PACKED ({fmt.bits}-bit as 5 bytes per "
                f"4 pixels). Its width is not a pixel count at any scale and "
                f"nothing here unpacks it. Use the unpacked variant "
                f"({fmt.name.replace('_CSI2P', '')}).")
    return f"raw format {name!r} cannot be admitted"


def best_format(candidates: list[str]) -> str | None:
    """The best admissible format from what the sensor advertises.

    Widest bit depth first, because a 10-bit sensor recorded at 8 has thrown
    away two bits that cannot be recovered. Unpacked only -- a packed name is
    not a fallback, it is a format this code would mis-read.
    """
    usable = [f for f in (classify(c) for c in candidates) if f and f.admissible]
    if not usable:
        return None
    return max(usable, key=lambda f: f.bits).name


@dataclass(frozen=True)
class Admitted:
    """A buffer that has been established to be pixel values, plus the evidence."""

    array: np.ndarray
    meta: dict[str, object]


def admit(
    data: np.ndarray,
    format_name: str | None,
    sensor_size: tuple[int, int],
    *,
    check_values: bool = True,
) -> Admitted:
    """Turn a raw buffer into an image, or raise saying why it is not one.

    `sensor_size` is (width, height) in PIXELS, before any orientation: this
    runs on the buffer exactly as the sensor delivered it, because row padding
    is on the right at that moment and on some other edge afterwards.

    Every check is against something independent of the buffer's own shape:

      1. the format is known, uncompressed and unpacked;
      2. the row count equals the sensor's height (rows are not padded);
      3. the row length in BYTES equals the sensor's width times the format's
         bytes per pixel, plus a bounded stride pad. Note the direction --
         the format states the pixel size and the shape confirms it. Inferring
         the pixel size from the shape, which is what this replaced, cannot
         distinguish "10-bit, 1456 wide, padded" from "8-bit, 2944 wide";
      4. no value exceeds the bit depth the format claims.

    The fourth is the one with teeth against a driver that hands back something
    other than what it negotiated. It costs about 1 ms on a 1.6 Mpx frame,
    which is why it is on the still path and off the preview path.
    """
    fmt = classify(format_name)
    if fmt is None or not fmt.admissible:
        raise RawFormatError(describe_refusal(format_name))

    if data.ndim != 2:
        raise RawFormatError(
            f"raw buffer is {data.ndim}-dimensional; a raw frame is one plane")

    height, row_len = data.shape
    sensor_w, sensor_h = int(sensor_size[0]), int(sensor_size[1])
    if height != sensor_h:
        raise RawFormatError(
            f"raw buffer has {height} rows and the sensor is {sensor_h} tall. "
            f"Row padding is a per-row phenomenon; a row COUNT that disagrees "
            f"means this is not the frame it claims to be.")

    row_bytes = row_len * data.dtype.itemsize
    want_bytes = sensor_w * fmt.bytes_per_pixel
    pad_bytes = row_bytes - want_bytes
    if pad_bytes < 0 or pad_bytes > MAX_STRIDE_PAD_BYTES:
        raise RawFormatError(
            f"raw buffer rows are {row_bytes} bytes; {fmt.name} at "
            f"{sensor_w} px wide needs {want_bytes} plus at most "
            f"{MAX_STRIDE_PAD_BYTES} of stride padding. Off by {pad_bytes}. "
            f"If that is close to a factor of two, the negotiated format and "
            f"the delivered buffer disagree about the pixel size.")

    # Re-view to the format's own dtype. A C-contiguous uint8 row of 2N bytes
    # IS N little-endian uint16 pixels: no copy, no arithmetic, and the values
    # become the counts the sensor produced rather than their halves.
    out = data
    if fmt.bytes_per_pixel == 2 and data.dtype.itemsize == 1:
        if row_len % 2:
            raise RawFormatError(
                f"{fmt.name} is two bytes per pixel but the buffer row is "
                f"{row_len} bytes, an odd number -- it cannot be whole pixels")
        out = np.ascontiguousarray(data).view(np.uint16)
    elif data.dtype.itemsize != fmt.bytes_per_pixel:
        raise RawFormatError(
            f"{fmt.name} is {fmt.bytes_per_pixel} byte(s) per pixel and the "
            f"buffer is {data.dtype}")

    stride_px = out.shape[1]
    if stride_px < sensor_w:
        raise RawFormatError(
            f"raw buffer is {stride_px} px wide and the sensor is {sensor_w}")
    trimmed = out[:, :sensor_w]

    if check_values and trimmed.size:
        peak = int(trimmed.max())
        if peak > fmt.max_value:
            raise RawFormatError(
                f"buffer contains a value of {peak}, above the {fmt.max_value} "
                f"maximum for {fmt.bits}-bit {fmt.name}. The negotiated format "
                f"and the delivered data do not agree, and the data is not "
                f"what it says it is.")

    return Admitted(
        array=trimmed,
        meta={
            "raw_format": fmt.name,
            "raw_bits": fmt.bits,
            "raw_bytes_per_pixel": fmt.bytes_per_pixel,
            "raw_stride_bytes": int(row_bytes),
            "raw_stride_px": int(stride_px),
            "raw_padding_px": int(stride_px - sensor_w),
            "raw_values_checked": bool(check_values),
        },
    )
