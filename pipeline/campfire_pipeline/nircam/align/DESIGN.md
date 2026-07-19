# NIRCam `align` — coarse-per-pool + gated fine

**Status:** implemented. Opt-in per field, **default off** — a field enables it
in `fields.toml` (`[<field>.align].enabled = true` + `refcat = "<file>.ecsv"`);
otherwise the reduction is byte-for-byte the JHAT path. Lives in
`campfire_pipeline/nircam/align/` (`detect.py`, `footprint.py`, `solve.py`,
`apply.py`), driven from `nircam/orchestrate.py` and enumerated by
`nircam/association.py`.

This document describes the code that exists. It **supersedes** an earlier
"hierarchical joint solve" proposal (Layer 1/2/3, one cross-filter attitude)
that was *not* built — that design is gone; do not re-derive from it.

---

## 1. What it is, and why it replaces `jhat`

`align` is a field-level astrometric step that ties every NIRCam exposure to a
Gaia-tied reference catalog. It replaces the JHAT-based `jhat` step while keeping
what jhat got right — careful matching against a good refcat, a robust rigid fit,
per-detector correction — and fixing where jhat is fragile (SW detectors with few
sources, no footprint clip on the refcat, a soft count-only acceptance).

**What jhat actually does, from reading its source.** jhat's per-detector
correction is a **linear** `rshift` (shift + rotation, no scale skew) computed by
`jwst`'s `TweakRegStep`, followed by a `to_fits_sip` **serialization** of the
already-linear GWCS into FITS SIP keywords. jhat does **not** fit per-detector
distortion — the SIP terms it writes are just the JWST distortion model
re-expressed as a polynomial, not a new fit. So the honest target for `align` is:
reproduce jhat's *linear per-detector fit*, not invent a distortion solve.

**Distortion stays with CRDS/SIAF.** `align` never touches the detector
distortion model; it corrects only the rigid placement (the same thing jhat
corrects). The SIAF distortion rides through `tweakwcs`'s `JWSTWCSCorrector`
untouched, and `apply.py` writes FITS SIP keywords via `jwst`'s
`update_fits_wcsinfo` — the identical `to_fits_sip` serialization jhat produces,
so the on-disk WCS keyword contract is unchanged. This matches the whole JWST
ecosystem (distortion is a calibration product, not a per-exposure fit); jhat's
SIP *serialization* is the only ecosystem-wide idiom `align` deliberately mirrors.

What `align` adds over jhat is a **pooled coarse solve** for robustness: instead
of asking each small SW detector to find its own solution against a field-wide
catalog, it clips the refcat to the frame and fits one rigid transform per module
pool first, then only frees a per-detector fit where the residual demands it.

---

## 2. Where it sits: per-filter = per-channel, inside the process loop

`align` runs as a per-filter step **inside the NIRCam process loop**, in the slot
`jhat` used (last, on post-`image2` cal data). `_active_process_steps`
(`orchestrate.py`) swaps `jhat → align` for align-enabled fields and leaves
everything else — including `wcs_shift` — in place:

- **`wcs_shift` is kept.** It still applies manual per-exposure offsets for
  exposures with corrupted pointing metadata, and feeds a good input WCS into
  `align`. `align` supersedes only jhat's *automatic* solve, not the manual lever.
- **Per-filter is per-channel for free.** Canonical files are channel-segregated
  by directory — SW filter dirs hold SW detectors, LW dirs hold LW — so running
  `align` on one filter's exposures already yields one channel's detectors. There
  is **no cross-filter reach**: `_run_align` builds groups from the single filter
  it was handed. This is what retired the old joint solve's cross-filter
  concurrency race and its tile-union gap at filter boundaries — a per-filter step
  can't race a sibling filter it never touches.

One exposure token (`rootname` minus the `_<detector>` suffix,
`association.exposure_key`) still identifies one physical dither across both
channels, but each channel is solved in its own per-filter invocation against the
**same shared refcat** (§7), so SW and LW register to one frame anyway.

---

## 3. Architecture: coarse per pool → group gate → fine ladder

