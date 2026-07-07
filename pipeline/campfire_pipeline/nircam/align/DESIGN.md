# NIRCam `align` — hierarchical joint-solve design

**Status:** proposal, not yet built. Targets the NIRCam field-level astrometric
`align` phase (`campfire_pipeline/nircam/align/`). Written for implementation and
review.

**Supersedes** the earlier *serial LW-anchor cascade* proposal (see §4 — it was
considered and rejected after an adversarial review). **Scope of the change:** the
**solve** and **orchestration** layers (`solve.py`, `apply.py`,
`orchestrate.run_align`), the refcat schema/handling, additive detection changes,
and a combine-side quarantine. The blind matcher (`matcher.py`, `tristars`) is
reused but reframed honestly (§6).

---

## 1. Where this sits in the align PR series

The align phase replaces the JHAT-based `jhat` / `wcs_shift` alignment and ships
**opt-in, default-off** (`[<field>.align].enabled = true`). When a field opts in the
process phase skips `jhat` + `wcs_shift` — exactly one alignment path per field — and
JHAT stays the default while align is validated.

| PR | Branch | What it added |
|----|--------|---------------|
| #322 | `nircam-align-foundation` | `CFP_ALGN` key, `[nircam.align]` config, deps |
| #323 | `nircam-align-association` | exposure-association (`association.py`: `ExposureGroup`, SW/LW/module helpers) |
| #324 | `nircam-align-matcher` | `TriangleMatch` — `tristars` triangle matcher |
| #325 | `nircam-align-detect` | centroid-only detection (`detect.py`, DAOStarFinder) |
| #326 | `nircam-align-solve` | per-exposure solve core (`solve.py`) |
| #327 | `nircam-align-apply` | exposure I/O + `CFP_ALGN` + `WCS_BAK` (`apply.py`) |
| #328 | `nircam-align-run` | `run_align` + CLI |
| #329 | `nircam-tiles-prefilter` | `--tiles` pre-filter; A/B astrometry harness |
| *(this branch)* | `cfpipe-nircam-import-error-d6034o` | matcher spread fix (unmerged) + this doc |

This document proposes the next step: replace the single pooled whole-focal-plane
solve with a **hierarchical joint solve** — one shared exposure attitude plus gated
per-detector residual shifts — carrying the matcher/refcat repairs everyone agrees on.

---

## 2. What exists today (the pooled solve) and why it needs work

For each exposure, `run_align` groups every detector on disk across filter dirs (≤8
SW + 2 LW) and fits **one shared shift+rotation** via `tweakwcs` against the field
refcat, matched by `TriangleMatch`, then an adaptive per-detector shift for stragglers.

Defects found:

- **Refcat never footprint-filtered.** The full field refcat (e.g. 550k rows for
  COSMOS) reaches the matcher and is capped to the 150 *globally* brightest, almost
  all outside the frame (verified in `campfire_pipeline` + `tweakwcs`).
- **The brightest-N cap bounded the fit, not just the bootstrap.** `tristars`
  enumerates every `C(N,3)` triangle, so a cap is needed — but it was allowed to gate
  the final fit; the solution rests on ~17–30 pairs.
- **Pooled-catalog starvation** (the `mag`-strip bug; fixed on this branch by an even
  spread, and made moot by the redesign).
- **Weak acceptance.** `_match` uses `SkyCoord.match_to_catalog_sky` (solve.py:102),
  which is **not one-to-one** — `n_matched` can count several detections onto one
  reference, so `n_match`+RMSE is a soft gate.

These are **matcher/refcat defects, not proof that pooling is wrong** — a key point
from review (§4).

---

## 3. Corrected physics: what constrains the solution

Rotation uncertainty for a rigid fit is roughly

```
σ_θ  ≈  σ_centroid / (√N · R)          (R = spatial spread of matched sources)
```

- **SW and LW image the same field of view**, so `R` is the same between channels —
  there is no "SW wide baseline" (an earlier draft claimed this; it was wrong).
