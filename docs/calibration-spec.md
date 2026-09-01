# Calibration specification

Geometric calibration of the TrilobiteVision plenoptic head.

**Configuration (settled).** Focused plenoptic (2.0): MLA behind a main
objective. ~100 px pitch on a 1456 × 1088 IMX296, mono, ~14 × 10 = 140
micro-images, minimal overlap. Rotation is **MLA-to-sensor only** — the main
objective is square to the sensor. No usable defocus blur. Geometry only.

**Requirement.** Every sensor pixel maps to a ray in object space, valid at any
distance. Registration between views follows from that; it is not a separate
model.

Equations are plain text so they render without a LaTeX extension.

---

## 1. Imaging model and definitions

### 1.1 Frames and symbols

| symbol | units | meaning |
|---|---|---|
| `X = (X,Y,Z)` | mm | point in the **camera frame**: origin at the main lens principal plane, `Z` toward the object, `x`,`y` parallel to the sensor rows/columns |
| `(R_p, t_p)` | — | pose `p` of the checkerboard: `X = R_p·X_board + t_p` |
| `(i,j)` | — | lenslet index, `(0,0)` on the grid origin |
| `p = (u,v)` | px | sensor pixel |
| `ĉ[i,j]` | px | **micro-image centre** — where the ray parallel to the optical axis through lenslet (i,j) lands. This is what is observable, and it is *not* the lenslet's geometric centre (§1.3) |
| `F` | mm | main objective focal length |
| `d_L` | mm | main lens principal plane → MLA |
| `b` | mm | MLA → sensor |
| `f_μ` | mm | lenslet focal length; `1/a + 1/b = 1/f_μ` |
| `s` | mm/px | sensor pixel pitch (3.45 µm for IMX296) |

Note `a` and `b` are both mechanical constants. Neither depends on the scene.

### 1.2 The model, stated

**Each sub-aperture is an ordinary pinhole camera.** Sub-camera (i,j) has

```
    projection centre   C[i,j] = ( κ·ĉ[i,j] ,  D )     transverse mm, axial mm
    principal point     ĉ[i,j]                          px, on the sensor
    focal length        f                                px, SHARED by all tiles
    distortion          k1, k2                           SHARED, main objective only
```

and projects a camera-frame point `X` to

```
    (1)   p[i,j]  =  ĉ[i,j]  +  f · ( X_d − κ·ĉ[i,j] ) / ( Z − D )
```

where `X_d` is `X` after the main objective's distortion (§1.4).

The system is therefore **a planar array of 140 identical pinhole cameras**,
differing only in where their centres sit. The centres lie on one plane at
depth `D` in front of the lens, arranged on a lattice that is a scaled copy of
the micro-image lattice:

```
    (2)   ĉ[i,j] = ĉ0 + U·(i,j)ᵀ + Δĉ[i,j]          U is 2×2: two pitches,
                                                     rotation, skew
          C[i,j] = κ · ĉ[i,j]
```

Three consequences worth stating explicitly:

- **No rotation term appears in (1).** The MLA rotation lives entirely in `U`.
  Because the rotation is MLA-to-sensor and each lenslet is rotationally
  symmetric, the micro-image *content* is not rotated — only the lattice of
  centres is. (This is why `derotate_views` defaults to off.)
- **Depth-independence is structural.** (1) is a projection from a fixed
  centre. Rays through `C[i,j]` are the angular receptive field of that
  sub-aperture, defined for all `Z`. Nothing is calibrated "at a distance".
- **Baselines are not free.** `C[i,j] − C[k,l] = κ·U·((i,j)−(k,l))`. The whole
  array geometry is 6 lattice numbers and one scalar.

### 1.3 Derivation, and why `ĉ` is not the lenslet centre

Chief rays pass undeviated through a lens centre, so for lenslet centre `c` at
axial `d_L`:

```
    main lens:     Z' = F·Z/(Z−F)          axial position of intermediate image
                   x' = −(Z'/Z)·X          its transverse position
    lenslet:       p  = c·(1+β) − β·x'     β = b/(d_L − Z')
```

