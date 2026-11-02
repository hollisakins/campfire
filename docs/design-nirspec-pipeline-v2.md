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
| Master background split into step 1 (before 2a) and step 2 (end of 2a) | Decided (user, 2026-10-30); Built (master background 1.2+) | MB session | 3.3, 3.4 |
| Step 2 reselects clean shutters in calibrated units | Decided (user, 2026-10-30) | MB session | 3.3 |
| Step 1's tables inside the rate file, `_mbkgr.fits` retired; `_mbkg.fits` the only extra per-exposure file, compressed | Decided (user, 2026-11-01); Built (master background 1.5) | MB session | 3.4 |
| One trace map per exposure, built in step 1 from every commanded shutter, stored in the rate file, stuck shutters removed when read; replaces the coverage counter and the stage-3 trace-map cache | Decided (user, 2026-11-01); builder Built (`trace_overlap.ShutterMap`, step 1 1.4; 99.7 % of pixel-slitlet pairs as `tracemaps/` on p1); stage 3 reads it (ecaf2f22, after the merge into extraction-v2, user 2026-11-01), `tracemaps/` kept as its working copy. The master background keeps its coverage counter (each slitlet's whole extent, from the same step-1 geometry pass): counting from the padded map admits 27-38 % more G395M empty-shutter pixels that read 3.3 ± 0.4 % high, a neighbour's light past `LIGHT_PAD` (MB study §9, 2026-11-01) | both | 3.4 |
| No per-slitlet and no smooth (row/column) scaling of the master background, any disperser; the opt-in removed from the code | Decided (user, 2026-10-30; reconfirmed and extended to smooth scaling 2026-11-02 after the nod-repeatability and slitlet-left-out tests, MB design doc); Built (1bd8d7c6; opt-in removed on `claude/mbkg-paint`) | both | 3.3 |
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
| First pass on master-background-subtracted single frames | Decided (user, 2026-10-29); Built as a module (c72d10cb, 0f466d6c, 5b8d6851), never wired into stage 3; removed (user, 2026-11-01: superseded by the joint first pass; in the git history) | extraction session | 3.8 |
| Pass-1 contamination mask | Replaced (user, 2026-10-30): it is circular on G395M | extraction session | 3.9 |
| Joint linear solve of local neighbourhoods | Decided (user, 2026-10-30); Built as the joint first pass, the only pass 1 (0a746f33, 8792506e, 45db22af). The masked pass 1, pass 1b, the echo correction and the old group solve (`contam_joint`) are removed (user, 2026-11-01) | extraction session | 3.9 |
| No per-exposure pointing offsets in the profile fit | Built (b6e62302) | extraction session | 3.7 |
| Same-slitlet overrides in stage 2b, by position from `SOURCE_INFO` (0.75 slit units, 0.02 of the sky, unmeasurable neighbours count as bright, no clean partner: keep both and flag) | Decided (user, 2026-11-01); Built (3795e228, 7e7c160f); p1 rerun and PRISM check done | extraction session | 3.9 |
| Shipped errors include the subtracted sky exposures' noise (today about 20 % low with three exposures) | Follow-up (user, 2026-11-01); not built; blocks a fair V26 | extraction session | 3.9 |
| Contamination past the calibrated range fitted in count-rate units | Decided (user, 2026-10-30); Built in part (`profile_extend.py`, e3af87b0) | extraction session | 3.9 |
| `fnu` not contamination-subtracted; `fnu_contam` separate, NaN where inseparable | Decided (user, 2026-10-30); Built (af58617f: `contam_1d = "report"` default, `"subtract"` opt-in) | extraction session | 4 |
| Column set `fnu`, `fnu_opt`, `fnu_3px/4px/5px`, `fnu_prof`, `fnu_contam` | Decided (user, 2026-10-29); Built (686eedaf) | extraction session | 4 |
| `_pf.fits` deleted; `PROF2D` + `CONTAM2D` kept in `spec.fits` | Decided (user, 2026-10-29); Built (4ca99b5a, 2916c182) | extraction session | 4 |
| "Unpredicted light" removed | Decided (user, 2026-10-29); Built (dbe63ef0, bca17510) | extraction session | 3.9 |
| No per-exposure sky-difference term in nodded products | Decided (user, 2026-10-29) | both | 3.3 |
| Master-background spectrum as a secondary product, archive-wide | Decided (user, 2026-10-29 and 2026-11-01); Built on `claude/mbkg-extraction` (`mbkg_extraction.py`; `SPEC1D_MBKG`, `SCI_MBKG`, `ERR_MBKG`, `PROF1D_MBKG`, `MBKGPREF` / `MBKGWHY`, `NEXP` / `NEXPBKG`); end-to-end p1 run pending | extraction session | 4 |
| Per-source runs give the same products as full runs | Decided (user, 2026-10-29); Built (a7094d9e) | extraction session | 3.9 |
| Stage 1 detector-background mask extended past the calibrated range | Open (recommended in the merge plan) | MB session | 3.1 |
| Speed up stage 2's per-source work: the rectification after stage 2a and the nodded subtraction in stage 2b | Goal (user, 2026-10-31); not started; profile first | extraction session | 5 (V30) |

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
   stuck-closed shutters. Writes its tables into the rate file itself (`MBKG1DR`, `MBKGQSR`,
   `MBKGSHR`, `SHUTAMP`, `CALLS`; since 1.5) and auto entries in the stuck-shutter list.
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
  (user, 2026-10-30). 2026-11-02: no smooth row/column scaling either, on any disperser; on
  G395M neither gains anything (the scatter is specific to each shutter) and both add noise
  (MB design doc, 2026-11-02 entry). The opt-in code is removed.
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
`nirspec_mbkg`; since 1.5 the 2-D extensions are compressed losslessly, 2.8 MB on PRISM and
about 8 MB on G395M): `MBKG1D`, `SHUTTERS`, `MBKG2D`, `MBKG2DN`, `MBKG2DDQ`. The count-rate
table `MBKG1DR` is read from the rate file (`load_product` follows `MBKGRATE`).
`sky_for_slit(product, path, extver=1, table=None, mode='total')` is the stable consumer
interface; it returns `sky`, `var`, `sky_rate`, `rate`, `calibrated`, `own_sky`.

**Open.**
- The +2.5 % offset inside the calibrated range: the painted sky is right at shutter centres,
  so the shortfall is in the profile between centre and bars. Measured before b0c08026;
  remeasure (MB session).
- The G395M blue extension (+7 % at the filter cut-on) (MB session).
- Bimodal clean-shutter scales on G395M NRS1 (0.95 / 1.15), unexplained (MB session).
- Grating sky smoothing, trial only (MB session; user decides).
- Deploy/pull of `_mbkg.fits` next to the rate file (MB session; decided 2026-11-01, not built).
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

**Interface** (built; file structure decided by the user 2026-11-01: as few file types as
possible, the calibrated part kept out of the rate file because stage 2a reads the rate file
before step 2 runs).

A. Step 1 writes its tables into `<base>_rate.fits`, after the jwst extensions, as stage 1 does
with `CFBKG` (`master_bkg_rate.write_into_rate`, atomic by copy). All five are written every
time, empty when there is nothing to hold, because a jwst save of the rate file records them in
its ASDF tree and a later rewrite must find every name it recorded (tested:
`test_write_into_rate_survives_a_jwst_save_and_a_rewrite`). `masks.restore_pre_bkgsub` drops
them with the detector background. `<base>_mbkgr.fits` (1.2-1.4) is retired.
- `MBKG1DR`: the count-rate sky. Its header holds the step's cards: the version, and the cards
  its skip key uses: the rate file's `CFBKGDT`, the metafile name and id, the bar-shadow stamp
  `MBKGBSDT`, the cutoffs, the hash of the manual / web stuck entries, and the decision
  thresholds.
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

B. Step 2 reads `MBKG1DR` and `MBKGQSR` from the rate file. It sees the stuck state only
through the canonicals' shutter state, because stage 2a has applied the list. It reselects its
clean shutters in calibrated units, then writes `MBKG1D`, the painting and QA as now. Its skip
key adds step 1's derivation date (`MBKGRDT`). `SHUTAMP` lives in step 1 only; step
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
- No per-slitlet and no smooth row/column scaling of the master background (user, 2026-10-30,
  extended 2026-11-02; Built 1bd8d7c6, opt-in code removed on `claude/mbkg-paint`).
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

**Implementation plan (extraction session, 2026-10-30).** Per disperser, inside
`stage3.run_profile_fit_phase`, replacing pass 1 and pass 1b:
1. *Trace maps*: unchanged (version 7, both ends, stuck shutters left out).
2. *Single frames*: each source's exposures minus the master sky (`first_pass.load_source_sky`),
   in calibrated units; each exposure's conversion between sources is its calibrated frame over
   the count rate.
3. *Shapes*: the chain, once per source: the PRISM shape of the same source when the PRISM
   product took its own fit (PRISM runs first); else the source's own fit on its single frames
   above the grating gate; else the default shape. Per-exposure pointing offsets fitted at that
   shape. Recorded per source (`shape_src`).
4. *Neighbourhood*: the source plus every source whose source shutter lights its fit region
   (trace maps), and their direct neighbours (two steps, capped at 25; one step decided the
   target's covariance, two keep the members' own neighbours modelled).
5. *Solve*: one weighted linear fit of all members' spectra at full resolution, 5σ clipping
   twice, no mask, no smoothing (`profile_joint.design`). Only the target's spectrum is kept;
   each source is the target of its own neighbourhood.
6. *Errors and flags*: the target's columns of the inverse normal matrix (one sparse solve per
   target wavelength, about 1,200), giving its marginal errors and its covariance with every
   member; the members' variances only for the columns it correlates with. A wavelength whose
   most negative correlation with another member is below -0.9 is "not separable" and stored
   in the pass-1 file.
7. *Pass-1 file*: the same format the prediction reads (`write_pass1`), `why = 'joint'`,
   reliable, frames from the target's own spectrum, plus the not-separable mask. The light past
   the calibrated range is solved as now (`profile_extend`, count-rate units).
8. *Final products*: the prediction of other light (`predict`) reads the neighbours' joint
   pass 1 and needs no echo correction (a joint spectrum holds no target light). `fnu_contam` is
   NaN at the target's not-separable wavelengths. `fnu` stays unsubtracted.
9. *Retired*: the pass-1 mask, pass 1b, the echo correction, pass-1 smoothing and the gated
   group solve (`contam_joint`). Kept behind `contam_pass1 = "fixed"` until validation is done,
   then removed.