- **The exposure attitude is a single 3-parameter quantity** (Δx, Δy, roll θ) shared
  by all 10 detectors — one spacecraft pointing. It is best estimated from **all**
  usable sources across both modules and both channels at once (maximal `N`, full `R`).
- **A single small detector is a poor place to fit rotation.** One SW detector spans
  ~half the module's linear extent; a handful of clustered matches gives a
  well-fit-looking but badly-conditioned θ.
- **SIAF placement residuals are stable in time.** They belong in a calibration layer
  estimated across many exposures, not re-fit (degenerate with attitude) every exposure.

These four facts drive the architecture: solve the attitude **jointly and globally**,
demote per-detector freedom to **gated shifts**, and push persistent detector offsets
into a **calibration** layer.

---

## 4. Rejected alternative: the serial LW-anchor cascade

An intermediate proposal solved modules A/B independently, anchored each on its LW
detector to the external refcat, then aligned each SW detector (full shift+rotation)
to the LW-derived catalog. An adversarial review (GPT/Codex) plus source cross-checks
retired it. Recorded here so it isn't re-proposed:

- **A and B share one attitude** — independent per-module rotations discard the
  strongest roll constraint (the A–B baseline) and permit mosaic shear across the
  module gap. A module offset is a *calibration* term, not per-exposure attitude.
- **Per-SW-detector rotation is under-conditioned** — half the lever arm, few
  cross-band common sources, and an acceptance gate (`n_match`+RMSE, non-one-to-one)
  too weak to catch a wrong-but-low-RMSE θ.
- **"LW is the better anchor" is field-dependent** — false for blue/stellar fields,
  narrow/medium LW filters (F430M/F460M/F466N/F470N/F480M), nebulous star-forming
  regions, and dropout fields where SW∩LW is tiny.
- **"SW→LW guarantees mutual registration" is false** — cross-band centroids shift
  (galaxy color gradients, blends splitting, emission-line-only objects), so the
  serial fit propagates LW centroid bias coherently into all four SW detectors.
- **Serial state is brittle** — channel ordering, LW-final/SW-stale coupling,
  `--filters`/`--tiles` selection deciding astrometry, mixed-generation crashes.

The joint solve below keeps the *good* parts (footprint filter, bootstrap-only cap,
all-source robust refine) and drops the cascade.

---

## 5. Architecture: hierarchical joint solve

```
per exposure (one dither — every detector on disk, both modules, SW + LW)
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ PREP  (per detector)                                                   │
│  • detect sources: SNR / sharpness / roundness + magnitude-range cuts, │
│    per-filter PSF fwhm, saturation & nonlinearity masked from DQ       │
│  • project pixels → v2/v3 (SIAF) → common tangent plane (arcsec)       │
│  • footprint-filter refcat to the exposure's detector-union + border,  │
│    epoch/proper-motion propagated to the exposure mid-time             │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 1 — shared exposure attitude   (Δx, Δy, roll θ : 3 params)       │
│   bootstrap  : tristars — hypothesis generator, scale+roll CONSTRAINED │
│                (uses the pipeline WCS as the prior; see §6)             │
│        ↓ coarse (Δx, Δy, θ)                                            │
│   refine     : ALL unique 1-to-1 matches, robust (RANSAC / σ-clip),    │
│                iterate match→fit to convergence                        │
│   → ONE attitude for all detectors, weighted by centroid precision;    │
│     whichever channel is informative drives it (no hard LW/SW anchor)  │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 2 — per-detector SHIFT residuals   (SIAF placement, shift-only)  │
│   each detector: 1-to-1 NN to refcat around the Layer-1 WCS,           │
│   accept a shift ONLY if gated: normal-matrix condition number,        │
│   unique-match count, radial coverage, held-out residual improves.     │
│   NO per-detector rotation per exposure.                               │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
   quality gate ─► ACCEPTED : write corrected gwcs + CFP_ALGN + WCS_BAK
        │          REJECTED : NOT_ALIGNED  →  QUARANTINED from combine (§9.1)
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 3 — persistent calibration  (offline / cross-exposure, later)    │
│   pool Layer-2 residual vector fields over many dithers → stable       │
│   per-detector & A↔B-module offsets → SIAF-residual/distortion term,   │
│   rather than per-exposure freedom.                                    │
└──────────────────────────────────────────────────────────────────────┘
```