Substituting and collecting terms in `1/(Z − D)` gives (1) exactly, with

```
    (3)   G   = d_L − F
          f   = b·F/G   / s          (px)
          D   = F·d_L/G              (mm)
          α   = (G + b)/G            micro-image centre scaling
          κ   = −F·s/(G + b)         (mm per px)
          ĉ   = α·c
```

Verified numerically against the exact chain: agreement to 6e-10 mm over 2000
random `(X, Z, c)`.

`α > 1`, so **micro-image centres are radially expanded relative to the lenslet
centres**. This is standard in the literature and it matters here: `ĉ` is what
you measure, so parameterise `ĉ` and never mix the two.

The three fitted scalars invert to physical values:

```
    (4)   f_mm = f · s                            f is in px, so convert first
          F    = (D + κ_px·f_mm) / (1 − κ_px)     κ_px = κ/s, dimensionless
          G    = −F² / (κ_px·(F + f_mm))
          b    = f_mm·G/F           d_L = G + F
```

Note the unit conversion on the first line. `f` is fitted in pixels and `D` is
in millimetres; mixing them here yields a plausible-looking `F` that is wrong
by a factor of the pixel pitch. There is a round-trip test on this.

so the fit is checkable against the datasheet and the mechanical drawing. A
recovered `F` 20 % from nominal means the model or the data is wrong, and that
check costs nothing.

*Worked scale, for intuition:* `F` = 50, `d_L` = 55, `b` = 1.2 mm gives
`f` = 3478 px, `D` = 550 mm, `κ_px` = −8.06. Adjacent virtual centres are then
8.06 × 100 px × 3.45 µm ≈ **2.8 mm apart, on a plane 55 cm in front of the
lens**. That is the camera array this rig is equivalent to.

### 1.4 Distortion

The only distorting element is the main objective, so distortion is applied
**once, about the optical axis**, before the per-tile projection:

```
    (5)   x_n = X/Z ,  y_n = Y/Z ,  r² = x_n² + y_n²
          X_d = Z·x_n·(1 + k1·r² + k2·r⁴)     (same for Y_d)
```

Two radial terms only: minimal overlap means each tile sees a narrow field, so
higher orders and tangential terms are not supported by the data. `Z` is
unchanged — this is a transverse distortion.

*Approximation:* strictly the distortion depends on the ray's height at the
lens, which differs slightly between sub-cameras. The error is second order in
(baseline / object distance) and is below the corner-detection noise here.
§5 verification 3 tests it rather than assuming it.

---

## 2. Calibration parameters

### 2.1 Unknowns

| group | symbol | count | notes |
|---|---|---|---|
| **Global** | | | |
| lattice origin | `ĉ0` | 2 | px |
| lattice matrix | `U` | 4 | pitches, rotation, skew |
| focal length | `f` | 1 | px, shared by all sub-cameras |
| centre-plane depth | `D` | 1 | mm |
| centre scaling | `κ` | 1 | mm/px |
| distortion | `k1, k2` | 2 | main objective |
| | | **11** | |
| **Per lenslet** | | | |
| centre offset | `Δĉ[i,j]` | 2N = **280** | manufacture + mount error |
| **Per pose** | | | |
| board pose | `R_p, t_p` | 6P = **~90** | P ≈ 15, shared by both cameras |
| **Stereo** | | | |
| relative pose | `R_LR, t_LR` | 6 | §2.4 |
| second head | its own global + `Δĉ` | 291 | the right camera repeats §2.1 |
| | **total, pair** | **≈ 678** | against ~80 000 observations |

`f_μ` does not appear. Without usable defocus blur it is not observable, and
the model does not need it: it is absorbed into `f`, `D` and `κ` through (3).

### 2.2 Deliberately absent

- **No per-lenslet focal length or distortion.** One moulded array; the
  micro-images are too narrow-field to constrain them separately. §5.4 tests
  this rather than assuming it.
- **No rotation in the projection** (§1.2).
- **No depth-dependent term.** `a`, `b` are mechanical. Object depth changes the
  *disparity between* micro-images, which is the measurement the camera makes,
  not a parameter.

