# Sub-aperture calibration strategy

**Configuration this is written for**, as specified:

- Focused plenoptic (Plenoptic 2.0): microlens array behind a main objective,
  each lenslet re-imaging the intermediate image formed by that objective.
- Lenslet pitch ≈ 100 px on a 1456 × 1088 IMX296 → roughly 14 × 10 ≈ 140
  lenslets, 100 × 100 px microlens images.
- Minimal overlap between adjacent microlens images.
- Geometry only; no radiometric calibration required.
- **The deliverable is pixel-to-pixel registration across views**, not a
  photometric or absolute-scale calibration.

That last point is what makes this tractable, and it changes the whole
formulation. Read §1 before anything else.

---

## 1. What "registration" actually requires

Registration is a statement about *relative* geometry. If every sub-aperture's
pixels can be mapped into one common coordinate system such that the same
physical point lands at the same coordinate regardless of which lenslet saw
it, the rig is registered — and it does not matter whether that common system
is metric, or which absolute focal length produced it.

This kills most of the parameters before we start:

- **Absolute scale is a free gauge.** No need to determine millimetres per
  pixel, so no need for a metrically known target, and one strong correlation
  (focal length against target distance) disappears with it.
- **A global rotation between sensor and optics is also a free gauge.** Define
  the common frame *as the sensor frame* and it vanishes identically. There is
  no rotation term anywhere in the model below, which is worth noticing because
  the grid overlay has a rotation slider — that slider describes where the
  lenslets *are*, not a transformation the registration has to undo.
- **Per-lenslet distortion is not free to be arbitrary.** Every lenslet views
  the same intermediate image through the same main objective. The objective's
  distortion is applied once, upstream, and is common to all views.

Your instinct in the brief is correct, and §2 derives exactly why.

---

## 2. The map, derived

### 2.1 Notation

- $\mathbf{m}$ — transverse position in the **intermediate image plane**, the
  aerial image formed by the main objective. This is the common frame; call
  its coordinates the *canonical* coordinates. Registration means every pixel
  gets a canonical coordinate.
- $\mathbf{p}$ — a sensor pixel coordinate.
- $\mathbf{c}_{ij}$ — the centre of lenslet $(i,j)$, in sensor pixels. From the
  lattice: $\mathbf{c}_{ij} = \mathbf{c}_0 + \mathbf{U}\,(i, j)^\top$ with
  $\mathbf{U} \in \mathbb{R}^{2\times 2}$ carrying both pitches, the rotation
  and any skew — the four numbers the grid overlay approximates with pitch and
  rotation.
- $a$ — distance from the intermediate image plane to the MLA.
- $b$ — distance from the MLA to the sensor.
- $f_\mu$ — lenslet focal length, with $\tfrac{1}{a} + \tfrac{1}{b} = \tfrac{1}{f_\mu}$.

### 2.2 One lenslet

Lenslet $(i,j)$ is a thin lens on axis at $\mathbf{c}_{ij}$. A point at
canonical position $\mathbf{m}$ has object height $\mathbf{m} - \mathbf{c}_{ij}$
measured from that lenslet's own axis. The thin lens images it with
magnification $-b/a$, so its image height is $-\tfrac{b}{a}(\mathbf{m} - \mathbf{c}_{ij})$
and its absolute sensor position is

$$\mathbf{p} \;=\; \mathbf{c}_{ij} \;-\; \frac{b}{a}\left(\mathbf{m} - \mathbf{c}_{ij}\right)$$

Invert it. Writing $\boxed{\lambda \equiv a/b}$ for the inverse magnification of
the microlens relay:

$$\boxed{\;\mathbf{m} \;=\; \mathbf{c}_{ij} \;-\; \lambda\,\bigl(\mathbf{p} - \mathbf{c}_{ij}\bigr)\;}$$

**This is the entire registration map.** Read it physically: take a pixel,
measure its offset from its own lenslet's centre, scale that offset by
$-\lambda$, and add it back to the lenslet centre. Three ingredients: the
lattice $\{\mathbf{c}_{ij}\}$, one scalar $\lambda$, and nothing else.