**One-line summary:** *fit the exposure attitude once from everything; correct
per-detector placement with gated shifts; learn the stable offsets as calibration.*

---

## 6. The matcher, described honestly

Cross-checking `tristars 0.1` corrected our understanding: with `TriangleMatch`'s
defaults `ignore_scale=True, ignore_rot=True`, the hash uses **absolute side lengths**
(scale *constrained*) and the **longest-edge position angle** (rotation *constrained*).
So it is **translation-invariant only** — it assumes both catalogs are already at the
same scale and roll, which for JWST they are (assign_wcs gives scale and roll to
≪1°). tristars' own docs warn the fully scale/rotation-free mode "doesn't work well."

Implications:

- The matcher is **not** "blind / rotation-invariant / no offset prior" — it *relies
  on the pipeline WCS prior*. The `matcher.py` and old-doc language saying otherwise is
  wrong and should be corrected (a one-line docstring fix can land now).
- Because we *have* a roll+scale prior, exotic rotation-invariance is unnecessary; the
  bootstrap's job is only to seed Layer 1.
- `tristars` with `auto_keep=False` returns vertices from a few top triangle-hash
  pairs — **not** RANSAC, **not** one-to-one, **not** inlier-validated. Treat it as a
  **hypothesis generator only**: never trust raw top-triangle pairs as matches. Layer-1
  refine (robust, all unique matches) is what earns trust.
- If a mode with large roll error is ever supported, `ignore_rot` must go `False`
  (true invariance, less robust) — an explicit, tested decision, not a silent default.

---

## 7. Detailed design

### 7.1 Detection (`detect.py`) — quality selection, not a count cap

Return the full quality-cut catalog; the only cap left is `bootstrap_max` on the
triangle stage. Cuts (configurable; DAOStarFinder emits the needed columns):

- `snr_min`; `sharpness_lim`; `roundness1_lim`; `objmag_lim=(bright,faint)` magnitude
  **range** (drops saturated-bright and low-SNR-faint).
- **Per-filter PSF `fwhm`** — a single `fwhm=2.5` is wrong across F070W→F480M; key
  DAOStarFinder off the filter's PSF. (Note `flux`/`mag` here are a kernel matched-
  filter estimate, adequate only for *ranking* the bootstrap subset — not photometry.)
- **Saturation/nonlinearity masked from DQ + ramp**, not inferred from a mag cut
  (saturation corrupts the measured flux).
- The old `brightest` count cap is **removed**.

### 7.2 Reference catalog — footprint + epoch (the two real gaps)

- **Schema** grows an epoch/motion contract: `source_id, ref_epoch, pmra, pmdec,
  parallax` (where available) + uncertainties, alongside `RA, DEC, mag, mag_err`.
  Distinguish stationary extragalactic anchors from stars; for survey-derived galaxy
  positions record the effective coadd epoch.
- **Propagate to the exposure mid-time** (`apply_space_motion`) **before** footprint
  clipping and projection. Today's schema (`RA/DEC/mag/mag_err`) silently assumes zero
  motion — fine for a pure-galaxy anchor, wrong for stars or mixed HSC/LS/Gaia rows.
- **Footprint clip** to the exposure's detector-union sky polygon + `ref_border_arcmin`
  (default 0.5′), using actual GWCS bounding boxes and spherical polygons. Because the
  footprint derives from the (possibly offset) input WCS, the border **is** an implicit
  pointing prior: size it for the max supported acquisition error and fail
  `outside-acquisition-bound` distinctly from `source-starved`.

