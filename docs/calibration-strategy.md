# Sub-aperture calibration strategy

A proposal to consider before any code is written. Mathematics first, then the
bench procedure, then what to check.

---

## 0. The problem, stated honestly

The obvious plan is: treat each sub-aperture as a small camera, point a
checkerboard at it, run the usual calibration, get a polynomial per
sub-aperture. **That plan does not survive contact with the numbers**, and the
reason is worth working through before choosing anything else.

A standard pinhole-plus-distortion model per sub-aperture carries

| group | parameters | count |
|---|---|---|
| intrinsics | $f_x, f_y, c_x, c_y$ | 4 |
| radial distortion | $k_1, k_2, k_3$ | 3 |
| tangential distortion | $p_1, p_2$ | 2 |
| pose in the rig frame | $\mathbf{R}, \mathbf{t}$ | 6 |
| | | **15** |

On the IMX296 the sensor is $1456 \times 1088$. Write $p$ for the lenslet pitch
in pixels and $N$ for the number of lenslets that fit:

$$N \;\approx\; \left\lfloor \frac{1456}{p} \right\rfloor \left\lfloor \frac{1088}{p} \right\rfloor$$

| pitch $p$ | tile size | $N$ | free parameters if each is independent |
|---|---|---|---|
| 20 px | 20×20 | ~3900 | 58 500 |
| 40 px | 40×40 | ~972 | 14 580 |
| 100 px | 100×100 | ~140 | 2 100 |

Now count the *observations*. A checkerboard corner is detectable only if the
square is several pixels across; with squares at, say, 8 px you fit two or
three squares across a 20 px tile, giving **1 to 4 corners per tile per pose**.
Each corner is 2 scalar observations. So one pose contributes at most ~8
numbers per sub-aperture against 15 unknowns. Even with 30 poses the system is
poorly conditioned, and the distortion coefficients — which need corners near
the tile edge, at large field angles — are essentially unobservable.

**The conclusion that shapes everything below: you cannot calibrate the
sub-apertures independently. You calibrate the *array*, using a model in which
almost all parameters are shared, and then allow a small, regularised
per-lenslet correction.** This is not a shortcut taken for speed; it is the
only formulation that is identifiable.

---

## 1. Notation

Fix these once.

- $\mathbf{X} \in \mathbb{R}^3$ — a point on the calibration target, in the
  *target frame*. For a planar target, $\mathbf{X} = (X, Y, 0)^\top$; the third
  coordinate being identically zero is what makes planar-target calibration
  work at all, and it is used explicitly in the initialisation below.
- $p = 1 \dots P$ indexes **target poses** (one physical placement of the
  target). Pose $p$ has rotation $\mathbf{R}_p \in SO(3)$ and translation
  $\mathbf{t}_p \in \mathbb{R}^3$ carrying target-frame points into the *rig
  frame*.
- $(i,j) \in \mathbb{Z}^2$ indexes **lenslets**, with $(0,0)$ the one on the
  grid origin — the same indexing the software already uses.
- $\mathbf{C}_{ij} \in \mathbb{R}^3$ — the centre of lenslet $(i,j)$ in the rig
  frame. Physically: where that little camera's entrance pupil sits.
- $\mathbf{u}, \mathbf{v} \in \mathbb{R}^3$ — the two lattice basis vectors of
  the microlens array, expressed in the rig frame. Physically: the step from
  one lenslet to its neighbour, in millimetres, in two directions. Their
  lengths are the physical pitch; the angle between them is the array's
  skew; together they span the array plane.
- $\boldsymbol{\theta}$ — the shared intrinsic and distortion parameters.
- $\Delta \mathbf{c}_{ij} \in \mathbb{R}^2$ — the per-lenslet principal-point
  correction, in pixels. Physically: how far this individual lenslet's optical
  axis lands from where the perfect lattice says it should. This is the term
  that absorbs manufacturing error.