### 2.3 Optional extension, only if §5.4 demands it

MLA tilt makes `b`, and therefore `f` and `κ`, vary linearly across the array:

```
    (6)   f[i,j] = f0·(1 + a_i·i + a_j·j)
```

Two extra parameters, physically motivated. Add only on evidence.

### 2.4 The stereo pair

Both cameras see the same board in each pose, so the pair adds a **relative
pose** `(R_LR, t_LR)` and the board pose is shared rather than duplicated:

```
    independent calibration :  6P + 6P            = 180 params at P = 15
    joint calibration       :  6P + 6             =  96 params
```

Fewer parameters *and* better conditioned, because every corner in both cameras
constrains one shared pose.

**On the 2-parameter proposal.** Two angles describe the relative *pointing*,
and that is the number to report. But the model needs six, and the two you
would be dropping are not free:

*Roll* is the third rotation, and it is not small in effect. Displacement at
the field corner (r = 728 px) for a relative roll between the two heads:

| roll | displacement at corner |
|---|---|
| 0.2° | 2.5 px |
| 0.5° | 6.4 px |
| 1.0° | 12.7 px |
| 2.0° | 25.4 px |

Mounting tolerance alone will give a few tenths of a degree. At a 0.2 px
registration target, roll must be estimated, and it costs one parameter.

*Translation* is the part your reasoning does dispose of — but only past a
range worth stating. With a 60 mm baseline and f = 3478 px, the disparity
`f·B/Z` is:

| range | disparity |
|---|---|
| 1 m | 209 px |
| 5 m | 42 px |
| 20 m | 10.4 px |
| 100 m | 2.1 px |
| 400 m | 0.5 px |

So "looking at infinity" means **beyond roughly 400 m** for translation to be
ignorable at the sub-pixel level. Below that it dominates roll.

The practical resolution: **estimate all six.** During calibration the board
sits at 0.3–1 m, where translation is strongly observable, so you get it for
free and well-conditioned — there is no reason to fix what the data determines.
Then at operating range, compute `f·B/Z` and either use `t_LR` or drop it
knowingly. Report the three angles as the alignment result; keep the
translation in the model.

If the heads are ever rigidly co-boresighted such that `t_LR` genuinely cannot
be resolved, regularise it toward the mechanical drawing rather than removing
it — a soft prior degrades gracefully, a hard constraint biases the rotation.

### 2.5 Gauge constraints

`Δĉ` is exactly degenerate with `(ĉ0, U)`: a constant offset is absorbable into
`ĉ0`, a linear trend into `U`. Constrain

```
    (7)   Σ Δĉ[i,j] = 0      Σ i·Δĉ[i,j] = 0      Σ j·Δĉ[i,j] = 0
```

Without these the fit is not wrong, it is **non-unique**: two runs on the same
data give different answers and neither is identifiably right.

**Identifiability requirement.** `κ` and `D` enter (1) only through
`κ/(Z − D)`. At a single object depth they are one number. **Poses must span at
least three well-separated depths** or the array geometry is not recoverable —
this is a hard requirement on the capture session, not a refinement.

---

## 3. Measurements

### 3.1 What is measured

For pose `p`, tile `(i,j)`, board corner `n`:

```
    z[p,i,j,n]  =  detected corner position, sensor pixels
```

That is the only primary measurement. Everything else is derived.

### 3.2 How

Per cropped micro-image, using OpenCV:

1. `findChessboardCornersSB` (the 2019 detector — more robust on partial views
   and small patches than the classic one).
2. `cornerSubPix` refinement → σ ≈ 0.05–0.1 px at 20 px squares.
3. Corner identity from the board layout. With a partial view, resolve identity
   by predicting each tile's field from the current parameter estimate; the
   grid model tells you approximately where the tile is looking, which is
   enough to disambiguate. A ChArUco board removes the ambiguity outright and
   is worth using if partial views prove troublesome.

### 3.3 Derived per-tile quantities, for initialisation only