### 7.3 Layer 1 — shared attitude

Bootstrap (`tristars`, §6) → coarse (Δx, Δy, θ). Refine with `tweakwcs.XYXYMatch`
against the footprint-filtered refcat, but specified exactly to avoid footguns:
**immutable baseline corrector, transformed catalogs each iteration, one-to-one
rematch, robust refit (`fitgeom='rshift'`, σ-clip), convergence test, one final WCS
application.** Do *not* re-run the 2-D histogram acquisition once a good transform
exists (it composes corrections); rebuild fresh correctors per outer iteration.
Attitude is fit from all detectors sharing one `group_id`, weighted by centroid
precision.

### 7.4 Layer 2 — gated per-detector shifts

For each detector: one-to-one NN to the refcat around the Layer-1 WCS; propose a
shift-only correction; **accept only if gated** on normal-matrix condition number,
unique-match count, radial coverage, and a held-out residual that actually improves.
No per-detector rotation. Rejected detectors keep the Layer-1 attitude
(`dof=attitude`).

### 7.5 Layer 3 — persistent calibration (later)

Aggregate Layer-2 residual vector fields across many dithers to estimate stable
per-detector and A↔B-module offsets; fold into a SIAF-residual/distortion term. Out
of scope for the first build, but the parametrization above is chosen so it can be
added without rework.

### 7.6 Acceptance & provenance

- **Acceptance** never rests on `n_match`+RMSE alone (they hide the non-one-to-one
  and conditioning problems). Use unique matches, condition number, predicted edge
  displacement, radial coverage, held-out residuals; reject-to-identity otherwise.
- **Provenance**: a short **solution ID** in the `CFP_ALGN` card, backed by a
  structured per-exposure/module record — refcat content hash + epoch, source counts
  at every cut, transform + covariance, residual quantiles/vector diagnostics, and
  code/CRDS/config baseline + parent generation. (Full record is phaseable; the card
  ID + counts + rmse land first.)

### 7.7 Config (`[nircam.align]`)

| Knob | Default | Controls | Status |
|------|---------|----------|--------|
| `ref_border_arcmin` | `0.5` | Footprint margin (sized for acquisition error) | new |
| `snr_min` | — | Detection SNR floor | new |
| `sharpness_lim` / `roundness1_lim` | windows | DAOStarFinder shape cuts | new |
| `objmag_lim` | `(bright, faint)` | Detection magnitude range | new |
| `psf_fwhm_by_filter` | table | Per-filter detection PSF FWHM | new |
| `bootstrap_max` | `150` | Density-matched vertex cap — **bootstrap only** | changed |
| `refine_searchrad` / `refine_tolerance` / `refine_niter` | arcsec / arcsec / `3` | Layer-1 refine | new |
| `detector_shift` | `gated` | Layer-2: `off` / `gated` | new |
| `brightest` | — | **removed** | changed |

---

## 8. What stays the same

`association.py` (already module/channel-aware); `tristars` bootstrap; `tweakwcs`
correctors + tangent-plane machinery; `CFP_ALGN` + `WCS_BAK` + reject-to-identity;
opt-in/default-off; `--tiles` and multiprocessing dispatch; the matcher spread-fix.

---

## 9. Correctness gaps to close (independent of the layering)

### 9.1 `NOT_ALIGNED` must be quarantined from combine

Confirmed: combine enumerates canonicals unconditionally with no `CFP_ALGN` check, so
a reject-to-identity exposure drizzles with its raw WCS (doubled sources, CR-rejection
failure). Pre-existing (JHAT shares it) and usually ≲0.5″ for JWST, but real. For
align-enabled fields, exclude `NOT_ALIGNED` from combine (or require an accepted
alignment), with an explicit `--include-unaligned` opt-in — never implicit inclusion.

### 9.2 Cross-filter / tile dependency closure