10. *Performance*: each worker keeps its 60 most recent members. Targets are ordered greedily:
    the next target is the unvisited source whose neighbourhood shares the most members with
    those (f139f690). A breadth-first order ended on far-apart sources and rebuilt 23 of 25
    members per target there; greedy needs 596 member builds for 477 sources instead of 1,366.
11. *Validation*: p1 G395M, every source with a PRISM spectrum against PRISM (joint vs fitted
    alone vs the masked pass), the not-separable fraction over all 477 sources, runtime per
    exposure; tests on synthetic data (a stuck neighbour, a same-row pair, a line in the
    overlap).

**Result: full p1 G395M run (2026-10-31, module 693beb9e + 57ca68ed, 4 workers).** All 477
sources got a joint pass 1; none failed (one crash on short extension ends, fixed in
57ca68ed, then resumed). Shapes: 33 own fit, 444 default; the PRISM shape was not passed in
this run, so the 'prism' source is untested on the full field.
- Not separable: median 0 % of a source's wavelengths, mean 12.7 %. 335 of 477 sources have
  none; 142 have some; 93 have more than 25 %.
- Runtime: 15 s per source per worker where members were reused (about 10 s solve, 5 s per
  member build). The breadth-first order made one worker's last 53 sources take 98 s each,
  and the run 2 h. With the greedy order (f139f690) the whole disperser took 33.5 min on 4
  workers, same results.
- PRISM shapes: 156 of the sources have a PRISM product, but the production PRISM files are
  v0.2 and carry no `PFSHAPE` card, so none was used. The 'prism' shape source is tested only
  once PRISM has been run through the v2 extraction.

**Result: end-to-end p1 run (2026-10-31, 311cace0, isolated root `scratch/claude/e2e_p1`, 6
workers).** Stage 2a with master-background steps 1 and 2, stage 2b, stage 3 with the joint
first pass; PRISM reprocessed first, so G395M took the PRISM shapes. 3 h 14 min in all; no
errors; no source fell back to the boxcar. Stage 3: spec3 15 min, trace maps 1 min, PRISM
joint pass 3.6 min and products 5 min, G395M joint pass 24 min and products 25.5 min, final
extraction 1 min. Figure `extraction-v2/e2e_compare/e2e_vs_prism.png`, numbers
`extraction-v2/e2e_compare/compare.json` (script `e2e_compare.py`).
- Shapes for the 156 G395M sources with a PRISM spectrum: 112 took the PRISM shape, 4 their
  own fit, 40 the default (their PRISM fit was below the gate too).
- Against PRISM (G395M averaged onto the PRISM grid, 2.9-5.2 µm, rms deviation in σ):

  | G395M spectrum | median rms | 90th percentile | median bias |
  |---|---|---|---|
  | old v0.2 `fnu` | 1.40 | 3.89 | -0.11 |
  | new `fnu` (nothing subtracted) | 1.44 | 3.21 | -0.15 |
  | new `fnu_prof - fnu_contam` (142 with a prediction) | 1.05 | 1.53 | -0.01 |

  With the neighbour light taken off, 126 of 142 sources are closer to PRISM than v0.2. `fnu`
  alone is about as close as v0.2, as expected, since it keeps the neighbour light by design.
- 632748 and 925236 (the earlier shape discrepancy): both now take the PRISM shape;
  subtracted rms 0.86 and 0.84 (v0.2: 2.39 and 1.35). 632748's `fnu_contam` is a neighbour's
  emission-line spectrum (4.3 and 4.75 µm) that `fnu` carries as spurious lines.
- Remaining worst cases: 635314 (rms 17 → 4.5): the neighbour outshines the target by
  about 8×, and `fnu_contam` stops at 4.25 µm, so 4.25-4.5 µm keeps the neighbour's light;
  920424 (7.7): matches PRISM by eye, and the rms comes from its strong lines at different
  resolution.
- PRISM: no other target's trace lands on any PRISM target (`PFNCONT = 0` in all 204), so
  `fnu_contam` is NaN throughout, as expected for PRISM. The 13 sources with `PFNBR = 1` are
  same-slitlet candidates from the nod scan (a second object inside the target's own
  slitlet); that light is flagged, not modelled, and is not in `fnu_contam`.