With a provisional shared `f`, `solvePnP` per tile gives a per-tile board pose.
Two uses:

- **Initialisation of `κ`.** Neighbouring tiles viewing the same board differ
  by a pure translation `κ·U·Δ(i,j)`. Differencing their PnP translations
  measures the baseline directly — this is your "relative centres compared to
  neighbours", and it works because the shared board content cancels.
- **Outlier rejection.** A tile whose PnP pose disagrees with its neighbours by
  more than a few times the noise has a misdetection or a mis-identified
  corner. Drop it before the global fit.

### 3.4 Why not `calibrateCamera` per tile

It will return numbers, and they will be wrong with high confidence.

A 100 px tile at `f` ≈ 3500 px subtends about **1.6°**. Over that field:

- `f` and the pose distance are near-degenerate — perspective change across the
  patch is negligible, so scaling the board and moving it produce almost the
  same image.
- Radial distortion is unobservable: `r²` varies by ~0.0002 across the tile.
- The principal point is degenerate with the tile's lateral position.

140 ill-conditioned fits do not average into a good global answer; they average
into a confident wrong one. **Use OpenCV for detection and PnP. Use the model
in §1 for the parameters.**

### 3.5 Observation count

At 20 px squares, ~16 corners per tile. Assume 60 % of tiles see the board in a
given pose:

```
    140 × 0.6 × 16 × 2 coords            ≈ 2 700 observations per pose
    × 15 poses                           ≈ 40 000
    against ~381 unknowns                ≈ 100× over-constrained
```

Per lenslet: ~16 × 15 × 0.6 ≈ 145 observations against 2 unknowns.

---

## 4. The fitting problem

### 4.1 Residual

For each observation, the residual is the difference between the detected
corner and its prediction under (1), (2) and (5):

```
    r[p,i,j,n]  =  z[p,i,j,n]  −  π( X_board[n] ; R_p, t_p, ĉ0, U, Δĉ[i,j],
                                     f, D, κ, k1, k2 )
```

### 4.2 Objective

```
    minimise over all unknowns

        Σ  ρ( ‖r[p,i,j,n]‖² / σ² )   +   μ · Σ ‖Δĉ[i,j]‖²

    subject to the three gauge constraints (7)
```

- **ρ = Huber.** A few misdetections are certain; under squared loss a 10 px
  outlier carries 100× the weight of a good 1 px point.
- **σ ≈ 0.07 px**, from repeat frames. Makes the residual dimensionless so μ
  has a scale.
- **μ**, the ridge on per-lenslet offsets, chosen by cross-validation on
  held-out poses — not on training residual. Edge lenslets see the board in
  fewer poses and will otherwise absorb noise.

### 4.3 Structure and solver

The Jacobian is block-sparse: a pose couples only to the tiles that saw it, a
`Δĉ` only to its own tile's observations. This is a bundle adjustment; do not
form the dense Jacobian (≈ 40 000 × 381, almost all zeros).

`scipy.optimize.least_squares(method='trf', loss='huber', jac_sparsity=S)` is
adequate at this scale. Enforce (7) by projecting `Δĉ` onto the orthogonal
complement of span{1, i, j} after each iteration.

### 4.4 Staged fit

One acquisition, staged optimisation — fitting everything at once lets the
weakly-constrained parameters pull the strong ones around:

| stage | free | fixed | from |
|---|---|---|---|
| 0 | — | — | `ĉ0, U` from the UI grid parameters; `f, D, κ` from (3) and nominal `F, d_L, b`; poses from per-tile PnP (§3.3) |
| 1 | `ĉ0, U`, poses | rest | lattice against real data |
| 2 | + `f, D, κ` | `Δĉ = 0` | array geometry; **needs the multi-depth poses** |
| 3 | + `k1, k2` | | distortion |
| 4 | + `Δĉ` with (7) and μ | | per-lenslet residual |

Stop when held-out residual stops improving. Report which stage was reached.

---

## 5. Capture session

Checkerboard poses only. One session, one operator, no flat field required —
the lattice is initialised from the alignment parameters already set in the UI
and refined in stage 1.

