# NIRCam `align` — two-stage LW-anchor design

**Status:** proposal, not yet built. Targets the NIRCam field-level astrometric
`align` phase (`campfire_pipeline/nircam/align/`). Written for an independent
design review — the goal is to catch footguns *before* implementation.

**Scope of the change:** a rework of the **solve** and **orchestration** layers
(`solve.py`, `apply.py`, `orchestrate.run_align`) plus additive changes to
`detect.py` and the refcat handling. The `association.py` layer already exposes
everything the new structure needs (`sw_members`, `lw_members`, `modules`,
`is_single_member`). The blind matcher (`matcher.py`, `tristars`) is reused.

---

## 1. Where this sits in the align PR series

The align phase is a ground-up replacement for the JHAT-based `jhat` / `wcs_shift`
alignment, built as a stack of small PRs and shipped **opt-in, default-off**
(`[<field>.align].enabled = true`). When a field opts in, the process phase skips
`jhat` + `wcs_shift` — exactly one alignment path per field — and JHAT remains the
default while align is validated.

| PR | Branch | What it added |
|----|--------|---------------|
| #322 | `nircam-align-foundation` | `CFP_ALGN` provenance key, `[nircam.align]` config namespace, deps |
| #323 | `nircam-align-association` | exposure-association layer (`association.py`: `ExposureGroup`, SW/LW/module helpers) |
| #324 | `nircam-align-matcher` | `TriangleMatch` — `tristars` blind triangle/asterism matcher |
| #325 | `nircam-align-detect` | centroid-only source detection (`detect.py`, DAOStarFinder) |
| #326 | `nircam-align-solve` | per-exposure solve core (`solve.py`) |
| #327 | `nircam-align-apply` | exposure I/O + `CFP_ALGN` stamp + `WCS_BAK` (`apply.py`) |
| #328 | `nircam-align-run` | `run_align` + CLI (`cfpipe nircam align` / `run --align`) |
| #329 | `nircam-tiles-prefilter` | `--tiles` pre-filters process/align/combine; A/B astrometry harness |
| *(this branch)* | `cfpipe-nircam-import-error-d6034o` | fix: matcher spreads vertex cap across pooled detectors (unmerged) |

This document proposes the **next** step on that arc: replacing the single pooled
whole-focal-plane solve with an automatic two-stage, per-module, LW-anchored solve.
It supersedes the pooled `solve_exposure_group` design from #326.

---

## 2. What exists today (the pooled design)

For each physical exposure (one dither), `run_align` builds an `ExposureGroup` of
**every detector on disk across all filter directories** — up to 8 SW (nrca1–4,
nrcb1–4) + 2 LW (nrcalong, nrcblong) — and solves it as one unit:

1. Detect centroids per detector (DAOStarFinder), capped to the brightest 150.
2. Give every detector a `JWSTWCSCorrector` sharing one `group_id`, so `tweakwcs`
   pools all sources into one group catalog and fits **one shared shift+rotation**
   (`fitgeom='rshift'`) against the field reference catalog, matched by
   `TriangleMatch`.
3. Recompute per-detector residuals; for any detector over `tolerance`, an
   **adaptive** shift-only refit against the ref sources it already matched.
4. Write the corrected gwcs + `CFP_ALGN` stamp; original gwcs stashed in `WCS_BAK`;
   reject-to-identity (`NOT_ALIGNED`) below `min_matched`.

### Problems found (the motivation for this rework)

- **The reference catalog is never footprint-filtered.** The full field refcat
  (e.g. 550,005 sources for COSMOS) reaches the matcher; `TriangleMatch` then caps
  it to the 150 *globally* brightest — almost all outside the exposure footprint.
  Verified in both `campfire_pipeline` and `tweakwcs`: `run_align` passes the whole
  table, and `tweakwcs.RefCatalog.calc_tanp_xy` / `align_to_ref` project and match
  every row with no spatial cut.
- **The brightest-N cap is a triangle-count bound applied in the wrong place.**
  `tristars.parse_triangles` enumerates every `C(N,3)` triangle globally, so the cap
  is required for tractability — but it was allowed to bound the **final fit**, not
  just the blind bootstrap. In practice the shared solution rests on ~17–30 matched
  pairs.
- **The cap silently starved multi-detector pools.** `tweakwcs` strips the `mag`
  column when it builds the pooled group catalog, so the cap fell back to "first N in
  input order" and consumed one detector's whole block. (Fixed on this branch by an
  even spread; the rework removes the need.)
- **The SW/LW pooling rationale was wrong.** See §4.

### The consensus this rework adopts