- Why 635314's prediction stops at 4.25 µm (2026-10-31). Its neighbours 632748, 631983 and
  630932 light its main shutter out to 4.76, 4.50 and 4.12 µm, but their pass-1 frames have
  no light past their own cutouts (neighbour wavelengths above 5.20 µm). Two causes in
  `profile_extend.solve_extension`:
  1. The wavelength grid of both ends came from the first exposure's row only. For a
     spectrum across the detector gap that is NRS1, which has no red end, so the red end was
     never solved: none of the 230 G395M sources on both detectors had one. Fixed (grid from
     the first exposure that reaches each end).
  2. Each source's light past its cutout is solved alone, with every pixel another source's
     shutter lights masked. That is the masked first pass the joint solve replaced in range,
     and it fails the same way in crowded rows: 87 % of 632748's NRS2 band is masked and no
     pixel past 5.20 µm survives. Decided (user, 2026-10-31): "the joint solve needs to do
     the full range, not just the calibrated one". The past-cutout unknowns (count-rate
     units) go into the joint solve, so overlapping extensions are separated like the rest.

  Built (5829e460, `JOINT_PASS_VERSION` 2): past the wavelengths a member's fit pixels have,
  its light is a second set of unknowns per end (count rate, native grid, blue then red),
  fitted on the single frames' rate minus `MBKG2D` on the pixels its own trace lights,
  together with every member (`design_full`): every member's extension light on the cutout
  rows (times the receiving pixel's calibration) and every member's in-range light on the
  extension rows (over the giving member's calibration). A datum already in a cutout is used
  once. The unknowns are scaled to unit diagonal before the solve; with an absolute ridge
  set by the in-range columns the count-rate columns (about 20 orders smaller on the
  diagonal) were pinned to zero. Check on 635314 (`extraction-v2/e2e_ext_fix/635314_pred.png`):
  the predicted light on its main shutter now follows the data from 4.25 to 4.55 µm, where
  it stopped at 4.25 µm; what remains is 635314's own light (about 0.25e-10 MJy summed).
  Four targets with their neighbourhoods: 6.4 min in one process.

**Result: stage 3 on p1 with the full-range solve (2026-10-31, 5829e460, 6 workers).**
Figure `extraction-v2/e2e_compare/e2e_fullrange_vs_prism.png`, numbers `compare.json`
(calibrated-range run kept as `compare_calrange.json`). G395M joint pass 32.0 min (was 24.1;
CPU +34 %), PRISM unchanged; no fallbacks. Every G395M source now has both extension ends.
- `fnu_prof - fnu_contam` against PRISM: median rms 1.05 → 0.96σ, 90th percentile 1.53 →
  1.36σ; of 131 sources with at least 50 compared points in both runs, 110 closer to PRISM.
  635314 4.46 → 1.09, 632334 3.99 → 1.32.
- Not separable: more than 90 % of the spectrum in 25 sources (was 4); 15 sources dropped by
  more than 25 points, 6 rose. 630448, 632617, 635047 and 919635 lost their prediction (now
  94-100 % not separable; they matched PRISM at 0.54-1.23σ before).
- Cause, 919635: it and 918842 sit on the same detector rows, slitlets 178 columns (0.32 µm)
  apart, so their traces share pixels over most of their length. The chain was pinned by each
  source's columns that no one else lights (918842's blue end, 919635's red end); each
  source's free unknowns past its cutout now cover the other's, and the split is ambiguous.
  The blue unknowns reach 2.60 µm, but the count-rate sky (`MBKG1DR`) shows no light below
  2.72 µm (7 % at 2.725, 46 % at 2.80, 74 % at 2.875) and none above 5.60 µm (33 % at 5.45,
  10 % at 5.50, 3 % at 5.55). Unknowns where the instrument passes no light are free to
  absorb anything.
- 632334: `fnu_contam` negative above 4.5 µm (about -0.3 µJy), the subtracted spectrum above
  PRISM there. To check after the fix below.

**Correction: the PRISM comparison above flattered the subtraction (2026-10-31).** The rms of
`fnu_prof - fnu_contam` was computed with `fnu_prof_err` and `fnu_contam_err` added in
quadrature, so a noisy prediction enlarges its own error bar and looks closer to PRISM. With
`fnu_prof_err` alone (script `e2e_compare_noinflate.py`), on the same 131 sources:

  | G395M spectrum (full-range run) | median rms | 90th percentile |
  |---|---|---|
  | `fnu_prof`, nothing subtracted | 1.43 | 3.43 |
  | `fnu_prof - fnu_contam`, error incl. the prediction's | 0.98 | 1.37 |
  | `fnu_prof - fnu_contam`, the spectrum's error only | 1.59 | 4.34 |

  Subtracting helps in 52 of 131. The prediction's error exceeds the spectrum's own in 87
  of 131 (median ratio 1.59; above 3 in 29). Where it is smaller (44 sources) subtraction
  helps a little (median 1.38 → 1.32). The earlier calibrated-range numbers (1.05, and the
  1.05 → 0.96 step) carry the same inflation; that run's spectra were overwritten, so they
  cannot be recomputed.
- Why the prediction is noisy, 884662: 78 % of it is not separable from 950032 (the same
  pair geometry as 919635 / 918842). In 950032's own solve its spectrum is then poorly
  determined (median error 2.6e-12, about ten times its other neighbours'), and that
  spectrum, moved onto 884662's trace, is most of `fnu_contam` (pixel-to-pixel scatter
  1.0 µJy against `fnu_prof_err` 0.11). A target's prediction is taken from each neighbour's
  separate solve, so the anti-correlated split between the two lands in full in the target.
- Why 632334's prediction goes negative: nothing in the solve keeps a neighbour's light
  positive. Its red end is mostly 666343 and 633605, faint neighbours whose spectra are
  consistent with zero (median |f|/err 0.85 and 0.95); where their solved light is negative,
  -4 to -25 % of the target's own light is added back. No frame here is a nod difference
  (master sky only), so there is no physical negative image.

**Test: the target's own joint-solve spectrum as its answer (2026-10-31).** Proposed after
the correction above: report T's spectrum from its own joint solve (with that solve's error
and not-separable flags) instead of `fnu_prof - fnu_contam`. Explainer with a simulated pair
(`extraction-v2/e2e_compare/explain_joint_vs_subtract.png`, script
`explain_joint_vs_subtract.py`): when the neighbour's spectrum comes from the same solve,
the two answers are identical (least squares: T solved with C held at its solved value is
T's joint value); only the error differs, and where the profiles are nearly the same the
subtracted answer is off by up to ~9x its stated error. On p1 (`e2e_joint_vs_prism.py`,
`e2e_joint_summary.py`, figure `joint_vs_prism.png`), the saved pass-1 spectrum times the
main-shutter factor, all three scored against PRISM with the same error (`fnu_prof_err`):

  | G395M, 156 sources | median rms | 90th percentile |
  |---|---|---|
  | `fnu_prof` (neighbour light left in) | 1.44 | 2.93 |
  | `fnu_prof - fnu_contam` | 1.56 | 3.99 |
  | target's joint-solve spectrum | 2.22 | 7.38 |

  The joint spectrum is closer to PRISM in 24 of 156. It is worse even where nothing is
  inseparable (117 sources: 1.88 against 1.40), and with its own error it scores 1.31, so
  that error is too small as well. Faint isolated sources (629743, 924455) show why: their
  joint spectrum sits about 0.15-0.2 µJy above PRISM and `fnu_prof` across the band, and is
  noisier. The joint pass fits single frames of rate minus `MBKG2D` with the shape held
  fixed and no local background term, so a residual in the master sky is taken up as source
  light. Not adopted. The same offset is in every neighbour's pass 1 and so in every
  `fnu_contam`; to look into.
- Correction to the 632334 explanation above: the product fit works on nod differences
  (`profile_fit.load_group`, stage-2b partners); only the joint pass uses single frames. Its
  negative prediction is real nod-difference light: 666343 and 633605 contribute -1 to -10 %
  in the nod difference and nothing in the frame alone (negative images from the partner
  nods); 630784's +49 to +68 % in its frames is +13 to +22 % after the partners
  (`e2e_contam_sign.py`).

**The master-sky floor (2026-10-31).** In the joint pass's frames the slitlet's other open
shutters read +0.47 to +0.88 of the source shutter's median pixel for 629743 and 924455
(`e2e_sky_resid.py`). The master-background session's measurement on the same products
(`~/campfire-data/scratch/claude/sky_resid/`): G395M single frames sit +2 to +6 % above the
painted sky across the whole shutter (NRS1 +5 to +7 %, NRS2 +2.4 to +4.8 %; PRISM +1 %),
roughly additive (+0.0003 to +0.0005 DN/s, so largest as a fraction in the blue), plus a
per-shutter spread (16/84: -3 / +17 % NRS1, -5 / +9 % NRS2) that repeats on the same
shutter in other nods. A nod difference subtracts the same pixels and cancels both.
Corrected by the master-background session the same day: on held-out clean shutters the
painted sky is +2.6 % low on G395M (+1.3 to +3.3 per rate file) and +1.1 % on PRISM; the
+5-6 % above came from the pixel selection (JUMP_DET-flagged sky pixels left out, pixels
near sources kept). Two causes, proven by building the sky from half the clean shutters
and measuring it on the other half: the inverse-variance shutter weights (about 2 points on
G395M, 0.2 on PRISM; the per-pixel variance comes from the pixel's own slope, so a pixel
that fluctuated low gets more weight) and the bar shadow applied twice (about 0.9 points
on both). With both fixed the held-out residual is -0.3 % (G395M) and +0.1 % (PRISM).
The per-shutter spread is real and remains. Their fix waits on the user
(`~/campfire-data/scratch/claude/sky_resid/proof.png`).

**Test: the joint solve on nod differences (2026-10-31).** `e2e_joint_nod.py`: members built
from the product's own nod differences (`profile_joint.design` already has the +1 / -1/n
partner terms), same fixed shapes, same neighbourhoods, calibrated range only. 16 targets,
rms against PRISM with `fnu_prof_err` (common) and with the solve's own error (own):

  | Targets | nod, common | nod, own | master sky, common | `fnu_prof` |
  |---|---|---|---|---|
  | 7 faint isolated (629743, 924455, 921026, 880007, 925780, 629350, 629639) | 1.12-2.33 | 0.86-1.14 | 3.58-7.28 | 1.06-1.51 |
  | 3 with a bright neighbour (632334, 635314, 632748) | 2.02-4.19 | 1.51-3.31 | 1.96-4.10 | 2.13-13.9 |
  | 925236 | 1.73 | 1.12 | 2.31 | 1.33 |
  | 5 same-row pairs (884662, 630448, 632617, 635047, 919635) | 7.8-16.2 | 0.91-1.14 | 7.7-15.4 | 1.28-2.78 |

  On nod differences the faint-source offset is gone and the errors are honest (own rms
  about 1). Where a bright neighbour lights the target the errors are still too small
  (1.5-3.3): the fixed shapes. The same-row pairs stay 78-84 % not separable on nod
  differences too, with honest errors; `fnu_prof` is close to PRISM there only because
  their neighbour is faint in the target's aperture, which the data alone cannot show.

**Test 1: where to measure a G395M shape (2026-10-31).** The question (user): whether the
master-sky single frames, with no negative nod copies, are the better place to measure
shapes where traces overlap. How each source's shape is chosen today (`shape_for`, once,
then held during the solve): the PRISM shape when the same source's PRISM product fitted
its own; else its own fit on its master-sky frames, alone (others' light left in), above the
grating gate; else the default. p1 G395M: 342 default, 116 PRISM, 19 own; the products
agree for 458 of 477, and 78 product shapes are the default because masking removed too
many pixels.

Method (`e2e_shape_test.py`, `e2e_shape_tdef_summary.py`, figure `shape_test_tdef.png`): for
the 116 sources with a PRISM shape (PRISM traces do not overlap, so it is the reference),
the joint solve of the neighbourhood on nod differences with the target started from the
default shape, then the target's shape fitted (a) alone on master-sky frames, as today;
(b) on master-sky frames minus the other members' solved light and a leftover sky per
exposure and wavelength bin (median of the slitlet more than 1.2 shutter from the source);
(c) on nod differences minus the other members' solved light, and (c2) once more after
re-solving at (c)'s shape. A first version started the target at its PRISM shape; its
(b)/(c) agreed with PRISM to 0.002 in crowded sources because the neighbours' solved light
already assumed that shape (circular, discarded).

  | | a (today) | b | c | c2 | default shape |
  |---|---|---|---|---|---|
  | fits ending on a bound | 94 / 116 | 67 / 116 | 3 / 116 | 8 / 116 | |
  | median abs. centre error [shutter] | 0.009* | 0.036 | 0.035 | 0.035 | 0.039 |
  | median abs. width error [shutter] | 0.056* | 0.055 | 0.037 | 0.041 | 0.055 |

  (*) over the 22 fits not on a bound, mostly the brightest. Above the gate (38 sources), (c)
  gives 0.033 / 0.027 against the default's 0.038 / 0.055.

- Master-sky frames do not support a shape fit: alone, 81 % of fits end on a bound (the
  width runs to its 0.6 limit, the sky floor read as a wide source); removing the others'
  light and a per-exposure leftover sky still leaves 58 %, from the per-shutter sky spread.
- Nod differences with the others' modelled light removed fit nearly every source (3
  failures) and halve the width error above the gate. The centre barely improves on the
  default; part of that may be the reference, since PRISM is a different MSA configuration
  and the offset from the planned position need not carry over. A second round does not
  help.

**Test 2: do refitted shapes improve the joint spectra (2026-10-31).** `e2e_shape_round.py`,
`e2e_shape_round_summary.py`. The joint solve on nod differences (calibrated range only) for
the 156 G395M targets with PRISM, with today's shapes (round 0) and with 18 bright non-PRISM
sources' shapes from test 1 (c) (not on a bound, above the gate; 630784 among them). Median
rms against PRISM, `fnu_prof_err` for all (with the solve's own error in brackets):

  | Targets | `fnu_prof` | round 0 | round 1 |
  |---|---|---|---|
  | all 156 | 1.44 | 1.60 (1.02) | 1.60 (1.02) |
  | 120 with nothing inseparable | 1.40 | 1.47 (1.02) | 1.47 (1.02) |
  | 18 inseparable over > 50 % | 1.49 | 7.18 (0.97) | 7.20 (0.97) |

- The joint errors are honest throughout (own-error rms 1.02). Where everything is
  separable the joint spectrum is within 0.07 of `fnu_prof`, which keeps the neighbours'
  light.
- Refitted shapes change little: closer to PRISM in 43 of 114 affected targets. 632334
  improves (3.03 → 2.49; its bright neighbour 630784 refitted); 925855 worsens (3.44 → 6.40)
  because 885086's refitted centre (-0.09 shutter) makes the pair less separable (20 → 59 %).
- 635314 and 632748 (own-error rms 3.3, 1.5) do not change: their bright neighbour 632748
  keeps its PRISM shape. This test has no unknowns past the cutouts, and 635314's
  neighbours light it from past theirs (4.25-4.76 µm), so the full-range solve is needed to
  judge them.
- Master background 1.3 (`claude/mbkg-v13`, b004a711, products in
  `~/campfire-data/scratch/claude/sky_resid/v13/products`): through `sky_for_slit` with
  `first_pass.master_sky` pointed at that folder, the sky on 629810's slits is about 40 % of
  1.2's (1.45e-13 vs 3.61e-13; the frame 3.6-3.8e-13), against the +2-4 % the MB session
  measured in count rate. Cause: that folder had no rate files, and `sky_for_slit` fell back
  silently to the slit's own slitlet (fixed by the MB session: rate files linked, and a
  warning in 09187922). With them 1.3 gives 1.02-1.05 x the 1.2 sky, as intended.