Note what is *absent*: no per-lenslet focal length, no per-lenslet rotation, no
per-lenslet distortion. All of that is either common or absorbed. The minus
sign is the microlens image inversion, and it is why adjacent tiles appear to
move opposite to the scene.

### 2.3 The main objective's distortion

$\mathbf{m}$ is a coordinate in the aerial image, which is itself a distorted
picture of the world. If registration between views is the requirement, **you
do not need to remove that distortion at all** — every view inherits the same
$\mathbf{m}$, so two views of one point agree in $\mathbf{m}$ whether or not
$\mathbf{m}$ is a distorted rendering of the object.

Correct it only if you additionally want the assembled image to be rectilinear.
Then it is a standard Brown radial model applied *once*, in canonical
coordinates, about the optical axis $\mathbf{m}_0$:

$$\mathbf{m}^{\text{rect}} = \mathbf{m}_0 + \left(1 + k_1 r^2 + k_2 r^4\right)\left(\mathbf{m} - \mathbf{m}_0\right),
\qquad r = \lVert \mathbf{m} - \mathbf{m}_0 \rVert$$

Four parameters ($k_1, k_2, \mathbf{m}_0$), shared by every lenslet, estimated
after registration and separable from it. Keeping the two stages separate is
deliberate: a registration failure and a distortion failure then look
different, instead of both showing up as a vague increase in residual.

### 2.4 Manufacturing error

Real lenslets are not exactly on the ideal lattice and their axes are not
exactly at their geometric centres. Both defects have the same first-order
effect — the tile's effective centre is displaced — so both are absorbed by a
single per-lenslet 2-vector:

$$\mathbf{c}_{ij} = \mathbf{c}_0 + \mathbf{U}(i,j)^\top + \Delta\mathbf{c}_{ij}$$

This is what you meant by each lenslet carrying only a flat correction. Worth
being explicit that it is *geometric* — a centre offset — and unrelated to
radiometric flat-fielding, which you have ruled out. It is 2 parameters per
lenslet, and §4 says how to get most of them for free.

---

## 3. The one complication: λ depends on depth

$\lambda = a/b$. The sensor–MLA distance $b$ is fixed by the mechanics, but $a$
is the distance from the MLA to *the intermediate image*, and the main
objective puts the intermediate image at a different place for every object
depth. So:

$$\text{object depth changes} \;\Rightarrow\; a \text{ changes} \;\Rightarrow\; \lambda \text{ changes}$$

Two consequences, and the first is not optional:

**A registration calibrated at one object depth is not valid at another.** With
a single $\lambda$, a point at the wrong depth reprojects to a different
canonical coordinate through each lenslet, and the disagreement grows linearly
with distance from the lenslet: for two lenslets separated by
$\Delta \mathbf{c}$, the canonical disagreement is

$$\delta \mathbf{m} \;=\; \left(\lambda_{\text{true}} - \lambda_{\text{used}}\right)\bigl(\mathbf{p}_2 - \mathbf{p}_1\bigr) \;\approx\; \frac{\Delta\lambda}{\lambda}\,\Delta\mathbf{c}$$

At 100 px pitch and a 1% error in $\lambda$, adjacent-lenslet registration is
off by 1 px and corner-to-corner by ~14 px. **$\lambda$ must be known to
roughly $10^{-3}$ for sub-pixel registration across the array.** This is the
tightest numerical requirement in the whole procedure and it is worth stating
before designing anything around it.

**That same sensitivity is the depth signal.** This is a plenoptic camera; the
depth-dependence of $\lambda$ is the feature, not a defect. Which gives two
operating modes, and you should choose deliberately:

| mode | what you do | when |
|---|---|---|
| **Fixed depth** | calibrate $\lambda$ once at the working depth; registration is then a fixed linear map | the experiment operates in a thin depth range |
| **λ per frame** | fit $\lambda$ per capture from the data itself; registration and depth estimation become the same computation | depth varies, or depth is a measurand |

