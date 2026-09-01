# Sub-aperture calibration strategy

**Rig:** focused plenoptic (2.0), MLA behind a main objective, ~100 px pitch on
a 1456 × 1088 IMX296 → ~14 × 10 ≈ 140 micro-images, minimal overlap, mono, two
cameras. **Goal:** pixel-to-pixel registration across views, valid at *any*
object distance. Geometry only.

Equations are plain text so they render without a LaTeX extension.

---

## 0. Correction to the previous draft

The earlier version said λ = a/b depends on object depth. **That was wrong.**

`a` is the object-side conjugate of the *microlens*, fixed by `1/a + 1/b =
1/f_μ` with `b` the mechanical MLA-to-sensor spacing. Both are properties of
the camera, not of the scene. **λ is a camera constant.**

What depends on object depth is where the main objective forms its intermediate
image relative to the microlenses' object plane Π. That offset is what changes
the *disparity* of a scene point between neighbouring micro-images — Raytrix
call it virtual depth `v`. Depth is a measurement the camera makes, not a
parameter of the calibration.

The consequence matters: the pixel→ray map below is depth-independent as it
stands, and there is no per-depth recalibration to do.

---

## 1. Does λ give you angular receptive fields? Partly.

Your question is the right one. Taken alone, λ is not enough — but not for the
reason of depth.

A pixel `p` under lenslet (i, j) determines **two points**:

```
    on the MLA plane          :  c[i,j]          (the lenslet centre)
    on the microlens object   :  m  =  c[i,j] − λ · ( p − c[i,j] )
    plane Π, at distance a
```

Two points define a **ray**, not a point. So `{lattice, λ}` already gives every
pixel a ray in the space between the main objective and the MLA — position and
direction, no depth assumption anywhere. That is the angular receptive field,
expressed inside the camera.

The reason λ is not sufficient is the *other* end: to express that ray **in the
real world** you must refract it through the main objective. That needs the
objective's effective focal length, principal-plane (or exit-pupil) position,
and distortion. Those are 4–8 more parameters, all global.

```
    pixel ──► (m, c[i,j]) ──► ray in MLA space ──► main objective ──► ray in object space
              λ + lattice                          F, pupil, distortion
```

Everything in this chain is depth-independent. Calibrate once, use at any
distance.

### Should a(i,j) be a polynomial?

Yes, but low order — and for a different reason than depth.

`a` is fixed *per lenslet*, but not necessarily *equal across lenslets*:

- **MLA tilt.** If the array is not exactly parallel to the sensor, `b` varies
  linearly across it, so λ does too. Two parameters, not 140.
- **Multi-focus arrays.** Raytrix deliberately interleave 3 lenslet types with
  different `f_μ`. Labussière et al. fit one focal length *per type*, not per
  lenslet. If your array is single-focus, this term is absent.
- **Field curvature** of the main objective — enters as a slowly varying term
  in where Π effectively sits.

So the honest model is

```
    λ[i,j]  =  λ0  +  α·i  +  β·j        (+ per-type offsets if multi-focus)
```

Three parameters, physically motivated. Fit 140 free λ values only as a
*diagnostic* (§5); if the spread is structured you have a tilt, and the linear
model absorbs it. If the spread is noise, drop back to a single λ.

---

## 2. What the literature does

Light-field calibration is a small field and the focused-plenoptic corner of it
is smaller. The relevant threads:

**Ray-space intrinsics.** [Dansereau, Pizarro & Williams (CVPR 2013)][dpw] is
the standard for lenslet cameras: a 5×5 homogeneous matrix `H` with 12 non-zero
terms (10 free) mapping pixel index `[i,j,k,l,1]` directly to a ray
`[s,t,u,v,1]`, plus a 5-parameter distortion applied in *ray direction* space.
Explicitly depth-independent, for exactly the reason in §1. RMS ray-reprojection
errors of 0.063–0.363 mm. This is the formulation your requirement points at —
and Donald Dansereau is at USYD, which makes this the cheapest expertise you
can access.

**Focused-plenoptic specifically.** [Johannsen et al. (2013)][joh] fit 15
intrinsics globally (f, focus distance h, MLA-sensor distance b, lateral and
*depth* distortion) plus 6 per target pose, with `b_L = h + v·b` linking virtual
depth to metric depth, and a depth-distortion term for Petzval curvature. They
report 0.36 mm relative accuracy but note absolute distance can be off by up to
20 cm — the thin-lens model's limit.

[Strobl & Lingenauber (CVIU 2016)][sl] make the case for a **stepwise** fit:
lateral parameters (f, k1, k2, distortion centre, poses) from total-focus
brightness images first, then the inner lengths b and h from virtual-depth
images. Their argument is the one that matters for you: fitting everything at
once lets noisy depth data corrupt the well-conditioned lateral parameters.
Notably, they take the MLA grid as **estimated separately and held fixed**.