```
per FILTER (one channel), per EXPOSURE (build_exposure_groups)
        │  split_pools: pool_modules=false → module A, module B are SEPARATE pools
        ▼                pool_modules=true  → both modules are one pool
┌──────────────────────────────────────────────────────────────────────┐
│ PER DETECTOR (detect.py)                                               │
│  • DAOStarFinder centroids on the SCI image (0-indexed px), per-filter │
│    PSF fwhm; DQ mask = DO_NOT_USE | SATURATED | NO_LIN_CORR; snr_min,  │
│    objmag_lim, shape + edge cuts. mag rides along only as a rank proxy.│
└──────────────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ PER POOL (solve.solve_exposure_group)                                  │
│  1. FOOTPRINT-CLIP the field refcat to this pool's detector union +    │
│     border (footprint.py, gnomonic tangent plane).                     │
│  2. COARSE — ONE pooled rigid `rshift`, all the pool's detectors on one │
│     group_id, matched by the JHAT-ported OffsetHistogramMatch          │
│     (histmatch.py): gross 2-D-hist shift (pass 0), then unbounded 1-NN │
│     + offset-histogram consensus; iterated match→fit→rematch to        │
│     converge (later passes peel off roll).                             │
│  3. GROUP GATE — recompute one-to-one (mutual-NN) matches DIRECTLY;    │
│     accept the pool only if ≥ min_matched matches AND they span        │
│     ≥ min_coverage_arcsec of sky; else → NOT_ALIGNED (WCS preserved).  │
│  4. FINE — each detector over `tolerance` gets an individual fit down  │
│     the ladder general → rshift → shift → keep-coarse, geometry chosen │
│     by its unique-match count + coverage, ACCEPTED only if it reduces  │
│     the residual.                                                      │
└──────────────────────────────────────────────────────────────────────┘
        ▼
   ACCEPTED → write corrected gwcs + CFP_ALGN + WCS_BAK (apply.py)
   REJECTED → CFP_ALGN = NOT_ALIGNED  →  quarantined from combine (§6)
```

### 3.1 Pools and the shared-attitude argument

A **pool** is the set of detectors sharing one coarse rigid fit (`split_pools`).
The default (`pool_modules = false`) makes module A and module B **separate**
pools; `pool_modules = true` pools both into one fit.

The physics that justifies pooling *within* a pool: every detector of one NIRCam
exposure shares one spacecraft attitude — a single (Δx, Δy, roll) — so a rigid
fit is best estimated from **all** of a pool's sources at once. For a rigid fit
the rotation uncertainty scales as

```
σ_θ  ≈  σ_centroid / (√N · R)          (R = spatial spread of matched sources)
```

so a single small SW detector — half a module's linear extent, a handful of
clustered matches — is a badly-conditioned place to fit roll, while the pooled
catalog maximizes both `N` and `R`. That is exactly why the coarse solve pools
and the group gate demands `min_coverage_arcsec` of spread before it trusts a
rotation.

The **A/B split** (the default) trades a little roll leverage — the long A–B
baseline — for isolation: a spurious per-module SIAF offset then cannot
cross-contaminate the other module's solution, and each SW module (a 2×2 mosaic,
~130″) still has enough sources and lever arm to condition its own rotation. A
per-module LW "pool" is a single detector — precisely the jhat-equivalent
single-chip solve. Set `pool_modules = true` to recover the full A–B baseline when
a field wants it.

Mechanically: all of a pool's detectors get one `group_id`, so `tweakwcs`
`align_wcs` fits and applies one rigid transform to them; distinct per-pool
`group_id`s + a static refcat + `expand_refcat=False` keep pools independent.

### 3.2 Coarse solve (`solve._coarse` + `histmatch.OffsetHistogramMatch`)

The coarse matcher is a **faithful port of JHAT's matching algorithm**
(`find_good_refcat_matches` + `histogram_cut`), which is structurally different
from `tweakwcs.XYXYMatch`:

- **Why not XYXYMatch.** `XYXYMatch` *enumerates* candidate pairs
  (`stsci.stimage.xyxymatch`) and sizes its output array by the detection
  count. On real extragalactic catalogs — where detections and refcat rows are
  the same clustered galaxies and the refcat resolves substructure (several
  reference rows within the match tolerance of one detection) — the number of
  reference sources finding a partner exceeds the detection count and the
  matcher dies with `MatchSourceConfusionError`. This killed **39% of COSMOS
  LW exposures** (48/124 in tile A1 f444w) that JHAT solved at 100%.
- **What JHAT does instead.** Pair **every** image source with its single
  nearest reference (unbounded 1-NN — most pairs are wrong *by design*), then
  find the true correspondence by **consensus**: true pairs pile into one
  narrow peak of the pairwise-offset histogram while false pairs scatter
  ~uniformly. Per axis (dx vs y first, then dy vs x — `histocut_order`), a
  rotation-slope scan (`slope_max`, `slope_nsteps`) de-rotates the offsets, the
  Gaussian-smoothed histogram peak is located, survivors of a rough cut around
  the peak (`nfwhm`·FWHM clamped to `[rough_cut_px_min, rough_cut_px_max]`)
  are sigma-clipped (`hist_nsigma`). Nothing is enumerated; nothing can
  overflow. The `*_px` knobs are image pixels (converted per pool via the
  `tweakwcs` `tp_pscale`), so the validated JHAT COSMOS configuration carries
  over verbatim.
- **Pooling is native, and stronger.** The matcher sees the tangent-plane
  catalogs *after* `tweakwcs` concatenates the pool (shared `group_id`), so
  every detector's pairs accumulate into ONE shared offset histogram — more
  detectors mean a taller consensus peak. The port strengthens the pooled
  design rather than trading it away.
- **Gross-shift stage (pass 0 only).** JHAT's 1-NN assumes the WCS error is
  below the local source spacing (true for normal JWST pointing). To keep
  acquisition-failure recovery, pass 0 first locates the gross translation as
  the peak of the 2-D pairwise-offset histogram within `coarse_searchrad` —
  the same idea `XYXYMatch(use2dhist=True)` used for its initial estimate
  (bake-off-validated), but accumulated directly into the histogram so no pair
  list is ever materialized.

Later passes start from the corrected WCS and re-match with the same consensus
matcher minus the gross stage (JHAT's `iterate_with_xyshifts`, generalized:
the re-pairing starts from the *fitted* WCS, not just a median shift); the
rigid `rshift` peels off the roll. The loop stops when a pass moves the WCS by
< 0.01″ (well below a NIRCam pixel) or after `refine_niter` passes. A
matcher/fit crash degrades the pool to NOT_ALIGNED — it never aborts the
worker or the field.

Provenance: pass 0's fit is the gross shift/rot the input WCS was off by (stored
for logging); the converged pass's RMSE is the reported quality.

### 3.3 Group acceptance gate (`solve`, step 3)

`align_wcs` reports `status='SUCCESS'` even for a geometrically wrong fit, so
acceptance never rests on it. The gate **recomputes** matches directly:
`solve._match` transforms each detector's sources through the corrected gwcs and
pairs them to the refcat by **mutual nearest neighbour** (a source and a reference
match only if each is the other's closest, within `match_radius`). The pool is
accepted only if the pooled one-to-one count ≥ `min_matched` **and** the matched
references span ≥ `min_coverage_arcsec` — a rotation you can't condition is a
rotation you don't trust. This is the substantive fix over the old count-only gate
(which could count several detections onto one reference and pass a wrong-θ fit).

### 3.4 Fine per-detector ladder (`solve._choose_fitgeom`, `solve._fine_fit`)

Only detectors whose one-to-one residual exceeds `tolerance` are refit. Each picks
a geometry down the ladder `general → rshift → shift → keep-coarse`, starting from
the `fine_fitgeom` **ceiling** and dropping when it has too few unique matches for
the current geometry, skipping rotating geometries (`general`, `rshift`) when its
own matched sources don't span `min_coverage_arcsec` (an unconditioned rotation).
The default ceiling is `rshift` — **exactly what jhat fits per detector**. The
refit consumes the detector's own mutual-NN pairs *as a pre-matched list*: the
row-aligned pair catalogs go to `align_wcs` with `match=None` — precisely
JHAT's `already_matched=True` design (no matcher runs in the fit; the
sigma-clipped fit alone decides) — on a deep-copied corrector so a rejected
trial never touches the shared solution, and the new WCS is **accepted only if
it measurably reduces** the one-to-one residual. The chosen geometry is
recorded per detector as the `dof` (`coarse` | `shift` | `rshift` | `general` |
`identity`). No `tweakwcs.XYXYMatch` remains anywhere in `align`.