grizli (`align_drizzled_image`), JHAT (`st_wcs_align`), and tweakwcs (`XYXYMatch`)
all use the same shape: **spatially restrict the reference → coarse/blind bootstrap
on a bounded bright subset → nearest-neighbour refine on _all_ inliers.** grizli —
whose author, Gabe Brammer, also wrote the `tristars` library CAMPFIRE calls — caps
its `tristars` bootstrap to ~200 "to avoid triangle-matching combinatorics," then
runs `NITER` NN refinement on everything. The cap belongs on the bootstrap, never on
the fit. The pooled design had the capped bootstrap and no refine stage.

---

## 3. Corrected physics: what actually constrains the solution

The uncertainty on the fitted rotation is roughly

```
σ_θ  ≈  σ_centroid / (√N · R)
```

where `N` is the number of matched sources and `R` is their spatial spread about the
fit center. Two consequences drive the whole design:

- **SW and LW image the same field of view.** A module's four SW detectors tile the
  same sky as that module's one LW detector, so `R` is *identical* between channels.
  There is no "SW has a wider baseline." (An earlier version of this design claimed
  SW pins rotation — that was wrong.)
- **With `R` equal, the better anchor is whichever channel yields more clean
  matches**, and in deep extragalactic fields that is **LW**: more sensitive to the
  red population, *contiguous* (one detector per module — no inter-chip gaps that
  drop sources), and only 2 SIAF placements to trust vs 8. SW's one edge is sharper
  per-source centroids (PSF ~2× smaller in arcsec), which does not overcome LW's
  advantage in `N` and cleanliness for the anchor role.

**Design consequence:** anchor on LW; treat each SW detector as a differential
refinement tied to LW. Rotation is a large-scale quantity best measured from the
dense LW solution over the module FOV, not re-derived from one small, undersampled
SW chip against a sparse external catalog.

---

## 4. Final decisions

The pooled single-solve is replaced by an **automatic two-stage, per-module,
LW-anchored** solve.

- **D1 — Modules A and B are solved independently.** Each module's LW detector
  anchors that module's SW detectors. Rationale (from heavy JHAT use): a single LW
  detector aligns to an external reference catalog robustly on its own. What is hard
  is aligning an *individual SW detector* to a sparse external catalog — that would
  need a dense reference derived from LW imaging, which is exactly why SW is tied to
  LW instead. Keeping A and B independent also absorbs any real module-to-module
  placement offset that SIAF misses.

- **D2 — Each SW detector gets a full alignment (shift *and* rotation) tied to LW.**
  Not shift-only. The enabling condition is that SW's reference is the **dense,
  co-observed LW source catalog** (see §5), which supplies enough references across a
  single SW chip to constrain rotation — the regime JHAT operates in routinely.

- **Two-stage, automatic.** The pipeline runs the LW anchor when LW canonicals exist
  and the SW tie when SW canonicals exist *and* the module's LW anchor is already
  solved. No manual sequencing; the phase discovers what is runnable. This also
  resolves the partial-processing case (§7).

---

## 5. Detailed design

### 5.1 Per-module hierarchy

Work is organized per `(exposure, module)`. `association.ExposureGroup` already
splits members by `.module` and `.channel`, so a module unit is
`{lw: <1 LW member>, sw: [<≤4 SW members>]}`.

```
exposure (one dither)
├── module A
│   ├── LW: nrcalong        ← anchor (Stage 1)
│   └── SW: nrca1..4        ← each tied to A's LW (Stage 2)
└── module B
    ├── LW: nrcblong        ← anchor (Stage 1)
    └── SW: nrcb1..4        ← each tied to B's LW (Stage 2)
```

### 5.2 Stage 1 — LW anchor (per module → absolute frame)

Reference = the field's Gaia-tied `campfire-refcat-v1`, **footprint-filtered** to the
LW detector's sky footprint + `ref_border_arcmin` (default **0.5′**).

1. Detect sources in the LW detector — quality cuts, not a count cap (§5.4).
2. **Bootstrap:** `TriangleMatch` (`tristars`) — rotation-invariant, no offset prior
   — on a density-matched bright subset (cap = `bootstrap_max`, default 150). Yields
   a coarse shift+rotation.
3. **Refine:** `tweakwcs.XYXYMatch` (2-D offset-histogram → NN within tolerance) on
   the *full* LW catalog vs the footprint-filtered refcat, `fitgeom='rshift'`, looped
   `refine_niter` (default 3) with sigma-clipping. The LW WCS is now absolutely
   registered.
4. Write the LW canonical (corrected gwcs, `CFP_ALGN`, `WCS_BAK`) or reject-to-identity.

The LW anchor is **final** once written; it is never re-solved when SW arrives.