Choose the **rig frame to be the array frame**: origin at $\mathbf{C}_{00}$,
$z$ along the array normal. This is a gauge choice and it matters — without it
the whole array can rotate and translate while every target pose
counter-rotates, leaving the residual unchanged, and the optimiser will drift
along that null direction indefinitely.

---

## 2. The forward model

Build it in five steps. Each is a physical statement, not an algebraic one.

**Step 1 — target point into the rig frame.** Standard rigid motion:

$$\mathbf{X}^{(r)} = \mathbf{R}_p \mathbf{X} + \mathbf{t}_p$$

**Step 2 — where lenslet $(i,j)$ is.** The array is rigid and periodic, so its
centres are a 2D lattice embedded in 3-space:

$$\mathbf{C}_{ij} = i\,\mathbf{u} + j\,\mathbf{v}$$

This is the single biggest reduction in the problem: $3N$ unknown positions
collapse to the 6 numbers in $(\mathbf{u}, \mathbf{v})$. It is justified
because the array is one moulded or lithographic part; departures from
periodicity are microns, and they get absorbed by $\Delta \mathbf{c}_{ij}$
later.

**Step 3 — which way lenslet $(i,j)$ looks.** Two architectures, and you must
pick the one that matches your optics:

*Apposition / camera-array* (no shared objective; each lenslet images the scene
directly). All optical axes are parallel to the array normal:

$$\mathbf{R}_{ij} = \mathbf{I}$$

*Focused plenoptic (Plenoptic 2.0)* (an MLA behind a main objective, imaging
the intermediate image). Each lenslet's chief ray points at the centre of the
main lens exit pupil $\mathbf{P} = (0,0,-d)^\top$, so the axis direction is
fixed by geometry with **one** free scalar, the pupil distance $d$:

$$\mathbf{a}_{ij} = \frac{\mathbf{P} - \mathbf{C}_{ij}}{\lVert \mathbf{P} - \mathbf{C}_{ij}\rVert},
\qquad \mathbf{R}_{ij} = \mathrm{Rot}\!\left(\mathbf{e}_z \to \mathbf{a}_{ij}\right)$$

where $\mathrm{Rot}(\mathbf{a} \to \mathbf{b})$ is the minimal rotation taking
$\mathbf{a}$ onto $\mathbf{b}$. Either way, $3N$ rotational unknowns collapse to
**0 or 1**. This is an assumption with teeth — see §5 for how to test it rather
than trust it.

**Step 4 — project through the lenslet.** Move into the lenslet's local frame
and perspective-divide:

$$\mathbf{Y} = \mathbf{R}_{ij}^\top\left(\mathbf{X}^{(r)} - \mathbf{C}_{ij}\right),
\qquad \mathbf{x} = \left(\frac{Y_1}{Y_3},\; \frac{Y_2}{Y_3}\right)$$

$\mathbf{x}$ is the normalised image coordinate: where the ray lands on a plane
one focal length in front, in units of that focal length.

Apply the **shared** distortion. With $r^2 = x_1^2 + x_2^2$:

$$\mathbf{x}' = \underbrace{\left(1 + k_1 r^2 + k_2 r^4 + k_3 r^6\right)}_{\text{radial}} \mathbf{x}
\;+\; \underbrace{\begin{pmatrix} 2p_1 x_1 x_2 + p_2\left(r^2 + 2x_1^2\right) \\[2pt]
p_1\left(r^2 + 2x_2^2\right) + 2 p_2 x_1 x_2 \end{pmatrix}}_{\text{tangential}}$$

This is the Brown–Conrady model, the same one OpenCV uses. It is shared across
all lenslets because they are replicas of one another — this is the second
large reduction, $9N \to 9$.

**Step 5 — to pixels.** The lenslet's own principal point sits at its lattice
centre projected onto the sensor, plus its individual error:

$$\mathbf{p}_{ij} = f \,\mathbf{x}' \;+\; \underbrace{\boldsymbol{\pi}(\mathbf{C}_{ij})}_{\text{lattice, in px}} \;+\; \Delta \mathbf{c}_{ij}$$