---

## 4. Detection (`detect.py`)

`detect_star_centroids` runs `photutils.DAOStarFinder` on the SCI image and
returns `(x, y)` centroids (0-indexed detector pixels, the gwcs / `tweakwcs`
convention) plus **calibrated AB magnitudes** whenever the frame carries an AB
zeropoint (`BUNIT = MJy/sr` + `PIXAR_SR`, i.e. every jwst cal product):
annulus-free circular-aperture photometry (radius `2×FWHM`, jhat's
`radii_Nfwhm=[2.0]`) on the median-subtracted frame, with
`ZP = −2.5·log10(PIXAR_SR·1e6/3631)` — the same surface-brightness ×
pixel-area conversion jhat uses. **No sky annulus, deliberately**: jhat's
annulus on CAMPFIRE's already sky-subtracted frames averages negative and
trips a `-99.99` sentinel that floods the matcher — the annulus is exactly the
piece of jhat photometry that must not be ported. No aperture correction
either (a ~0.2–0.4 mag point-source constant, irrelevant to the wide mag
windows, ill-defined for galaxies). `table.meta['mag_calibrated']` records
which regime a catalog is in; without a zeropoint, `mag` falls back to the
uncalibrated DAO kernel estimate.

- **Per-filter PSF FWHM.** NIRCam's core width runs from ~F070W to ~F480M, so one
  `fwhm` is wrong across an exposure's SW+LW channels; `apply.py` keys detection
  off each member's filter via `psf_fwhm_by_filter`, falling back to the scalar
  `fwhm`.
- **DQ masking, not a magnitude guess.** `DETECT_DQ_BITS = DO_NOT_USE | SATURATED
  | NO_LIN_CORR` are masked before detection — a saturated or nonlinearity-
  uncorrected core corrupts both the centroid and the kernel flux, and those are
  exactly the bright-star failures a magnitude cut can't catch.
- **Quality cuts:** `snr_min` (peak / background RMS, above the kernel `nsigma`
  threshold), `objmag_lim` (**calibrated AB window**, jhat's COSMOS value
  `[19, 28]`; skipped loudly when the frame has no zeropoint — an AB window on
  instrumental mags would cut everything), sharpness/roundness windows, and an
  `edge` reject. No `brightest` count cap on the align path — the full
  quality-selected catalog reaches the solve.
- **Pair-level brightness agreement** (`delta_mag_lim`, jhat's COSMOS value
  `[-3, 4]`): the matcher can additionally drop 1-NN pairs whose
  `image_mag − refcat_mag` falls outside the window. `tweakwcs` drops
  brightness columns when pooling, so image mags ride the surviving `id`
  column through an id→mag lookup the solve builds from calibrated catalogs.
  Pairs missing either mag are never punished. Off by default: refcat `mag`
  zeropoints are heterogeneous across build backends — enable per field when
  the refcat's photometry is trusted.

---

## 5. Provenance, idempotency, and staleness (`apply.py` + `orchestrate.py`)

**`CFP_ALGN` stamp.** Each accepted detector's primary header gets
`CFP_ALGN = "dof=<geom> res=<median-resid-arcsec> n=<n_matched> rc=<refcat-hash>"`
— per-detector and short enough to fit one 80-char FITS card (the pool-level
coarse shift/rot is logged once per pool, not stamped on every card). A rejected
exposure gets the sentinel `CFP_ALGN = NOT_ALIGNED`.