`run_align` builds associations only from the *selected* filters, so `--filters f200w`
can't see its paired F444W, and per-filter tile selection can split a physical
exposure at a tile edge. Resolve the physical-exposure membership across **all** field
filters before applying the write scope; select tiles on the cross-filter exposure
union; log any out-of-selection file read as input.

### 9.3 Observing-mode gating

Grouping by filename doesn't validate `EXP_TYPE`, aperture, or subarray, or compute
real SW∩LW overlap. Define supported modes; unsupported (subarray/coronagraph/TSO/dead
detector) stop or take a separately-validated path rather than falling through generic
per-detector logic.

### 9.4 State & staleness

A single FITS keyword can't encode a dependency graph. Even in the joint solve, an
exposure re-solved when more channels/filters arrive (or after `--overwrite`, a new
refcat, or new CRDS) needs a **generation ID + content hash**; children/derived
products mark **stale** when a parent changes; write a module/exposure solution
transactionally with `PENDING/ACCEPTED/REJECTED/STALE` states. Phaseable, but the
generation ID should exist from the first build.

---

## 10. Partial processing (LW present, SW not — etc.)

The joint solve removes the serial LW→SW coupling, but partial data still matters:
solve the attitude from whatever usable detectors are present, stamp its **generation**
(§9.4), and re-solve when more arrive if the new data would change it (staleness-
gated), rather than silently leaving a mixed-generation exposure. An exposure with too
few usable detectors/sources rejects to identity and is quarantined (§9.1).

---

## 11. Open questions — validate on real data before committing parametrization

The layer *structure* is settled; the *parametrization* (how much per-detector
freedom) is an empirical question. **Shared-attitude-first is the safe default;**
enable Layer-2 shifts only where the data support them. Before finalizing, measure on
the `cosmos_align` A4 harness and a spread of regimes:

1. **Unique 1-to-1 matches and weighted radial leverage per detector**, after real
   quality/saturation/blend/morphology cuts — the quantity that decides whether any
   per-detector freedom is justified.
2. **A↔B closure** and **shared-attitude residual vector fields** — do residuals show
   real per-detector/module structure, or just noise?
3. Regimes: COSMOS/CEERS broad-band pairs; an F070W/F090W field; a crowded stellar
   field; a nebulous star-forming field; a narrow/medium LW pairing; tile-edge and
   missing-detector cases.

If residuals are noise, ship Layer 1 only. If they show stable structure, that's a
Layer-3 calibration signal, not per-exposure freedom.

---

## 12. Affected files (implementation sketch)

- `orchestrate.py::run_align` — cross-filter exposure membership + tile union; drive
  Layer 1 → Layer 2; generation stamping; NOT_ALIGNED quarantine hand-off.
- `solve.py` — replace pooled `solve_exposure_group` with a joint attitude fit +
  gated per-detector shift; robust one-to-one refine; conditioning-based acceptance.
- `apply.py` — extend `CFP_ALGN` provenance (solution ID + counts + rmse); structured
  record hook.
- `detect.py` — SNR/shape/mag-range cuts, per-filter PSF fwhm, DQ saturation masking;
  drop `brightest`.
- `matcher.py` — correct the invariance docstring now; density-matched bootstrap subset
  (spread fix stays as fallback).
- `refcat/{io,query}.py` — epoch/PM schema + `apply_space_motion`.
- combine path — `NOT_ALIGNED` exclusion + `--include-unaligned`.
- config defaults + `tests/test_align_*`.

---

## 13. Staged rollout (PR plan)

Sequenced so that **nothing waits on the empirical question that isn't blocked by it.**
Align is opt-in/default-off, so every stage has a small blast radius and each carries
its own `## Unreleased` changelog entry. Stages 1–3 are improvements to the *current
pooled* solve and can land (and be validated on real data) before the architecture
pivot; the pieces they build (footprint filter, robust refine, quality detection) are
reused by the joint solve, not thrown away.