For the fixed-depth mode, quantify the tolerance: differentiating the thin-lens
relation, a fractional change in $\lambda$ of $10^{-3}$ corresponds to a
fractional change in $a$ of about the same, which for a typical macro
conjugate is a working-distance window of well under a millimetre. **Check this
against your intended depth range before committing to a single $\lambda$.** If
the range is wider, you are in the second mode whether you wanted to be or not.

---

## 4. Parameter budget and the estimation ladder

| level | adds | count at $N = 140$ | what it buys |
|---|---|---|---|
| L0 | $\mathbf{c}_0, \mathbf{U}, \lambda$ | 7 | ideal array, one magnification |
| L1 | $\Delta\mathbf{c}_{ij}$ | +280 | per-lenslet decentring |
| L2 | $\lambda_p$ per pose/depth | +$P$ | depth variation |
| L3 | $k_1, k_2, \mathbf{m}_0$ | +4 | rectilinear output (optional, §2.3) |
| L4 | per-lenslet scale $\lambda_{ij}$ | +140 | **expect not needed — test it** |

Fit them in that order and **stop when held-out residual stops improving**.
That stopping rule is the whole of what makes this "simplified": not choosing a
small model up front and hoping, but adding structure only when the data pays
for it. L4 in particular should be tried once and, if it does not help,
recorded as tested and discarded — a per-lenslet scale that improves training
residual but not held-out residual is the array absorbing noise.

Compare with a per-sub-aperture pinhole model: 15 parameters × 140 = 2100, most
of them unobservable from 100 px tiles. L0+L1+L2 is ~300 and every one of them
is identifiable.

### Gauge fixing

$\Delta\mathbf{c}_{ij}$ is exactly degenerate with $(\mathbf{c}_0, \mathbf{U})$
unless constrained. Impose

$$\sum_{ij}\Delta\mathbf{c}_{ij}=\mathbf{0}, \qquad
\sum_{ij} i\,\Delta\mathbf{c}_{ij}=\sum_{ij} j\,\Delta\mathbf{c}_{ij}=\mathbf{0}$$

— a constant offset is absorbable into $\mathbf{c}_0$, a linear trend into
$\mathbf{U}$. Without these the fit is not wrong, it is *non-unique*: two runs
on the same data give different answers and neither is identifiable as the
right one. Enforce them by projecting $\Delta\mathbf{c}$ onto the orthogonal
complement of $\{1, i, j\}$ after each iteration.

---

## 5. Getting the lattice for free: the white image

Before any checkerboard, capture a **uniformly illuminated flat field** — a
diffuser sheet backlit, or an integrating sphere. Each lenslet projects a
bright disc; its intensity-weighted centroid is that lenslet's centre:

$$\hat{\mathbf{c}}_{ij}=\frac{\sum_{\mathbf{q}\in W_{ij}} I(\mathbf{q})\,\mathbf{q}}{\sum_{\mathbf{q}\in W_{ij}} I(\mathbf{q})}$$

over a window $W_{ij}$ about the nominal centre. At 100 px tiles with decent
SNR this is good to a few hundredths of a pixel. Then fit $(\mathbf{c}_0, \mathbf{U})$
by ordinary linear least squares over all lenslets, and take the residuals as
your initial $\Delta\mathbf{c}_{ij}$.

Three returns on one capture:

1. It fixes 4 of the 7 L0 parameters and initialises all 280 of L1, without a
   target and without an optimiser.
2. The residual spread is a direct measurement of how good the lattice
   assumption is. Sub-pixel: the model in §2 is sound. Several pixels with
   spatial structure: the array is not what you think, and no amount of bundle
   adjustment will fix that.
3. It measures the lenslet disc radius, which tells you the true usable crop
   and hence the actual overlap — worth knowing precisely rather than by
   design intent.