**`rc=` refcat hash.** `orchestrate._refcat_hash` is the first 8 hex of the refcat
file's SHA-256 — cheap, computed before the catalog is even loaded. Stamping it on
every solution makes the alignment self-describing about *which* reference produced
it.

**`ALGN_BAK`.** On first apply, the pre-align gwcs is stashed as an
ASDF-in-FITS `ALGN_BAK` image extension. An `--overwrite` re-solve reads *from*
`ALGN_BAK`, so re-running never composes a second correction on top of the first —
the solve is idempotent. align deliberately uses its **own** backup extension
rather than `wcs_shift`'s `WCS_BAK`: `wcs_shift` runs before align and backs up
the pre-*shift* WCS in `WCS_BAK`, so solving from `WCS_BAK` would discard the
manual offset. align solves from the current (post-`wcs_shift`) WCS and leaves
`WCS_BAK` untouched (the gwcs↔ASDF-in-FITS technique is shared with
`steps/wcs_shift.py`). The dependency also runs the other way: whenever
`wcs_shift` rewrites a WCS (a retuned rule re-applied with `--overwrite`, or a
new rule matching an already-aligned exposure), it scrubs `CFP_JHAT`/`CFP_ALGN`
and drops `ALGN_BAK` in the same write — the alignment was solved from the old
WCS, so it must re-solve rather than survive as a stale, trusted-looking stamp.

**Skip / re-solve.** The orchestration skip check (`_pending_pools`) + apply's
`_aligned_ok` treat a detector as done only if it carries a *completed,
non-rejected* `CFP_ALGN` whose `rc=` matches the **current** refcat hash. So on a
normal re-run (no `--overwrite`):

- an already-solved pool against the same refcat is **skipped**;
- a `NOT_ALIGNED` pool is **re-attempted** (the user may have retuned
  `[<field>.align]`) — the loud end-of-run warning (`_warn_not_aligned`) hands
  them the levers;
- a pool solved against a now-changed refcat (rc mismatch) or by an older,
  `rc=`-less generation is **re-solved**.

No `--overwrite` needed to retune params or swap the refcat; already-good work is
left alone.

---

## 6. Combine quarantine (`field.py::_quarantine_not_aligned`)

For an align-enabled field, only exposures carrying a real align solution (a
`dof=…` `CFP_ALGN`) enter the drizzle. Two failure modes are quarantined from the
combine working tree — both would otherwise drizzle with a raw WCS, doubling
sources and defeating CR rejection:

- `CFP_ALGN = NOT_ALIGNED` — align tried and could not tie the exposure; retune
  and re-run, or force it in.
- **no `CFP_ALGN` at all** — align never solved it (never run, run over a
  different filter/tile subset, or the worker died). Because an align-enabled
  field replaces jhat's automatic solve, such an exposure has no automatic
  astrometric correction (at most a manual `wcs_shift` offset).

Quarantine is the single gate into the working tree every combine step reads.
Omitting data is never silent (it is surfaced with the fix), and never automatic:
`combine --include-unaligned` is the explicit opt-in to drizzle the raw-WCS
exposures anyway.

---

## 7. Reference catalog (`nircam/refcat/`)

