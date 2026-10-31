# Design: the NIRSpec pipeline, version 2 (extraction v2 + master background)

**What this is.** The one maintained design document for the redesigned CAMPFIRE NIRSpec
reduction. It covers the master background, the empirical bar shadow, stuck-shutter detection,
the trace maps, the first pass, contamination and the final profile extraction, and the plots the
eventual pull request must carry. Much earlier documentation was lost when `/private/tmp` was
wiped at a reboot (the extraction report's LaTeX sources, the round 1 to 8 findings, the
calibration-star scripts). This document replaces all of it. Keep it current: every decision,
result and figure path goes here.

**Branch.** `claude/extraction-v2` (worktree `.claude/worktrees/extraction-v2-work`), which merges
`claude/loving-bell-cpxlbg` repeatedly. Merges to `main` only as a MAJOR pipeline release,
together with the full-archive re-reduction.

**Related.** [Master-background feasibility study and results](design-nirspec-master-background.md)
(owned by the master-background session; its section 9 holds the measurements this document
summarises). The [emission-line fitting design](design-emission-line-fitting.md) reads the
products this pipeline writes.

**Status labels used below.**
- **Decided** (user, date): the user ruled on it.
- **Proposed** (by whom, date): written down, not yet approved.
- **Built** (commit): code exists on the branch.
- **Prototyped** (path): run as a script on real data, not in the pipeline.
- **Open** (owner): not settled.

Owners: **MB session** is the master-background session (branch `claude/loving-bell-cpxlbg`).
**Extraction session** is the forward-model session (branch `claude/extraction-v2`). The user
approves final decisions.

Dates are local (EDT) dates. Where sources disagreed, the most recent dated user decision is
used and the conflict is listed in section 9.

---

## 1. Purpose and status summary

The pipeline today extracts each source from nod differences with a fixed optimal profile, treats
every source as a point source, and ignores light from other slitlets. On the gratings, traces
sit edge to edge (38 % of each G395M detector is under more than one slitlet's light), so this
leaves other sources' light in many spectra. The redesign does four things:

1. It measures the sky of every exposure from its empty shutters (the master background) and
   paints it back onto the detector, including where several slitlets overlap and past the
   calibrated wavelength range.
2. It finds stuck-closed shutters with a per-shutter fit to that sky, on every disperser.
3. It fits every source with a physical forward model (a Gaussian galaxy blurred by the
   star-measured PSF) whose shape is fixed by a chain of rules, not left free.
4. It separates overlapping sources with a joint linear solve over small neighbourhoods, and
   reports the other sources' light as its own column instead of hiding it.

| Component | Status | Owner | Where |
|---|---|---|---|
| Release: MAJOR, merged only with the full re-reduction | Decided (user, 2026-10-24 and 2026-10-29) | both | section 8 |
| Stage 2a single `Spec2Pipeline` call per rate file, `bkgslits/` | Built (loving-bell, in CHANGELOG Unreleased) | MB session | 3.1 |
| Empirical bar-shadow tables, PRISM + G140M/G235M/G395M | Built (7ab786bf, 47d19c46, fea58a6e, b0c08026); not signed off by the user | MB session | 3.2 |
| No bar-shadow division of science frames | Decided (user, 2026-10-29) | both | 3.2 |
| Master background, calibrated sky + painting (`_mbkg.fits`) | Built (0d9dd3c7, 53b1c5af, 2f898100, 65c1d825) | MB session | 3.3 |
| Master background split into step 1 (before 2a) and step 2 (end of 2a) | Decided (user, 2026-10-30); not built | MB session | 3.3, 3.4 |
| Step 2 reselects clean shutters in calibrated units | Decided (user, 2026-10-30) | MB session | 3.3 |
| Interface between step 1 and step 2 (`_mbkgr.fits`) | Proposed (MB session, 2026-10-30) | MB session | 3.4 |
| Per-slitlet sky scale off permanently | Decided (user, 2026-10-30); Built (1bd8d7c6) | both | 3.3 |
| Sky painted past both ends of the calibrated range, nothing masked | Decided (user, 2026-10-30); Built (65c1d825, 5b8d6851) | MB session | 3.3 |
| Smoothing the per-exposure grating sky | Open: trial only, not approved (user, 2026-10-30) | MB session | 3.3 |
| Stuck-closed shutters by a per-shutter template fit, all dispersers | Decided (user, 2026-10-30); fit module Built (8ae424bf on `claude/stuck-shutter-fit`, matches the prototype to 1e-7 on p1 G395M, same 21 calls); not wired into step 1 yet | MB + extraction | 3.5 |
| Stuck list keyed by physical shutter everywhere, table-form entries, origins manual / web / auto | Decided (user, 2026-10-30), incl. the ownership split; loader and writer details Proposed | MB + extraction | 3.5 |
| Deploy pull mirrors the web: a mark cleared on the web clears the local web entry (manual and auto never touched; kept when the DB has no web rows for the observation) | Decided (user, 2026-10-30); Built (36cf3b5e) | extraction session | 3.5 |
| `spectrum_exposures.msa_shutters`, filled by deploy and by the backfill for older rows | Decided (user, 2026-10-30); Built (36cf3b5e, backfill extended) | extraction session | 3.5 |
| nods.pdf prints each shutter's physical label and the fit's call | Decided (user, 2026-10-30); Built (93cb2aea, ffa50361); the fit's calls appear once step 1 writes them | extraction session | 3.5 |
| Trace maps (version 7) followed past both ends of the calibrated range | Built (1d946bb3, e3af87b0) | extraction session | 3.6 |
| Forward-model profile extraction on by default | Decided (user, 2026-10-29); Built (4ca99b5a) | extraction session | 3.7 |
| Shape chain: own fit above the gate, else PRISM shape, else default | Decided (user, 2026-10-22/23); Built (49d0fe83 and earlier) | extraction session | 3.7 |
| Instrument model (PSF, shutter geometry) fixed, never fitted | Decided (user, 2026-10-24) | extraction session | 3.7 |
| First pass on master-background-subtracted single frames | Decided (user, 2026-10-29); Built as a module (c72d10cb, 0f466d6c, 5b8d6851), not wired into stage 3 | extraction session | 3.8 |
| Pass-1 contamination mask | Replaced (user, 2026-10-30): it is circular on G395M | extraction session | 3.9 |
| Joint linear solve of local neighbourhoods | Decided (user, 2026-10-30); Prototyped (`joint_scope/`, `joint_real2/`, `joint_sim/`); old group solve Built but off | extraction session | 3.9 |
| Contamination past the calibrated range fitted in count-rate units | Decided (user, 2026-10-30); Built in part (`profile_extend.py`, e3af87b0) | extraction session | 3.9 |
| `fnu` not contamination-subtracted; `fnu_contam` separate, NaN where inseparable | Decided (user, 2026-10-30); Built (af58617f: `contam_1d = "report"` default, `"subtract"` opt-in) | extraction session | 4 |
| Column set `fnu`, `fnu_opt`, `fnu_3px/4px/5px`, `fnu_prof`, `fnu_contam` | Decided (user, 2026-10-29); Built (686eedaf) | extraction session | 4 |
| `_pf.fits` deleted; `PROF2D` + `CONTAM2D` kept in `spec.fits` | Decided (user, 2026-10-29); Built (4ca99b5a, 2916c182) | extraction session | 4 |
| "Unpredicted light" removed | Decided (user, 2026-10-29); Built (dbe63ef0, bca17510) | extraction session | 3.9 |
| No per-exposure sky-difference term in nodded products | Decided (user, 2026-10-29) | both | 3.3 |
| Master-background spectrum as a secondary product, archive-wide | Decided in principle (user, 2026-10-29); not built | extraction session | 4 |
| Per-source runs give the same products as full runs | Decided (user, 2026-10-29); Built (a7094d9e) | extraction session | 3.9 |
| Stage 1 detector-background mask extended past the calibrated range | Open (recommended in the merge plan) | MB session | 3.1 |

---

## 2. Pipeline order, end to end

Each step lists what it reads and what it writes. "Target" is the agreed design. "Today" says
what the branch does now where it differs.

1. **Stage 1 (detector).** Reads the uncal files. Writes `<root>_<nod>_<nrsN>_rate.fits` with
   the detector background removed outside every open shutter's footprint (`CFBKG`,
   `CFBKGMASK`). Unchanged by this redesign, except the open item of extending that footprint
   past the calibrated range (section 3.1).
