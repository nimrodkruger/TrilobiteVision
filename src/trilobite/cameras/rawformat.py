"""The boundary where a buffer becomes a science frame, or does not.

Review finding F7, and the generalisation the review draws from the whole
failure log:

> The common pattern is **accepting plausible structure as proof of correct
> meaning**: shaped arrays accepted as sensor counts, named files accepted as
> complete data, writable paths accepted as the selected disk.

A raw buffer arriving from libcamera is an array of the right shape and, at a
glance, obvious structure. That says nothing at all about whether its values
are sensor counts. Ways it can fail to be, all of which were once admitted:

  * **Compressed.** On a Pi 5, libcamera's default raw format for the mono
    IMX296 is `MONO_PISP_COMP1` -- the imaging pipeline's compressed transport,
    one byte per pixel. `make_array` hands those bytes back as a plain uint8
    image. It has the right shape and visible structure, so it looks like a
    picture that has gone slightly wrong rather than a decode failure. A whole
    recording session was lost to it.
  * **Packed.** 10-bit as five bytes per four pixels. Its width is not a pixel
    count at any scale, and nothing here unpacks it.
  * **Right format, wrong interpretation.** The original `_trim_stride`
    INFERRED the bytes per pixel from the row length -- it tried 1, then 2, and
    took the first that fitted within a plausible stride pad. That is the same
    mistake one level down: deducing meaning from shape. Here the negotiated
    format states the bytes per pixel and the shape must *confirm* it.
  * **Wrong sample representation.** Supervisory review R3. Checking
    `dtype.itemsize` is not checking the dtype: `int16` and `float16` are both
    two bytes, and both were accepted. A negative or fractional "sensor count"
    is not a sensor count. Byte order likewise -- a big-endian buffer has the
    right item size and the wrong values.
  * **Right format name, wrong bit alignment.** Also R3. The Picamera2 manual
    (raw stream configuration, pp. 21-22) describes Pi 5 uncompressed samples
    as **left-shifted within their 16-bit word**, and explicitly warns against
    deriving the sensor bit depth from the format name. So `R10` names a
    10-bit sensor sample, and says nothing about whether that sample sits in
    bits 0-9 or bits 6-15 of the container. Those two readings differ by a
    factor of 64 in every pixel.
  * **Requested, not negotiated.** Supervisory review R5. The format the code
    asked for is not the format the driver gave it, and the main stream's
    resolution is not the raw stream's. Both are read back after `configure`
    and passed in here; nothing in this module trusts a request.

So: nothing is admitted as `science` unless its format is known, uncompressed
and unpacked, its samples are unsigned integers in native byte order, its
geometry and stride reconcile with what the driver actually negotiated, and its
values fit the depth and alignment it claims.

**But failing that is graded, not refused, and the distinction is the whole
shape of this module.** An earlier version treated every failure as a refusal
and handed the buffer back untouched, which put pairs of bytes on screen as
pixels -- white noise with row structure -- for the ordinary case of a 10-bit
buffer whose stride did not reconcile. That was wrong twice over: it conflated
"we cannot vouch for these values" with "we will not interpret these bytes",
and it made the rig undiagnosable at exactly the moment somebody needed to
diagnose it.

The split now follows what a person can SEE:

  * **Refused** (`RawFormatError`, needs `allow_unvalidated_raw` to capture at
    all, tagged `diagnostic`): the format says the bytes are not pixel values.
    Compressed, packed, or unknown. There is no reading to produce, and the
    failure is invisible by construction -- a PiSP buffer looks like a slightly
    damaged photograph. This is the one with a body count.
  * **Graded** (`unvalidated`, with the reasons recorded): a row count that
    disagrees, a stride that does not match, values past the ceiling, a dtype
    that cannot hold unsigned samples. The pixels come back as the best reading
    available. Every one of these is visible on screen -- wrong alignment is
    64x too bright, wrong stride skews the aspect ratio -- so refusing to
    produce a frame buys no protection and costs the diagnosis.

**Three separate quantities, kept separate on purpose.** The sensor sample
depth (nominal, from the format name -- evidence of nothing on its own), the
container width in the buffer, and the alignment of the one inside the other.
Collapsing them into a single `bits` field is what the manual warns against.

This module has no camera dependency, which is the point: every rejection path
can be exercised from a byte array in a test, with no Pi and no libcamera.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..types import DIAGNOSTIC, SCIENCE, UNVALIDATED  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

# Rows are padded to a hardware-friendly stride -- 64 bytes on this pipeline.
# Used only when the driver's own stride is unavailable; when it is available
# the row length must match it EXACTLY and this bound never comes into play.
# The allowance is generous because the alignment is not ours to promise, and
# bounded because an unbounded one would accept a buffer of the wrong format
# whose row happens to be longer.
MAX_STRIDE_PAD_BYTES = 256

# Where the sensor sample sits inside its container word.
#
#   LSB   right-aligned. A 10-bit sample occupies bits 0-9; values run 0..1023.
#   MSB   left-aligned, which is what the Picamera2 manual describes for the
#         Pi 5. A 10-bit sample occupies bits 6-15; values run 0..65472 in
#         steps of 64, and the true sample is `value >> 6`.
#
# This is a property of the pipeline, not of the format name, so it is declared
# in the config and recorded in every sidecar. It is NOT guessed from the data:
# a dark MSB frame and a bright LSB frame are indistinguishable by their
# histogram, and guessing would put a factor of 64 into the record with the
# same confidence as everything else there.
LSB = "lsb"
MSB = "msb"
ALIGNMENTS = (LSB, MSB)


class RawFormatError(ValueError):
    """A buffer cannot be admitted as sensor data, and why."""


@dataclass(frozen=True)
class RawFormat:
    """What a libcamera raw format name means, in the terms admission needs."""

    name: str
    # The sensor sample depth the NAME implies. Nominal: the manual warns
    # against treating this as established, so it is recorded as
    # `raw_bits_nominal` with its source, never as measured evidence.
    bits: int
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
    def container_bits(self) -> int:
        """Width of the word each sample is delivered in, which is not `bits`."""
        return self.bytes_per_pixel * 8

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.uint8 if self.bytes_per_pixel == 1 else np.uint16)

    @property
    def max_value(self) -> int:
        """Largest legal SAMPLE value. Not the largest legal container value."""
        return (1 << self.bits) - 1

    def shift_for(self, alignment: str) -> int:
        """How far left the sample sits inside its container, in bits."""
        if alignment == MSB:
            return max(0, self.container_bits - self.bits)
        return 0


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

    Widest nominal depth first, because a 10-bit sensor recorded at 8 has
    thrown away two bits that cannot be recovered. Unpacked only -- a packed
    name is not a fallback, it is a format this code would mis-read.

    Choosing on the nominal depth is legitimate; *claiming* the nominal depth
    as the delivered sample depth is not, and admission does not do that.
    """
    usable = [f for f in (classify(c) for c in candidates) if f and f.admissible]
    if not usable:
        return None
    return max(usable, key=lambda f: f.bits).name