One **shared, Gaia-tied** refcat per field (`[<field>.align].refcat`, a
`campfire-refcat-v1` ECSV in the field's astrometric-catalog dir), resolved once
by `_resolve_align_refcat` — there is no per-filter mapping. This is the mechanism
that makes SW and LW register to the *same* frame: both channels' per-filter
invocations tie to one catalog, so cross-band registration comes for free from the
shared reference rather than from any SW↔LW coupling.

Positions are **proper-motion propagated to the exposure mid-time**
(`apply._propagate_refcat`, `refcat/motion.py`) before the footprint clip, when
the catalog carries the optional `ref_epoch`/`pmra`/`pmdec` columns; a pure-galaxy
(stationary) catalog and rows with non-finite PM are left in place per-row.
Exposure epoch comes from `EXPMID`/`MJD-AVG` or the mean of `EXPSTART`/`EXPEND`;
propagation fails open (align against catalog positions rather than reject).

The **footprint clip** (`footprint.clip_refcat_to_exposure`) restricts the
field-wide catalog (~550k rows for COSMOS) to the pool's detector-union sky polygon
plus a `ref_border_arcmin` margin, in a local gnomonic tangent plane. It is
deliberately a superset — the border doubles as an implicit pointing prior, so it
must exceed the acquisition error to be recovered (default 1.2′ ≥
`coarse_searchrad`). Over-inclusion is harmless (the robust fit rejects extras);
under-inclusion starves the solve. The clip fails open (keeps the full catalog) on
any geometry error — a per-pool worker must never crash the field.

---

## 8. Configuration (`[nircam.align]`)

All knobs live in `config_default.toml`; per-field overrides go in
`fields.toml [<field>.align]`.

| Knob | Default | Controls |
|------|---------|----------|
| `enabled` | `false` | Opt-in; off ⇒ the JHAT path runs unchanged |
| `refcat` | — | Field's Gaia-tied refcat filename (required when enabled; set in `fields.toml`) |
| `pool_modules` | `false` | `false`: coarse fit per module (A, B separate); `true`: pool both modules |
| `coarse_searchrad` | `70.0` | arcsec — gross-shift 2-D-hist radius (≥ max acquisition offset) |
| `refine_niter` | `3` | coarse match→fit→rematch iterations (recovers roll) |
| `d2d_max` | `1.5` | arcsec — drop 1-NN pairs farther than this (post gross shift) |
| `binsize_px` | `0.02` | offset-histogram bin (image px, JHAT value) |
| `gaussian_sigma_px` | `0.2` | histogram smoothing sigma (image px) |
| `rough_cut_px_min` / `_max` | `2.5` / `2.5` | clamp on the rough cut around the peak (px); min = max pins it (COSMOS config) |
| `nfwhm` | `2.5` | rough cut = `nfwhm` × peak FWHM before clamping |
| `hist_nsigma` | `3.0` | sigma-clip of the de-rotated offsets |
| `histocut_order` | `"dxdy"` | cut dx-vs-y first, or `"dydx"` |
| `slope_max` | `10/2048` | rotation-scan half-range, dimensionless (≈ ±0.28°) |
| `slope_nsteps` | `200` | rotation-scan steps |
| `delta_mag_lim` | unset | pair cut: keep `image_mag − refcat_mag` in this AB window (jhat COSMOS: `[-3, 4]`) |
| `objmag_lim` | unset | detection cut: keep this calibrated AB window (jhat COSMOS: `[19, 28]`) |
| `aper_radius_px` | `2×fwhm` | aperture radius for calibrated detection mags |
| `fine_fitgeom` | `"rshift"` | per-detector fine ceiling: `general` \| `rshift` \| `shift` |
| `fine_min_general` | `10` | ≥ this many 1-to-1 matches ⇒ allow `general` |
| `fine_min_rshift` | `4` | ≥ this ⇒ allow `rshift` |
| `fine_min_shift` | `2` | ≥ this ⇒ allow `shift`; below ⇒ keep coarse |
| `tolerance` | `0.05` | arcsec — a fine fit must beat this to read "within" |
| `match_radius` | `0.5` | arcsec — 1-to-1 NN radius for residuals + fine match |
| `min_matched` | `6` | per-pool reject-to-NOT_ALIGNED floor (1-to-1 count) |
| `min_coverage_arcsec` | `5.0` | matched sources must span ≥ this (conditions rotation) |
| `ref_border_arcmin` | `1.2` | refcat footprint margin (≥ `coarse_searchrad`); arcmin |
| `nsigma` | `5.0` | detection threshold (convolved-background σ) |
| `snr_min` | `5.0` | drop detections below this peak SNR |
| `fwhm` | `2.5` | fallback detection PSF FWHM (px) |
| `edge` | `8` | drop detections within this many px of a border |
| `psf_fwhm_by_filter` | table | per-filter detection PSF FWHM (F070W→F480M) |

(`nclip=3`, `sigma=3.0` are the σ-clip params passed through to `tweakwcs`.)

---

## 9. Retired vs jhat / tristars

- **jhat's automatic solve.** Replaced by `align` for opted-in fields; jhat's
  linear per-detector `rshift` behavior is reproduced (`fine_fitgeom="rshift"`
  default), its **matching algorithm is ported outright** (`histmatch.py`, §3.2),
  and its `to_fits_sip` GWCS serialization is preserved via
  `update_fits_wcsinfo`.
- **`tweakwcs.XYXYMatch` in the coarse path — retired.** Its pair enumeration
  (`stsci.stimage.xyxymatch`) sizes output by the detection count and dies with
  `MatchSourceConfusionError` on clustered extragalactic catalogs (39% of
  COSMOS LW exposures; JHAT solved 100%). The 2-D-histogram *initial-offset*
  idea it validated in the bake-off survives as the matcher's gross-shift
  stage; the enumeration does not. `XYXYMatch` remains only in the fine
  per-detector refit, where the local refcat is a subset of that detector's
  own one-to-one matches, so the overflow condition (#refs finding a partner >
  #detections) is impossible by construction.
- **The triangle matcher (`tristars`) — retired.** The earlier `align` drafts
  bootstrapped correspondences with a `tristars` triangle/asterism matcher
  (`matcher.py`); the coarse-matcher bake-off (`scripts/align_matcher_bakeoff.py`)
  showed the 2-D-histogram + iterate config recovers offsets to tens of arcsec and
  roll to ~1° at **97–100%** correct in realistic (contaminated) fields, while the
  `stimage` triangle matcher hard-crashes on sparse input. `matcher.py` was
  **deleted**, no code path uses `tristars` anymore, and the `pyproject.toml`
  pin is removed.
- **The rotation-scan wrapper (jhat's approach) — retired.** A brute-force roll
  scan around `XYXYMatch` (bake-off config C, what jhat does) was *worse* and
  ~**17× slower** than the plain 2-D-hist + iterate; the iterated rigid fit
  recovers the ~1° roll on its own.
- **The old "hierarchical joint solve".** The Layer-1/2/3 single cross-filter
  attitude design was never built. Its concurrency race across filters and its
  tile-union gap are moot: a per-filter (= per-channel) step has no cross-filter
  reach.

---

## 10. Open / future work

- **Real-data jhat A/B validation — still to run.** The bake-off that fixed the
  coarse defaults is *synthetic* (tangent-plane catalogs, no gwcs). The
  side-by-side comparison against jhat on real reductions is the remaining
  validation: per-detector RMS to Gaia, SW↔LW registration (do the two channels
  land on the same frame?), and source-doubling in the combined mosaic. Reference
  the intended harness `scripts/nircam_ab_astrometry.py` alongside the existing
  `scripts/align_matcher_bakeoff.py`. Until this is run on a spread of regimes
  (COSMOS/CEERS broad-band pairs, an F070W/F090W field, a crowded stellar field, a
  nebulous star-forming region, a narrow/medium LW pairing, tile-edge and
  missing-detector cases), `align` stays opt-in.
- **Magnitude-cut parity is implemented but default-off.** Detection mags are
  calibrated AB (§4), so jhat's `objmag_lim = [19, 28]` and
  `delta_mag_lim = [-3, 4]` transfer directly — but both default unset:
  DQ saturation masking already guards the bright end, and refcat `mag`
  zeropoints are heterogeneous across build backends. Enable them per field
  (e.g. COSMOS) where the refcat photometry is trusted; the consensus matcher,
  not the mag cuts, is what carries jhat's robustness either way.
- **Per-detector distortion fitting — deliberately not built.** A genuine SIP
  distortion solve (`fit_wcs_from_points(sip_degree=…)`) was considered and
  rejected: jhat doesn't do it (its SIP is a serialization, not a fit, §1), and a
  real distortion fit needs many well-distributed sources per detector — exactly
  what SW chips lack. Distortion stays a CRDS/SIAF calibration product. If
  cross-exposure residuals ever show stable per-detector/A↔B structure, that is a
  *calibration* signal (a SIAF-residual term learned across many dithers), not
  per-exposure freedom to add here.