**Master background 1.2 / 1.3 / 1.4 on single frames (2026-10-31).** `e2e_v13_check.py`,
`e2e_v13_summary.py`. The 53 isolated G395M sources of test 1 (others' light above 1 sigma
on < 1 % of the pixels, so only the sky is tested): the spectrum at the PRISM shape against
PRISM (`fnu_prof_err` for all), and the shape fitted alone against the PRISM shape.

  | Data | spectrum, median rms | 90th pct | shape fits on a bound |
  |---|---|---|---|
  | single frames - 1.2 sky | 1.87 | 3.57 | 46 / 53 |
  | single frames - 1.3 sky (equal weights, bar shadow once) | 1.96 | 3.21 | 45 / 53 |
  | single frames - 1.4 sky (pooled over exposures) | 1.68 | 4.39 | 44 / 53 |
  | nod differences | 1.39 | 1.74 | |
  | `fnu_prof` (product) | 1.38 | 1.72 | |

  No master-sky version brings single frames to the nod differences for faint spectra, and
  shape fits on single frames fail for most sources with every version (with the leftover
  sky per exposure removed too: 39-41 of 53). What remains is the per-shutter sky that
  repeats between nods, which only the nod difference removes. Production p1 was regenerated
  with 1.3 and then 1.4 by the MB session (user approval); the e2e_p1 root keeps 1.2.

**Built: the joint pass on nod differences (2026-10-31, 8792506e, 45db22af).** Decided by the
user after the tests above ("yes, build it"). `JOINT_PASS_VERSION` 3: members on their nod
differences (`profile_fit.load_source`), the light past the cutouts on nod differences of
the rate files (`_rate_nod_diff`), the partner terms (+1, -1/n) in every part of
`design_full`; each target's solve writes the other members' light on its own pixels
(`_jointlight.npz`, pixels above 1 % of the noise, about 1.5 MB per G395M source) and the
product takes its neighbourhood's light from there (`add_joint_light`; `predict` leaves those
members out, `exclude_ext`). The master sky is no longer used by the extraction.
`contam_joint_cache` sets the members per worker (44 MB each; a solve adds about 2.6 GB at
its peak, so 18 workers with 60 members do not fit in 64 GB).

**Result: stage 3 on p1 (2026-10-31, 12 workers, cache 25).** 57 min, no errors, no fallback
(spec3 8.4 min, PRISM joint 1.5 + products 3.0, G395M joint 27.5 + products 15.7, extraction
0.7). Against PRISM with `fnu_prof_err` alone (`e2e_compare_noinflate.py`), against the
full-range run on master-sky frames:

  | G395M | median rms | 90th percentile |
  |---|---|---|
  | `fnu_prof`, nothing subtracted (both runs) | 1.41-1.43 | 3.43 |
  | `fnu_prof - fnu_contam`, master-sky joint pass (131) | 1.59 | 4.34 |
  | `fnu_prof - fnu_contam`, nod-difference joint pass (141) | 1.50 | 2.60 |

- Subtracting now cuts the worst cases (90th percentile 3.43 → 2.60): 635314 14.1 → 2.5
  (the neighbours' light past their cutouts removed across 4.25-4.5 µm too), 629668 11.6 →
  5.7 (a neighbour's negative image of -4 µJy removed). It helps in 60 of 141, and the
  median is 0.09 above leaving the light in: for faint targets with faint neighbours it
  adds their noise (629743 1.68 → 2.02).
- 632334 1.90 → 2.65: the predicted light has a bump at its own 4.02 µm line, so part of
  the target's line is taken as a neighbour's (not flagged: its not-separable fraction is
  0). 629668: the removed spectrum's 5.05 µm line is twice the light-left-in one.
- `fnu_contam_err` is now the conditional error (others held), a lower bound: prediction
  error / spectrum error median 0.07, so the inflated score no longer flatters.
- Not separable: over 90 % in 31 of 477 G395M sources, over 50 % in 72, none in 358.

**The 632334 line (2026-11-01).** 632334's 4.017 µm line and 629668's 5.046 µm fall on the
same detector pixels, inside both sources' main shutters in all three nods (the two traces
overlap and nod together). PRISM puts the line on 632334 (1.9 µJy against 0.35 continuum;
629668's PRISM is flat at 5.05). The joint solve gives 629668 22 % of the line peak in
632334's solve, and 632334 62 % of it in 629668's, which is 629668's false 5.05 µm line. The
correlation of the two spectra there is about -0.55, so the -0.9 flag does not fire: the two
profiles do differ (632334 narrow and centred, 629668 broad at +0.42), and the split is
decided by the profiles.
- Width is not the cause: the data prefer 632334's narrow PRISM width (0.027); wider is worse.
- The centre is. 632334's G395M profile takes the PRISM fit's offset from plan (`map_shape`;
  only the per-exposure offsets are refitted). Moving it by -0.1 slit units: χ² over the line
  pixels 507 → 197 (89 pixels), 629668's share of the line 2.4e-11 → -0.2e-11 (data 10.1e-11),
  632334's line flux +47 %. The continuum (4.3-4.9 µm, 4441 pixels) prefers the same shift
  (χ² 2936 at 0, 2877 at -0.1, 2903 at -0.15), so the PRISM centre is off in G395M for the
  whole source, not only at the line.
- The user (2026-11-01): "When the model adopts prism shapes, does it also adopt prism
  centering? that may not be accurate." Yes: the offset from the planned position.
- Correction, same day: the PRISM centre is not the problem. PRISM (03101) and G395M (05101)
  have the same pointing (RA_REF / DEC_REF within 0.0005", roll within 1e-4 deg, the same
  dither offsets) and the same planned positions (SRCYPOS 0.030, 0.037, 0.023), so the
  carried offset means the same thing. Over test 1's free G395M fits (113, not on a bound),
  G395M minus PRISM centre is +0.006 median with 0.05 scatter, no trend with detector,
  row, planned position or crowding; 632334's own G395M fit is within 0.013 of PRISM. What
  the -0.1 shift compensates instead: 632334's per-nod offsets sit at their limit (0, +0.1,
  +0.1; limit 0.1, prior 0.015). A common shift of -0.1 is what puts nods 2 and 3 back near
  the PRISM centre. Open: why the nods disagree.
- Why the model has per-nod shifts at all: added with the first profile fit (f4f62e0b) for
  pointing errors between nods (prior 0.015 from stars, cap 0.1). The headers say the nods
  land where commanded: 632334's achieved steps 0.5295" and 0.5289" against 0.5290" (about
  0.001 slit units). The user (2026-11-01): "The nod offsets are a part of our modeling? why?"
- Census, every p1 source, the shifts as the joint pass fits them (`MemberCache._build`,
  alone on the source's nod differences; `e2e_eoff_census.py`): PRISM (204, mostly
  isolated) median 0.009, 90th percentile 0.023, 2 at the cap, as the stars. G395M (477,
  nearly all with overlapping traces) median 0.008, 90th 0.065, 59 at the cap (12 %),
  rising with crowding (8 / 11 / 18 % by tercile). The same source in the same visit:
  632334 PRISM [0, +0.006, -0.005], G395M [0, +0.1, +0.1]. In G395M the shifts absorb
  neighbours' light, then stay fixed in the joint solve.
- Re-solved with the shifts at zero (`e2e_line_eoff.py`; χ² of the target's main-shutter
  pixels):

  | target, window | shifts | χ² | neighbour's share of the data | target line flux |
  |---|---|---|---|---|
  | 632334, 4.012-4.026 µm (89 px) | as fitted | 507 | 629668 23 % | 3.4e-14 |
  | | target's zero | 72 | 629668 -1 % | 5.1e-14 |
  | | all zero | 73 | 629668 -3 % | 5.1e-14 |
  | 629668, 5.035-5.055 µm (116 px) | as fitted | 490 | 632334 79 % | 1.5e-13 (false line) |
  | | target's zero | 510 | 632334 78 % | 1.2e-13 |
  | | all zero | 75 | 632334 127 % | -0.3e-13 (no line) |

  Continuum windows improve too (632334 4.3-4.9 µm: 2936 → 2894, 4441 px; 629668
  4.6-4.95 µm: 1434 → 1390, 2409 px). 629668 needs its neighbour's shifts zeroed too: they
  are fixed per member, so one bad member spoils every solve it is in. With every shift at
  zero the line goes to the source PRISM gives it, in both solves, with no other change.
- **Decision (user, 2026-11-01: "Yes, go ahead"):** no per-exposure pointing offsets in any
  fit (profile fit, shape chain, joint pass); they stay in the interfaces as zeros.
  `default_fit_offsets`, `prior_offset_sigma`, `bound_offset` removed; FIT_VERSION 2,
  PASS1_VERSION 11, JOINT_PASS_VERSION 4. Given up: ~1.5 % less bias of the default shape on
  isolated injections.
- p1 stage 3 rerun (12 workers, no errors, no fallbacks), against PRISM with `fnu_prof_err`
  only, 138 sources scored in both versions (`e2e_v4_compare.py`):

  | G395M | before (shifts fitted) median / 90th | after median / 90th | after better in |
  |---|---|---|---|
  | light left in (`fnu_prof`) | 1.42 / 3.47 | 1.41 / 3.22 | 83 |
  | neighbours' light removed | 1.49 / 2.53 | 1.47 / 2.47 | 85 |

  No source worse by more than 0.5; 4-5 better by more. 629668 5.72 → 2.50 (its false
  5.05 µm line gone), 632554 6.35 → 3.45 (scored over fewer wavelengths: more of it is
  now not separable), 924438 7.03 → 4.50, 635314 2.52 → 1.62, 629743 2.02 → 1.59.
- 632334 2.65 → 2.76 although its line is now right (predicted neighbour light at 4.02 µm
  0.97 → 0.09 µJy). The loss is at 2.9-3.6 µm: a negative nod image of 925403 (centre +0.49,
  at its shutter's edge; its shifts were at the cap in every nod) predicted at about -0.2 µJy
  against a target of 0.17, partly offset by 630784 and 629668; removing it overshoots PRISM
  by about 2x. Open. Cause found (in memory): 925403's light past its cutout (5.20-5.70 µm,
  279 count-rate unknowns, all landing in 632334's cutout and constrained only by its
  pixels; 71 % solved negative) carries the negative light, and trades with 632334
  (correlation -0.86 to -0.92). Tying 925403's in-range spectrum to its PRISM (+0.10 µJy,
  was -0.9) does not change 632334; removing those past-cutout unknowns does (632334 0.7-1.0x
  PRISM; Δχ² +424 for 279 unknowns). The user (2026-11-01) was not concerned by 632334's.

**629668's negative contamination (2026-11-01).** The user: "i was worried about 629668".
- About -4 µJy across 4.4-5.2 µm against a PRISM spectrum of ~0.1. It is the negative nod
  image of 630784 (G395M ~5.3 µJy, 50x brighter): 630784's 2.9-4.8 µm lands on 629668's
  4.4-5.2 µm (another MSA column), and in the nod 2 frame its source shutter covers all of
  629668's nod-1 main shutter, in the nod 1 frame all of its nod-3 one (trace maps;
  `e2e_partner_light.py`). Nod 2's difference is clean. 63 % of the constraint on those
  630784 unknowns comes from 629668's pixels.