$\boldsymbol{\pi}$ converts a rig-frame array position to sensor pixels — for a
coplanar array parallel to the sensor this is a scale and an offset, which is
exactly the `MLAGeometry` grid the UI already draws. **The sliders you tune by
eye are a coarse estimate of $\boldsymbol{\pi}$.**

### Parameter count after all this

| group | count |
|---|---|
| shared intrinsics $f$, distortion $k_1,k_2,k_3,p_1,p_2$ | 6 |
| lattice $\mathbf{u}, \mathbf{v}$ | 6 |
| pupil distance $d$ (focused plenoptic only) | 0 or 1 |
| target poses, 6 each | $6P$ |
| per-lenslet corrections $\Delta \mathbf{c}_{ij}$ | $2N$ |

For $P = 20$ and $N = 972$: $6 + 6 + 1 + 120 + 1944 = 2077$, against
$14\,580$ for the independent-camera formulation, and — more to the point —
every one of these is actually observable.

---

## 3. The two things that make this tractable

### 3.1 Fix the lattice from a white image, not from the checkerboard

Before any checkerboard, capture a **uniformly illuminated flat field**:
integrating sphere, or a diffuser sheet lit from behind, filling the field.
Each lenslet then projects a bright disc onto the sensor and the *centroid of
that disc is the lenslet centre*, recoverable to a small fraction of a pixel by
intensity-weighted centroiding:

$$\hat{\mathbf{c}}_{ij} = \frac{\sum_{\mathbf{q} \in W_{ij}} I(\mathbf{q})\, \mathbf{q}}{\sum_{\mathbf{q} \in W_{ij}} I(\mathbf{q})}$$

over a window $W_{ij}$ around the nominal centre. Then fit the lattice by
linear least squares over all lenslets:

$$\min_{\mathbf{c}_0, \mathbf{U}} \sum_{ij} \left\lVert \hat{\mathbf{c}}_{ij} - \left(\mathbf{c}_0 + \mathbf{U}\begin{pmatrix} i \\ j\end{pmatrix}\right) \right\rVert^2$$