### 5.3 Stage 2 — SW tied to LW (per detector → differential)

Reference = the **LW-derived source catalog**: sources detected in the *aligned* LW
image of the same exposure+module, at their now-registered sky positions. Dense,
co-observed, and already tied to the absolute frame through Stage 1.

For each SW detector in the module:

1. Starting WCS = SW's own gwcs with the module's LW shift+rotation applied (SIAF
   places SW relative to LW, so this lands the SW frame within ~the SIAF residual —
   sub-arcsec, near-zero residual rotation).
2. Detect SW sources (same quality cuts).
3. **Refine directly to the LW catalog:** `XYXYMatch`, `fitgeom='rshift'` (full
   shift+rotation per D2), looped with sigma-clipping. Because the start WCS is
   already close, no blind bootstrap is normally needed; a `tristars` bootstrap is a
   fallback if the SW↔LW residual is too large to match.
4. Write the SW canonical (`CFP_ALGN`, `WCS_BAK`) or reject-to-identity.

Tying SW to LW (rather than independently to the external refcat) is deliberate:
SW and LW were co-observed, so they share sources with no epoch/proper-motion
mismatch, and — critically for downstream **multiband aperture photometry** — it
**guarantees SW/LW are mutually registered**. Two independent refcat solves would
each carry their own residual and could leave a small SW–LW relative offset.

### 5.4 Detection quality cuts (replaces the count cap)

`detect.py` returns the *full* quality-cut catalog; the only cap left is
`bootstrap_max` on the triangle stage. Cuts (JHAT-style, all configurable; DAOStarFinder
already emits the needed columns):

- `snr_min` — SNR floor.
- `sharpness_lim`, `roundness1_lim` — reject cosmics / streaks / extended sources.
- `objmag_lim = (bright, faint)` — a magnitude **range** dropping *saturated bright*
  as well as low-SNR faint sources.
- `brightest` (the old per-detector count cap) is **removed**.

The same cuts apply to the LW-derived reference catalog built in Stage 2, so SW never
aligns to LW noise/false-positives.

### 5.5 Provenance & reporting

`CFP_ALGN` distinguishes the two solution types and records per-stage accounting so a
run *shows* what drove each fit:

- LW: `role=lw-anchor dof=rshift n_ref=… n_match=… rmse=…`
- SW: `role=sw→lw dof=rshift n_match=… rmse=… (or fallback=refcat)`
- Rejected: `NOT_ALIGNED` (WCS preserved) with the reason.

Log lines report, per module: LW n_detected/n_matched/rmse, and per SW detector
dof/n_matched/rmse/accepted.

### 5.6 Config (`[nircam.align]`)

| Knob | Default | Controls | Status |
|------|---------|----------|--------|
| `ref_border_arcmin` | `0.5` | Sky margin around a detector footprint for refcat clipping | new |
| `snr_min` | — | Detection SNR floor | new |
| `sharpness_lim` | `(lo, hi)` | DAOStarFinder sharpness window | new |
| `roundness1_lim` | `(-0.75, 0.75)` | Roundness window | new |
| `objmag_lim` | `(bright, faint)` | Detection magnitude range | new |
| `bootstrap_max` | `150` | Density-matched vertex cap for the triangle bootstrap **only** | changed |
| `refine_searchrad` | arcsec | `XYXYMatch` 2-D histogram search radius | new |
| `refine_tolerance` | arcsec | NN match tolerance after the coarse shift | new |
| `refine_niter` | `3` | match→fit iterations in the refine loop | new |
| `sw_to_lw` | `true` | Tie SW to the LW-derived catalog (vs external refcat fallback) | new |
| `brightest` | — | **removed** — detection is no longer count-capped | changed |

---

## 6. What stays the same

- The `association.py` exposure model (already module/channel-aware).
- `tristars` `TriangleMatch` as the blind bootstrap.
- `tweakwcs` correctors and the tangent-plane machinery.
- `CFP_ALGN` + `WCS_BAK` provenance and the reject-to-identity contract.
- Opt-in, default-off; process phase skips `jhat`/`wcs_shift` for align fields.
- `--tiles` pre-filtering and multiprocessing dispatch.

---

## 7. Partial processing (LW done, SW not — the "Point 0" case)

Because filters are processed independently, LW canonicals for an exposure often
exist before SW canonicals (or vice versa). The two-stage split handles this by
**temporal decoupling**:

- **LW present, SW absent** → run Stage 1. The LW anchor is written and is final.
- **SW present, LW anchor already solved** → run Stage 2 against the stored LW
  solution + LW-derived reference. LW is *not* re-solved.