- The model predicts that image to ~10 %: 629668's joint spectrum ~0.4 µJy above PRISM;
  630784 centre ±0.04 swings 629668 from on PRISM to 0.9 above, while χ² prefers the fitted
  centre. Reaching PRISM by subtraction needs ~2-3 % on 630784's light in that shutter.
- Avoiding the image instead: nods 1 and 3 rebuilt in memory against their clean partner
  only (nod 1 - nod 3, nod 3 - nod 2) from PRE_BKGSUB_SCI (rebuild reproduces stage 2b to
  < 0.2σ; `e2e_clean_partner_make.py`, copies in scratch). 630784's light on the main
  shutter -2.25e-9 → +0.54e-9 (edge overlap only); χ² 3178 → 2149 (5617 px); joint error
  1.46 → 1.28 µJy. 629668 stays ~0.45 µJy above PRISM: the image was not the cause.
- That excess follows 632334 (compact, line + continuum, same shutter at another spot):
  632334 centre -0.03 → 629668 at 0.2-0.35 µJy and its false 5.05 µm line gone (χ² 2146),
  +0.03 → 0.6-0.85 and the line doubled (χ² 2211); -0.013 (632334's free G395M centre) →
  the lowest χ² (2141.5), line halved, still ~0.35 above PRISM. Width does not matter.
  A 0.1 µJy source under a brighter one in the same shutter depends on that one's centre
  at the 0.01 level, which the data barely constrain.
- Correction (2026-11-01): the geometry is not "several sources in one slitlet". 629668,
  630784 and 632334 are three slitlets in MSA shutter rows 343, 251 and 122 (a slitlet
  keeps one shutter row, the dispersion direction, and runs along shutter columns; the
  trace maps' `xcen` is the row). A G395M spectrum crosses both detectors, so slitlets on
  the same shutter columns put their spectra on the same detector rows, shifted along
  the detector (where 629668 is at 5.0 µm, 630784 is at
  ~4.55 and 632334 at ~3.95). 632334 sits in 629668's rows in every nod and nods with it:
  real overlap, kept whole by any nod difference, only the model removes it. 630784 sits
  one shutter column further along: in frame 2 it is on the column of 629668's nod-1
  shutter, in frame 1 on its nod-3 one, so nods 1 and 3 subtract half its spectrum (a
  negative image) and nod 2 is clean. Figure
  `extraction-v2/e2e_compare/geometry_629668.png` (`e2e_geometry_fig.py`; listing
  `e2e_geometry_dump.py`; its axis labels use the trace maps' names, row and column
  swapped against the MSA metadata).
- The clean-partner copies (`e2e_clean_partner_make.py`) also changed the variance
  arrays. The canonical variance is the frame's own (the fit adds the partners' from
  `BKGNODS`, `diff_variance`), so those copies over-stated the changed nods' variance and
  the error quoted above (1.46 → 1.28 µJy) is not reliable. The χ² comparison is.

**Archive products (user, 2026-11-01).** Choosing the background per exposure or per
source would ship different products within one reduction; this is an archive decision.
Agreed: nodded spectra for every source as the preferred product, master-background
spectra for every source as the secondary product (as decided 2026-10-29), plus
informational columns saying where the master-background spectrum is likely better. A
nod override is not a switch to master background: it only says "don't use this shutter
in this nod as background for this source", so the product stays nodded.

**Decision: the two methods have separate jobs (user, 2026-11-01).** "The overrides are
*only* for neighbors in the same slitlet. Contamination from overlapping slitlets is
handled via the joint model, left to users to subtract and evaluate themselves."
Dropping a nod from the background for a neighbour in the slitlet is standard and
reliable, so it goes into the default product; joint modelling of overlapping spectra is
tricky, so it stays an extra column (`fnu_contam`), never in `fnu`. The prototype below
targets the other-slitlet case and is superseded; the order of the same-slitlet analysis
relative to the joint solve is open (other slitlets' light must not read as a same-slitlet
neighbour). Catalogued same-slitlet pairs exist in the MSA metadata (`SHUTTER_INFO`: a
slitlet whose shutters list more than one real source): p1 34 of 442 G395M slitlets, 14
of 190 PRISM (`extraction-v2/msa_same_slitlet.py`). Six of the prototype's worst cases
are such pairs (634547/634522, 880045/880017, 921026/921027, 666978/632454,
668641/668623, 664977/629472).

**Same-slitlet overrides: decisions and prototype (2026-11-01).**
- User: drop a partner only when the neighbour is bright enough, "if the source is above X
  percent of the sky, maybe?" (not always). The residual scan (uncatalogued neighbours)
  works on pixels that no other slitlet's trace covers (trace maps), with no dependence on
  the joint model; its reporting uses the PRISM cross-check; the overrides and the scan's
  findings are shown in the background-subtraction plots (`<root>_<sid>_bkgsub.pdf`).
- `campfire_pipeline/nirspec/slitlet_neighbours.py` (prototype; tests
  `tests/test_nirspec_slitlet_neighbours.py`): from `SHUTTER_INFO`, the open shutter of
  every catalogued source of a shared slitlet at every dither point; a partner frame is
  dirty for a nod when it puts another catalogued source on the column of the target's
  main shutter in that nod (`PATT_NUM` gives each exposure's dither point). The
  neighbour's brightness: its light over the sky in its own shutter, from the target's
  frame before subtraction against the master sky (`sky_for_slit`), in 20-column bins,
  median over the spectrum, in the frame where it is farthest from the target. A dirty
  partner is dropped at or above the threshold (unmeasured counts as bright) when another
  partner remains; otherwise the nod is reported.
- p1 census (`e2e_slitlet_census.py`, `e2e_slitlet_summary.py`, `e2e_slitlet/census.json`).
  Every dirty nod has a clean partner left (with three nods, the neighbour falls in one
  partner frame per nod).

  | | sources in shared slitlets | with a neighbour in a partner frame (nods) | neighbour light / sky, 10/50/90 % | targets' own light / sky, 10/50/90 % |
  |---|---|---|---|---|
  | PRISM | 28 | 22 (30) | 0.012 / 0.025 / 0.057 | 0.012 / 0.024 / 0.061 |
  | G395M | 70 | 61 (86) | 0.010 / 0.064 / 0.304 | 0.011 / 0.057 / 0.169 |

  | threshold (of the sky) | 0 | 0.01 | 0.02 | 0.05 | 0.10 |
  |---|---|---|---|---|---|
  | PRISM nods dropping a partner (of 30) | 29 | 28 | 21 | 6 | 0 |
  | G395M nods dropping a partner (of 86) | 83 | 78 | 71 | 55 | 25 |

  The neighbours are as bright as the targets. With three nods a kept neighbour puts half
  its light, negative, on the target's shutter, so one at X of the sky biases the target
  by X/2 of the sky; the faintest tenth of the targets are at about 0.01 of the sky.
- Threshold 0.02 (proposed; the user: "keep going", 2026-11-01). Built into stage 2b
  (cde23368): `slitlet_neighbour_min_sky = 0.02`; brightness is the larger of the two
  detectors', so a nod gets one choice; hand overrides win, then the catalogued
  neighbours, then the scan's file (where both change a nod, the partners both keep).
  `BKGWHY` records the reason (copied to `EXPOSURES.bkg_why`). The bkgsub plots label each
  nod `nod - [partners]` and write the reason on an overridden nod; the scan's findings
  (applied or proposed, with Delta, the statistic and the PRISM cross-check, now in each
  proposal's comment) go under the title. The scan now refits the target at its chosen
  shape on the nod differences as stage 2b made them, leaving out every pixel another
  slitlet's source shutter lights in any frame of the difference (trace maps), instead of
  using the joint model's subtraction; above `scan_max_masked` (0.3) of such pixels it
  reports only. `partner_choice` removed.
- p1 rerun in the isolated root (stage 2b for the 73 sources in shared slitlets, 12
  workers, no errors): 97 source x disperser pairs share a slitlet, 65 got overrides on 92
  nods (as the census). The main-shutter light per nod before and after
  (`e2e_slitlet_fig.py`, `e2e_slitlet/main_shutter_before_after.png`): 923747's nods 1-2
  lose a -2 µJy dip at 5.05 µm (923748's line); 950038 (PRISM) loses a -0.01 to -0.025 µJy
  continuum offset; 880017's nod 2 moves up but stays ~-0.3 µJy; 634547 (neighbour at
  0.024 of sky) barely changes. Nod agreement does not measure this (the same neighbour
  usually biases two nods alike): median chi2/dof 1.83 → 1.82 over 61 groups
  (`e2e_slitlet_nodcheck.py`).
- Stage 3 for the same 73 sources (12 workers, no errors; G395M joint pass 20 min). The
  shipped spectra (`fnu_prof`, light left in) against PRISM, both dispersers rerun
  (`e2e_slitlet_spectra.py`), 19 G395M sources with PRISM: rms 1.42 → 1.55, worse in all
  19 (none by more than 0.5). The stated error did not change (after/before median 1.000)
  while the point-to-point scatter over it grew 1.00 → 1.10 over the 50 changed G395M
  spectra: `fnu_prof_err` comes from the resampled image's `ERR`, which comes from the
  canonicals' variance, and jwst's background step does not add the subtracted frames'
  variance. So every nodded spectrum's error leaves out its partners' noise (about 20 %
  low with three nods), and a dropped partner's extra noise does not show. With each
  "after" error scaled by its measured scatter growth (both dispersers): 1.42 → 1.38,
  better in 8, worse in 11; |median offset| 0.20 → 0.27. At 0.02 the gain is within the
  noise on average; clear where the neighbour is bright (923747's line image).
- The scan on clean pixels (proposals, report only): 6 of the rerun sources. It finds
  923747's catalogued neighbour from the data and proposes the same partners as the
  catalogue (Delta -1.70). In the slitlets with three catalogued sources (924889, 924937,
  949820) it names a dirty partner the catalogue does not, for one nod each: `SHUTTER_INFO`
  lists one source per shutter per dither, so a shutter two sources share shows only one.
  None was withheld for other-slitlet pixels.
- Open: the threshold (0.02 against 0.05 / 0.1, where the census puts the clear cases).
  Shared shutters in `SHUTTER_INFO`: answered by the positions from `SOURCE_INFO` (below).
- Follow-up (user, 2026-11-01: noted, not now): the shipped errors leave out the partner
  frames' noise (stage 2b change: write the propagated variance, with the profile fit
  reading the frame's own from `PRE_BKGSUB_VAR_*`).
- Frames without the source (`SRCFLUX = F`) are calibrated by jwst as extended (uniform
  path loss) and the others as a point source, so in a plot of all nods they sit 3-4x
  lower (950038 PRISM: 3.77 against a path-loss ratio of 3.73). `fix_units` makes the
  units agree; it does not touch the path loss. Nothing downstream is affected: stage 2b
  removes each frame's own path-loss correction before subtracting, and stage 3 never
  combines these frames (`stage3.py` `srcflux` filter and `_science_files`, since the
  package's first commit, 24a9dd92). The bkgsub plots now draw them on the others'
  point-source calibration (median uniform over point-source path loss) and label them
  "source not in the slitlet: not combined".
- PRISM check (`e2e_slitlet_removed.py`, `e2e_slitlet/prism_removed.png`): in the
  target's shutter, the light an override removed against the neighbour's own PRISM
  spectrum, one fitted scale. 950038 (both nods), 925591, 879928, 924216: the removed
  light has the neighbour's spectrum, scale 0.16-0.26 (half the neighbour times the share
  of its light in its shutter). 925591 nod 3 loses two negative lines (-0.22 µJy at 3.4
  and 4.5 µm: 925550's lines); small dips remain (the neighbour 0.92 slit units away in
  the kept frame). 923334: nothing removed. It sits at its shutter's edge (y 0.04); the
  dropped frame puts 923362 in its shutter 0.67 away, the kept one across the bar 0.48
  away (`e2e_shutter_light.py` confirms where the light is). Same shutter is the wrong
  test: by position (`e2e_slitlet_distance.py`: column x pitch + y - 0.5 from
  `SHUTTER_INFO`), pairs one nod step apart often put the neighbour 0.5-0.65 slit units
  from the target in both partners (633676 / 667731, 923334, 880045, 950310 / 950331,
  885305 / 885945, 924889 / 949820), where no partner choice helps; and pairs about one
  nod step apart with neither near a bar (629432 / 629466 0.01, 668601 / 668614 0.02,
  629451 / 629452 0.02, 921026 / 921027 0.09) land exactly on the target, the cases
  overrides are for. Proposed next: decide by position (a partner is dirty when it
  puts the neighbour within R of the target; both dirty: keep the partners and flag).
- Master-background session's measurement (2026-11-01, p1 G395M, count rate): empty-shutter
  pixels 0.25-0.6 slit units beyond another slitlet's padded footprint (LIGHT_PAD 0.25)
  read 1.033 ± 0.004 of the sky in the same shutter (1.08 under two such slitlets): light
  from an open shutter reaches a few per cent beyond what the trace map marks as lit. The
  sky part cancels in nod differences; a neighbour source's own wings there are missing
  from the joint model's support, small next to its ~10 % accuracy on images. It also
  argues for the position rule: a neighbour just across a bar still reaches the target.
  (`~/campfire-data/scratch/claude/mbkg_ship/tracemap/extra_pixels_clean.py`)
- **Position rule (user, 2026-11-01: "yes, i think that's fine"; "the full SOURCE_INFO table
  is worth using").** Built (3795e228). `SkyToMsa`: a quadratic map from sky offsets
  (`SOURCE_INFO` RA/Dec) to MSA shutter coordinates (`row + x - 0.5`, `column + y - 0.5`),
  fitted per configuration and quadrant to the plan's own placements with one offset per
  dither point (`msa_source_info_fit.py`): on p1 it reproduces them to 0.001-0.003
  shutter (an affine map leaves 0.08). The fit also shows the plan's `y` is in pitch
  units, so the position along the slitlet is `(column + y - 0.5) x 1.15` slit units (the
  formula above was off by up to 0.075). Every `SOURCE_INFO` source within 0.75 row
  pitch of the slitlet's row and 0.75 slit units of one of its open shutters, at any
  dither, is a neighbour (a source the fit cannot place falls back to its `SHUTTER_INFO`
  listing). A partner is dirty when it puts a neighbour within
  `slitlet_neighbour_max_distance` (0.75 slit units) of the target's position in the
  nod; dropped as before (brightness threshold, another partner remains). When every
  partner is dirty the nod keeps its partners and `BKGWHY` says `no clean partner:
  neighbour <id> at <frac> of sky` (written on a nod with no override too; the bkgsub
  plots show it). The neighbour's brightness is measured over one shutter height around
  its position, in a frame that holds the target and puts the neighbour at least 1 slit
  unit from it (inside an open shutter preferred, then the farthest).
- p1 census by position (`e2e_position_census.py`, no brightness): PRISM 14 targets with
  a neighbour within reach, 21 nods with a dirty partner, 4 with none clean; G395M 39, 57,
  11. `SOURCE_INFO` holds 526 sources, all but one in some shutter, so on p1 it adds
  positions rather than sources: one pair `SHUTTER_INFO` does not list in the slitlet
  (880414 / 879626, 0.10 slit units). 923747 nod 2 is now `no clean partner` (923748 at
  0.39 in partner 1 and 0.75 in partner 3), where the shutter rule dropped partner 1 and
  removed the -2 µJy line image.
- Merged `claude/mbkg-ship` (master background 1.3-1.5, the stored trace map) into
  `claude/extraction-v2` (user, 2026-11-01). Stage 3 takes each exposure's map from its
  rate file where step 1 stored one (ecaf2f22; stuck physical shutters removed on read),
  else builds it with jwst as before; `tracemaps/` stays as the working copy (pass-1
  validity keys on it) until those keys move to the rate files. The stored map's
  `rows_of` counts a second catalogued source of a slitlet as the slitlet's own, so the
  joint model no longer models same-slitlet neighbours (they are the overrides' job).
  Isolated p1 root after step 1 (`e2e_rate_maps.py`, `e2e_rate_maps_diff.py`,
  `e2e_rate_maps_stuck.py`): the stored map, stuck shutters removed, against the jwst-built
  `tracemaps/` of PRISM nod 1 NRS1: the failed-open shutters are named differently
  (`failed_open_12` against `failed_open_q3_156_163`, same pixels); 6922 slitlet
  (pixel, slitlet) pairs are lit only in the old map, nearly all from four 3-shutter
  slitlets (117, 138, 105, 102) whose middle (source) shutter is on the stuck list: the old
  build kept lighting it (its shutter state still `1x1`), the stored map removes it. So the
  switch also fixes stuck source shutters in the contamination model. The rest are edge
  pixels (tens to ~140 per slitlet).
- p1 rerun with the position rule and the stored maps (isolated root, stage 2b + 3 for the
  74 sources with a neighbour within reach, 12 workers, no errors, no boxcar fallbacks;
  `e2e_position_run.sh`): 98 source x disperser pairs measured, 55 with overrides on 67
  nods (the shutter rule: 92), 19 nods flagged `no clean partner` (among them 923747 nod 2
  and 950038 nod 2, both dispersers, which the shutter rule fixed). Stage 3's 12 maps came
  from the rate files in 5 s.
- PRISM check (`e2e_position_removed.py`, `e2e_slitlet/position_prism_removed.png` and
  `_nrs2.png`), 15 overridden PRISM nods: in 8 the removed light has the neighbour's own
  spectrum (scale 0.21-0.44): 668002 nod 3 loses a -0.2 µJy line at 3.75 µm (634072's)
  and 879903 nod 2 one at 2.85 µm (879928's), both new with the position rule; 925591's
  3.4 / 4.5 µm lines, 950038 nod 1, 879928, 924186, 924216, 629466 as before. In 7 nothing
  measurable is removed (923334, 923362 nods 1 and 3, 925550, 634522 nods 1 and 2, 667731):
  the dropped partner held little of the neighbour's light, so they only cost noise.
  634522's neighbour 634547 measures 0.284 of the sky yet removes nothing; its brightness
  measure is suspect (not followed up).
- G395M against PRISM (`e2e_slitlet_spectra.py`, against the products before any override;
  this also includes the stored maps' change to the joint model): 16 sources, rms 1.48 →
  1.59 with the stated errors (better in 3; scatter over the stated error 0.98 → 1.08, the
  known missing partner noise), 1.48 → 1.56 with the errors scaled (better in 9, worse in
  7). Largest changes: 925404 6.03 → 2.04 and 925403 3.64 → 2.78 (a pair 0.19 slit units
  apart); 924186 1.44 → 1.86, 629432 1.51 → 1.81, 921027 1.24 → 1.44 worse. On average
  neutral, as with the shutter rule; clear where the neighbour is bright. Before / after
  pages of every affected source (`e2e_final_before_after.py`,
  `e2e_slitlet/final_before_after.pdf`, 51 pages) sent to the user.
- User review (2026-11-01): a neighbour too close to the target to measure (within 1 slit
  unit in every frame) keeps counting as bright ("I'd be concerned if it was <0.5 or so");
  the threshold stays at 0.02 ("depends a bit on how well the background level is
  constrained"). 666336: the negative trace in its G395M spectrum before came from other
  slitlets (879755, 664830, 634324 put 60-89 % of their pixels on its source shutter from
  exposures 1 and 3); the override, triggered by its unmeasured same-slitlet neighbour
  666345, happened to pick the sky exposures that put little of that light there. The user
  is fine with it (light on the detector, and it helps). No ordering conflict: stage 3's
  joint model reads the sky exposures stage 2b used. Measuring the neighbour's brightness
  only on pixels no other slitlet lights was offered and not taken up.
- A / B trial for exposures whose two sky exposures both put a bright neighbour near the
  source (`slitlet_neighbour_no_clean`, 2c346df2): A keeps both (flagged, the default), B
  keeps only the one that puts it farther when at least 0.2 slit units farther. B used one
  sky exposure for 11 exposures in 10 sources; 8 stayed as A. Against PRISM (5 G395M
  sources) B is worse in all 5: rms 1.35 → 1.59 with the stated errors, 1.35 → 1.39 with
  them scaled for the extra noise (`e2e_slitlet/final_a_vs_b.pdf`, `e2e_farther_run.sh`).
  With the neighbour near the source in both, one sky exposure costs more noise than the
  bias it removes. Decided (user, 2026-11-01): A ("yes, i think A is fine"); the trial
  option is removed again.
- Vocabulary (user, 2026-11-01): "background", not "sky"; American spelling. The config
  keys are now `slitlet_neighbor_min_background` / `slitlet_neighbor_max_distance` and
  `BKGWHY` reads "neighbor <id> at <f> of background" or "every background exposure: ..."
  (b18e5d49).
- Validation of the neighbor brightness measure (user: "worth validating"). Most neighbors
  are targets too, so each is measured twice with the same function: in the target's
  exposures at the neighbor's position (what stage 2b uses) and in its own exposures at
  its own shutter (`e2e_brightness_check.py`, `e2e_brightness_fig.py`,
  `e2e_brightness_overlap.py`; 143 pairs).
  - Before: PRISM agreed (median ratio 1.08) except 634547, 0.28 of the background
    against 0.004 own: its band ran past the last open shutter into the cutout margin,
    where the master background is near zero (the margin read 19x its background).
    G395M: median 1.10 but 35 of 115 pairs off by more than 2x. Pairs whose band no other
    slitlet's source lights agree to ~15 % (29 pairs); where one lights more than 20 % of
    it (66 pairs) they differ by a factor 1.6 median.
  - Fix (d7069b89): the measure uses only pixels inside the slitlet's open shutters and
    tries the next candidate exposure when one gives no value. PRISM: median 1.03,
    634547 now 0.016. Leaving out the pixels other slitlets light was tried and dropped
    (9bacabc8): on G395M it left 49 of 115 neighbors unmeasured (18 before) without
    better agreement.
  - Open: G395M stays scattered (median 1.28, 35 of 115 off by more than 2x, 26 on the
    other side of 0.02), mostly other slitlets' light in the band; the errors are mostly
    overestimates, so they cost extra dropped exposures (noise) more than missed ones
    (bias). Note that the reference (the neighbor's own exposures) has the same problem.
  - The user does not accept this as a known limitation (2026-11-01). The joint first
    pass's model of each neighbor (its pass-1 frame of that exposure, divided by the
    point-source path loss) gives the same quantity with other slitlets' light separated
    (`e2e_brightness_model.py`, `e2e_slitlet/brightness_model.png`, 120 pairs). PRISM, band
    free of other slitlets: data / model median 0.87 (units and placement check out).
    G395M, band more than 20 % lit by another slitlet's source (51 pairs): data / model
    median 3.1, 38 off by more than 2x, 20 across 0.02; the neighbor's own exposures read
    1.9x the model. So the data measure counts other slitlets' light; the joint model
    separates it. Proposed: take the brightness from the joint model (needs the joint
    first pass before the stage-2b decision; see the open-decisions table).
  - PRISM check (user's request; `e2e_pass1_prism.py`): for the 17 G395M neighbors with a
    PRISM spectrum (integrated PRISM S/N > 5, 2.9-5.1 um), the joint pass's spectrum
    against PRISM deviates by more than 3 sigma in 2 of 17; the shipped G395M `fnu_prof`
    in 10 of 17, several strongly negative (629466 -51 sigma, 925550 -9 sigma, 880017
    -8 sigma), and `fnu_prof - fnu_contam` also in 10 of 17. The joint pass's errors are
    1.5-2.6x the shipped ones, which explains part of the difference but not 629466
    (+0.006 against -0.136 uJy, PRISM positive). Open problem: why the shipped G395M
    spectra of these crowded same-slitlet sources disagree with PRISM when their joint
    pass does not.
  - Built (cc135800; user approval 2026-11-01): after each disperser's joint first pass,
    stage 3 redoes the same-slitlet decisions of the re-run sources with the neighbors'
    modeled light (`slitlet_neighbours.model_light`; the 1-slit-unit separation limit
    no longer applies, since the model holds none of the target's light) and runs stage
    2b and the combined 2-D spectrum again for the groups whose choice changes
    (`stage2.revise_slitlet_overrides`). Stage 2b keeps the data measure as its first
    guess. `slitlet_neighbor_brightness = "model"` ("data" keeps stage 2b's choice).
  - p1 run (stage 2b + 3 for the 74 sources, with the error-propagation fix; first attempt
    failed: the padding of groups whose cutouts differ in size left the variance arrays
    unpadded, fixed in 1b4a0758). Stage 2b's data measure: 54 sources with overrides on
    65 exposures, 19 flagged. From the joint model, G395M: 22 sources on 26 exposures, 8
    flagged (PRISM: 15 on 18, 4 flagged); fewer drops, as the data overestimated. The
    revision redid 71 of 110 G395M groups, many only for a new brightness in BKGWHY; now
    those are relabelled without a new subtraction (1000d709). G395M neighbors against
    PRISM: shipped `fnu_prof` beyond 3 sigma in 7 of 17 (10 before), `fnu_prof -
    fnu_contam` in 6 of 17 (10 before); part of that is the larger, now correct, errors.
    629466 stays at -44 sigma: not explained by the background choice.
  - 629466 explained (`e2e_629466_geometry.py`, `e2e_product_2d.py`, `e2e_overlap_of.py`,
    `e2e_slitlet/629466_g395m.png`): a faint source (~0.05 uJy) on which another slitlet's
    source, 632748 (~4 uJy past 4.4 um, 80x brighter), lands only through the background
    exposures (exposure 2's partners 1 and 3 put it on 629466's shutter; it is never in
    629466's shutter in 629466's own exposures). The nodded spectrum carries its negative
    image (-1 to -1.5 uJy past 4.4 um); `fnu_contam` predicts it, but a 10 % error on a
    source 80x brighter is 8x the target, and 629466's own joint pass is negative there
    too. Its master-background spectrum is 2.2x PRISM (+5.3 sigma) against -10.6x (-44
    sigma) nodded. Proposed: a "likely better" reason for this case (another slitlet's
    light entering only through the background exposures, from the joint model's
    prediction split by sign).
- Master-background spectrum on p1 (stage 3 for the 74 sources after the merge of
  `claude/mbkg-extraction`, no errors; Spec3 169 s against 65 s): all 99 products carry
  the four extensions on the nodded grids; 12 have MBKGPREF (no_clear_background). Against
  PRISM (20 G395M sources): beyond 3 sigma 16 master-background spectra against 9 nodded
  (median 8.1 against 2.8 sigma), mostly too high (921027 38x); subtracting `fnu_contam`
  does not help (15 against 9). Cause (`e2e_mbkg_residual.py`, `e2e_mbkg_level.py`): on
  G395M the master background is too low in these crowded slitlets, (frame - master
  background) / master background away from all sources +0.02 to +0.20 per exposure
  (921027 +0.19 / +0.12 / +0.07, 925403 +0.14 / +0.20), against +-0.02 on PRISM; for
  sources at 1-5 % of the background that dominates. Reported to the master-background
  session (its painting). Its diagnosis (2026-11-01,
  `~/campfire-data/scratch/claude/mbkg_ship/g395m_level/`): background pixels read +1.4 %
  where nothing is left out, +5 % / +10 % where one / two other slitlets' margins are
  dropped (the painter adds a margin only where no open shutter is painted), +54 % on a
  failed-open shutter (never painted; 921027 has a bright object behind one on rows
  640-647 in all three exposures, which nodding cancels and the master background
  cannot), and about +-5 % scatter between slitlets otherwise. Its proposals (to the
  user): paint failed-open shutters and flag the bright ones, add margins, split DQ bit 2.
  Open for the extraction: treat MSA_FAILED_OPEN pixels as missing in the
  master-background extraction (recommended), not in the nodded one.
  The deploy reader on `claude/mbkg-web` accepts all 99 p1 products.
- The 1D combination of several exposure groups did not carry the master-background
  spectrum (not built, no other reason); built (530bedb5): each group's `SPEC1D_MBKG`
  combined as the nodded columns are, the 2-D spectrum from one master-background Spec3
  over all exposures, the cards from the combined `EXPOSURES`; `NEXP` / `NEXPBKG` now
  on combined products too. Unit-tested; p1 has no source with several exposure groups,
  so the end-to-end check needs another observation.
- Cleanup (user, 2026-11-01): the masked pass 1, pass 1b, the echo correction, the old
  group joint solve and `first_pass.py` removed (merge of `claude/extraction-v2-cleanup`,
  about 2,800 lines). End-to-end check on p1 (stage 3 only, the 74 sources,
  `e2e_cleanup_run.sh`, `e2e_compare_products.py`): all 99 products have identical SCI;
  `fnu` / `fnu_prof` within 3e-6 of their error, `fnu_contam` within 1e-4 (rounding of
  the recomputed pass 1). 17 config keys are gone (`contam_pass1*` except
  `contam_pass1_smooth`, `contam_echo`, `contam_joint*` except `contam_joint_cache`);
  left in a config they are ignored.

**Automatic nod-partner overrides: prototype (2026-11-01, superseded).** The user asked to build them
from estimated light, the light estimated from the master-background products.
`campfire_pipeline/nirspec/partner_choice.py` (prototype; tests
`tests/test_nirspec_partner_choice.py`). Per source and nod, both detectors:
- Each partner frame's light beyond its master sky (`sky_for_slit`) on the source's
  main-shutter pixels of that nod, binned along the trace (20 columns); the trace maps
  say where another catalogued source's shutter covers at least half the main shutter
  in that frame.
- A partner's contamination per bin is its light above both the least-lit partner's and
  the nod's own frame's, never negative. The per-shutter floor of the master sky is the
  same shutter in every frame and cancels; light the own frame carries too (in the
  target's row in its own frame as well) is light the subtraction should remove.
- Dropped when the expected squared error of the nod difference falls (bias from the
  partners' contamination plus their noise, both partners against the kept one), summed
  over the bins where that partner's frame has another source in the row, leaving out the
  10 % of bins with the largest gains (a feature in a bin or two is masked, not paid for
  with a partner). One partner always remains. Output in the hand-override format.
- What each step fixed, on the p1 G395M sources (477, 1431 nods with two or more
  partners): v1 (least-lit partner only, any partner's flagged bins) changed 556 nods,
  driven by single-bin spikes fixed on the detector but brighter in one exposure
  (666978, 922323); v2 (own flagged bins, trimmed) 261 nods, but dropped partners that
  carry the same light as the own frame (885945: frames 1 and 2 +30-40 µJy on every
  nod's shutter, frame 3 not); v3 (own frame compared too) 130 nods, 98 sources.
  629668 gets nod 1 against nod 3 only and nod 3 against nod 2 only, the hand choice.
- Figures: `extraction-v2/e2e_auto/partner_light_<sid>.png` (`e2e_auto_fig.py`), each
  frame's light on the main shutter per nod against wavelength.

Tests (copies with the chosen partners in scratch, `e2e_auto_make.py`; only `SCI` and
`BKGNODS` change):
- Nod agreement (`e2e_auto_nodcheck.py`): χ² of the nods' main-shutter light about their
  mean per bin; light every nod shares (the target, sources that nod with it) cancels,
  negative images do not. 98 sources: median 3.49 → 2.76 (median bin 2.86 → 2.38),
  better in 74, worse by more than 2x in 14. Large gains where the image was strong
  (630802 324 → 3.4, 633426 91 → 2.5, 629668 84 → 3.5); large losses in 885945, 664977,
  668641, 634547, 950331, 921026, 880045.
- Against PRISM (`e2e_auto_test.py`, the 20 changed sources with a PRISM spectrum, same
  error for both):

  | spectrum | better | worse | notable |
  |---|---|---|---|
  | target alone, other light left in (what `fnu` is) | 13 | 5 | 629466 2.26 → 1.19, 922707 1.78 → 1.17, 884902 2.77 → 2.16, 629668 3.51 → 2.89; 634547 0.97 → 4.47, 921026 0.92 → 3.77 |
  | joint solve | 0 | 20 | median 1.01 → 1.06 (the noise of the dropped partner; the joint model already removes these images) |

- Two failure kinds remain. (a) Light shared by the own frame and one partner but
  modelled as the partner's contamination when the own frame also holds the target
  (it cannot be told from the target's light on the same pixels). (b) Partner light
  that is not a source: 880045's frame 3 swings -2 to +3.5 µJy about the master sky
  on its nod 1 and 2 shutters while frames 1 and 2 are flat (634547 is dropped for the
  same neighbour, 631816). A check that the excess has the named neighbour's spectrum
  (its own master-background light, at the wavelengths that land there) would separate
  both from real images.

**Open (extraction session).**
- Proposed then withdrawn: end each extension grid where the exposure's count-rate sky falls
  below a few percent of its level at the cutout end. The user (2026-10-31): "I'm hesitant
  about reducing the cutoff, since real sources can be much brighter than the sky."
- Build steps 1-11 above. Done: 1-7 (module), the stage 3 wiring and step 8 (0a746f33):
  `contam_pass1 = "joint"` is the default, `"fixed"` keeps the masked pass. A neighbour that
  cannot be built is left out of the solve; a target that cannot be built takes its stage-3
  3-pixel boxcar. The cached pass 1 is redone when a member's PRISM shape or the master
  background changes. `fnu_contam` is NaN at the target's not-separable wavelengths
  (`NOTSEP` table in the `_pf.fits`, `PFNOTSEP` = the fraction). End-to-end run done (above).
  To do: step 9 (remove the old pieces), after the two checks above.
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

**Per-exposure products.** `<base>_rate.fits` (stage 1, with master background step 1's
tables, and the trace map once built); `<base>_mbkg.fits` (step 2); canonicals per source; `bkgslits/` (local,
regenerable); trace maps (cache).

**Master-background spectrum (built on `claude/mbkg-extraction`; schema agreed with the MB
session, user decisions 2026-11-01: 2-D in the FITS, three reasons, exposure counts).**
`nirspec/mbkg_extraction.py`. The same extraction as the nodded spectrum on the exposures
with their master background subtracted, so the two differ only in how the background was
removed:
- Frames: each science exposure's calibrated frame (`PRE_BKGSUB_SCI`) minus its master
  background (`stage2.master_background_on`, `sky_for_slit`), written as a copy of its
  canonical in `<workspace>/mbkg/` (variance plus the master background's; the
  `PRE_BKGSUB_*` arrays kept for the pixel calibration and the frame's own variance).
- 2-D: jwst's Spec3Pipeline on the copies under `<product>_mbkg`, in the product's Spec3
  task. Same exposures, so the same 2-D grid (p1 923747 G395M: identical WAVELENGTH array;
  the 1-D x1d wavelengths differ by 1e-15 um, so the check is on the 2-D grid and the
  spectrum is written on the nodded `wave`). One extra Spec3 run per product.
- Fit: the copies as single frames (`load_source(single_frames=True)`), the forward model at
  the nodded chain's shape, the spectrum solved; `PROF2D` / `CONTAM2D` / `CONTERR2D` drizzled
  through the master-background crf. Other sources' light from their pass 1s, once and
  positive, catalogued same-slitlet neighbors included (`predict(include=...)`, from
  `slitlet_neighbours.scene`): the stored map counts them as the slitlet's own light.
- Extraction: `stage3.extract_columns` (factored out of `opt_ext_single_source`, so both
  spectra run the same code) on the nodded aperture rows.
- Extensions: `SPEC1D_MBKG` (SPEC1D's columns, same `wave`; header: the extraction cards,
  `MBKGVER`, per-exposure `MBKGSRC` combined / exposure), `SCI_MBKG`, `ERR_MBKG` (SCI's
  grid), `PROF1D_MBKG`. Primary: `NEXP`, `NEXPBKG` (always), `MBKGPREF`, `MBKGWHY` (first
  of `no_clear_background`: an exposure's `BKGWHY` says every background exposure holds a
  bright neighbor; `single_exposure`: an exposure with no background exposure; `extended`:
  the nod subtraction removes more than `mbkg_extended_frac` = 0.10 of the source's own
  main-shutter light, `PFSELFSB` from the nodded fit), `MBKGNEXP`.
- The science hash (`SCIHASH`, `spectra.science_hash`) leaves out `*_MBKG` extensions and
  `MBKG*` primary cards, so the master-background spectrum never marks line fits stale; no
  earlier file has either, so stored hashes are unchanged.
- Stage 2a rectifies the master-background-subtracted exposure, after step 2 (nods plots,
  `S2D_SCI` card `MBKGSUB`).
- Config `[nirspec.stage3] mbkg_spectrum = true`, `mbkg_extended_frac = 0.10` (proposed).
- Not built: the 1-D combine of several exposure groups (`combine_method = "1d"` with more
  than one group) does not carry the master-background spectrum; a source with a single
  exposure gets no product at all (stage 3 combines background-subtracted exposures only),
  so `single_exposure` cannot fire yet.

**Open.** Web: read `fnu_contam` and `CONTAM2D`, offer the toggle (later; not in this PR unless
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
| V30 | Runtime per exposure | wall time per phase (step 1, 2a, step 2, maps, first pass, joint, final) | no step adds an hour per exposure | In progress. End-to-end p1 run (2026-10-31, isolated root `scratch/claude/e2e_p1`, 6 workers, 12 rate files, 2,886 cutouts): step 1 1 min; stage 2a `Spec2Pipeline` 24 min; `fix_units` 7 s; rectification of every cutout (`resample_single_exposure`) 25 min; plots 1 min; step 2 9 min; stage 2b about 60 min for 962 groups (about 22 s per group per worker); stage 3 pending. The rectification and stage 2b are the two largest pieces and neither is new in v2 | `scratch/claude/e2e_p1/run.log` |

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
| Retire `tracemaps/` once pass-1 validity keys on the rate files (the superseded passes are removed) | extraction session | |
| Error propagation: write the variance including the subtracted sky exposures in stage 2b (profile fit reads the frame's own from `PRE_BKGSUB_VAR_*`); before V26 | extraction session | user (when) |
| Update the CHANGELOG extraction entry (joint first pass, no per-exposure offsets, same-slitlet overrides, maps from the rate file) | extraction session | |
| Neighbour scan: stay `report_only` or apply (`auto_override`) | user | |
| Second field (a spurs observation) and G140M / G235M end to end | extraction session | |
| Full-field separability count (477 p1 G395M sources) | extraction session | |
| Same-row inseparability cause | extraction session | |
| Master-background spectrum: end-to-end p1 run and review (built, section 4); the 1-D combine of several groups; the `extended` threshold | extraction session | user |
| Default galaxy width 0.15 vs 0.21 | user | |
| Forward-model shutter edge vs measured bar shadow | extraction session | |
| Stage 1 footprint past the calibrated range | MB session | user |
| Runtime baseline on one full observation | both | |
| Speed up stage 2's per-source work (user goal, 2026-10-31). First profile one stage-2b group and one rectification: in stage 2b the log line "Inverting the pathloss" only marks the start of a group, which also subtracts the nods, reapplies the pathloss and rectifies again, so the slow part is not yet known. Candidate fixes once measured: rectify once instead of in both 2a and 2b, invert the pathloss arithmetically from the stored correction instead of a `PathLossStep` call, and batch per exposure instead of per source | extraction session | |
| Rebuild the extraction report as final documentation (main text = what ships; appendices = explored and rejected) | extraction session | |
| Same-slitlet overrides by position (3.9, `slitlet_neighbours.py`, built in stage 2b): decided 2026-11-01 (0.75 slit units, 0.02 of the sky, unmeasurable neighbours count as bright, no clean partner: keep both and flag); p1 rerun and PRISM check done. Left: the CHANGELOG entry before a PR | extraction session | |
| Informational "master background likely better" columns (archive products, 3.9) | extraction session | user |

---

## 7. Implementation map

| Area | Files |
|---|---|
| Geometry, cutoffs, overlap counter | `pipeline/campfire_pipeline/nirspec/coverage.py` |
| Master background | `nirspec/master_bkg.py`; hook `stage2.py:384` |
| Bar shadow | `nirspec/barshadow_calibration.py`, `data/Generate_Barshadow_Cals.py`, `data/empirical_jwst_nirspec_barshadow_*.fits` |
| Stuck shutters (today) | `nirspec/stuck_shutters.py`; hook `stage2.py:304-372` |
| Trace maps | `nirspec/trace_overlap.py` |
| First pass (joint, on nod differences) | `nirspec/joint_first_pass.py`; its linear system `nirspec/profile_joint.py` |
| Prediction of other slitlets' light | `nirspec/profile_contam.py`, `nirspec/profile_extend.py` |
| Profile model, shape chain | `nirspec/profile_fit.py`, `nirspec/profile_chain.py`, `nirspec/profile_product.py` |
| Orchestration | `nirspec/stage3.py` (`run_profile_fit_phase`, `extract_columns`, `mbkg_spectrum_hdus`) |
| Master-background spectrum | `nirspec/mbkg_extraction.py`; `stage2.master_background_on`; `metadata/science_hash.py` (MBKG left out) |
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