### 5.1 Before

- Lock and tape focus and aperture. Any change moves `d_L` or `F` and voids
  the calibration.
- `AeEnable: false`, fixed exposure and gain (default in `config/pi.yaml`).
- 20 minutes powered before the first frame: thermal drift of the MLA-sensor
  spacing moves `b`, and `f`, `D`, `κ` all depend on it.
- Record the board's square size to ±0.1 %. This is the only thing setting
  absolute scale, so `D` and `κ` inherit its error directly.

### 5.2 Board

| property | value | why |
|---|---|---|
| square size on sensor | 20–25 px | ~16 corners per tile; below ~10 px corner localisation degrades |
| pattern | checkerboard, or ChArUco | ChArUco if partial-view corner identity proves troublesome (§3.2) |
| flatness | < 0.1 mm over the used area | board non-flatness maps straight into `Δĉ` |
| mounting | rigid, matte | glass or aluminium composite, not paper on foam |

### 5.3 Poses

| set | count | requirement |
|---|---|---|
| working depth | 8–10 | board reaching all parts of the sensor across the set, corners included |
| depth 2 | 3–4 | ≥ 1.5× working depth |
| depth 3 | 3–4 | ≤ 0.7× working depth |

**≈ 15 poses, spanning ≥ 3 depths.** The depth spread is what makes `κ` and `D`
separable (§2.5) — without it the fit is under-determined and will still
converge, to a wrong answer.

Coverage matters more than pose count: a lenslet that never sees the board has
its `Δĉ` set entirely by the gauge constraint. Tilt matters less than in
classical calibration (no focal length to disentangle from distance), so stay
near fronto-parallel where corners localise best; ±15° is enough.

10 frames averaged per pose. Cheap, and it improves corner localisation by ~√10.

### 5.4 Both cameras

Both see the same board placement in each pose, and the inter-camera transform
is estimated inside the same fit — 6 more parameters. Composing two independent
calibrations pushes each camera's pose error into the baseline, which is the
quantity the stereo pair exists to measure.

`capture-all` is sequential and the sensors free-run, so the two frames are tens
of ms apart. Harmless for a static board. Do not extend this to a moving target
without wiring the IMX296 XVS pins together.

### 5.5 Session log

Written automatically into the session sidecar: board square size, pose index
and nominal depth, exposure, gain, frames averaged, and the UI grid parameters
in force. A calibration whose acquisition conditions are not recorded cannot be
re-fitted later with a better model.

---

## 6. Verification

Run on **held-out poses**, never on the fitted set.

1. **Cross-view consistency.** For each corner seen by ≥ 2 tiles, back-project
   to rays through the respective `C[i,j]` and measure their closest approach.
   This is the deliverable. Target < 0.2 px equivalent.
2. **Depth extrapolation.** Register a target at a depth not in the fit. Error
   must not grow with distance from the calibration depths. If it does, the ray
   model is absorbing something wrongly — most likely `D`.
3. **Physical plausibility.** Invert (4) and compare `F`, `d_L`, `b` with the
   datasheet and the drawing. Also checks the §1.4 distortion approximation:
   if it is failing, `F` comes out biased.
4. **Free `f[i,j]` once, as a diagnostic.** Structured spread across the array
   ⇒ MLA tilt ⇒ adopt (6), two parameters. Noise ⇒ a single `f` is justified
   and you can say so with evidence rather than assumption.
5. **Residual map over (i,j).** Should look like noise. A radial pattern means
   uncorrected distortion; a linear ramp means a gauge constraint fighting the
   data.

---

## 7. Next

With this settled, the two remaining pieces are:

- **UI** — capture-session workflow: pose counter, per-pose coverage map,
  live corner-detection feedback per tile, accept/reject, session log.
- **Algorithm** — corner detection and identity, PnP initialisation, the staged
  bundle, and the validation report.

Both should be built against the `synthetic` backend first, rendering a known
lattice with planted `κ`, `D`, `f` and `Δĉ`. Recovering planted ground truth is
the only way to separate an estimator bug from a rig problem.