- **SW present, LW anchor not yet solved** → SW is deferred (nothing to anchor to)
  unless the no-LW fallback (§8) is enabled.

Contrast with the pooled design, where an LW-only group aligns on LW, then SW's later
arrival re-forms a 10-detector group and re-solves — silently mutating an LW WCS that
may already be deployed. The anchor design makes the LW solution monotonic.

---

## 8. Fallbacks / graceful degradation

- **No LW filter processed for the field/selection at all** → fall back to
  SW-direct-to-refcat (the pooled/per-detector external solve). Must remain a
  supported path so SW-only reductions still align.
- **LW detector too source-starved to anchor** → widen `ref_border`, or fall back to
  a pooled-LW (A+B shared) anchor for the exposure. Quality-gated.
- **SW↔LW too few common sources** (blue SW filter on a red field) → fall back to
  SW→external-refcat using the LW-derived starting WCS.
- **A single SW detector source-starved** → fall back to shift-only, or inherit the
  module's LW solution unchanged (`dof=inherited`), rather than fitting a poorly
  constrained rotation.
- **Single-member / missing-detector groups** → existing per-detector fallback.

---

## 9. Open questions & footguns for review

1. **Per-SW-detector rotation robustness.** D2 fits full shift+rotation per SW
   detector (~1.1′, undersampled). Is the dense LW reference reliably enough to
   constrain θ per chip across real fields? What is the minimum matched-source count
   below which we must drop to shift-only or inherit? Is there a risk of a plausible
   but wrong per-detector rotation passing the quality gate?
2. **A/B independence vs downstream mosaic.** Solving modules A and B independently
   can leave a small relative A↔B rotation/shift. Does `combine`/resample assume a
   single consistent per-exposure frame anywhere? Is per-module independence safe end
   to end, or should A/B share rotation and differ only in shift?
3. **SW inherits LW's absolute error.** Every SW detector in a module is tied to the
   same LW solution, so LW's residual to the sky is a *correlated* error across all
   its SW detectors. Intended (registration > absolute), but confirm it doesn't bias
   downstream absolute astrometry beyond spec.
4. **Astrometric color terms.** SW centroids matched to LW centroids of the same
   object assume no color-dependent centroid offset. Negligible at this precision?
5. **LW-derived reference quality.** False positives / blends in LW detection would
   inject bad references for SW. Are the §5.4 cuts sufficient on the reference side?
6. **State & idempotency.** SW Stage 2 depends on the LW canonical's corrected WCS +
   `WCS_BAK`. Re-running align with `--overwrite`: does SW re-derive from the LW
   canonical correctly? If LW is *re-aligned* later, SW becomes stale — do we detect
   and re-run SW, or is LW-final enforced?
7. **Interaction with `--tiles` (#329).** Tile pre-filtering selects exposure groups
   overlapping a tile. Ensure both the LW anchor and its SW leaves for a tile are
   selected together, or that Stage 2 can find the LW anchor even if tile filtering
   dropped it.
8. **Frozen-canonical / `nircam_work` model (#261 N7).** Align writes the *canonical*
   WCS (with `WCS_BAK`). Confirm Stage 2 reads the aligned LW canonical's corrected
   WCS, and that the working-copy materialization in `combine` picks up both channels'
   updated WCS.
9. **Reference frame consistency.** LW anchors to the external refcat; SW anchors to
   LW. Confirm both express the same tangent-plane/units so the composed transform is
   exact.

---

## 10. Affected files (implementation sketch)

- `orchestrate.py::run_align` — discover module units; drive Stage 1 then Stage 2;
  footprint-filter the refcat per unit; enforce LW-before-SW ordering + fallbacks.
- `solve.py` — replace `solve_exposure_group` (pooled) with `solve_lw_anchor` and
  `solve_sw_to_lw`; add the `XYXYMatch` refine loop; keep `tristars` bootstrap.
- `apply.py` — build the LW-derived reference from an aligned LW canonical; SW
  starting-WCS construction from the module LW solution; extend `CFP_ALGN` provenance.
- `detect.py` — add SNR/sharpness/roundness/mag-range cuts; drop the `brightest` cap.
- `matcher.py` — density-matched bootstrap subset (the even-spread fix stays as the
  fallback for any pooled/rank-less catalog).
- refcat helpers — footprint clip to a detector footprint + margin.
- config defaults + tests (`tests/test_align_*`).

---

## 11. Versioning / changelog

Per repo policy this is an **Algorithm** change (it alters which sources match and
therefore the fitted WCS) → MINOR. The align phase is opt-in and default-off, so no
existing (JHAT) reduction changes. One `## Unreleased` → Algorithm entry covers the
rework before the PR opens.