@dataclass(frozen=True)
class Admitted:
    """A buffer read as an image, plus what could and could not be established.

    `validity` is `science` only when nothing was left unresolved. Anything
    unresolved goes in `reservations` and the verdict drops to `unvalidated` --
    which is a grade, not a rejection. The array is still the best reading of
    the buffer this code can produce, because a frame nobody can look at helps
    nobody, and "we cannot vouch for these values" is a different statement
    from "we will not interpret these bytes".
    """

    array: np.ndarray
    meta: dict[str, object]
    validity: str
    reservations: tuple[str, ...] = ()

    @property
    def is_science(self) -> bool:
        return self.validity == SCIENCE


def _representation_reservations(data: np.ndarray, fmt: RawFormat) -> list[str]:
    """Is this array a plausible container for unsigned sensor samples?

    Supervisory review R3. The original check was `data.dtype.itemsize`, which
    is not a check on the dtype: `int16`, `float16` and `uint16` are all two
    bytes wide, and probes admitted `int16(-1)` and `float16(0.5)` as 10-bit
    sensor counts. A negative count and a fractional count are both impossible,
    so the invariant this module advertised was false.

    These are reservations rather than refusals. A buffer of the wrong dtype
    is certainly not admissible as science, and it is still something an
    operator may want to look at while working out what the driver is doing.
    """
    out: list[str] = []
    if data.dtype.kind != "u":
        kinds = {"i": "signed integer", "f": "floating point", "b": "boolean",
                 "c": "complex"}
        what = kinds.get(data.dtype.kind, f"kind {data.dtype.kind!r}")
        out.append(f"buffer is {data.dtype} -- {what}, not unsigned; a sensor "
                   f"count cannot be negative or fractional")
    if data.dtype.byteorder not in ("=", "|"):
        out.append(f"buffer is {data.dtype}, non-native byte order; every "
                   f"value would be byte-swapped")
    if fmt.bytes_per_pixel == 1 and data.dtype.itemsize != 1:
        out.append(f"{fmt.name} is one byte per pixel and the buffer is "
                   f"{data.dtype}")
    if fmt.bytes_per_pixel == 2 and data.dtype.itemsize not in (1, 2):
        out.append(f"{fmt.name} is two bytes per pixel; a {data.dtype} buffer "
                   f"cannot be a view of it")
    return out