[Labussière et al. (CVPR 2020)][lab] is the closest to a modern recipe for
multi-focus: (16 + I) intrinsics — main lens F and 5 distortion terms,
principal point, sensor distance, 6-DOF MLA misalignment, and one focal length
per lenslet *type*. Their contribution is the Blur-Aware Plenoptic feature
(centre + blur radius ρ): defocus blur is what makes per-type microlens focal
lengths observable at all, "impossible to retrieve" from a pinhole model.

**Against sub-aperture calibration.** [Bok, Jeon & Kweon (TPAMI 2017)][bok]
argue it directly: sub-aperture images "must be generated *after* geometric
calibration of raw images", which is circular; and micro-lens images are "too
small (10 × 10 pixels for Lytro)" for reliable corner detection. They use
**line** features on the raw image instead, since black/white borders survive in
a tiny micro-image where corners do not.

**That objection is much weaker for you.** At 100 px micro-images you have 100×
the area Lytro has. Corner detection in a 100 px tile is ordinary. Their
circularity objection still applies to *decoded* sub-aperture images, but not to
what you are doing — cropping raw micro-images at a known lattice is not
decoding.

**Pattern-free / neighbour disparity.** [Pattern-free Plenoptic 2.0
Calibration (MMSP 2022)][pf] recovers MLA-sensor and main-lens-MLA distances
from disparity between neighbouring micro-images with no target at all,
σ ≈ 0.03–0.1 mm. Good as an independent cross-check on λ, not as the primary
method.

---

## 3. White image, or relative geometry from neighbours?

You suspected the white image is less accurate. **Correct, and the literature
uses it accordingly — as pre-calibration, not as the answer.**

- Dansereau uses white images to find lenslet centres and correct vignetting,
  taking "the brightest spot in each white lenselet image" as the centre.
- Labussière compute micro-image centres by intensity centroid, then *optimise*
  the grid parameters afterwards.
- Strobl estimates the grid separately and holds it fixed — the opposite
  choice, and the one that gives up accuracy for separability.

The centroid of a defocused disc is limited by the disc edge profile and by
vignetting asymmetry; target corners use the full image content and are
intrinsically better conditioned. So:

**Use the white image for what it is uniquely good at** — deciding which pixels
belong to which lenslet, measuring the true usable disc radius (hence your real
overlap), and giving a lattice initialisation good to ~0.1 px with no target.

**Then let the target data refine the centres inside the global fit.** Your
neighbour-relative idea is exactly right and is what the pattern-free work
exploits: a corner seen in two adjacent micro-images constrains the *difference*
of their centres far better than either centroid constrains its own, because
the shared scene content cancels. Include those constraints; do not replace the
white image with them, because they cannot tell you which pixels belong to
which lenslet in the first place.

---

## 4. The recipe, in one acquisition

**Detection with OpenCV, fitting without it.** `findChessboardCornersSB` +
`cornerSubPix` on each cropped 100 px micro-image is the right tool and will
give ~0.05 px corners. **Do not run `calibrateCamera` per tile.** A 100 px tile
subtends a tiny field angle; focal length, principal point and distortion are
near-degenerate from it, and 140 independent ill-conditioned fits will not
average into a good global answer — they will average into a confident wrong
one. OpenCV supplies correspondences; the model in §1 supplies the constraints.

**One capture session:**

| # | what | count | for |
|---|---|---|---|
| 1 | dark frames, capped | 50 | offset and fixed-pattern noise |
| 2 | flat field | 50 | lattice, vignetting, disc radius, overlap |
| 3 | checkerboard poses | 15–20 × 10 frames | everything else |
| 4 | flat field again | 50 | **invariance check** |

Board: squares at 20–25 px on the sensor → 4×4 to 5×5 corners per tile, i.e.
~40 observations per lenslet per pose. Poses must cover every lenslet including
the corners across the set; tilt matters less than in classical calibration
(no focal length to disentangle from distance), so stay near fronto-parallel
where corners localise best. Include ≥3 distinct depths — not to recalibrate λ,
but to *verify* depth-independence and to constrain the main objective.

Step 4 is the cheapest insurance in the procedure: refit the lattice from the
closing flat field and compare to the opening one. Movement beyond a fraction
of a pixel means the rig shifted mid-session and the poses are mutually
inconsistent. Discard and repeat.

Lock and tape focus and aperture; any change to the objective moves Π and
invalidates λ. `AeEnable: false` (the default in `config/pi.yaml`). Warm up 20
minutes — thermal drift of the MLA-sensor spacing moves `b`, and λ is the
ratio. Both cameras must see the same board placement, with the inter-camera
transform estimated inside the same bundle rather than composed from two
independent solutions.

**Then one joint fit:**

```
    minimise over  { λ0, α, β,  c0, U,  Δc[i,j],  F, pupil, k1, k2,  poses }

        Σ  ρ( ‖ predicted corner − detected corner ‖² )   +   μ·‖Δc‖²
```