with $\mathbf{U} \in \mathbb{R}^{2\times2}$ the sensor-plane lattice matrix
(four numbers: two pitches, rotation, skew — strictly more general than the
UI's pitch-plus-rotation, and worth the extra two parameters).

Three reasons this belongs first:

1. It is **one capture** and yields the geometry to sub-pixel accuracy, where
   eyeballing the overlay gets you maybe half a pixel on a good day.
2. The residuals $\hat{\mathbf{c}}_{ij} - (\mathbf{c}_0 + \mathbf{U}(i,j)^\top)$
   *are* an estimate of $\Delta \mathbf{c}_{ij}$, obtained without any target.
   That is a free initialisation for 1944 parameters.
3. It gives an honest measurement of how good the periodicity assumption is. If
   those residuals are 0.1 px, Step 2 is safe. If they are 3 px with structure,
   the array is not what you think it is and no amount of bundle adjustment
   will rescue the fit.

The same white image gives the per-lenslet vignetting profile, which you need
anyway for radiometric work, and the lenslet radius, which sets the usable
crop.

### 3.2 Choose the target by tile size, not by habit

This is where I would push back on "conventional checkerboard". A checkerboard
gives you **corners**: a handful of point correspondences per tile. A
**phase-shifted sinusoid displayed on an LCD** gives you a correspondence at
*every pixel*.

Project horizontal and vertical sinusoids at several spatial frequencies,
shifting each by $2\pi/M$ steps ($M = 4$ typical). For pixel $\mathbf{q}$ the
intensity over the shift index $m$ is

$$I_m(\mathbf{q}) = A(\mathbf{q}) + B(\mathbf{q}) \cos\!\left(\phi(\mathbf{q}) + \tfrac{2\pi m}{M}\right)$$

and the phase follows in closed form from the standard $M$-step estimator

$$\phi(\mathbf{q}) = \operatorname{atan2}\!\left(-\sum_m I_m \sin \tfrac{2\pi m}{M},\;\; \sum_m I_m \cos \tfrac{2\pi m}{M}\right)$$

Unwrap across frequencies (or add a Gray-code sequence for absolute phase) and
every sensor pixel now carries an unambiguous *screen coordinate*. The
comparison that matters:

| | checkerboard | phase-shift on a monitor |
|---|---|---|
| correspondences per 24×24 tile per pose | 1–4 | ~500 |
| correspondence identity | must be solved | unambiguous by construction |
| works below ~30 px tiles | no | yes |
| poses needed | 25–40 | 6–10 |
| extra unknowns | none | screen plane pose, pixel pitch, cover-glass offset |
| equipment | printed board on glass | a monitor you already own |

The phase-shift route also removes the single most annoying failure in this
problem: deciding *which* physical checkerboard corner a given blob in a 24 px
tile corresponds to, when adjacent lenslets see overlapping and nearly
identical scene patches.

**Decision rule, by tile size:**

- **tile ≥ 80 px** — conventional per-tile checkerboard is fine; you can even
  run OpenCV per tile for initialisation. Use it.
- **tile 30–80 px** — checkerboard works, but only in the global-bundle
  formulation of §2, with 25+ poses and a coarse board (large squares).
- **tile < 30 px** — use the monitor. Checkerboards will waste weeks.

Note the caveats on the monitor route, because they are real: the LCD's pixel
pitch must be known or fitted; the cover glass puts the emitting layer ~1 mm
behind the front surface, which biases depth unless modelled or calibrated
out; and the screen must be flat, which most are to well under 1 mm but not
all.

---

## 4. The estimator

### Objective

$$\min_{\boldsymbol{\theta},\, \mathbf{u},\mathbf{v},\, d,\, \{\mathbf{R}_p,\mathbf{t}_p\},\, \{\Delta\mathbf{c}_{ij}\}}
\;\; \sum_{p}\sum_{ij}\sum_{n \in \mathcal{O}_{p,ij}}
\rho\!\left( \left\lVert \mathbf{p}^{\text{obs}}_{p,ij,n} - \mathbf{p}_{ij}\!\left(\mathbf{X}_n; \cdot\right) \right\rVert^2 \right)
\;+\; \lambda \sum_{ij} \lVert \Delta \mathbf{c}_{ij} \rVert^2$$

Three pieces to justify:

**$\rho$, a robust loss.** Use Huber or Cauchy, not plain squares. A handful of
misdetected corners is certain, and least squares gives an outlier at 10 px
error one hundred times the influence of a good point at 1 px. Squared loss
here does not converge to something wrong slowly; it converges to something
wrong immediately.

**$\lambda$, the regulariser on the per-lenslet corrections.** Edge lenslets see
the target in fewer poses and have less data; without a prior pulling
$\Delta\mathbf{c}$ toward zero they will absorb noise and produce a distortion
map with garbage at the rim. Set $\lambda$ by cross-validation over held-out
poses — sweep it logarithmically and take the minimum of held-out reprojection
error, not training error. Physically, $\lambda$ encodes "I believe this array
was manufactured well"; its scale should correspond to the white-image
residual spread from §3.1.

**Gauge fixing.** There are exact degeneracies and the optimiser will find them:

1. A **constant** shift in all $\Delta \mathbf{c}_{ij}$ is indistinguishable
   from moving $\mathbf{c}_0$. Constrain $\sum_{ij} \Delta \mathbf{c}_{ij} = 0$.
2. A **linear trend** in $\Delta \mathbf{c}_{ij}$ across $(i,j)$ is
   indistinguishable from changing the lattice matrix $\mathbf{U}$. Constrain
   $\sum_{ij} i\,\Delta \mathbf{c}_{ij} = \sum_{ij} j\,\Delta \mathbf{c}_{ij} = \mathbf{0}$.
3. A **rigid motion** of the whole rig is absorbable into every target pose.
   Fixed by the gauge choice in §1 (rig frame ≡ array frame).

Impose 1 and 2 by projection at each iteration or as soft penalties. Skipping
them does not produce a wrong answer so much as a *non-unique* one, which is
worse: two calibration runs on the same data will disagree and you will not
know which to trust.

### Solving it

This is a bundle adjustment and it has bundle adjustment's structure: the
Jacobian is block-sparse, because a target pose couples only to the lenslets
that saw it and a lenslet correction couples only to its own observations. Do
not form the dense Jacobian — for $P=20$, $N=972$ it is roughly
$10^5 \times 2000$ and mostly zeros.

- Levenberg–Marquardt with an explicit sparsity pattern:
  `scipy.optimize.least_squares(..., jac_sparsity=S, method='trf')` is
  sufficient at this scale and needs no extra dependency.
- If it becomes slow, the standard move is the **Schur complement**:
  eliminate the $6P$ pose parameters analytically (they are the "point" block
  in ordinary BA), leaving a small dense system in the shared parameters. That
  is a later optimisation, not a starting requirement.

### Initialisation

Bundle adjustment is a local method; the initial guess decides whether it works.

1. $\mathbf{U}, \mathbf{c}_0, \Delta\mathbf{c}$ — from the white image (§3.1).
2. $f$ — from the lenslet specification, $f = f_{\text{lenslet}} / s_{\text{px}}$
   with $s_{\text{px}} = 3.45\ \mu\text{m}$ for the IMX296. Do not fit it blind;
   you know it to a few percent.
3. Distortion — start at zero. A small lenslet at low field angle has little.
4. Target poses — treat the **full sensor** as one conventional camera with the
   nominal main-lens intrinsics, run ordinary planar-target calibration
   (Zhang's method, which is what OpenCV `calibrateCamera` implements) to get
   $(\mathbf{R}_p, \mathbf{t}_p)$ per pose. These are rough but they are in the
   right basin, which is all that is required.

---

## 5. Bench procedure

### Before anything

Mechanical stability dominates everything. A calibration is valid only for the
exact optical configuration that produced it.

- Lock focus and aperture. Tape them. Note the settings.
- Lock every adjustment on the MLA mount and record its state.
- Fix exposure and gain; `AeEnable: false` (already the default in
  `config/pi.yaml`). Auto-exposure between poses makes frames incomparable and
  silently corrupts the intensity-weighted centroids.
- Let the camera reach thermal equilibrium — 20 minutes powered. Sensor and
  mount both move as they warm, and the drift is comparable to the accuracy
  you are chasing.

### Capture sequence

| # | what | count | why |
|---|---|---|---|
| 1 | dark frames, lens capped | 50 | fixed-pattern noise and offset; subtract from everything |
| 2 | flat field, uniform illumination | 50 | lenslet centres, vignetting, lenslet radius |
| 3 | target poses | $P$ × 10 frames | the calibration data; averaging 10 beats the small-tile SNR problem |
| 4 | flat field again | 50 | **the invariance check** — see below |

Average the repeats before fitting. The tiles are small and the corner or phase
estimates are noise-limited; averaging 10 frames buys a factor of ~3 in
precision for a few seconds of acquisition.

**Step 4 is not optional and is the step most people skip.** Refit the lattice
from the closing flat field and compare to the opening one. If the centres have
moved by more than a fraction of a pixel, something shifted during the session
— thermal, mechanical, a knock — and the pose data is inconsistent with itself.
Discard the session. Finding this out from a 5-second check is far cheaper than
finding it out from residuals you cannot explain a week later.

### Pose diversity — what actually needs to vary

Poor pose diversity is the most common cause of a calibration that fits well
and generalises badly. The specific requirements:

- **Tilt.** At least $\pm 30°$ about both in-plane axes. Fronto-parallel poses
  make $f$ and target distance nearly degenerate: moving the board closer and
  shortening the focal length produce almost the same image. Tilt is what
  breaks that.
- **Coverage.** Across the pose set, the target must reach every part of the
  sensor, including the corners. Edge lenslets have the largest field angles
  and carry most of the distortion information; if the board never reaches
  them, their $\Delta \mathbf{c}$ is fitted to nothing and the regulariser will
  quietly hold it at zero.
- **Depth.** Three or more distinct distances spanning the working range. This
  is what separates the pupil distance $d$ from $f$ in the focused-plenoptic
  model — they are strongly correlated at a single depth.
- **Roll.** Rotate the board in its own plane between poses. This decorrelates
  the tangential distortion terms from a systematic corner-detection bias.

$P = 25$–40 for the checkerboard route, $P = 6$–10 for phase-shift.

### Both cameras at once

For the stereo pair: capture with **both cameras seeing the same target
placement**, and estimate the left–right relative pose $(\mathbf{R}_{LR},
\mathbf{t}_{LR})$ *inside the same bundle*, as 6 additional parameters, rather
than calibrating each camera separately and composing the results afterwards.
Composing independent solutions accumulates each camera's pose error into the
baseline, and the baseline is the quantity the whole stereo rig exists to
exploit.

Note again the caveat already in the code: `capture-all` is sequential and the
sensors free-run, so the two frames are tens of milliseconds apart. For a
static target that is harmless. Do not extend this procedure to a moving target
without first wiring the IMX296 XVS sync pins together.

---

## 6. Validating the result

A reprojection error on the data you fitted is not evidence. Four checks, in
increasing order of how much they will tell you:

**1. Held-out poses.** Fit on 80% of poses, report RMS reprojection on the
remaining 20%. Expect 0.1–0.3 px for a well-behaved rig. Above ~0.5 px,
something is wrong with the model or the detection, and tuning $\lambda$ will
not fix it.

**2. Residual maps, per lenslet.** Plot mean residual as a vector field over
$(i,j)$. It should look like noise. If you see structure — a radial pattern, a
systematic tilt, a discontinuity at a tile boundary — the shared model is too
rigid and is pushing error into a spatial pattern. A radial pattern in
particular says the distortion model needs another term or that the "identical
lenslets" assumption is failing toward the array edge.

**3. Test the architecture assumption of Step 3.** Free the per-lenslet
rotations for a *small subset* of lenslets (say 20, spread over the array) and
refit. If those rotations come out near what the parallel-axis or pupil-pointing
model predicts, the assumption holds. If they diverge systematically, the model
is wrong and everything downstream inherits that. This costs one extra fit and
is the difference between an assumption and a measurement.

**4. An independent geometric check.** Image an object of known size at a known
distance, reconstruct it, compare. Calibration residuals measure
self-consistency; only an external standard measures accuracy. A rig can have
0.1 px reprojection error and a 3% scale error.

---

## 7. Suggested order of work

1. Flat-field capture and lattice fit. Delivers the geometry, the vignetting,
   and an honest measurement of array periodicity. **Do this before deciding
   anything else** — the periodicity residual tells you whether the model in §2
   is even appropriate.
2. From the measured tile size, choose the target per §3.2.
3. Build the corner or phase extractor, and validate it on synthetic data with
   known ground truth before pointing it at the rig. The `synthetic` camera
   backend already in the codebase is the right place for this: you can render
   a known lattice with known distortion and check that the estimator recovers
   it. Debugging an estimator against real data with unknown truth is the
   slowest possible way to work.
4. Global bundle with $\Delta \mathbf{c}$ fixed at the white-image values.
5. Release $\Delta \mathbf{c}$ with the gauge constraints and the regulariser;
   choose $\lambda$ by cross-validation.
6. Validate per §6.
7. Only then extend to the stereo pair.

## Open questions for you

1. **Which architecture?** §2 Step 3 branches on it, and the branch changes what
   is identifiable. Apposition array, or MLA behind a main objective?
2. **What pitch are you targeting?** It sets the tile size, which selects the
   target type in §3.2, which determines whether this is a two-week or a
   two-month exercise.
3. **Is a linear radiometric response needed**, or is this purely geometric?
   Geometric calibration tolerates the companded raw format; radiometric work
   does not, and that decides whether the `raw_format` question is urgent.