def _value_evidence(
    trimmed: np.ndarray, fmt: RawFormat, alignment: str,
) -> tuple[dict[str, object], list[str]]:
    """Do the values fit the depth and alignment the format claims?

    The check with teeth against a driver that hands back something other than
    what it negotiated: R10 data that is really R12 has the right stride, the
    right shape and the right dtype, and differs only in its values.

    What it can prove depends on the alignment, and the difference is recorded
    rather than glossed:

      * **LSB.** A 10-bit sample lives in 0..1023, so anything above that is
        proof of a disagreement. Strong.
      * **MSB.** A 10-bit sample left-shifted by six lives in 0..65472 in steps
        of 64. The ceiling still catches R12-in-R10 (which reaches 65520), but
        the low six bits are the real evidence and some pipelines replicate the
        high bits into them rather than zero-filling, so a non-zero low field
        is recorded, never held against the buffer.

    A value over the ceiling is a **reservation**, not a refusal, and the
    reason is worth being explicit about: it is a visible failure. A frame read
    with the wrong alignment is 64x too bright or too dark and an operator sees
    that immediately. The failure this module exists for -- a compressed
    transport that looks like a slightly damaged photograph -- is invisible,
    and that is the one that still stops the capture.
    """
    shift = fmt.shift_for(alignment)
    peak = int(trimmed.max()) if trimmed.size else 0
    ceiling = fmt.max_value << shift

    meta: dict[str, object] = {
        "raw_values_checked": True,
        "raw_observed_max": peak,
        "raw_value_ceiling": int(ceiling),
    }
    out: list[str] = []

    if peak > ceiling:
        other = fmt.max_value << (fmt.container_bits - fmt.bits)
        hint = ""
        if alignment == LSB and peak <= other:
            hint = (f"; the peak IS consistent with a {fmt.bits}-bit sample "
                    f"left-shifted into a {fmt.container_bits}-bit word, which "
                    f"is what the Picamera2 manual describes for the Pi 5 -- "
                    f"if that is this pipeline, set 'raw_alignment: msb'")
        out.append(
            f"peak value {peak} exceeds the {ceiling} maximum for a "
            f"{fmt.bits}-bit {fmt.name} sample {alignment}-aligned in a "
            f"{fmt.container_bits}-bit container{hint}")

    if shift and trimmed.size:
        low = int(np.bitwise_and(trimmed, (1 << shift) - 1).max())
        meta["raw_low_bits_max"] = low
        meta["raw_low_bits_zero_filled"] = low == 0
    return meta, out


