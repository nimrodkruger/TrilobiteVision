# MATLAB side

Offline reading of TrilobiteVision captures. Nothing here runs on the Pi, and
nothing here needs a toolbox: base MATLAB only, and it also runs unmodified
under GNU Octave 7+, which is what it was tested against.

Add the folder to the path and go:

```matlab
addpath('matlab');
demo_read_capture('trilobite-data/session_.../left/raw_left_000001.npy');
```

## The four functions

| function | what it gives you |
|---|---|
| `tv_read_npy` | a `.npy` file as a MATLAB array, native class, correct orientation |
| `tv_read_capture` | the image **and** its metadata, with the MLA geometry rescaled to the frame you are actually holding |
| `tv_micro_images` | an M×N cell of X×Y micro-images, de-rotated onto the lattice axes |
| `tv_sub_apertures` | the transpose of that cube: X×Y sub-aperture images, each M×N |

`tv_selftest` runs every claim they make as an assertion, headless:

```matlab
tv_selftest('trilobite-data/session_.../left/raw_left_000001.npy')
```

## Micro-images vs sub-apertures

These are the same numbers indexed two ways, and confusing them is the single
most common way to waste an afternoon.

A **micro-image** is what one lenslet puts on the sensor: X×Y pixels, a small
picture of the main lens's exit pupil, and there are M×N of them. It is the raw
structure of the sensor. It is what you look at to judge focus, pitch and
alignment, and it is where the calibration target has to be resolvable.

A **sub-aperture image** takes the *same pixel* out of every micro-image —
pixel (u,v) from all M·N lenslets, assembled in lattice order. It is an M×N
picture of the scene through one small patch of the main aperture, which is
geometrically an ordinary pinhole camera. That is why it matters: a calibration
model has a projection matrix for a pinhole camera and nothing at all for a
micro-image. One exposure yields X·Y of them, each with a slightly different
centre of projection.

The trade is resolution. A sub-aperture image is only as large as the lenslet
array — on this rig about 13×11. That is small, and it is exactly why the
target has to put detectable structure inside a *micro-image* rather than
relying on the sub-aperture views being detailed.

`tv_sub_apertures` is a `permute`. Nothing is interpolated and nothing is lost;
applying it twice returns the input.

## Three things that will bite you

**The rescale.** The MLA grid is aligned by eye against the *preview* (728×544)
and the sidecar records `pitch_px` and the offsets in those pixels, alongside
the `reference_width`/`reference_height` they were quoted against. A raw
capture is the full sensor frame (1456×1088), so pitch and both offsets must be
doubled before they mean anything. `tv_read_capture` does this and returns
`.mla` already in the frame's own coordinates, with the unscaled original kept
in `.mla.reference`.

Getting it wrong does not raise. It puts every crop between micro-images
instead of on one, and the only symptom is that nothing ever detects — which
from the outside is indistinguishable from an optics problem.

**C order vs Fortran order.** NumPy writes the last axis fastest; MATLAB's
`reshape` fills the first dimension fastest. Reading the bytes straight into
`reshape(v, shape)` returns the transpose, silently. `tv_read_npy` reshapes
into the reversed shape and permutes back.

**`meshgrid` argument order.** MATLAB's `meshgrid` varies its *first* output
along columns; NumPy's `meshgrid(..., indexing='ij')` varies its first along
rows. Writing the resampling grid the NumPy way transposes every de-rotated
tile while every dimension still matches, so no shape check can see it. This
was a live bug in the first version of `tv_micro_images`, found by comparing
against the Python extractor on the same file (mean difference 83.7 of 255).
`tv_selftest` now pins it with a coordinate-ramp assertion that has a known
closed form.

## Coordinates

`.mla` and `grid.centres` are in the **Python convention**: pixel centres at
0-based integers, x right, y **down**, frame centre at ((W−1)/2, (H−1)/2). That
is deliberate — every number is then directly comparable with the sidecar, with
`scripts/read_capture.py`, and with the corners the rig itself records. The
conversion to MATLAB's 1-based subscripts happens on exactly one marked line
inside `tv_micro_images`, and nowhere else.

## Verification

`tv_selftest` passes 19 of 19 on both cameras of a real capture pair, including
the rotated one (2°, crop scale 0.9). Separately, the de-rotated extraction was
compared pixel for pixel against `MLAGeometry.crop_derotated` in Python on the
same file: **max difference 0**.