**This is also the precise version of what the UI sliders do by eye.** The grid
overlay gets you to maybe half a pixel; the centroid fit gets you two orders of
magnitude better and reports its own uncertainty.

---

## 6. Estimating λ

Three routes, cheapest first. All three should agree; if they do not, that
disagreement is the most informative measurement in the procedure.

**6.1 From overlap, no target.** Any scene point in the overlap between
adjacent microlens images gives, from §2.2,

$$\mathbf{p}_2 - \mathbf{p}_1 = \left(1 + \tfrac{1}{\lambda}\right)\left(\mathbf{c}_{2} - \mathbf{c}_{1}\right)$$

so a normalised cross-correlation between neighbouring tiles yields $\lambda$
directly from the shift. Cheap, needs only a textured scene, and it works
frame by frame — which makes it the natural implementation of the per-frame
$\lambda$ mode in §3.

Its weakness is your stated configuration: **minimal overlap leaves little to
correlate**. Which leads to:

**6.2 Deliberately over-overlap during calibration.** Nothing forces you to
calibrate at the operating configuration. Move the target (or refocus the main
objective) so the microlens images overlap substantially, estimate the
parameters that do not depend on that choice — $\mathbf{c}_0$, $\mathbf{U}$,
$\Delta\mathbf{c}$, and the distortion — then return to minimal overlap and
re-estimate only $\lambda$. The model is parametric, so this is legitimate, and
it converts your hardest measurement condition into an easy one. Probably the
single most useful procedural trick here.

**6.3 From the checkerboard.** With a board spanning many lenslets, each
detected corner carries a known target-frame position. Fit $\lambda$ and the
pose jointly so that corners seen through different lenslets map to consistent
canonical coordinates. This is the definitive estimate and the one to report.

---

## 7. Checkerboard acquisition

At 100 px tiles a conventional board is entirely workable — this is the regime
where it is the right tool, so your instinct there is right too.

**Board design.** Squares at 20–25 px on the sensor give 4 × 4 to 5 × 5 corners
per tile, i.e. 32–50 observations per lenslet per pose. Against 2 unknowns per
lenslet that is generous, and the surplus is what makes the residual
diagnostics in §8 meaningful. Do not go finer: corner localisation degrades
below ~10 px squares, and blur from the microlens relay will already be
softening things.

**Poses.** Fewer than the classical recipe, because most parameters are shared:

| | count | why |
|---|---|---|
| poses at the working depth | 10–15 | $\Delta\mathbf{c}$ for every lenslet, at the depth that matters |
| poses at other depths | 3 each, ≥ 3 depths | $\lambda(z)$, and it separates $\lambda$ from the lattice |
| high-overlap poses (§6.2) | 5 | the easy-condition estimates |

Coverage matters more than count: across the set, the board must reach every
lenslet including the corner ones, because a lenslet that never sees a corner
has its $\Delta\mathbf{c}$ determined entirely by the gauge constraint. Tilt
matters much less here than in classical calibration — there is no focal length
to disentangle from distance — so keep the board close to fronto-parallel,
where corner detection is most accurate.

**Bench discipline.**

- Lock and tape focus and aperture. Any change to the main objective moves the
  intermediate image, changes $a$, and invalidates $\lambda$.
- Fixed exposure and gain; `AeEnable: false`, already the default in
  `config/pi.yaml`. Auto-exposure between poses corrupts the centroids.
- 20 minutes of warm-up before the first capture. Thermal drift of the
  MLA-to-sensor spacing moves $b$, and $\lambda$ is the ratio.
- Average ~10 frames per pose. Cheap, and it improves corner localisation by
  roughly $\sqrt{10}$.
- **Repeat the white image at the end of the session.** Refit the lattice and
  compare. If the centres have moved more than a fraction of a pixel, something
  shifted and the session is internally inconsistent — discard it. A
  five-second check against a week of confusing residuals.