def admit(
    data: np.ndarray,
    format_name: str | None,
    raw_size: tuple[int, int],
    *,
    stride_bytes: int | None = None,
    alignment: str = LSB,
    check_values: bool = True,
) -> Admitted:
    """Read a raw buffer as an image and grade what could be established.

    `raw_size` is the (width, height) in PIXELS **of the raw stream as the
    driver negotiated it** -- not the main stream's, and not what the config
    asked for. Those three can differ, and using the wrong one is supervisory
    review R5. It is also pre-orientation: this runs on the buffer exactly as
    the sensor delivered it, because row padding is on the right at that moment
    and on some other edge afterwards.

    **Two very different outcomes, and the split is the point.**

    `RawFormatError` is raised only when the FORMAT says the bytes are not
    pixel values at all -- compressed, packed, or a name this code does not
    know. There is no reading of such a buffer to produce, the failure is
    invisible by construction (a PiSP buffer looks like a slightly damaged
    photograph), and it is the one that cost a recording session. That stays a
    hard stop.

    Everything else -- a row count that disagrees, a stride that does not match
    what the driver reported, values past the declared ceiling, a dtype that
    cannot hold unsigned samples -- comes back as a **reservation** on an
    `Admitted` whose validity is `unvalidated`. The pixels are still the best
    reading available. Every one of those failures is VISIBLE: a wrong
    alignment is 64x too bright, a wrong stride skews the aspect ratio, and an
    operator sees both at a glance. Refusing to produce a frame for them buys
    no protection and costs the ability to diagnose the rig at all.

    The checks, each against something independent of the buffer's own shape:

      1. the format is known, uncompressed and unpacked  *(hard)*
      2. the samples are unsigned integers in native byte order  *(R3)*
      3. the geometry is positive and the row count equals the negotiated
         height -- rows are not padded, so a row COUNT that disagrees means
         this is not the frame it claims to be
      4. the row length in BYTES equals the negotiated stride, or failing that
         width x bytes-per-pixel plus a bounded pad. Note the direction: the
         format states the pixel size and the shape confirms it. Inferring the
         pixel size from the shape, which is what this replaced, cannot
         distinguish "10-bit, 1456 wide, padded" from "8-bit, 2944 wide"
      5. the values fit the depth AND the declared alignment  *(R3)*

    The pixels are returned **unshifted**. For MSB alignment the sample is
    `value >> raw_sample_shift`, and that shift is recorded rather than
    applied: silently rescaling every pixel on the way to disk is the same
    class of act this module exists to prevent.
    """
    fmt = classify(format_name)
    if fmt is None or not fmt.admissible:
        raise RawFormatError(describe_refusal(format_name))
    if alignment not in ALIGNMENTS:
        raise RawFormatError(
            f"raw alignment {alignment!r} is not one of {ALIGNMENTS}. It says "
            f"where the sensor sample sits inside its container word and "
            f"cannot be guessed from the data.")
    if data.ndim != 2:
        raise RawFormatError(
            f"raw buffer is {data.ndim}-dimensional; a raw frame is one plane")

    reservations = _representation_reservations(data, fmt)

    sensor_w, sensor_h = int(raw_size[0]), int(raw_size[1])
    if sensor_w <= 0 or sensor_h <= 0:
        raise RawFormatError(
            f"negotiated raw size is {sensor_w}x{sensor_h}; a frame cannot "
            f"have a non-positive dimension, and there is nothing to trim to")

    height, row_len = data.shape
    if height != sensor_h:
        reservations.append(
            f"buffer has {height} rows and the negotiated raw stream is "
            f"{sensor_h} tall; row padding is a per-row phenomenon, so a row "
            f"COUNT that disagrees means this is not the frame it claims to be")

    row_bytes = row_len * data.dtype.itemsize
    want_bytes = sensor_w * fmt.bytes_per_pixel

    if stride_bytes is not None:
        stride_bytes = int(stride_bytes)
        stride_source = "negotiated"
        if row_bytes != stride_bytes:
            reservations.append(
                f"buffer rows are {row_bytes} bytes and the driver negotiated "
                f"a stride of {stride_bytes}")
        pad = stride_bytes - want_bytes
        if pad < 0 or pad > MAX_STRIDE_PAD_BYTES:
            reservations.append(
                f"the negotiated {stride_bytes}-byte stride is not "
                f"{want_bytes} plus at most {MAX_STRIDE_PAD_BYTES} of padding, "
                f"so the configuration is internally inconsistent")
    else:
        stride_source = "inferred"
        pad = row_bytes - want_bytes
        if pad < 0 or pad > MAX_STRIDE_PAD_BYTES:
            reservations.append(
                f"buffer rows are {row_bytes} bytes; {fmt.name} at {sensor_w} "
                f"px wide needs {want_bytes} plus at most "
                f"{MAX_STRIDE_PAD_BYTES} of stride padding, off by {pad}. If "
                f"that is close to a factor of two, the negotiated format and "
                f"the delivered buffer disagree about the pixel size")

    # Re-view to the format's own dtype. A C-contiguous uint8 row of 2N bytes
    # IS N little-endian uint16 pixels: no copy, no arithmetic, and the values
    # become the counts the sensor produced rather than their halves.
    #
    # Attempted even when there are reservations, because this is exactly the
    # step whose absence produced white noise on screen: a 10-bit buffer left
    # as uint8 is pairs of bytes displayed as pixels.
    out = data
    if fmt.bytes_per_pixel == 2 and data.dtype.itemsize == 1:
        if row_len % 2:
            reservations.append(
                f"{fmt.name} is two bytes per pixel but the buffer row is "
                f"{row_len} bytes, an odd number -- it cannot be whole pixels, "
                f"so it is left as bytes")
        else:
            out = np.ascontiguousarray(data).view(np.uint16)

    stride_px = out.shape[1]
    if stride_px < sensor_w:
        reservations.append(
            f"buffer is {stride_px} px wide and the negotiated raw stream is "
            f"{sensor_w}, so nothing was trimmed")
        trimmed = out
    else:
        trimmed = out[:, :sensor_w]

    meta: dict[str, object] = {
        "raw_format": fmt.name,
        # Nominal, and named so. Derived from the format name, which the
        # Picamera2 manual warns is not evidence of the delivered sample depth.
        "raw_bits_nominal": fmt.bits,
        "raw_bits_source": "format-name",
        "raw_container_bits": fmt.container_bits,
        "raw_alignment": alignment,
        "raw_sample_shift": fmt.shift_for(alignment),
        "raw_bytes_per_pixel": fmt.bytes_per_pixel,
        "raw_dtype": str(out.dtype),
        "raw_stride_bytes": int(row_bytes),
        "raw_stride_source": stride_source,
        "raw_stride_px": int(stride_px),
        "raw_padding_px": int(stride_px - sensor_w),
        "raw_width": sensor_w,
        "raw_height": sensor_h,
    }

    if check_values and trimmed.dtype.kind == "u":
        value_meta, value_reservations = _value_evidence(trimmed, fmt, alignment)
        meta.update(value_meta)
        reservations.extend(value_reservations)
    else:
        # About 1 ms on a 1.6 Mpx frame, which is why it can be skipped on a
        # preview path. Recorded, so a sidecar never implies a check that did
        # not happen.
        meta["raw_values_checked"] = False

    validity = SCIENCE if not reservations else UNVALIDATED
    meta["raw_admitted"] = validity == SCIENCE
    meta["raw_reservations"] = list(reservations)
    return Admitted(array=trimmed, meta=meta, validity=validity,
                    reservations=tuple(reservations))