with a robust loss ρ (Huber — a few misdetections are certain, and squared loss
gives a 10 px outlier 100× the weight of a good 1 px point) and a ridge term on
the per-lenslet centre offsets. Parameter count ≈ 3 + 6 + 280 + 8 + 6P ≈ 400.
Sparse Levenberg–Marquardt; `scipy.optimize.least_squares(jac_sparsity=…)` is
adequate at this scale.

**Gauge fixing** — `Δc[i,j]` is exactly degenerate with `(c0, U)` unless
constrained:

```
    Σ Δc[i,j] = 0            Σ i·Δc[i,j] = 0            Σ j·Δc[i,j] = 0
```

A constant offset is absorbable into `c0`, a linear trend into `U`. Without
these the fit is not wrong, it is non-unique: two runs on the same data
disagree and neither is identifiably right.

**Follow Strobl's stepwise warning within the single session:** fit the lateral
parameters first from in-focus data, then release the depth-coupled ones. One
acquisition, staged optimisation.

---

## 5. Verification

1. **Cross-view consistency, on held-out poses.** For each corner seen by two
   or more lenslets, map to object-space rays and take the spread of their
   closest approach. This is the deliverable, so it is the metric. Target
   < 0.2 px equivalent; Dansereau's 0.063 mm ray reprojection is the published
   comparison.
2. **Depth-independence.** Register a target at a depth *not* in the fit. Error
   must not grow with distance from the calibration depth. If it does, the ray
   model is not actually depth-independent and something is being absorbed
   wrongly — most likely the objective's pupil position.
3. **Residual map over (i, j).** Should look like noise. A radial pattern
   points at uncorrected objective distortion, a linear ramp at a gauge
   constraint fighting the data.
4. **Free λ[i,j] once**, as a diagnostic (§1). Structured spread = MLA tilt,
   absorbed by the 3-parameter model. Noise = single λ is sufficient, and you
   can say so with evidence.

---

## 6. Order of work

1. Flat field → lattice, disc radius, **actual overlap**. Everything downstream
   is conditional on the periodicity residual being small.
2. Set the UI grid parameters from the fit rather than by eye — same numbers.
3. Corner detection per micro-image, validated against the `synthetic` backend
   with a planted lattice, planted λ and planted Δc. Recovering known ground
   truth is the only way to tell an estimator bug from a rig problem.
4. Global bundle, lateral parameters first.
5. Verification §5.1 and §5.2.
6. Stereo pair.

## Open

- **Single-focus or multi-focus MLA?** Decides whether per-type focal lengths
  (Labussière) are needed.
- **Is defocus blur usable?** If micro-images are ever defocused, the BAP
  feature makes `f_μ` observable. With minimal overlap and in-focus operation it
  may not be, in which case `f_μ` comes from the datasheet.
- **Rotation source** — MLA-to-sensor or sensor-to-optics? Does not affect the
  ray map, but decides `derotate_views` for display. See the note in
  `processing/stages/plenoptic.py`.

---

## References

[dpw]: https://openaccess.thecvf.com/content_cvpr_2013/papers/Dansereau_Decoding_Calibration_and_2013_CVPR_paper.pdf
[joh]: https://www.cg.informatik.uni-siegen.de/sites/www.grk1564.uni-siegen.de/files/inm2013/plenoptic.pdf
[sl]: https://elib.dlr.de/103337/2/strobl16cviu.pdf
[lab]: https://openaccess.thecvf.com/content_CVPR_2020/papers/Labussiere_Blur_Aware_Calibration_of_Multi-Focus_Plenoptic_Camera_CVPR_2020_paper.pdf
[bok]: https://link.springer.com/content/pdf/10.1007/978-3-319-10599-4_4.pdf
[pf]: https://dipot.ulb.ac.be/dspace/bitstream/2013/352469/3/MMSP2022_pattern_free_calibration_postprint.pdf

- Dansereau, Pizarro, Williams — *Decoding, Calibration and Rectification for
  Lenselet-Based Plenoptic Cameras*, CVPR 2013. [PDF][dpw]
- Johannsen, Heinze, Goldluecke, Perwass — *On the Calibration of Focused
  Plenoptic Cameras*, 2013. [PDF][joh]
- Strobl, Lingenauber — *Stepwise Calibration of Focused Plenoptic Cameras*,
  CVIU 2016. [PDF][sl]
- Labussière, Teulière, Bernardin, Ait-Aider — *Blur Aware Calibration of
  Multi-Focus Plenoptic Camera*, CVPR 2020. [PDF][lab]
- Bok, Jeon, Kweon — *Geometric Calibration of Micro-Lens-Based Light Field
  Cameras Using Line Features*, ECCV 2014 / TPAMI 2017. [PDF][bok]
- *Pattern-free Plenoptic 2.0 Camera Calibration*, MMSP 2022. [PDF][pf]