2. **Master background, step 1 (count-rate units, before stage 2a).** Per exposure group. Reads
   the rate files, the original MSA metafile, the failed-open shutter list (msaoper) and the
   bar-shadow table. Does one `assign_wcs` pass per rate file, shared with the overlap counter.
   Measures the count-rate sky (`MBKG1DR`) from empty shutters lit by one slitlet only, and
   count-rate quadrant scales. Fits every commanded-open shutter's sky template to find
   stuck-closed shutters. Writes `<base>_mbkgr.fits` and auto entries in the stuck-shutter list.
   Today: does not exist; `MBKG1DR` is computed inside the single master-background step at the
   end of stage 2a, and stuck shutters come from the old `low_frac` detector inside stage 2a.
3. **Stage 2a (calibration).** Reads the rate files and the stuck list (already including step
   1's calls). One `Spec2Pipeline` call per rate file. Writes one canonical cutout per source
   (`<root>_<nod>_<nrsN>_<sid>.fits`, point-source calibration, MJy) and the MSA plan's
   background-only slitlets (`bkgslits/<root>_<nrsN>_bkgslits.fits`, MJy/sr).
4. **Master background, step 2 (calibrated units, end of stage 2a).** Reads step 1's
   `MBKG1DR` and quadrant scales, the canonicals and `bkgslits/`. Sees the stuck state only
   through the canonicals' shutter state. Reselects its clean shutters in calibrated units,
   measures the calibrated sky (`MBKG1D`), paints the sky onto the detector (`MBKG2D`), writes
   QA. Writes `<base>_mbkg.fits`.
5. **Stage 2b (nod subtraction).** Unchanged: each nod minus the mean of its partners. The
   master background is never used to correct the nodded frames.
6. **Trace maps.** Per exposure and detector: which source's light falls on which pixel, at
   which wavelength, including past both ends of the calibrated range. Reads the rate file's
   WCS and the stuck list. Cached. Target: share step 1's cached `assign_wcs` model. Today:
   built at the start of the stage-3 profile phase (`stage3.py:450`), `trace_overlap.py`.
7. **First pass.** Every source, from single exposures with the master background subtracted
   (no nod differences, so no negative copies). Gives each source's spectrum and shape for
   the contamination model and seeds the final shape. Today: `first_pass.py` exists and is
   tested on p1, but stage 3 still runs the old nod-difference pass 1 (`profile_contam.run_pass1`).
8. **Contamination (joint local solve).** For each source, its neighbours on the trace maps
   (sources whose light shares its pixels) are solved together with it in one linear fit, with
   every shape fixed. Gives each source's own spectrum free of its neighbours, and each
   neighbour's predicted light on the target. Flags wavelengths where two sources cannot be
   told apart. Today: the masked one-pass prediction (`profile_contam.predict`) with a
   gated group solve (`profile_joint`, off).
9. **Final extraction.** Stage 3 resampling (`Spec3Pipeline`), then the nodded profile
   extraction of each source (`profile_fit.py`), with the neighbours' light known from step 8.
   Writes `<obs>_<grating>_<sid>_spec.fits`.
10. **Products.** `fnu` (best), the boxcars, `fnu_prof`, `fnu_contam` (NaN where inseparable),
    `PROF2D`, `CONTAM2D`, and the master-background spectrum as extra extensions (section 4).

Nothing is computed twice (user, 2026-10-30): the count-rate sky step 1 needs is the master
background without calibration; the `assign_wcs` pass is shared by the overlap counter, step 1,
step 2 and (target) the trace maps.

---

## 3. Components

### 3.1 Stage 2a single call and background slitlets

**What and why.** Stage 2a used to call `Spec2Pipeline` once per source. It now calls it once
per rate file with an edited full metafile and splits the result. This is faster, and the same
call extracts the MSA plan's background-only slitlets, which the master background needs.

- Built: loving-bell, CHANGELOG Unreleased ("Stage 2a runs one `Spec2Pipeline` call per rate
  file"). Checked array-for-array against the per-source loop on `ember_cosmos_p1`.
- `bkgslits/` is a regenerable local intermediate (layout kind `nirspec_bkgslits`).
- Open (MB session): extend stage 1's `mask_slits` footprint past the calibrated range, so the
  detector background is not fitted on sky that runs past a trace's calibrated end. Recommended
  in the merge plan (decision 10); not decided.

### 3.2 Empirical bar shadow

**What and why.** The bar shadow is how the MSA bars and shutter edges dim a uniformly lit
shutter, as a function of position across the shutter and of wavelength. The CRDS references
are misplaced by a fraction of a pixel row and have the wrong width, which leaves 10 to 23 %
errors in a sky painted with them. We measure the profile from millions of empty-shutter
pixels instead.

**Decisions.**
- Science frames are never divided by a bar shadow (user, 2026-10-29). The measured bar
  shadow calibrates the shutter edges and the blur after the shutter in the forward model, and
  multiplies the sky model. There is no separate extended-source route. (Earlier the same day the
  user said the correction should "eventually" reach the science frames; the later ruling is the
  one recorded, see section 9.)
- The measured table is used where one exists, CRDS otherwise (user, 2026-10-29).

**Evidence.**
- PRISM table from the seven EMBER prism pointings (8,474 clean shutters, 11.0 M pixels). With
  EGS p1 held out, residuals inside the shutters are 2.5 / 2.3 / 1.5 / 0.9 % per segment against
  17.8 / 10.6 / 17.5 / 3.5 % with CRDS. QA: `~/campfire-data/scratch/claude/bs_prism7_all/empirical_barshadow_prism_qa.pdf`.
- Grating tables from the four SPURS fields. With GOODS-S held out (empirical vs CRDS): G140M
  0.7-1.4 % vs 1.6-13.4 %; G235M 1.2-4.2 % vs 2.2-18.7 %; G395M 1.3-2.6 % vs 2.7-22.9 %. QA:
  `~/campfire-data/scratch/claude/bs_gratings/bs_<grating>_heldout_qa.pdf`.
- The rows just past the outer shutters were painted 2 to 10 times too bright. Two causes: the
  derivation never admitted those pixels (fixed in fea58a6e), and the cell statistic was a mean
  clipped to (-0.5, 2.5), which keeps only the bright tail where noise is comparable to the
  value (fixed in b0c08026: median per cell). All four tables now fall to 0.03 of the open
  level at 0.6-0.7 pitch and 0.01-0.02 beyond. Before/after:
  `~/campfire-data/scratch/claude/bs_v5/residual_vs_f_clean_{before,after}.png`; current QA
  `~/campfire-data/scratch/claude/bs_v5/empirical_barshadow_<disperser>_qa.pdf`.
- Calibration-star result that the forward model already handles the shutter edge (2026-10-17):
  a hard clip at the nominal half-width 0.50 with an even blur split fits off-centre stars best;
  no separate bar-shadow model is needed for point sources.

**Implementation.** `nirspec/barshadow_calibration.py`, `data/Generate_Barshadow_Cals.py`,
tables `data/empirical_jwst_nirspec_barshadow_{prism,g140m,g235m,g395m}.fits`.
`[nirspec.stage2].empirical_barshadow` (default off) passes a table to `BarShadowStep` for the
background slitlets.

**Open.**
- User sign-off on the grating tables (MB session).
- Held-out validation has not been rerun on the b0c08026 tables (MB session).
- Whether the forward model's shutter edge (clip at 0.50, blur split 50/50) agrees with the
  measured uniform-sky profile: edge 50 % point, narrowing with wavelength (open region ±0.45
  pitch at 1 µm, ±0.38 at 5 µm on the prism), floor under the bars (extraction session; merge
  plan T0 item).
- The forward model must not count the shutter edges twice once the measured profile feeds it.

### 3.3 Master background (calibrated sky and painting)

**What and why.** For every exposure and detector, the sky spectrum seen through the MSA,
measured from every open shutter that holds no source, and painted back onto the detector
through each slitlet's geometry and bar shadow. Uses: the first pass (section 3.8), the
master-background spectrum (section 4), and a per-exposure sky record. It is not used to
correct the nodded spectra.

**Decisions.**
- Last sub-step of stage 2a, not a separate stage (user, 2026-10-29). Now split in two steps,
  with step 2 at the end of stage 2a (user, 2026-10-30; section 3.4).
- 1-D sky per exposure × detector, with the exposure group's pooled sky as fallback below
  `master_bkg_min_shutters` clean shutters (user, 2026-10-29). Reason: UDS p2's second exposure
  sits 14 % above its neighbours at 3 µm.
- `MBKG1D` is stored in MJy/sr, but everything downstream is converted to consistent units:
  "in the end everything should be in consistent units; the pipeline's choice of units for
  extended sources is silly" (user, 2026-10-29). `sky_for_slit` returns the slit's own units.
- No per-exposure sky-difference term in the nodded products (user, 2026-10-29). The master
  background is about 3 to 5 % noisier than the nodded background, so it would add noise
  everywhere. Each exposure's scale and offset against the pool go into header cards
  (`MBKGSCL`, `MBKGOFF`) and a QA plot.
- With `extend_g140m_g235m` on, the calibrated region out to 5.3 µm is covered (user,
  2026-10-29).
- The per-slitlet sky scale is off permanently: "that's not master background at that point"
  (user, 2026-10-30).
- No masking anywhere. The sky is painted past both ends of the calibrated range, in
  count-rate units, out to measured wavelength cutoffs (user, 2026-10-30). Past the calibrated
  range, work in count-rate units as much as possible.
- Step 2 reselects its clean shutters in calibrated units, option (ii), "more robust" (user,
  2026-10-30). Both sessions had leaned to reusing step 1's set; the user chose otherwise.
- Smoothing the per-exposure grating sky: a trial only, not approved. Some extra noise from the
  master background is expected (user, 2026-10-30).

**Evidence.**
- PRISM on `ember_egs_p1` and `ember_cosmos_p1`: about 90 % of empty shutters clean; each
  exposure within 0.2-0.4 % of the pool in scale; quadrant term ±2.5 %; shutter-to-shutter
  scatter ±4 %, of which about 2.7 % is a per-shutter floor that nodding cancels and an ensemble
  sky cannot. Master-background study section 9.
- G395M: the grating sky agrees with the prism sky in the same field to 1-5 %. Light runs past
  the nominal red end by about 200 px on NRS2; ignoring it made 34 % of NRS2 shutters look 1.4
  times too bright. Study section 9, figures `docs/figures/master-bkg-prototype/ember_cosmos_p1_g395m_*`.
- Wavelength cutoffs, measured where the light past the calibrated end falls below 2 % of the
  in-band level (`WAVELENGTH_CUTOFFS`): PRISM 0.55 / 5.6 µm; G140M/F100LP 0.96 µm / detector
  edge; G235M/F170LP 1.55 / 5.05 µm; G395M/F290LP 2.60 / 5.7 µm. Figure
  `~/campfire-data/scratch/claude/bs_v3/cutoffs.png`; trace ends
  `~/campfire-data/scratch/claude/bs_v3/trace_ends.png`, `dispersion_edges_*.png`.
- Painting past the range joins the in-range painting at the per-cent level (`MBKGRRAT`
  0.988-1.007 on the twelve p1 products; 0.968-1.007 after b0c08026).
- Accuracy past the range (measured 2026-10-30, before the b0c08026 median fix), pixels lit
  by one slitlet, `(data - painted) / painted`: inside the calibrated range +2.2 to +2.6 %;
  G395M blue extension +7 % (2.75-2.87 µm, the filter cut-on); G395M NRS2 red extension +5 %;
  PRISM +3 / +4 %. Figure `~/campfire-data/scratch/claude/bs_v3/extension_accuracy.png`.
- Painted sky against the rate after b0c08026: GOODS-S margins to 0.01; p1 G395M 0.02-0.05
  low at the margins.

**Implementation.** `nirspec/master_bkg.py` (`run_master_bkg`, `sky_for_slit`, `load_product`),
`nirspec/coverage.py` (`slit_geometry_extended`, `slitlet_geometry_extended`,
`coverage_from_wcs`, `WAVELENGTH_CUTOFFS`), called at `stage2.py:384`. Config
`[nirspec.stage2] master_bkg_*`. Product `<root>_<nod>_<nrsN>_mbkg.fits` (layout kind
`nirspec_mbkg`, about 20 MB): `MBKG1D`, `MBKG1DR`, `SHUTTERS`, `MBKG2D`, `MBKG2DN`, `MBKG2DDQ`.
`sky_for_slit(product, path, extver=1, table=None, mode='total')` is the stable consumer
interface; it returns `sky`, `var`, `sky_rate`, `rate`, `calibrated`, `own_sky`.

**Open.**
- The +2.5 % offset inside the calibrated range: the painted sky is right at shutter centres,
  so the shortfall is in the profile between centre and bars. Measured before b0c08026;
  remeasure (MB session).
- The G395M blue extension (+7 % at the filter cut-on) (MB session).
- Bimodal clean-shutter scales on G395M NRS1 (0.95 / 1.15), unexplained (MB session).
- Grating sky smoothing, trial only (MB session; user decides).
- Compressed `MBKG2D`; deploy/pull of the kind (MB session).
- Stuck shutters not in any list painted sky where no light arrives on p1 G395M; fixed by
  section 3.5 once built.

### 3.4 Master background split: step 1 and step 2

**What and why.** Stuck-shutter detection needs a count-rate sky and the full slitlet geometry,
which are exactly what the master background already builds. Running that part before stage 2a
lets stage 2a run once with the final stuck list, so nothing downstream has to be redone.

**Decision.** Split approved (user, 2026-10-30). Step 1 before stage 2a, from rate files only:
the `assign_wcs` / extended-geometry pass shared with the coverage counter, the count-rate sky
from empty single-coverage shutters (`MBKG1DR`), count-rate quadrant scales, and the
stuck-shutter template fit. Step 2 at the end of stage 2a: calibrated sky (`MBKG1D`) and
painting (`MBKG2D`), reusing step 1's geometry and stuck list, reselecting its clean shutters in
calibrated units.

**Proposed interface** (MB session, 2026-10-30; not yet approved by the user).

A. Step 1 writes one product per rate file, `<base>_mbkgr.fits`, next to `<base>_mbkg.fits`.
- `PRIMARY`: the version, and the cards its skip key uses: the rate file's `CFBKGDT`, the
  metafile name and id, the msaoper reference, the bar-shadow stamp `MBKGBSDT`, the cutoffs, the
  stuck-list file time, and the decision thresholds.
- `MBKG1DR`: the count-rate sky. It moves here from `_mbkg.fits`, unchanged.
- `MBKGQSR`: count-rate quadrant scales. Step 2 uses these past the calibrated range.
- `MBKGSHR`: the count-rate shutter selection, with the same columns as today's `SHUTTERS`.
- `SHUTAMP`: one row per commanded-open shutter: `q`, `row`, `col`, `slitlet_id`, `j`,
  `source_id` (negative for a background slitlet), `role`, `detector`, `n_pix`, `lam_lo`,
  `lam_hi`, `a`, `a_err`, `call`.
- `CALLS`: the call per root, combining both detectors and the three nods, made once per
  exposure group.
- Geometry is not stored (about 400 MB per frame). The rate model after `assign_wcs` (a few MB)
  is cached under `$CAMPFIRE_ROOT/cache/coverage/`, keyed like the coverage counter. The
  counter, step 1 and step 2 all evaluate geometry from it. If that proves slow, a compact
  per-slitlet cache is the fallback.

B. Step 2 reads `MBKG1DR` and `MBKGQSR` from `_mbkgr.fits`. It sees the stuck state only
through the canonicals' shutter state, because stage 2a has applied the list. It reselects its
clean shutters in calibrated units, then writes `MBKG1D`, the painting and QA as now. Its skip
key adds the `_mbkgr` stamp (date and a hash of `CALLS`). `SHUTAMP` lives in step 1 only; step
2 copies two count cards.

C. How the stuck list reaches stage 2a. **Decided (user, 2026-10-30): physical-shutter keys
everywhere** (the MB session had proposed keeping source × shutter ordinal; the user chose the
alternative). Format, loader and writer in section 3.5.

D. Order within stage 2a.
1. Rate files are final.
2. Step 1 per exposure group: `assign_wcs` cache, count-rate sky, template fit, drop the dark
   shutters from the sky and recombine once, calls per root, then write the auto entries of the
   stuck list (skipped when the merged content is unchanged).
3. Canonicals are built with the list applied.
4. Calibration.
5. Step 2.
The stuck list's file time (`STKSHTIM`) already drives the canonicals' skip key, so a new call
reruns exactly the affected observations.

E. Ownership. For A and B (proposed): MB session takes A, B and the skip keys. For the stuck
list (decided by the user, 2026-10-30): see section 3.5.
- Proposed signatures: `fit_shutters(rate_path, geometry, rate_sky, qscales, table, msaoper) ->
  Table` (the `SHUTAMP` columns) and `call_shutters(tables_of_group) -> calls`.

**Notes from the MB session (2026-10-30).** Count-rate units carry no flat field, so shutters
scatter about 7 % more from slitlet to slitlet; step 1's rejection thresholds need retuning, or
real throughput differences get rejected and the G395M pool (75-135 single-coverage shutters per
frame) shrinks. Step 1 must measure its own quadrant scales in count-rate units.

**Open.** User approval of A, B, D, E (MB session to present).

### 3.5 Stuck-closed shutters

**What and why.** A stuck-closed shutter passes no light. If the pipeline thinks it is open, the
sky is painted where no light arrives, the shutter counts as sky in nod subtraction, and a source
in it is extracted from nothing. Stuck shutters are transient: the MSA is reconfigured between
configurations (EMBER 03101 vs 05101), so a list never carries over from one configuration to
another. The old detector (fraction of low pixels in a shutter's rows) works on PRISM and fails
on the gratings, where other traces fill a dark shutter's rows.

**Method.** For each rate frame, fit
rate = c + Σ_s a_s T_s,
with one sky template T_s per commanded-open shutter: the count-rate sky × quadrant scale × bar
shadow, over the shutter's full extended footprint, including overlaps and the light past the
calibrated range. Failed-open shutters enter as free nuisance templates. An open shutter has
a ≈ 1, a stuck one a ≈ 0. The fit is weighted, clipped (six rounds at 4σ) and solved densely.

**Decisions** (user, 2026-10-30).
- The template fit replaces the `low_frac` detector outright, for every disperser.
- Rule per physical shutter of one configuration, over measurements with at least 200 fitted
  pixels: **stuck** if the median a over nods × detectors is below 0.3 (at least 2
  measurements); **review** if the median is 0.3-0.8 or the detectors disagree by more than 0.5;
  **open** at 0.8 or above.
- Entries made by people keep winning: precedence manual > web > auto.
- nods.pdf must print each shutter's physical label and the fit's call, so users can enter
  stuck shutters by hand from the plots.
- The user reviews the candidates from the nods.pdf pages before the thresholds are trusted.
- Keys: physical shutter everywhere (item C of section 3.4), in the TOML, in Supabase, on the
  web and in deploy.

**The stuck list format.** Decided (user, 2026-10-30).
- *Key:* `"q{quadrant}r{row}c{col}"`, unpadded, matching `^q([1-4])r(\d+)c(\d+)$`. Row and
  column are the MSA metafile's `shutter_row` (along the dispersion direction) and
  `shutter_column` (along the slitlet). They are MSA shutter coordinates, not detector rows or
  columns.
- *Scope:* per root, that is one MSA configuration, shared by its nods.
- *Value:* always a table. Required `origin` and `stuck`; optional `a` (the fitted amplitude)
  and `note`. Example: `"q2r195c64" = {origin = "manual", stuck = true}`, or as the writer emits
  it, `"q2r195c64" = {origin = "auto", stuck = true, a = -0.02}`. There is no shorthand string
  form. Provenance lives in the value, not in trailing comments.
- *Origins:* `"manual"` (typed by a person, for example from nods.pdf), `"web"` (from the web
  nods review) and `"auto"` (from the template fit). Precedence manual > web > auto.
- *Veto:* `stuck = false` from a manual or web entry overrides an auto call (for example a
  false alarm), so a wrong auto call is not reinstated on the next run.
- *Supabase:* the jsonb in `nirspec_source_review.stuck_shutters`
  (`supabase/schemas/tables.sql:1121`) mirrors the TOML, for example
  `{"q2r195c64": {"origin": "web", "stuck": true}}`. That shape is the contract shared by the
  pipeline, the web and deploy.
- *Legacy entries* (`sid = [ordinals]` with a trailing tag) are mapped through the root's
  metafile: quadrant and row from the source's slitlet, column = slitlet's lowest column +
  ordinal - 1. A `hand` tag or no tag maps to origin `"manual"`.
- *Needed:* a Supabase migration extending `nirspec_source_review.stuck_shutters`; the web nods
  review (`web/lib/nirspec-nods.ts`) and deploy's pull-stuck-shutters merge moved to physical
  keys.

**Ownership.** Decided (user, 2026-10-30).
- MB session: the TOML schema, the loader, stage-2a use (including background-only slitlets)
  and the auto writer.
- Extraction session: the Supabase migration, the web nods review, the deploy merge, the fit
  module (`fit_shutters`, `call_shutters`) and the nods.pdf labels.

**Loader and writer.** Proposed (agreed between the two sessions, 2026-10-30; awaiting the user).
- `load_stuck(obs) -> {root: {(q, r, c): Entry(origin, stuck, a, note)}}`, after precedence.
  Within one origin, a physical-key entry beats a legacy entry for the same shutter. The loader
  takes the observation because it needs the metafile to map legacy entries. Legacy entries it
  cannot map are reported, not dropped.
- `stuck_set(obs, root) -> {(q, r, c)}`, the shutters with `stuck = true`. Stage 2a and
  master-background step 2 consume this.
- The auto writer uses tomlkit and rewrites only auto entries. It does not write when the merged
  content is unchanged, because the file time is the canonicals' skip key. It never converts
  legacy entries; conversion to physical keys is a separate one-off command, so the user sees
  that diff on its own.

**Evidence** (`ember_cosmos_p1`, report `~/campfire-data/scratch/claude/stuck_shutters/REPORT.md`,
table `aggregate.md`).
- PRISM 03101: all 15 hand-vetted shutters at median a = 0.00-0.02; no other of 561 shutters
  below 0.92; no single live measurement below 0.3.
- G395M 05101: 1338 shutters; live shutters 16-84 % at 0.92-1.09, 1 % quantile 0.83. 23 below
  0.3: 2 artefacts (NRS1 templates that are blue stubs in the filter cut-on; the detector
  disagreement rule catches both) and 21 credible: 7 also vetted on PRISM, 14 new. Single
  measurements are not decisive on G395M (35 of 6039 live below 0.3; 11 of 129 dark at or above
  0.3); the median is.
- The old detector at its configured threshold finds 1 of 17 dark G395M shutters; at 0.3 it
  finds 3 with 72 false positives.
- Persistence: of 10 PRISM-vetted shutters also in the G395M plan, 7 are dark again and 3 are
  open. A prior, never a substitute.
- Repainting the sky with the 21 shutters closed (`~/campfire-data/scratch/claude/stuck_paint/summary.txt`):
  in the 79-95 k pixels per frame whose painting changes by more than 2 %, the median
  `(rate - painted)/painted` goes from -0.22 to -0.32 to between +0.03 and +0.10. Figures
  `frames_before_after.png`, `zoom_nrs1_40.png`, `zoom_nrs2_465.png` in the same folder.
- Cost: templates about 50 s, fit 10-20 s per G395M frame (1.4 GB peak).

**Implementation.** Prototype `~/campfire-data/scratch/claude/stuck_shutters/{templates,fit,aggregate,refine}.py`.
Today's code: `nirspec/stuck_shutters.py` (`detect_stuck_shutters`, `merge_stuck_shutters`),
called at `stage2.py:304-372`; config `detect_stuck_shutters*`. Trace maps drop stuck shutters
(`trace_overlap.drop_stuck_shutters`) and rebuild on the list's signature.

**Open.**
- The user's verdict on the review packet
  `~/campfire-data/scratch/claude/stuck_shutters/p1_g395m_stuck_review.pdf` (27 shutters). Four
  of the five shutters at 0.61-0.78 are not in it (extraction session).
- Repeat on one G140M and one G235M observation, whose light past the calibrated range is
  longest (extraction session).
- Background-only slitlets have no nods.pdf; they need a plot of their own to be checked by eye
  (extraction session).
- Partly open shutters (0.61-0.78): partial transmission, slit-to-slit throughput, or a source
  in a sky shutter. Undecided how to report them (MB session).
- Whether stage 2a's column removal reaches background-only slitlets (MB session).

### 3.6 Trace maps

**What and why.** For every exposure and detector, a map of which sources' light lands on
which pixels, at what wavelength and through which shutter. The contamination model and the
first pass need it. Maps follow each trace past both ends of the calibrated range, because
source light continues there just as the sky does.

**Decisions.**
- Traces fully extended; gaps inside the range do not matter, because a chip gap in the
  contaminant is the same chip gap on the spectrum it contaminates; a contaminant without a
  spectrum means something is wrong (user, 2026-10-29).
- Maps name each slitlet by its source at every dither point, and leave out stuck-closed
  shutters (Built, dbe63ef0).

**Evidence.** p1 maps `~/campfire-data/scratch/claude/extraction-v2/maps_p1/` (`traces_per_pixel.png`,
`named_<sid>.png`).

**Implementation.** `nirspec/trace_overlap.py` (`TraceMap`, `get_trace_map`, map version 7),
extended with `coverage.geometry_on_block` and `WAVELENGTH_CUTOFFS` (e3af87b0). Built in
`stage3.run_profile_fit_phase` today.

**Open (extraction session).** Build from step 1's cached `assign_wcs` model; register
`tracemaps/` as a regenerable layout kind.

### 3.7 Profile extraction and the shape chain

**What and why.** The final spectrum is a fit of a physical model to every nod difference: a
Gaussian galaxy convolved with the star-measured PSF, clipped by the open shutters, with the nod
partners modelled as negative copies at their own WCS positions. One true spectrum is shared by
all exposures. The reported flux is the light that entered the target's shutter. This replaces
the fixed optimal profile, which loses light for extended and off-centre sources.

**Decisions.**
- On by default (user, 2026-10-29).
- The instrument model (PSF table, shutter geometry, centre law) is fixed and never a fit
  parameter, "not even as a suggestion" (user, 2026-10-24).
- PSF: star-measured, per detector, lower-quartile width ("err on the side of a
  narrower-than-reality PSF", user, 2026-10-16). Table from 74 NRS1 / 61 NRS2 calibration
  stars (programs 12497 and 6642). Centre law: trace drift by MSA quadrant plus a red pull
  toward the shutter centre (2026-10-17).
- Galaxy shape: one Gaussian (user, 2026-10-17). A gated two-Gaussian shape was built and
  rejected (user, 2026-10-24; code removed in c40acc9e).
- Shape chain: own fit above the detection gate, else the PRISM shape of the same source, else
  the default shape (user, 2026-10-22: PRISM shape for the gratings "yes"; imaging-based shapes
  "never reliable"). Gates: PRISM 25, gratings 70 (config `gate_snr_*`).
- Success is judged on delivered S/N (integrated over the spectrum, from nod-split noise),
  together with line width and centroid, not on stated error ratios (user, 2026-10-22).
- Nothing that degrades the wavelength solution (msaexp-style 1-D binning of a pixel table is
  out; user, 2026-10-22).
- The flux convention (Gaussian through the PSF, target-shutter light only) goes in the docs,
  not the header (user, 2026-10-24).

**Evidence.** Earlier rounds (2026-10-19 to 2026-10-24) found the extraction unbiased within
about 1 % in injections, with noise 1-8 % above the old stage 3. Those reports and figures were
lost in the wipe; only the numbers in the session notes survive. The real-data joint test
(section 3.9) shows the joint solve failing with default shapes and working with chain shapes,
so the chain is required.

**Implementation.** `nirspec/profile_fit.py` (`PSF_TABLES`, `CENTRE_LAW`, priors: centre 0.15
slit units from the planned position, width median 0.15), `nirspec/profile_chain.py`
(`choose_shape`, `ChainConfig`), `nirspec/profile_product.py`, `stage3.run_profile_fit_phase`.

**Open.**
- Default width 0.15 vs 0.21 slit units (user to decide; raised 2026-10-23).
- Whether the shutter edge needs revising against the measured bar shadow (section 3.2).
- Re-run the injection and shape tests on the merged pipeline, since the old figures are lost
  (extraction session).

### 3.8 First pass

**What and why.** Every source is extracted once from single exposures with the master
background subtracted. A single frame holds each source once, positive, with no negative nod
copies. That makes each source's own spectrum and shape far easier to measure where traces
overlap. The first pass feeds the contamination solve and seeds the final shape. It is the
user's two-pass idea (2026-10-29).

**Decisions.**
- Two passes: first on master-background-subtracted single frames for every source, then the
  final nodded profile extraction (user, 2026-10-29).
- No masking on the master sky in the first pass (user, 2026-10-30; Built 5b8d6851).
- No per-slitlet sky scale (user, 2026-10-30; Built 1bd8d7c6).
- The master-background noise enters only where contamination is subtracted or the
  master-background spectrum is used, never the nodded `fnu` (follows from the 2026-10-29
  decisions).

**Evidence.**
- Geometry: G395M sources with under 30 % clean own-shutter pixels drop from 147 of 279 in nod
  differences to 38 in single frames (p14), 114 of 252 to 28 (p9). Merge plan T0
  (`~/campfire-data/scratch/claude/extraction-v2/merge-plan.md`).
- p1 smoke test, first pass vs nodded (`~/campfire-data/scratch/claude/extraction-v2/first_pass_smoke_v7/first_pass_vs_nodded.{png,txt}`):
  PRISM sources agree to within 0.6σ except bright 631396 (3.8σ); G395M within 0.1-0.8σ except
  631564 (+2.0σ). The remaining excesses (631564, 946728, 632747) are light from neighbouring
  sources that nod differences cancel, not sky. Single-frame errors are 0.79-1.14 of the nodded
  ones in this table. Earlier versions `first_pass_smoke_v3..v6`.

**Implementation.** `nirspec/first_pass.py` (`master_sky`, `load_group_sky`,
`compute_first_pass`, `run_first_pass`). Writes an ordinary pass-1 file
(`profile_contam.pass1_path`). Light past the cutout ends is solved on nod differences of the
rate files in count-rate units (`profile_extend.py`).

**Open (extraction session).** Wire into `stage3.run_profile_fit_phase` in place of the
nod-difference pass 1; replace the boxcar pass-1 fallback with a native 3-row boxcar; decide
the fate of the echo correction and pass 1b once the joint solve is in (they are workarounds
for what the joint solve does properly).

### 3.9 Contamination: the joint local solve

**What and why.** On G395M a pixel often carries light from two or more sources at different
wavelengths. The old approach predicted each neighbour's light from its own pass-1 spectrum,
fitted with every pixel another source also lights masked out. That is circular: the masked
pixels are exactly the ones whose light the prediction needs. The joint solve instead fits a
source and its neighbours together. Each pixel's model is the sum of every source that lands on
it, each with its fixed shape, so the fit is linear in the spectra and nothing is masked. Each
source also has pixels of its own, which pin it down.

**Decisions.**
- The pass-1 mask goes; it is replaced by a joint linear solve of local neighbourhoods (user,
  2026-10-30: "this approach is approved"). This supersedes "joint solve stays off" (user,
  2026-10-29).
- Iterative schemes were never ruled out (user, 2026-10-30). They remain an option, but only
  the variant that refits each source on every pixel its light reaches is equivalent to the
  joint solve (see evidence).
- Contamination past the calibrated range is fitted directly in count-rate units from the
  neighbour's own pixels on the same detector; extraction stays within the calibrated range
  (user, 2026-10-30).
- Wavelengths where two sources cannot be separated give `fnu_contam` = NaN, never 0 (user,
  2026-10-30).
- Predicting a neighbour's grating light from its PRISM spectrum is "careful modelling" and out
  of scope for the archive (user, 2026-10-30).
- Handle the obvious cases; most contaminants are faint (user, 2026-10-22). Flagging and
  dropping nods is too extreme as a default (user, 2026-10-22).
- "Unpredicted light" is removed; a contaminant without a spectrum is an error (user, 2026-10-29).
- With `--source-ids`, neighbourhoods form from all sources but only the requested products are
  written (user, 2026-10-29).
- Runtime: "an additional hour per exposure" is too much (user, 2026-10-30).
- No smoothness prior on the other members, which would push a contaminant's lines into the
  target (user, 2026-10-24).

**Evidence.**
1. *The mask is circular* (`~/campfire-data/scratch/claude/extraction-v2/mask_scope/mask_scope.{png,csv}`,
   all 204 PRISM and 477 G395M sources of p1). PRISM loses nothing. G395M: median 34 % of own
   pixels kept; 226 sources (47 %) fall below 30 % and fall back to unmasked; median 20 % of each
   spectrum has no own pixel left; 225 sources lose more than a quarter of their spectrum; of the
   light a source puts on neighbours, 27 % (median) / 39 % (mean) sits at wavelengths where it has
   no data of its own.
2. *Separability, geometry and noise only* (`~/campfire-data/scratch/claude/extraction-v2/joint_scope/`,
   `coupling.json`, `groups.json`, `joint_scope.png`). A typical G395M source has 7 neighbours
   (at least 100 shared pixels), 14 at most. 11 of 13 sampled targets are fully separable with
   0-16 % error growth, including 946728 and 629810, which the mask removes entirely. 920191 and
   923250 are inseparable over 67 % and 55 % of their spectra; in both the neighbour sits in the
   same MSA row. 47 % of G395M sources have a same-row neighbour, so same row alone does not
   decide it. Adding neighbours of neighbours changed no target's result in 6 of 6 tests, so
   each source is solved with its direct neighbours only. Inseparable wavelengths are flagged
   where the correlation between two sources is below -0.9.
3. *Real data against PRISM* (`~/campfire-data/scratch/claude/extraction-v2/joint_real2/joint2_<sid>.png`,
   `joint_real2.json`; round 1 with default shapes in `joint_real/`). rms deviation from PRISM in
   σ, G395M averaged onto the PRISM grid, chain shapes:

   | Source | Joint, default shapes | Joint, chain shapes | Fitted alone |
   |---|---|---|---|
   | 925582 | 1.39 | 1.56 | 3.96 |
   | 632748 | 2.40 | 1.27 | 1.75 |
   | 925236 | 2.16 | 1.39 | 1.79 |
   | 921035 | 3.16 | 3.21 | 3.47 |
   | 885374 | 1.58 | 1.36 | 1.27 |

   With default shapes, neighbours absorbed the target's light (negative neighbours); with chain
   shapes those went away. 885374 is a same-row pair, inseparable over 2.9-4.05 µm (52 % of
   the spectrum), and the correlation flag marks all of it. The remaining negative neighbours
   (879315, 629834, 925077) are negative even fitted alone; 879315 and 925077 are on stuck
   shutters found in section 3.5.
4. *Simulation with known truth* (`~/campfire-data/scratch/claude/extraction-v2/joint_sim/run1.json`,
   `run1_summary.png`, `run1_convergence.png`, `run1s_realistic_spectra.png`). 116 sources in 8
   neighbourhoods on the real p1 G395M geometry and noise, three shape-error scenarios (exact,
   realistic, pessimistic). Errors in units of the joint fit's error.
   - Joint solve: rms 0.99-1.06 for all sources; bias at most 0.14σ with realistic shapes,
     0.43σ pessimistic; line flux mostly 0.95-1.05 of truth (outliers 1.8-1.9 on 924843 and
     629437, where fitting alone gives 3.5-4.3).
   - Today's masked pass: biased low (-0.4 to -0.5σ typical, tail to -4.7σ) and loses lines
     (median 0.8-0.85 of truth, many at or below 0).
   - Iterating on every pixel a source lights equals the joint answer where it converges (4 of
     8 neighbourhoods) but stalls 2.5-14σ away in the tightly coupled ones after 30 rounds.
     Iterating on own pixels only diverges.
   - Simulated light is the group's own only: no sky errors, no uncatalogued sources, no stuck
     shutters.
5. *Runtime.* The joint solve takes 0.02-0.08 s per neighbourhood (13-17 sources, about 20,000
   unknowns, 0.5 M pixels); building the fit 0.1 s. The full covariance takes 15-30 s and is
   most of the cost; the flags need only parts of it. Loading a neighbourhood's files took
   130-170 s in the prototype, work the first pass does anyway.

**Implementation.** Prototype scripts `~/campfire-data/scratch/claude/extraction-v2/{joint_scope,joint_real2,joint_sim,joint_sim_plots,same_row_pairs}.py`.
Existing code: `nirspec/profile_joint.py` (group solve, `joint_flags`, correlation threshold
`contam_joint_degenerate = 0.9`; groups capped at 6 members, gated, off), `profile_contam.py`
(masked prediction, `predict`, `echo_corrected`), `profile_extend.py` (light past a cutout, in
count-rate units). The target is a per-source neighbourhood solve on first-pass single frames
with chain shapes and no gate.

**Open (extraction session).**
- Write the plan, then the code: neighbourhood per source from the trace maps, chain shapes,
  NaN flags, covariance limited to what the flags need.
- Full-field separability count over all 477 p1 G395M sources.
- What makes a same-row pair inseparable (suspected: trace offset relative to overlap length).
- Rerun the real-data check on p1 once the stuck shutters are fixed.
- A faint target next to a bright neighbour with a wrong shape is the main risk; measured only
  in simulation so far.
- Same-slitlet neighbours (not in the trace maps) stay with the neighbour scan
  (`scan_action = "report_only"`).

---

## 4. Products and units

**Decisions** (user, 2026-10-24 and 2026-10-29 unless noted).
- `SPEC1D` columns: `fnu` / `flam` = the best spectrum; `fnu_opt` = today's optimal extraction;
  `fnu_3px`, `fnu_4px`, `fnu_5px` unchanged; `fnu_prof` = the profile extraction with nothing
  subtracted; `fnu_contam` = the predicted light of other sources, with its own 1-D error, not
  added into `fnu_err`.
- `fnu` is **not** contamination-subtracted by default. `fnu_contam` lets users subtract it, and
  the web may offer a toggle (user, 2026-10-30).
- `fnu_contam` is NaN at wavelengths where the split is not determined, never 0 (user,
  2026-10-30).
- Fallback to `fnu_3px` only for named conditions; any other exception fails loudly.
- No user-facing names with "pf" or "stage3"; name things by what they are. No duplicated 2-D
  data; prefer few, clear outputs over DQ-style masks users will not read.
- `_pf.fits` is deleted after extraction; `spec.fits` keeps `PROF2D` and `CONTAM2D` for
  reproducibility. Readers find extensions by name (2916c182).
- The master-background spectrum is a secondary product, as extra extensions of the same
  `spec.fits`, intended archive-wide; it comes from the extraction's own fit, not a second
  stage 3 (user, 2026-10-29).
- Units: everything downstream in consistent units; MJy/sr exists only inside `MBKG1D`. Past
  the calibrated range everything stays in count-rate units; extraction stays within the
  calibrated range (user, 2026-10-29 and 2026-10-30).

**Per-exposure products.** `<base>_rate.fits` (stage 1); `<base>_mbkgr.fits` (step 1,
proposed); `<base>_mbkg.fits` (step 2); canonicals per source; `bkgslits/` (local,
regenerable); trace maps (cache).

**Open.** The extension names and columns of the master-background spectrum (extraction
session). Web: read `fnu_contam` and `CONTAM2D`, offer the toggle (later; not in this PR unless
decided).

---

## 5. Validation plots for the PR

The user's preferences: 2-D data / model / residual panels first; show the data, not only
metrics; the success metric is delivered S/N (integrated over the spectrum, nod-split noise),
judged with line width and centroid, not stated error ratios. Every figure uses current
products only.

Status: **Exists** (prototype figure on current code), **Redo** (prototype exists but predates
a fix or needs another field or disperser), **To make**.

| # | Plot | What it shows | Pass criterion | Status | Prototype path (under `~/campfire-data/scratch/claude/` unless absolute) |
|---|---|---|---|---|---|
| V1 | Master sky residual, whole frame, per disperser | `(rate - MBKG2D)/MBKG2D` image per exposure × detector, PRISM, G140M, G235M, G395M | median within ±1-2 % over covered pixels; no structure tracking slitlets | Redo (after b0c08026 and stuck fix) | `mbkg_p1/*_2d.png`, `mbkg_p1/residual_vs_level.png` |
| V2 | Master sky residual by overlap count | the same ratio binned by the number of traces covering a pixel (1, 2, 3, 4+) | no trend with overlap count | To make | script `bs_v3/overlap_residual.py` |
| V3 | Master sky residual by wavelength, incl. extensions | the ratio vs wavelength, calibrated range and both extensions, per disperser | inside range within ±1-2 %; extensions within ±5 % where the sky is not near zero | Redo (pre-b0c08026; shows +2.5 % offset, +7 % G395M blue) | `bs_v3/extension_accuracy.png` |
| V4 | Residual across the shutter | painted own-slitlet sky vs rate against position in the shutter, incl. the margin rows | table and data agree to 0.01-0.02 of the open level | Exists (GOODS-S, p1) | `bs_v5/residual_vs_f_clean_{before,after}.png`, `mbkg_p1/residual_vs_f_clean.png` |
| V5 | Trace ends and wavelength cutoffs | rate along the trace past each calibrated end, per disperser and filter, with the cutoffs | cutoffs at the 2 % level of the in-band light | Exists | `bs_v3/cutoffs.png`, `bs_v3/trace_ends.png`, `bs_v3/dispersion_edges_*.png` |
| V6 | Bar-shadow tables vs CRDS, held out | per-segment residual of a held-out field divided by each table | empirical 1-4 % rms vs CRDS 10-23 % | Redo on the b0c08026 tables | `bs_gratings/bs_<grating>_heldout_qa.pdf`, `bs_prism7_all/empirical_barshadow_prism_qa.pdf`, `bs_v5/empirical_barshadow_<disperser>_qa.pdf` |
| V7 | Forward-model shutter edge vs measured bar shadow | the model's response to uniform light against the table | edge, narrowing and floor consistent, or a stated revision | To make | none |
| V8 | Per-exposure sky vs pool, quadrant scales | `MBKG1D` per exposure against the pool; `MBKGSCL` / `MBKGOFF`; quadrant scales | scales and offsets recorded; outliers explained | Exists (product QA) | `<base>_mbkg_qa.pdf` in each workspace; `bs_v3/mbkg_qa_p1_g395m_nrs1.png` |
| V9 | Grating sky vs PRISM sky, same field | G395M `MBKG1D` against PRISM in bands | agree within 1-5 % | Exists (P0) | `docs/figures/master-bkg-prototype/ember_cosmos_p1_g395m_sky_tailaware.png` |
| V10 | Stuck-shutter amplitude histograms per disperser | distribution of a per measurement and per shutter, live vs dark | clean gap between 0.3 and 0.8; live 1 % quantile above 0.8 | Exists (PRISM, G395M); To make (G140M, G235M) | `stuck_shutters/amp_hist.png` |
| V11 | Recall on vetted lists | each hand-vetted shutter's a, per configuration | 100 % of vetted recovered, 0 false on PRISM; G395M candidates confirmed by the user | Exists as table; figure To make | `stuck_shutters/aggregate.md` |
| V12 | Rate profile under stuck shutters | the rate across a shutter against the open and dark models | the data follow the dark model | Exists | `stuck_shutters/profiles.png` |
| V13 | Repaint before/after with stuck shutters closed | whole frame and zooms of `(rate - painted)/painted` | the -0.2 to -0.55 troughs gone | Exists | `stuck_paint/frames_before_after.png`, `stuck_paint/zoom_nrs1_40.png`, `stuck_paint/zoom_nrs2_465.png` |
| V14 | nods.pdf with shutter labels and calls | every shutter row labelled `q r c` with the fit's call | labels readable; calls match the TOML | To make (review packet has the labels on a cover page) | `stuck_shutters/p1_g395m_stuck_review.pdf` |
| V15 | Trace maps past the calibrated range | which sources light which pixels, incl. extensions | own slitlet named at every dither point; no stuck shutters lit | Exists | `extraction-v2/maps_p1/traces_per_pixel.png`, `extraction-v2/maps_p1/named_*.png` |
| V16 | First pass vs nodded spectra | 2-D panels, then 1-D first pass against the nodded fit, per source | agree within noise except named contaminated sources | Exists (p1 smoke; 1-D only) | `extraction-v2/first_pass_smoke_v7/first_pass_vs_nodded.png`, `extraction-v2/first_pass_smoke_v6/excess_*.png` |
| V17 | Mask scope (why the mask goes) | one row per source, black where the mask removes all own pixels | documents the problem; no pass criterion | Exists | `extraction-v2/mask_scope/mask_scope.png` |
| V18 | Coupling and separability | neighbour counts, group sizes; per target error growth and inseparable fraction | most targets separable; inseparable ones flagged | Exists (13 targets); full field To make | `extraction-v2/joint_scope/joint_scope.png` |
| V19 | Simulation summary | rms, bias and line recovery for every method and shape scenario | joint rms about 1, bias ≤ 0.15σ realistic | Exists | `extraction-v2/joint_sim/run1_summary.png` |
| V20 | Simulation convergence | iterative methods' distance from the joint answer per round | documents why iteration is not used | Exists | `extraction-v2/joint_sim/run1_convergence.png` |
| V21 | Simulated spectra | truth and every method's spectrum for two neighbourhoods | joint follows truth, lines kept | Exists | `extraction-v2/joint_sim/run1s_realistic_spectra.png` |
| V22 | Real data, joint vs PRISM, 2-D panels first | for each hard source: 2-D data / model / residual per nod, then 1-D joint, alone and PRISM | joint closer to PRISM than alone; inseparable spans flagged | Redo (panel order to check; rerun after stuck fix) | `extraction-v2/joint_real2/joint2_<sid>.png` |
| V23 | Contamination NaN for an inseparable pair | `fnu`, `fnu_contam` with NaN spans, correlation, for 885374 / 920191 | NaN exactly where correlation < -0.9; `fnu` sane | To make | none |
| V24 | Joint residuals vs traces per pixel (real data) | (data - joint model)/noise against overlap count | no rise with overlap count | To make | none |
| V25 | Internal consistency | NRS1 vs NRS2 where they share wavelengths; two of three nods vs the third | agree within errors | To make | none |
| V26 | Delivered S/N vs today | integrated S/N (nod-split noise) of the new `fnu` vs the current archive, by disperser and brightness, with line width and centroid | no regression; gains where the old profile lost light | To make | none |
| V27 | Injection tests | recovered vs injected flux, bias and scatter against S/N | bias within about 1 % | To make (earlier results lost in the wipe) | none |
| V28 | Shape tests on bright sources | 5-pixel boxcar shape test; stars against the PSF table | shape within about 1 % | To make (earlier results lost) | none |
| V29 | Master-background spectrum vs nodded spectrum | the secondary spectrum against `fnu` for sources with clean nods | agree within the expected 3-5 % extra noise | To make | none |
| V30 | Runtime per exposure | wall time per phase (step 1, 2a, step 2, maps, first pass, joint, final) | no step adds an hour per exposure | To make (pieces timed: MB about 26 min per pointing at 2 workers; stuck fit about 1 min per frame; joint 0.02-0.08 s per neighbourhood) | `profile_timing` phases |

---

## 6. Open decisions and next steps

| Item | Owner | Waiting on |
|---|---|---|
| Approve the step 1 / step 2 interface (3.4 A, B, D, E) | MB session | user |
| Stuck-list loader and auto writer (3.5): approved in substance (user, 2026-10-30); Built on loving-bell (`stuck_list.py`, 75b654b0; `write_origin`, f4343bc2) | MB session | |
| Physical-key stuck list in the database (`nirspec_stuck_shutters`, migration 20261031011741), web nods review, deploy push/pull, physical deployment snapshot, deploy TOML writers routed through `stuck_list`: Built (36cf3b5e) | extraction session | |
| Run `scripts/backfill_stuck_shutters_physical.py` after the migration is live (dry run first): it writes the physical stuck rows and fills `spectrum_exposures.msa_shutters` for rows deployed before the column (approved by the user, 2026-10-30) | user | migration |
| `nirspec_mbkg` product type missing from the `storage_objects` CHECK (`test_registry` fails) | MB session | |
| One-off command converting legacy stuck entries to physical keys | MB session | |
| Verdict on the G395M stuck review packet; thresholds | extraction session | user |
| Stuck fit on G140M and G235M | extraction session | |
| Build step 1 and the physical-key stuck list, migration, web, deploy | both, per split | approvals above |
| Remeasure the +2.5 % in-range offset and G395M blue extension after b0c08026 | MB session | |
| Held-out validation of the b0c08026 tables; user sign-off on grating tables | MB session | user |
| Grating sky smoothing trial | MB session | user |
| Write the joint-solve plan, then code it; drop the mask, echo and pass 1b | extraction session | |
| Full-field separability count (477 p1 G395M sources) | extraction session | |
| Same-row inseparability cause | extraction session | |
| Wire the first pass into stage 3 | extraction session | |
| Master-background spectrum extensions | extraction session | |
| Default galaxy width 0.15 vs 0.21 | user | |
| Forward-model shutter edge vs measured bar shadow | extraction session | |
| Stage 1 footprint past the calibrated range | MB session | user |
| Runtime baseline on one full observation | both | |
| Rebuild the extraction report as final documentation (main text = what ships; appendices = explored and rejected) | extraction session | |

---

## 7. Implementation map

| Area | Files |
|---|---|
| Geometry, cutoffs, overlap counter | `pipeline/campfire_pipeline/nirspec/coverage.py` |
| Master background | `nirspec/master_bkg.py`; hook `stage2.py:384` |
| Bar shadow | `nirspec/barshadow_calibration.py`, `data/Generate_Barshadow_Cals.py`, `data/empirical_jwst_nirspec_barshadow_*.fits` |
| Stuck shutters (today) | `nirspec/stuck_shutters.py`; hook `stage2.py:304-372` |
| Trace maps | `nirspec/trace_overlap.py` |
| First pass | `nirspec/first_pass.py` |
| Pass 1, prediction (today) | `nirspec/profile_contam.py`, `nirspec/profile_extend.py` |
| Joint solve (today, off) | `nirspec/profile_joint.py` |
| Profile model, shape chain | `nirspec/profile_fit.py`, `nirspec/profile_chain.py`, `nirspec/profile_product.py` |
| Orchestration | `nirspec/stage3.py` (`run_profile_fit_phase`) |
| nods.pdf | `nirspec/plots.py` |
| Config | `pipeline/campfire_pipeline/data/config_default.toml`, `[nirspec.stage2]` and `[nirspec.stage3]` |
| Changelog | `pipeline/CHANGELOG.md`, `## Unreleased` (one Algorithm entry, MAJOR) |
| Stuck list storage | `supabase/schemas/tables.sql:1121`, `web/lib/nirspec-nods.ts`, deploy pull-stuck-shutters |

Running the pipeline on the user's machine: stage 1 at most 6 workers (3 on EMBER uncals, 2 on
deep uncals), stage 2a-sized work up to 12 when nothing else heavy runs, one heavy chain at a
time; isolated roots must set `CRDS_PATH` explicitly; keep scratch under
`~/campfire-data/scratch/claude/`, never `/private/tmp`.

---

## 8. Release

- MAJOR release, merged to `main` only together with the full-archive re-reduction (user,
  2026-10-24; reaffirmed 2026-10-29).
- One categorized CHANGELOG entry covers the pipeline changes (Built, 6c5dba84; the master
  background and bar shadow carry their own Unreleased entries from loving-bell).
- The PR carries the plots of section 5.

---

## 9. Conflicts found between sources

1. **`fnu` subtraction.** CHANGELOG Unreleased and `config_default.toml` (`contam_1d =
   "subtract"`, `contam_1d_min_sigma = 3.0`) subtract the predicted light from `fnu` above
   3σ. The user decided on 2026-10-30 that `fnu` is not subtracted. The decision wins; code
   and changelog must change.
2. **Joint solve.** "Joint solve stays off" (user, 2026-10-29; master-background doc decision
   7, merge plan decision 7, config `contam_joint = false`) is superseded by the approval of
   the local joint solve (user, 2026-10-30).
3. **Iterative schemes.** A session note and a 2026-10-30 message said the user had ruled them
   out on 2026-10-22. The user said on 2026-10-30 he never ruled them out.
4. **Step 2's clean shutters.** Both sessions recommended reusing step 1's set; the user chose
   reselection in calibrated units (2026-10-30).
5. **Stuck-list keys.** The MB session proposed source × ordinal keys; the study report
   recommended physical keys; the user chose physical keys (2026-10-30). A first format draft
   had a shorthand string value and an origin called "hand"; the user decided table-only values
   and the origin name "manual" (2026-10-30).
6. **Bar shadow on science frames.** A 2026-10-29 note says the empirical correction should
   eventually reach the science frames; the later 2026-10-29 ruling is that science frames are
   never divided by it and the bar shadow enters through the forward model. The later ruling is
   recorded.
7. **Tail representation.** A note says the tail becomes one cutoff wavelength written by the
   extraction session; what was built (65c1d825, MB session) is per-disperser
   `WAVELENGTH_CUTOFFS` at both ends. The built version is recorded. CHANGELOG line 107 still
   mentions `TAIL_PX`, which 65c1d825 retired.
8. **Master-background doc.** Its header says "Nothing in this document is implemented" and its
   decisions block says the joint solve stays off and masks tails; both are out of date. Not
   edited here (owned by the MB session).
9. **The +2.5 % painted-sky offset and the +7 % G395M blue extension** were measured before the
   b0c08026 median fix; whether they still hold is unknown.
10. **Merge plan section 2.4** (a per-exposure sky-difference term in stage 2b) was withdrawn by
    the user on 2026-10-29.

---

## 10. Change log of this document

- **2026-10-30.** First version, written from the session notes, the branch code and commits,
  the master-background design doc, the scratch results and the session transcript, after the
  reboot wipe. Includes the MB session's proposed step 1 / step 2 interface, and the user's
  decisions on physical-shutter keys, the stuck-list format and its ownership split (all
  2026-10-30). The stuck-list loader and auto writer are recorded as proposed.