```
S1 ─┐
S2 ─┼─► S4 (Layer 1 joint attitude) ─► S5 (measure) ─► S6 (Layer 2) ─► S7 (Layer 3)
S3 ─┘
(S1/S2/S3 independent, any order/parallel; S4 needs S1; S6 needs S4 + S5's verdict)
```

| # | PR | Scope / files | Ships alone? | Depends on | Changelog |
|---|----|---------------|--------------|-----------|-----------|
| **S1** | **Refcat footprint + robust refine** | Footprint-clip the refcat to the exposure/detector union + border (GWCS bboxes, spherical polygons); density-matched **bootstrap-only** cap; specified `XYXYMatch` all-source robust refine (immutable baseline, 1-to-1 rematch, σ-clip, convergence). Slots into the *current* pooled solve. `matcher.py`, `solve.py`, new refcat-footprint helper, tests | yes | — | Algorithm |
| **S2** | **Detection quality selection** | SNR/sharpness/roundness + magnitude-range cuts, per-filter PSF `fwhm`, DQ saturation/nonlinearity masking; drop the `brightest` count cap. `detect.py`, config, tests | yes | — | Algorithm |
| **S3a** | **`NOT_ALIGNED` combine quarantine** | Exclude reject-to-identity exposures from combine for align-enabled fields; explicit `--include-unaligned`. combine path, tests | yes | — | Algorithm |
| **S3b** | **Refcat epoch/PM contract** | Schema gains `source_id/ref_epoch/pmra/pmdec/parallax` + errors; `apply_space_motion` to exposure mid-time before clip/projection; stationary-vs-star handling. `refcat/{io,query}.py`, tests | yes | — | Calibration |
| **S3c** | **Dependency closure + mode gating** | Resolve physical-exposure membership across **all** field filters before the write scope; tile selection on the cross-filter union; `EXP_TYPE`/aperture/subarray gating; real SW∩LW overlap. `orchestrate.py`, `association.py`, tests | yes | — | Infrastructure |
| **S4** | **Layer 1 — hierarchical joint attitude** | Replace pooled `solve_exposure_group` with the single-attitude joint fit (all detectors, precision-weighted), reusing S1's footprint+refine; conditioning-based acceptance (condition #, unique matches, held-out residual); **generation ID** + solution-ID provenance. `solve.py`, `apply.py`, `orchestrate.py`, tests | yes (Layer-1-only, shared-attitude default) | S1 (S2/S3 recommended) | Algorithm |
| **S5** | **Data validation** *(measurement, not a PR)* | On the A4 harness + regimes in §11: unique 1-to-1 SW∩LW matches & radial leverage per detector, A↔B closure, shared-attitude residual vector fields. Decides whether Layer 2 is justified | — | S4 | — |
| **S6** | **Layer 2 — gated per-detector shifts** | Shift-only per-detector residual, accepted only on the §7.4 gate. *Only if S5 shows real per-detector structure.* `solve.py`, config, tests | yes | S4 + S5 verdict | Algorithm |
| **S7** | **Layer 3 + structured provenance** *(later)* | Cross-exposure per-detector/A↔B offset estimation → SIAF-residual/distortion term; full transactional solution records + staleness. Out of first scope | yes | S6 | Calibration/Infra |

**Guidance.** Land S1–S3 first (biggest correctness wins, reusable pieces, real-data
feedback early). S4 is the pivot but ships as *Layer-1-only* — shared-attitude-first is
the safe default. Do **not** build S6 before S5's measurement justifies per-detector
freedom. If S5 shows only noise, stop at S4 and defer any structure to S7 calibration.

## 14. Versioning / changelog

**Algorithm** change (alters which sources match → the fitted WCS) → MINOR. Opt-in and
default-off, so no existing (JHAT) reduction changes. Each stage above carries its own
`## Unreleased` entry in the category shown. The refcat epoch/PM change (S3b) is
**Calibration** (it moves reference positions → the fitted WCS); the dependency/mode
closure (S3c) is **Infrastructure**.