**Both cameras.** Capture with left and right seeing the same board placement,
and estimate the inter-camera transform inside the same fit rather than
composing two independent calibrations — composing accumulates each camera's
error into the baseline, which is the quantity the stereo pair exists to
measure. Note the existing caveat: `capture-all` is sequential and the sensors
free-run, so the two frames are tens of milliseconds apart. Harmless for a
static board; wire the IMX296 XVS pins together before extending this to
anything moving.

---

## 8. Verifying registration, in the units that matter

Reprojection error against the fitted data is not evidence. Since registration
is the deliverable, measure registration directly.

**8.1 Cross-view consistency — the primary metric.** For every target point
visible through two or more lenslets, map each observation into canonical
coordinates with §2.2 and take the spread:

$$e_n = \max_{(i,j),(k,l)} \left\lVert \mathbf{m}^{(ij)}_n - \mathbf{m}^{(kl)}_n \right\rVert$$

Report the distribution of $e_n$, in pixels, on **held-out poses**. This is
literally the quantity you care about. Target under 0.2 px; achievable given
0.05 px corner localisation and 140 lenslets of averaging.

**8.2 Residual map over the array.** Plot mean residual as a vector field over
$(i,j)$. It should look like noise. Structure means the shared model is too
rigid and is pushing error into a spatial pattern — a radial pattern points at
uncorrected main-lens distortion, a linear ramp at a gauge constraint fighting
the data, a discontinuity at a tile-boundary indexing error.

**8.3 Test the per-lenslet-scale assumption once.** Fit L4, free
$\lambda_{ij}$ per lenslet, and look at the spread. If it is consistent with
noise, the "identical lenslets" assumption is measured rather than assumed, and
you can cite that. If it varies systematically across the array, the MLA is not
plane-parallel to the sensor — a tilt makes $b$, and therefore $\lambda$, vary
linearly across the array. In that case add a 2-parameter linear model
$\lambda_{ij} = \lambda_0 + \alpha i + \beta j$ rather than 140 free ones; that
is the physically motivated extension, and it is cheap.

**8.4 Registration at a wrong depth, deliberately.** Register a target at a
depth you did *not* calibrate, and measure how the error grows. It should
follow the $\Delta\lambda$ relation in §3. Confirming that scaling law tells
you the depth model is right and gives you the usable depth-of-registration
window as a measured number rather than an estimate.

---

## 9. Suggested order

1. White image; fit lattice; **check the residual spread**. Everything else is
   conditional on this looking right.
2. Set the UI grid parameters from the fitted lattice rather than by eye. They
   are the same four numbers.
3. High-overlap captures; estimate $\lambda$ by tile cross-correlation (§6.1).
   A quick independent number to sanity-check the rest against.
4. Checkerboard at the working depth; fit L0 → L1, with the gauge constraints.
5. Cross-view consistency on held-out poses (§8.1). If it is under 0.2 px, the
   registration goal is met and levels L2–L4 are optional.
6. Multi-depth captures; fit $\lambda(z)$ if the depth range demands it (§3).
7. Distortion (L3) only if you need rectilinear output.
8. Extend to the stereo pair.

Build the estimator against the `synthetic` camera backend first, with a known
lattice, known $\lambda$ and known $\Delta\mathbf{c}$ injected. Recovering
planted ground truth is the only way to distinguish an estimator bug from a rig
problem, and it costs an afternoon against the alternative of never being quite
sure which one you are looking at.

---

## Open items

1. **Depth range.** §3 gives the tolerance: $\lambda$ to $\sim10^{-3}$ for
   sub-pixel registration across the array. Does your intended working range
   fit inside that, or is per-frame $\lambda$ required? This is the one
   decision that changes the architecture rather than the parameters.
2. **Source of the grid rotation.** MLA-to-sensor, or sensor-to-optics? It does
   not affect the registration map (§2.3), but it decides whether the
   sub-aperture tiles should be de-rotated for display — see the note at the
   top of `processing/stages/plenoptic.py` and the `derotate_views` flag.
3. **Overlap fraction as built.** The white image measures it directly (§5).
   Worth knowing before committing to §6.1 versus §6.2.
