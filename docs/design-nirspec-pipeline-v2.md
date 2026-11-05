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
| Empirical bar-shadow tables, PRISM + G140M/G235M/G395M | Built (7ab786bf, 47d19c46, fea58a6e, b0c08026); settled 2026-10-30 (user, in the MB session; the "not signed off" line here was a stale recap) | MB session | 3.2 |
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
| Stage 3's same-slitlet decision independent of the previous run (joint pass on the override files' nod differences, rebuilt from the frames before subtraction); Spec3 and every drizzle after the revision | Built (2026-11-02, `claude/bkg-revision`; p1 922323); p1 end-to-end check pending | extraction session | 3.9 |
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
9. **Final extraction.** The same-slitlet decisions redone from step 8, stage 2b again where
   they change (section 3.9), then stage 3 resampling (`Spec3Pipeline`) and every drizzle,
   then the nodded profile extraction of each source (`profile_fit.py`), with the neighbours'
   light known from step 8.
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
- The forward model's shutter is the geometric shutter with the smooth transmission T, capped at
  1, fitted to these tables (user, 2026-11-03; `FIT_VERSION` 5; evidence below, section 3.7).
  G140H, G235H and G395H use the M grating with the same filter (user, 2026-11-03).

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
  no separate bar-shadow model is needed for point sources. Superseded for uniform light on
  2026-11-03 (below): the hard clip does not reproduce the bar shadow in the red.
- **The forward model against the bar shadow (V7, 2026-11-03).** For uniform light the model's
  image is the open area blurred by the after-shutter part of the PSF and averaged over a row
  (dy 0.227-0.230 slit units per row from p1).
  - With the hard clip at +-0.50 slit-frame units (= +-0.435 pitch, `FIT_VERSION` 4) the 50 %
    half-width stays at 0.435-0.439 pitch at every wavelength. The measured one falls from 0.429
    (PRISM 0.6 um) to 0.388 (5 um), and on G395M from 0.414 (2.9 um) to 0.387 (5.2 um): up to
    0.05 pitch (0.25 rows) off in the red. Under the bar between two open shutters the model's
    floor is 0.49-0.61 of the open level against a measured 0.40-0.46. The model passed too much
    light near the shutter edges in the red (`scratch/claude/extraction-v2/bs_model_compare.py`,
    `v7_barshadow_{profiles,edges}.png`).
  - A narrower opening (the measured edge, tried and removed) fixes uniform light but makes the
    images of edge stars worse: uniform light and point sources disagree about where the edge is.
  - What fits uniform light and keeps the geometric edge the stars want is a loss across the
    shutter: the geometric shutter (+-0.50, a small fitted
    shift) transmitting T(x) = 1 - (1 - tedge) x^8 (x = position over the half-width), flat
    over the middle and falling to tedge at the edges. Light diffracted at the shutter edges
    leaves the spectrograph beam and is lost. Uniform light sees the loss as a narrower opening;
    a point source feels it mainly in its total, which the throughput normalisation carries.
    Fitted per disperser with the PSF split evenly before and after the shutter (section 3.7),
    the median misfit per wavelength is 0.0109 / 0.0184 / 0.0129 / 0.0137 (PRISM / G140M /
    G235M / G395M) against 0.061 / 0.035 / 0.043 / 0.082 with the hard clip.
  - T is capped at 1 (never above the centre's value). Without the cap the fit wanted T up to
    5 % above 1 off-centre (G395M) and fitted the bar shadow 3-8 % better (0.0097 / 0.0177 /
    0.0121 / 0.0124). Figures: `scratch/claude/psf_cal_T/figures/v7_T4_{edges,profiles}.png`.

**Implementation.** `nirspec/barshadow_calibration.py`, `data/Generate_Barshadow_Cals.py`,
tables `data/empirical_jwst_nirspec_barshadow_{prism,g140m,g235m,g395m}.fits`.
`[nirspec.stage2].empirical_barshadow` (default off) passes a table to `BarShadowStep` for the
background slitlets. The forward model's shutter: `profile_fit.SHUTTER`
(`data/nirspec_shutter_transmission.json`, fitted by `psf_calibration.fit_shutter`).

**Open.**
- (Done 2026-11-03, MB session.) Held-out check on the current b0c08026 tables, which the
  forward model's shutter is fitted to: each table derived without one field, with the code that
  made the shipped tables, and tested on it (gratings: spurs_egs, spurs_a2744, spurs_gdn
  trained, spurs_gds left out; PRISM: ember_egs_p1 left out, the other six pointings trained).
  Absolute difference inside the open area (|y| < 0.45 pitch), % of the open-shutter level:

  | Disperser | Empirical rms | Empirical max | CRDS rms |
  |---|---|---|---|
  | G140M | 0.3-0.7 | 1.8 | 1.1-2.3 |
  | G395M | 0.7-0.8 | 1.8 | 3-6 |
  | PRISM | 0.5-1.0 | 1.8 | 1.3-4.6 |
  | G235M (fewest training shutters) | 0.9-1.7 | 3.6 | 1.8-3.9 |

  As ratios at the outer edges inside the shutter: G140M 0.5 / 1.1 % (CRDS 3.9 / 4.7), PRISM
  1.2 / 1.6 % (9.0 / 2.5), G395M 1.1 % (9-11), G235M 2.6 / 2.2 %; the lower side of the interior
  bar 0.8-3.1 % against CRDS 13-28 %. On every disperser the left-out field sits slightly toward
  +y of the training fields: a flank of up to 7 % (PRISM, G395M), 10-25 % (G140M) or 13 %
  (G235M) as a ratio in the outermost 0.1-0.2 pitch, 1-4 % of the open level in absolute terms,
  a field-to-field registration difference no single table removes. So about 1 % of the open
  level (2 % on G235M) is the field-to-field limit inside the shutter for the shutter
  transmission. The QA-page rms (cut at |y| < 0.6) is not comparable with the 2026-10-30 values:
  since b0c08026 each cell is a median, and a ratio to the near-zero profile at 0.5-0.6 pitch is
  no longer bounded. Plots `heldout_{g140m,g235m,g395m,prism}.png`, QA PDFs, profiles and logs in
  `scratch/claude/bs_heldout_b0c0/`.
- (Done, kept for the record.) Whether the forward model's shutter edge (clip at 0.50, blur split 50/50) agrees with the
  measured uniform-sky profile: edge 50 % point, narrowing with wavelength (open region ±0.45
  pitch at 1 µm, ±0.38 at 5 µm on the prism), floor under the bars (extraction session; merge
  plan T0 item). **Checked 2026-11-03 (V7, `scratch/claude/extraction-v2/bs_model_compare.py`,
  figures `v7_barshadow_{profiles,edges}.png`): it does not agree in the red.** For uniform
  light the model is the open area (hard edges at ±0.50 slit-frame units = ±0.435 pitch)
  blurred by the after-shutter part of the shipped PSF and averaged over a row (dy 0.227-0.230
  slit units per row from p1). Its 50 % half-width stays at 0.435-0.439 pitch at every
  wavelength; the measured one falls from 0.429 (PRISM 0.6 um) to 0.388 (5 um), and on G395M
  from 0.414 (2.9 um) to 0.387 (5.2 um). The edge slopes match; the edge positions do not, by
  up to 0.05 pitch (about 0.25 rows) in the red. Under the bar between two open shutters the
  model's floor is 0.49-0.61 of the open level against a measured 0.40-0.46. The 2026-10-17
  star test chose h = 0.50 over 0.44 slit units on blue-dominated chi2; the measured edge is
  0.49 slit units in the blue and 0.45 at 5 um, so a single h was the wrong question: the
  effective opening narrows with wavelength. Consequences: the model passes too much light
  near the shutter edges in the red, for off-centre and extended sources, and its point-source
  transmission (the throughput normalisation) uses the too-wide edge; the star PSF was fitted
  with this edge, so the after-shutter PSF may be absorbing part of it. Resolved by the
  shutter transmission (Evidence above, section 3.7).
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
all exposures. The reported flux is the source's light inside a circle 0.8 arcsec across (1.5
shutter lengths) centred on it (user, 2026-11-04; before, the light that entered the target's
shutter). This replaces the fixed optimal profile, which loses light for extended and
off-centre sources.

**Decisions.**
- On by default (user, 2026-10-29).
- The reported flux (`fnu_prof`) is the fitted source's light inside a circle
  `profile_fit.REPORT_APERTURE` = 0.8 arcsec across, the round Gaussian of the fitted width
  (`reported_factor`, header `PFAPER`): the whole of a point source, and a bound on how far a
  wide or nearly uniform source is extrapolated. No second column (user, 2026-11-04: "I think
  the 1.5 shutter bound is the right choice"). The fit's spectrum is the source's total light:
  along the slit from the model at the fitted width, along the dispersion jwst's point loss
  times the stars' correction (`x_correction`) times the model's ratio for the fitted source
  (`x_extended_ratio`, the source taken as round). The 3-pixel boxcar, `fnu_opt` and the cal /
  s2d files stay on jwst's point-source calibration.
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

**Implementation.** `nirspec/profile_fit.py` (`PSF_TABLES`, `SHUTTER`, `CENTRE_LAW`, priors: centre 0.15
slit units from the planned position, width median 0.15), `nirspec/profile_chain.py`
(`choose_shape`, `ChainConfig`), `nirspec/profile_product.py`, `stage3.run_profile_fit_phase`.

**Open.**
- Default width 0.15 vs 0.21 slit units (user to decide; raised 2026-10-23). Since FIT_VERSION
  6 it sets a faint source's flux scale (19-28 % at 0.15). p1 own fits have median 0.143 and
  nothing supports 0.21 (review round, 2026-11-04). The width's spread (about +-15 % in flux)
  stays out of the reported error (user, 2026-11-04): `fnu_prof_err` is statistical only.
- Re-run the injection and shape tests on the merged pipeline, since the old figures are lost
  (extraction session).

**PSF and centre-law refit (2026-11-02; superseded by the three-component PSF below, and its
two-component code removed 2026-11-03).** The
October tables were measured on nod differences built with the path-loss correction still
applied. `nirspec/psf_calibration.py` refits them through the profile fit's own model:
`ProfileModel` (new optional `psf_tables` / `centre_law_tables`, defaults unchanged) on the
frames stage 3 reads, galaxy width at `W_FLOOR`, centre law off (`NO_CENTRE_LAW`). Data: the six
calibration fields of programs 12497 and 6642 reduced with the current stages 2a/2b
(`scratch/claude/e2e_nrscal`), PRISM only, 586 star x detector groups.
- Method: per star and range (the seven `PSF_LAM` ranges), core sigma, centre and drift free at
  a fixed wing fraction; then the chi2 profile over the wing fraction (0-0.6) with each star's
  width and centre refitted; the joint wing fraction minimises the clean stars' summed profiles
  (jackknife errors); stars refitted at it; core = 25th percentile. Weights: the extraction's
  variances plus a floor of (column peak / 30)^2, as in October.
- Selection: peak-pixel S/N >= 6 per range in the nod difference, width error < 0.1 rows, centre
  within 0.3 of the shutter pitch from the shutter centre, 3-MAD width outliers (per range, and
  star-wide from 1.0-1.5 and 2.6-3.3 um), reduced chi2 > 5 x median. 131 NRS1 / 133 NRS2 clean
  stars (October: 73 / 59; 66 / 49 in common; the rest of October's are outliers or edge stars
  here, `selection_vs_october.log`).
- First refit, nods at one shared position (superseded below): NRS1 core 0.322, 0.313, 0.332, 0.351, 0.376, 0.441, 0.609 rows, wing 0.341, 0.357,
  0.364, 0.384, 0.396, 0.344, 0.137; NRS2 core 0.323, 0.315, 0.318, 0.336, 0.395, 0.472, 0.586,
  wing 0.349, 0.373, 0.434, 0.459, 0.395, 0.295, 0.174. FWHM of core + wing 8-16 % narrower than
  the current table on NRS1 at 0.85-2.95 um, 1-12 % on NRS2, within 5 % in the red.
- But the stars do not prefer it: summed chi2 of the clean stars with the new table is 0-5 % above
  the current one at 0.7-3.3 um (1-7 % below it on NRS1 at 3.3-5.3 um), the narrow-side rule's
  price; a median-core table with the same wing fits
  0-1 % better than the current one in the blue, 5-10 % on NRS1 in the red. In the module's terms
  the current table sits near the population median, not on the narrow side.
- That refit found the nods misplaced: with a free position offset per exposure, bright stars'
  chi2 fell to 0.2-0.7 of its value, the offsets linear in the slitlet's detector row (+-0.08 rows
  at the edges, mirror-image for the two outer nods), and freeing them lowered the joint wing
  fraction by 0.05-0.11. The user chose a fixed offset law, no free parameter per source
  (2026-11-02).

**Nod offset law (2026-11-02, built, on by default: `profile_fit.NOD_OFFSET_LAW`).** Each
exposure's image of the source, and its negative copies in the other nods' differences, sits at
the source's planned position in that exposure (its own SRCYPOS), not at one shared position
(`nod_shifts`; shift = scale x (SRCYPOS_k - planned position), applied in `ProfileModel.centre`
before the centre law; `ProfileModel(nod_law=False)` is the old geometry).
- Measurement: per-exposure offsets fitted freely (no law) on 465 calibration stars, 5091
  exposure x range values, PRISM in all six fields and six gratings of 6642 obs 2
  (`psf_calibration.measure_offsets`; c4_p4 G140H too faint per exposure).
- Which description: the planned SRCYPOS differences (no parameter) take the weighted rms from
  0.0100 to 0.0043 slit units; a fitted (a + b x detector row) x nod step or (quadrant + b x MSA
  shutter row) x nod step does as well in sample (0.0043) but predicts a held-out observation worse
  (0.0048-0.0049 vs 0.0043); a law not proportional to the nod step is worse (0.0067). Nothing is
  left against detector column, row or wavelength (slopes 0 +- 1e-4 slit units). The planning
  metadata already carries the field-dependent nod step; the model dropped it.
- Scale: measured / planned = 1.03 overall, but per visit 0.85-1.21 for the PRISM, and the six
  gratings of 6642 obs 2 (0.5-0.88) follow that visit's PRISM (0.85): a visit effect, not a
  disperser effect, which a fixed law cannot follow. Scale 1 (the planned shifts as they stand).
- Effect: median chi2 of PRISM star fits 0.37 of the shared-position value above peak S/N 100,
  0.62 at 30-100, 0.93 at 10-30 (free offsets: 0.32 / 0.58 / 0.93); bright grating ranges of
  6642 obs 2 do not gain (1.01, 39 ranges). Out of sample (ember_cosmos_p1, six bright compact
  isolated PRISM sources and three G395M with < 2 % predicted contamination): chi2 on common pixels
  0.93-1.00 of without (brightest gains most), reported flux changes <= 0.3 %.
- A single exposure (no nod partners) is not shifted (its own SRCYPOS is the reference); frames
  with a placeholder SRCYPOS (0.000 next to real values) or without the source are not shifted.

**PSF refit with the law (pending the user's decision, tables unchanged).** Same method and cuts
as above; 141 NRS1 / 138 NRS2 clean stars.
- NRS1 core 0.334, 0.343, 0.367, 0.373, 0.393, 0.472, 0.607 rows, wing 0.315, 0.283, 0.281, 0.328,
  0.360, 0.294, 0.138; NRS2 core 0.338, 0.338, 0.360, 0.390, 0.406, 0.464, 0.571, wing 0.314, 0.317,
  0.324, 0.319, 0.366, 0.315, 0.221.
- Against the current table: FWHM of core + wing 6-12 % narrower on NRS1 at 0.85-2.95 um, within
  +-6 % elsewhere (NRS2 -5.8 to +1.1 %). Against the shared-position refit, at 0.7-3.3 um: the
  wing fraction 0.03-0.14 lower, the core 3-16 % wider, FWHM 1-8 % wider (closer to the current
  table); in the red within a few percent either way.
- Summed chi2 of the clean stars with the new table is 0-5 % above the current one at 0.7-3.3 um
  and 1-4 % above on NRS2 at 3.3-5.3 um (the narrow-side rule's price), 4-8 % below on NRS1 at
  3.3-5.3 um; the shared-position refit's table fits worse than or as well as the new one in
  every range.
- The PSF skew towards +y remains with the law: beyond +1.5 rows the stars carry 0.6-2 % of their
  light more than beyond -1.5 rows, where the symmetric model gives about the same on both sides.
- Centre law (no law applied, regressor the measured centre) unchanged by the nod law: within
  ~0.02 rows of `CENTRE_LAW` below 3 um; at 4.7 um NRS2 quadrant 2 -0.36 vs -0.25 rows, NRS1
  quadrant 4 -0.26 vs -0.21, slope with source position NRS1 -0.087 vs -0.107; NRS1 quadrant 1
  has no stars at 3.7-4.7 um (NaN; fill from the detector value before use).
- What a symmetric PSF leaves (each star's centre free in every exposure, `fit_centres`; figures
  `psf_stack_free_centres.png`, `star_own_width_<key>.png`): at the median star width the stack
  fits the core within ~1 % of the peak, but the star's own image is skewed towards slit +y on
  both detectors and at all wavelengths: a shoulder of +0.6-0.9 % of the light per pixel at +1.25 to
  +1.75 rows, a deficit of 0.3-0.8 % at -1.25 to -1.75, a tail of 0.2-0.5 % per pixel to +4 rows;
  the same whichever side the nod partners are on (so not their copies), at +-1.5 rows rather than
  on the core's steep sides (so not centring); about 1 % of the light in all. In bright stars it is
  beyond 5 sigma per pixel along the trace (own-width chi2/dof 5-60, edge stars up to 200, falling
  to 1-6 in the red); in a star of peak S/N 60, 3-5 sigma below 2.5 um and noise above 3 um.
  The pattern above 4.5 um is the core widening across 4.1-5.3 um (0.585 to 0.661 rows on NRS1):
  splitting the range at 4.6 um removes about two thirds of it. The table's last node is at 4.70 um
  and is held flat beyond it.

**A three-component PSF (2026-11-02; user: more terms, goal the free-centre stack within 1 % of
the peak in every range; tables unchanged pending the user's decision).** `profile_fit` now reads
a general PSF table (`psf_components`): a list of Gaussian components per wavelength node, each
with a share, a width, an offset towards slit +y, the share of its width before the shutter (0.5)
and whether its offset acts before or after the shutter. The two-Gaussian form was then its
special case and reproduced the previous model to 1e-12; the default was unchanged (both
removed 2026-11-03, below).
- What each term buys (40 brightest clean stars per detector and range, each at its own
  parameters linear across the range, centre free in every exposure; largest |median stack
  residual|, % of peak): core + wing at 2.2 x core 0.6-2.1 (8 of 14 ranges over 1 %); the wing with
  its own width and offset 0.2-0.7 (NRS2 4.1-5.3 um 1.1); plus a third component (the one-sided
  shoulder: share, width, offset) 0.08-0.85, all under 1 %, noise-only chi2/dof down to 1.4-4.0
  from 3-14. A skewed core was not needed.
- Before or after the shutter: centred stars cannot tell (chi2/dof equal to 0.01). Edge stars
  (|c| >= 0.3) prefer both offsets after the shutter (wing offset: chi2/dof 8.81 -> 8.08 at -y
  edge, 3.53 -> 3.29 at +y edge), and the shoulder (+1 to +1.5 rows) is still present for stars at
  the +y edge, where light displaced before the shutter would fall on the bar: the displacement
  arises after the shutter (spectrograph or detector). Form `wing3_after`.
- Fit: every clean star (279) in eight ranges over 0.6-5.3 um (the red end split at 4.7 um),
  every parameter and the centre linear in wavelength across each range, a free offset per
  exposure. A smooth fit of each star over the whole range (7 nodes, ~58 parameters) was tried:
  90 s a star and the components trade (shoulder width at its bound, wing share jumping), so the
  per-range fits feed the tables and the whole-range fit only the single-star figures.
- Table rules tested on the free-centre stack (all clean stars): (A) core at the 25th percentile of
  the stars, the rest median: 4.5-11 % (the narrowest cores are a trade with the shoulder, at the
  0.25-row bound); (B) every parameter the median: 0.6-3.3 %; (J) one PSF per detector and range
  fitted jointly to up to 60 bright stars (each with its own centre, offsets, spectrum; chi2 scaled
  by its own reduced chi2), the ranges joined at their ends: 0.44-0.63 % on NRS1 at 0.7-3.3 um,
  0.49-0.91 % on NRS2 at 0.7-1.0 and 2.0-3.3 um, 1.01-1.07 % on NRS2 at 1.0-2.0 um, 1.1-1.6 % at
  3.3-5.3 um (16-53 stars); (Jn) J with the core narrowed by the stars' 25th / 50th percentile
  (0.69-0.97): 1.8-8.5 %; the current table 1.1-4.3 %.
- Proposed rule (the user's call): the shared shape (wing share, width, offset; shoulder share,
  width, offset) by a joint fit, since the stars' individual values trade against each other and
  their medians do not make a PSF; the core width joint as well, since in a three-component PSF
  the narrow side of a per-star core percentile is the shoulder trade, not narrower stars (Jn
  shows what the narrow-side rule costs). If the narrow-side rule is kept, it is better applied
  to the whole profile (e.g. a FWHM percentile) than to the core alone.
- Open: parameters at their bounds in J (shoulder width 3.0 rows on NRS1 at 2-2.6 um; wing offset
  -1.5 rows at the 5.3 um node, few stars); the red end above 4.7 um rests on 8-19 bright fits and
  the shape there pools both detectors; edge stars still leave several sigma in the blue.

**The PSF table is a smooth joint fit (2026-11-02; user decision: a joint fit of the whole profile
to the stars, not a percentile rule, every parameter smooth in wavelength over 0.6-5.3 um; form
`wing3_after`; tables and extraction defaults unchanged pending adoption).** Candidate table S,
`psf_table_wing3_after_S.json` (general layout, 61 log-spaced nodes).
- Fit (`smooth_joint_fit_all`, `step_smooth_joint`): one PSF per detector, no pooling. Each of the
  seven parameters (core width; wing share, width, offset; shoulder share, width, offset) is a
  natural cubic spline in ln(lambda) through 7 nodes (0.6, 0.9, 1.4, 2.1, 3.0, 4.1, 5.3 um), fitted
  to all bright clean stars of every range at once (up to 40 per range at peak S/N >= 10: 269
  star-ranges from 49 stars on NRS1, 238 from 58 on NRS2). Each star-range keeps its own centre,
  drift and per-exposure offsets, solved in the same least-squares problem as the splines (sparse
  Jacobian), and its own spectrum (linear). Weights: the floored variances, no per-star rescaling.
  Start: table J at the nodes. Curvature penalty on the second differences of the node values
  (`SMOOTH_TAU`): 307 (NRS1) and 164 (NRS2) against chi2 1.5 and 1.9 x 10^5, so the data set the
  shape. Bounds widened for the fit (shoulder width 0.15-6 rows, wing offset +-3, wing width
  0.4-6).
- Why one problem: alternating the centres and the shape converged slowly, because a star's
  centre trades with the wing and shoulder offsets. On synthetic stars made with a known smooth
  PSF, after four alternations the profile was still off by 1.6-5 % of the peak; the one-problem
  fit recovers it to 0.1 % (unit test). The per-star chi2 rescaling of the per-range J fits held
  that fit near its start, which biased the earlier smooth runs; J itself was fitted that way.
- Runtime: 7-17 min per fit (10-19 iterations), six fits (both detectors x all / half A /
  half B) in parallel, as long as the alternating version took (7-15 min).
- Bounds: NRS1 shoulder width at the lower bound 0.15 rows at the 5.3 um node (also in half A and
  half B); half B also has the shoulder share at 0 at 4.1 um on NRS1 and the shoulder width at
  0.15 at 4.1 and 5.3 um on NRS2; half A has the NRS2 core at 0.25 rows at the 0.6 um node (no
  data below 0.7 um). NRS2 on all stars hits none. In the red the shoulder is a narrow feature
  2-2.8 rows towards +y (width 0.15-0.4 rows, about 1-3 % of the light), narrower than a pixel,
  so the pixel integration leaves its width poorly constrained; the bound was not widened further.
- Free-centre stack test, all clean stars, largest |median residual| in % of peak (S / J /
  current table): NRS1 0.41 / 0.45 / 1.40 (0.7-1.0 um), 0.80 / 0.63 / 1.88, 0.47 / 0.44 / 1.27,
  0.35 / 0.45 / 2.00, 0.54 / 0.60 / 2.42, 1.02 / 1.12 / 2.62, 0.87 / 1.63 / 4.26 (4.1-5.3 um);
  NRS2 0.35 / 0.49 / 1.14, 0.42 / 1.07 / 1.52, 0.58 / 1.01 / 2.37, 0.74 / 0.82 / 3.01,
  0.96 / 0.91 / 2.33, 1.31 / 1.25 / 2.10, 1.31 / 1.57 / 2.59. S is under 1 % in 11 of 14 ranges;
  over in NRS1 3.3-4.1 um (1.02) and NRS2 3.3-5.3 um (1.31 in both): a dip of about -1.3 % at
  +2 rows on NRS2 3.3-4.1 um, where the shoulder turns from a broad halo into the narrow red
  feature.
- Held-out test (clean stars split at random by star, seed 20261102; fit on one half, stack-test
  on the other; in-sample for comparison): fit A on B / fit B on A / A on A / B on B. NRS1 0.66 /
  0.41 / 0.33 / 0.52 (0.7-1.0 um), 1.11 / 0.48 / 0.86 / 0.78, 0.93 / 0.54 / 0.50 / 0.74,
  0.34 / 0.83 / 0.58 / 0.41, 0.76 / 1.22 / 0.84 / 0.66, 1.19 / 1.49 / 0.91 / 0.95,
  1.76 / 1.56 / 1.10 / 0.96 (4.1-5.3 um); NRS2 0.61 / 0.37 / 0.45 / 0.36, 0.42 / 0.51 / 0.46 /
  0.53, 0.73 / 0.61 / 0.92 / 0.81, 0.65 / 0.99 / 0.87 / 0.75, 0.88 / 1.09 / 0.89 / 0.63,
  1.49 / 1.15 / 1.25 / 1.27, 1.86 / 2.86 / 1.88 / 3.04. Below 3.3 um the held-out numbers are
  within 0.4 % of peak of the in-sample ones (largest held-out 1.22 %, NRS1 2.6-3.3 um): the table
  describes the stars, not the sample. Above 3.3 um a half has 8-27 stars per range and both held-out and in-sample numbers
  rise to 1.2-3 %: the stack there is limited by the number of stars, and half B's red end is
  fitted with its shoulder at the bounds.
- Pooling the detectors above 4.1 um: not needed for the fit to converge or stay off its bounds
  (NRS2 all-stars hits no bound; NRS2 wing width at 5.3 um 2.60 +- 0.13 rows, NRS1 2.89 +- 0.11;
  formal errors at fixed star parameters, not scaled by chi2/dof). The two detectors' red-end
  shapes agree within those errors on the wing, the shoulder offset (2.82 vs 2.64 rows) and the core
  (0.71 vs 0.70 rows). Pooling would reduce the held-out noise there but cannot be tested with
  this sample; S keeps the detectors separate.
- Single stars at their own smooth PSF (`star_own_psf_<key>.png`; regenerated after fixing a
  reference fit that clipped the bright cores on the unfloored variance): chi2/dof 4.4, 1.3 and
  13 for the three stars shown; a single star's components still trade (shoulder width at its
  bounds), so the single-star fits are figures only.
- Proposed: adopt S (the user's call). Open: the 3.3-5.3 um ranges, which rest on 16-53 stars and
  whose shoulder width sits at or near its lower bound; the edge stars' blue residuals.
- **Adopted 2026-11-03 (user decision).** S is the extraction's PSF: `PSF_TABLES` is read from
  `campfire_pipeline/data/nirspec_profile_psf.json` (table S as fitted), `FIT_VERSION` 4. No
  test runs with the old table (user, 2026-11-03): the image test writes out the formula for the shipped file and checks the default
  model against it; the calibration tests' reference fits use the shipped table. Two tests had
  depended on the old PSF. The shutter test of a PRISM shape transferred to a grating checked
  for no light beyond a fixed margin past the closed shutter; the three-component red wing
  (sigma up to ~3 rows, after the shutter) puts 0.18 % of the peak there. It now checks what
  holds for any PSF: the image with the bottom shutter closed plus the image through that
  shutter alone equals the image with all three open (clipping comes before the blur), and the
  missing light is not negligible. The neighbour-scan test now fits the target at its true
  shape. That is not a PSF effect: with an equally bright, unmodelled neighbour one shutter
  away, a free target fit has its lowest chi2 merged with the neighbour (centre +0.6, width 0.9)
  under both PSFs (chi2 92k merged vs 150k true with the legacy table, 85k vs 143k with S); the
  old PSF's starting grid happened to stay in the true basin, S's slides into the merged one.
  Next: the p1 rerun from stage 2a, and the flux change of bright sources against the previous
  p1 products.
- **The old two-Gaussian PSF is removed from the code (2026-11-03, user: "remove the old
  stuff").** `profile_fit` has no `LEGACY_PSF_TABLES`, `PSF_K` or `psf_params`, and
  `psf_components` reads the general layout only; `PSF_LAM` stays as the nodes of `CENTRE_LAW`.
  `psf_calibration` lost the steps whose only purpose was the two-component table (the wing
  fraction scan and its joint fit, the percentile table `psf_table_new.json`, the refit at the
  joint wing fraction, the old/new samples, summary and figures, the free-centre and own-width
  steps). What stays now starts from the shipped PSF. The star fits (`fit`, records in
  `stars/`) take the shipped PSF with a free core width (`width_tables`: the core scaled to S
  rows at the range centre, the wing and shoulder kept). The selection reads that width with
  the same cuts. The centre law is its own step (`centre_law`). The forms start from the shipped
  parameters with the core scaled to the star's width. The nod offsets use the same width fit.
  `table_test` compares candidates with the shipped table. A rerun of the calibration therefore
  starts from `fit` and `select`. The results above (selection, J, S) came from the
  two-component star fits, which were not rerun.
- **p1 full rerun with S (2026-11-03).** `cfpipe nirspec run --stage2a --stage2b --stage3
  --overwrite -p 12` on `scratch/claude/e2e_p1`, all merged changes (master background 1.7 in
  stage 2a, drizzle-once, background revision, nod law, S): 681 products, no failures; stage 3
  took 62 min. The previous products (all `PFVER` 2) are in
  `scratch/claude/extraction-v2/p1_before_psf`. The master-background session's failed-open
  edge check on these products passes (PRISM full-transmission shutters within +-2 % across
  the shutter, against +15-22 % at the edges in 1.6; partial shutters up to 14 % over-painted on
  one edge, the one-number limit; PRISM nrs1 failed-open shutter 16 reads 18-25 % above MBKG2D
  in its core, light in the shutter for the contamination side).
  Flux change from the PSF alone (`psf_flux_check.py`: the brightest clean sources fitted with
  `profile_fit.fit` on the same frames with S and with the two-Gaussian table rebuilt for the
  comparison; figure `extraction-v2/psf_flux_change_p1.png`): S raises the flux of all of them.
  PRISM (20): median +4 % at 0.7 um, +2 % at 1-1.5 um, ~0 at 2.5 um, +3.5 % at 5 um (sources
  +1.2 to +3.4 % integrated); G395M (4 with S/N > 5 pixels): +0.7 % at 2.9 um rising to +3.5 % at
  5.2 um. The fits' chi2 hardly changes on galaxies (common-pixel ratio 0.89-1.04, most within
  1 %); fitted widths shrink by 0.005-0.015 slit units. Against the PSF-free 5-pixel box in the
  same products the profile flux went from 1.000 / 1.010 / 1.016 (PRISM median, 0.7-1.5 /
  1.5-3 / 3-5.3 um) to 1.014 / 1.013 / 1.036. Cause: the stars put light 2.5-4 rows from the
  centre (the wide red wing, the blue shoulder) that the old PSF did not have, and the fit now
  counts it. All products against the saved ones (all changes together): bright PRISM +1.5 %,
  G395M +2.1 % (median), so the PSF is most of this rerun's flux change. Open (user): whether
  jwst's flux calibration counts all the light through the shutter (then S's fluxes are right)
  or was set with a narrower extraction (then part of the 2 % is counted twice).
- **Second round, everything from S (2026-11-03, user-approved).** The aim was to close the loop.
  S's shape was fitted freely, but its star selection, variance floors, pixel masks and start
  came from fits with the old table. In this round:
  - every star was refitted with S (floor and mask from S, the core width free on S's core);
  - the stars were selected again with the same cuts;
  - the smooth joint fit was run per detector on all stars, starting from S's own spline
    coefficients, giving table S2.

  Outputs: `scratch/claude/psf_cal_s2/` (scripts in `scripts/`, `s2_compare.log`,
  `psf_table_wing3_after_S2.json`, figures `psf_stack_tables_S2.png`,
  `psf_parameters_S2.png`). Runtime: star fits 5 min (6 processes), smooth fits 9 min each,
  stack test 3 min.
  - Masks: unchanged. Of 7.7 million pixels in the star and range fits, the used-pixel counts
    change by 285 in all, under 0.01 %. The 5-sigma clip on the floored variances removes
    almost nothing with either table.
  - Selection, as clean star-ranges:
    - NRS1 641 to 609: 40 left (19 as star-wide width outliers, 13 as range outliers, 6 edge,
      1 S/N, 1 width at its bound) and 8 entered.
    - NRS2 524 to 513: 32 left (22 outliers, 4 range outliers, 5 edge, 1 S/N) and 21 entered
      (19 former edge star-ranges, the edge test now reading the S fit's centre).

    Why the outliers change: the width is now the core of S with S's wing and shoulder held, so
    on NRS2 at 1.0-1.5 um the population's spread grows (MAD 0.045 against 0.023 rows) about a
    lower median (0.310 against 0.376). Stars at both ends become outliers (cores 0.15-0.23 or
    0.45-0.48 rows). Seven former outliers are back, among them the two widest (0.85 and
    0.95 rows before). The bright subset the smooth fit uses barely moves: 269 star-ranges
    from 50 stars on NRS1 (49 before), 238 from 58 on NRS2 (the same).
  - How far S2 is from S: on NRS1 the two coincide, with every node value within its formal
    error or a few hundredths of a row. The largest profile difference (PSF before shutter and
    pixel, % of peak) is 0.16-0.61 % at 1.0-4.1 um and 1.06 % at both ends, where the data stop.
    On NRS2 the share between wing and shoulder moves at 1.5-3 um: at 2.1 um the wing share goes
    from 0.29 to 0.34 and the shoulder width from 1.08 to 1.42 rows; at 3.0 um the wing width
    goes from 1.28 to 1.14 rows. These moves are many times the formal errors. The profile
    differs by 1.1-2.2 % of peak (largest at 2.0-3.3 um).
  - Free-centre stack test on the new selection (S2 / S / J, largest |median residual|, % of
    peak):

    | Detector | 0.7-1.0 | 1.0-1.5 | 1.5-2.0 | 2.0-2.6 | 2.6-3.3 | 3.3-4.1 | 4.1-5.3 um |
    |---|---|---|---|---|---|---|---|
    | NRS1 | 0.27 / 0.44 / 0.45 | 0.77 / 0.81 / 0.71 | 0.47 / 0.48 / 0.45 | 0.61 / 0.58 / 0.48 | 0.62 / 0.66 / 0.69 | 0.81 / 0.98 / 1.11 | 0.86 / 0.97 / 1.64 |
    | NRS2 | 0.30 / 0.32 / 0.48 | 0.42 / 0.42 / 1.10 | 0.63 / 0.52 / 0.93 | 0.97 / 0.70 / 0.81 | 0.55 / 0.96 / 0.99 | 0.83 / 1.27 / 1.38 | 1.37 / 1.48 / 1.75 |

    S2 is under 1 % in 13 of 14 ranges and S in 12. S2 is better at NRS2 2.6-4.1 um and worse
    at NRS2 1.5-2.6 um. In the stack plots the residual curves of the two lie on top of each
    other within their uncertainty bands.
  - Verdict:
    - NRS1: S is the self-consistent answer; the old floors, masks and selection did not bias
      it.
    - NRS2: the profile is fixed to about 1 % of peak in the data, but at 1.5-3 um the split
      between wing and shoulder is not. It moved by up to 2 % of peak in the PSF itself when
      the selection and floors changed. That is the size of the half-to-half differences of
      the held-out test, and the stack test does not prefer one split overall.

    S does not need replacing for accuracy. S2 is the self-consistent table and meets the 1 %
    goal in one more range; adopting it is the user's call. A third round would show whether
    NRS2 settles or keeps trading wing against shoulder.
- **What the PSF is for, and the order of work (user, 2026-11-03).** The split between core,
  wing and shoulder does not matter; what matters is a smooth description of the profile with
  wavelength that brings the residuals down, because the PSF is only used to convolve source
  profiles. What does matter is the part that acts before the shutter and the shutter edge
  itself, which set the clipping and the throughput normalisation. Neither S2 nor a third round
  is adopted. The bar-shadow check (V7, section 3.2) found the model's shutter opening too wide
  in the red, so the next round is, in order: (1) the edge position a smooth function of
  wavelength, fitted with the after-shutter blur to the measured bar-shadow tables per
  disperser; (2) the stars refitted with that edge, keeping the three-component form; (3) the
  star stack, the bar shadow, the edge stars and the p1 flux change checked together; (4) a
  free-form PSF basis (fixed Gaussians after the shutter with smooth amplitudes) only if
  residuals beyond 1 % remain where stars are plentiful.
- **Shutter edge: option (d) chosen (user, 2026-11-03).** The measured edge (fitted to the bar
  shadow; built on `claude/psf-table` 7eb4ead8) fixes uniform light but makes edge stars 2.4 %
  worse in median chi2, so uniform light and point sources disagree about the edge. The
  shipped hard edge stays the default. Next: a geometric edge with a smooth transmission
  across the shutter (light diffracted at the edges lost from the beam), fitted to the bar
  shadow, checked against the edge stars, the centred stars and the p1 fluxes, speed measured
  first.
- **Smooth transmission T adopted as the default shutter model (user, 2026-11-03).** Built on
  `claude/psf-table` a0457e16: bar-shadow misfit as low as the measured edge (PRISM 0.010,
  G395M 0.012), edge stars neutral (median chi2/dof 1.004 of the shipped model), centred stack
  unchanged, point-source transmission against position close to jwst's point path loss above
  3 um, p1 median fluxes 0.2-0.5 % lower, fits 22-26 % slower. Before shipping: T capped at 1
  (it exceeded 1 by up to 5 % on G395M near 5.3 um), the measured-edge form removed,
  `FIT_VERSION` 5; then p1 stage 3 rerun. G140H / G235H / G395H use the M grating's
  measurements with the same filter (user, 2026-11-03). The grating bar-shadow tables the
  default now depends on were settled on 2026-10-30 (user, in the MB session); open: their
  held-out check on the current (b0c08026) tables.
- **The measured shutter edge (2026-11-03; tried, not adopted, removed).** A narrower opening
  per disperser and wavelength (half-width 0.49 slit units in the blue to 0.45 at 5.3 um, a
  shift, a larger after-shutter share), fitted to the bar shadow, matched uniform light (median
  misfit 0.010 / 0.018 / 0.012 / 0.013, PRISM / G140M / G235M / G395M) but made the images of
  edge stars worse (median chi2/dof 1.015 of the hard edge's, 1.051 with the half-width alone),
  and the stars refitted with it (table SE) changed little. Uniform light and point sources
  disagreed about where the edge is. The edge, its file and its code were removed when the
  transmission below was adopted (outputs in `scratch/claude/psf_cal_edge/`).
- **The shutter model: the geometric shutter with a smooth transmission T (adopted 2026-11-03,
  user; `FIT_VERSION` 5).** Outputs: `scratch/claude/psf_cal_T/` (scripts in `scripts/`).
  - **Model** (`profile_fit.SHUTTER`, `shutter_at`; file `data/nirspec_shutter_transmission.json`
    with its provenance; header card `PFSHUT` names its version next to `PFVER`). The shutter is
    geometric, +-0.50 slit units about its centre moved by a small fitted `shift`. Inside it
    light is transmitted with T(x) = 1 - (1 - tedge) x^8, x the position over the half-width:
    1 at the centre, flat over the middle, falling to `tedge` at the edges and never above 1.
    Half of every PSF component's width^2 acts before the shutter and half after
    (`AFTER_SHARE`, the split S was fitted with; the first version fitted this share as `after`,
    see below). The image is [(galaxy x PSF before) x T, within the open
    shutters] convolved with the PSF after, then pixel-integrated. The point transmission in
    the throughput normalisation (`transmission`, hence `throughput`, `main_shutter_factor`,
    `boxcar_factor`) includes T, so the jwst point path loss and the model stay consistent.
    Physically: light diffracted at the shutter edges leaves the spectrograph beam and is lost.
    Uniform light sees that loss as a narrower opening; a point source feels it mainly in its
    total, which the normalisation carries. G140H, G235H and G395H use the M grating with the
    same filter (decided, user 2026-11-03).
  - **Fit, first version** (`psf_calibration.fit_shutter`, `shutter_table`; the shipped refit with
    the share held at 0.5 is below): `shift`, `after` and `tedge`, each
    a natural cubic spline in ln(lambda) (7 nodes for the PRISM, 4 per grating), fitted to both
    bar-shadow tables at once (one open shutter and the bar between two) with the shipped PSF S,
    soft-L1 residuals and a gentle curvature penalty. No parameter is at a bound.
    - PRISM: `tedge` falls from 0.85 (0.6 um) through 0.62 (2.1 um) to 0.01 (5.3 um); `after`
      0.45-0.55; `shift` within +-0.005.
    - G395M: `tedge` 0.47 (2.9 um) to 0.03 (5.3 um); `after` 0.44-0.57.
    - G140M: `tedge` 0.80-0.75, `after` 0.56-0.50, `shift` -0.013 to -0.016.
    - G235M: `tedge` 0.70-0.61, `after` 0.49-0.44, `shift` -0.007 to -0.009.
  - **Why x^8, capped.** T may not exceed 1. Uncapped, a quadratic + quartic form fitted the bar
    shadow best but rose up to 5 % above 1 off-centre (G395M). Capped, in that form the quadratic
    went to zero at every node; with a free mix of x^4 and x^8 (T monotone, at most 1) the fit took
    x^8 alone at every node of every disperser, so the shape parameter was dropped. Per column,
    x^12 fits slightly better below 3.5 um but reaches T(1) = 0 from 4 um and fits worse there.
  - **What the cap costs** (PSF S throughout; same tests and stars as the uncapped round):

    | Data set | Hard edge (`FIT_VERSION` 4) | T uncapped (x^2 + x^4) | T capped (x^8, `FIT_VERSION` 5) |
    |---|---|---|---|
    | Bar shadow, median misfit, PRISM / G140M / G235M / G395M | 0.061 / 0.035 / 0.043 / 0.082 | 0.0097 / 0.0177 / 0.0121 / 0.0124 | 0.0105 / 0.0183 / 0.0129 / 0.0133 |
    | Edge stars, median chi2/dof against the hard edge (312 star-ranges) | 1 | 1.004 (45 % better) | 1.018 (38 % better; per range and side 0.95-1.09) |
    | p1 flux against the hard edge, median over sources | — | 0.2-0.4 % lower (PRISM), 0.5 % (G395M) | 0.1-0.4 % lower (PRISM), 0.1-0.7 % (G395M) |
    | p1 flux, integrated per source (20 PRISM, 4 G395M) | — | 0.983-1.014 | 0.984-1.014 |
    | Fit time, PRISM / G395M source | 1.34 / 4.07 s | 1.26x / 1.22x | 1.76 / 5.24 s (1.31x / 1.29x) |

    - The 50 % half-width of the open region and the floor under the bar follow the measured
      ones as closely as uncapped (`figures/v7_T4_{edges,profiles}.png`).
    - Edge stars: the cap costs 1.4 % in median chi2/dof, mostly at 1-3.3 um toward -y (up to
      1.09 on NRS2 1.5-2.0 um); at the red end the capped and uncapped models agree
      (`figures/t4_edge_stars.png`). With the uncapped T the stars were as well fitted as with
      the hard edge; capped they are about where the measured edge left them.
    - Fluxes: the off-centre sources move as before (879604 -1.4 %, 632554 -1.6 %, 919995
      +1.4 % integrated; `figures/t4_flux_change_p1.png`). Centred stars need no PSF refit:
      the after-shutter share stays near the 0.5 S was fitted with.
    - The extra time is the eighth moment of the truncated normal (the recursion to n = 8 in
      `profile_fit._window_kernel`, numba); a Gaussian wider than the half-width takes 16-point
      Gauss-Legendre instead, where the recursion loses precision.
  - **The split fixed at 0.5 (user, 2026-11-03; the shipped model, `geomT-2026-11-03b`).** What
    the edge stars dislike (`figures/t5_worst_edge_stars.png`): the worst cases are stars whose
    centre is only 0.4-0.7 rows from the shutter edge, so the peak sits on the edge; the capped
    T dims the light just inside the edge more than the data show (one model peak at 0.8 where
    the star reaches 1.0, residuals of 2-4 % of the peak at the edge), up to 35-47 % worse chi2
    for those few. The bar-shadow fit had moved the after-shutter share to 0.44-0.57, away from
    the 0.5 PSF S was fitted with. With the share held at 0.5 (`profile_fit.AFTER_SHARE`; only
    `shift` and `tedge` fitted; `scripts/fit_T5.py`, `t5_stars.py`): bar shadow 0.0109 / 0.0184 /
    0.0129 / 0.0137, edge stars median 1.007 (better than the hard edge in 41 % of star-ranges;
    left: about 3 % worse toward -y at 2.6-3.3 um on both detectors, and 1.08 on NRS2 4.1-5.3 um
    -y from 5 star-ranges). `tedge`: PRISM 0.85 to 0.02, G140M 0.81-0.75, G235M 0.70-0.60, G395M
    0.45-0.04. The share is no longer a parameter of the shutter file or of `fit_shutter`.
    Possible reason the edge stars and uniform light still differ: the light lost to diffraction
    at an edge may depend on how the shutter is lit (filled, or one bright spot at the edge),
    which one T for every source cannot carry. Not tested.
  - **jwst's path-loss reference** (`pathloss_0010`, MOS1x1; `figures/pathloss_check.png`, made
    with the uncapped T): its point loss against position, PS(y)/PS(0), falls more steeply
    toward the edges than the hard edge (0.67-0.73 at y = +-0.40 against 0.82-0.91). T moves the
    model toward it at every wavelength and matches it within 0.04 at 4-5 um; in the blue jwst's
    PS stays steeper than T (0.70 against 0.86 at 0.9 um).
  - **Code.** `ProfileModel(shutter=...)` (None: `SHUTTER`) and `for_placement` take a shutter
    model in `load_shutter`'s layout; the models `profile_product` and `mbkg_extraction` derive
    from the chain's model carry its `psf_tables` and `shutter`. The fitted pixels
    (`fit_pixel_masks`, `_region`), `nod_scan.delta_grid` and the row selections in `nod_scan`,
    `profile_contam`, `trace_overlap` and `slitlet_neighbours` keep the nominal +-0.50, which
    contains the open area whatever the shift. `psf_calibration` takes `--shutter` for its star
    fits.
  - **p1 stage 3 rerun with `FIT_VERSION` 5 (2026-11-03).** Stages 2a / 2b from the morning's
    full rerun, stage 3 with `--overwrite -p 12` (`run_stage3_shutter5.log`): 681 products,
    no failures; stage 3 took 65 min against 62 with the hard edge. Against the morning's
    products (`extraction-v2/p1_before_shutter`, `FIT_VERSION` 4, same frames;
    `extraction-v2/shutter_flux_fig.py`, figure `shutter_flux_change_p1.png`), integrated new/old
    over pixels at S/N > 5: PRISM 62 sources median 0.998 (16-84 % 0.995-1.000), off-centre
    (0.25 slit units or more from the shutter centre, 28) 0.984-1.009; G395M 25 sources median
    0.997, off-centre 0.937-1.066. Per wavelength the median is -0.1 to -0.5 % (PRISM, most at
    the red end) and -0.4 to -0.7 % (G395M). The largest moves are all sources whose fitted
    centre is near or past the open edge (|offset| 0.29-0.57: G395M 920048 +6.6 %, 922891
    +6.3 %, 880165 -6.3 %; PRISM 925660 and 925403 -1.5 %).
  - **Check 1: the same star at different positions in its shutter (2026-11-03, PRISM).** A point
    source at its planned position gets jwst's point path loss whatever the shutter model (the
    model's own transmission cancels in `throughput`), so stars cannot choose between shutter
    models; they test the normalisation both share. 18 stars of program 12497 seen in more than
    one MSA configuration (c4_p1..p5; IDs 10034, 10043, 10050 differ in spectral shape between
    visits and are not the same object), each visit fitted as the extraction does
    (`extraction-v2/star_repeat.py`, figure `star_repeat_per_star.png`). Near-centre visits agree
    to 1-4 % (4210, five visits). Visits near an edge come out brighter, most in the blue: 3982 at
    -0.386 against +0.018 is +80 % at 0.7-1.5 um, +62 % at 1.5-2.5, +24 % at 2.5-3.5; 6826 (+0.373
    against -0.301) +73 % to -7 %; 3505 (-0.344 against -0.155) +28-31 %; 5209, 3018 +10-20 %.
    Others go the other way (4887 at +0.229: -17 % at all wavelengths). The hard edge gives the
    same. In every edge visit the fitted centre sits 0.05-0.09 slit units further out than
    planned (3982: -0.477 against -0.386), so the fit corrects for more clipping than the planned
    position implies. Not yet decided which is wrong: the fitted position of stars near the edge,
    or jwst's point path loss there. Holding the star at its planned position is not a clean test
    (near-centre visits then scatter by +-25 %). Next: the light in the shutter measured without
    a model, between visits of one star on one detector (3982, NRS1 in c4_p3 and c4_p5); the
    NIRSpec path-loss calibration programs with stars at deliberate offsets in the shutter
    (1493, 4465, 9278) would settle it.
- **A diffraction model of the PSF and the bars together (2026-11-03, in progress; user: keep going
  until a model describes the combined PSF + bars for the forward model).** Scratch prototype in
  `scratch/claude/extraction-v2/edge/` (`fourier1d.py`, `optics.py`); nothing in the pipeline yet.
  - Why. Measured without a model (`raw_ratio.py`: the light of a repeated star in a fixed window,
    path loss removed), stars near an edge keep more light than either jwst's point path loss or the
    shipped model predict (3982 at -0.386: 0.91 of its central visit at 0.7-1 um; jwst 0.76, shipped
    model at the fitted position 0.57). Their light really peaks where the fit puts them
    (`edge_profiles.png`; chi2 10-35 x lower than at the planned position), and over 35 visits the
    fitted position is 1.16 x SRCYPOS (rms 0.013 about 1.15 x, 0.036 without): SRCYPOS is in shutter
    pitches, the model's slit position in open heights (1 pitch = 1.15). jwst's MOS1x3 path-loss
    table's y axis also runs in pitches (-1.5..1.5). A constant pre/post split cannot satisfy the
    bar shadow, the edge-star shapes and their transmitted light at once (`explore_split.py`):
    the telescope's own image is much narrower than the observed PSF in the blue and as wide by
    3 um, and light diffracted at the shutter edges is partly lost at the spectrograph's pupil stop
    and partly spread under the bars, so a filled shutter and a point source lose light differently.
  - Model. One-dimensional Fraunhofer optics along the slit: the telescope's field at the MSA (the
    pupil's Fourier transform at each wavelength, D = 6.5 m), times the open shutters, through a
    pupil stop rho x the telescope pupil, imaged on the detector with a Gaussian blur, integrated
    over a row; uniform light is the incoherent sum over sources. Fitted to the bar-shadow tables
    with three numbers shared by every wavelength: PRISM rho 0.976, blur 0.031" (-0.003"/um), no
    obscuration, median misfit 0.015 (`fit_fourier_bs.py`, `fourier_bs_prism.png`); G395M rho 1.000,
    blur 0.031", misfit 0.016. A round pupil fits 0.019 (rho 0.96). The centred point image from the
    same numbers matches the measured star PSF's core at 1.5-4.5 um with nothing fitted to stars
    (`fourier_vs_psf.png`). The rest of a star's PSF (blue width and wings: the telescope's
    wavefront error and jitter; the one-sided shoulder) is an empirical kernel applied on the sky
    before the shutter: placed on the detector instead it raises the bar-shadow misfit to 0.022-0.027,
    placed before the shutter it leaves uniform light untouched. One kernel per detector, fitted to
    the centred PSF S (`fit_kemp_det.py`, within 0.2-1.6 % of its peak), then corrected from the
    star stack (`fit_kemp_iter.py`).
  - Engine (`optics.py`). Tables of point images per open-shutter pattern (wavelength x source
    position x detector position), a Gaussian galaxy as a Gauss-Hermite sum over them;
    `OpticsModel` is `ProfileModel` with `image` and `transmission` from the tables. Throughput:
    jwst's point path loss at the exposure's position, its own position dependence divided out
    with its MOS1x3 table, our transmission at the centre in the denominator (`norm='model_y'`): the
    absolute level and the dispersion-direction loss stay jwst's, the slit-direction loss is the
    model's. Fit time about the shipped model's.
  - Results so far.
    - Check 1 (the same star at different positions): rms of ln(flux / central visit), bands
      0.7-1 / 1-1.5 / 1.5-2.5 / 2.5-3.5 / 3.5-5 um, shipped 28 / 25 / 27 / 15 / 17 %, near-edge visits
      38 / 32 / 34 / 15 / 20 %; optics 12 / 9 / 19 / 11 / 17 %, near-edge 7 / 10 / 24 / 9 / 21 %.
      Fitted centres 1.144 x SRCYPOS, rms 0.013 about 1.15 x. 4461 (77 % at 1.5-2.5 um under every
      model) and 2828 (two detectors, 43 % in the blue) are not position effects.
    - The red-end scatter follows the position across the shutter (SRCXPOS): stars far off-centre
      in x come out relatively bright, so jwst's dispersion-direction loss also falls too steeply.
      The same diffraction model across the 0.20" width (SRCXPOS in 0.27" pitches) over-corrects
      more (`norm='model_xy'`: 3.5-5 um 22 % against 17 %). Kept jwst's for now; the path-loss
      calibration programs are the test.
    - Star stack, free centres, all clean stars: per-detector kernel from S, within 1 % in 9 of 14
      ranges (shipped PSF S 12; median worst residual 0.94 against 0.68 %); per star-range chi2/dof
      the same as the shipped model (median ratio 1.001 over 1,127 star-ranges, better in 47 %).
      After one correction of the kernels from the stack residuals (`fit_kemp_iter.py`,
      `kemp_slot_DET_it1.json`): 13 of 14 ranges within 1 %. Check 1 with those kernels:
      12 / 8 / 9 / 11 / 16 % (4461 excluded).
    - p1 PRISM fluxes (40 brightest clean sources, `p1_optics_flux.py`): centred compact sources come
      out 0.995 / 1.001 / 1.023 / 1.033 / 1.039 x the shipped model in the five bands. The light of
      each exposure's own source is the same in both models; the difference is how much of a
      source's light lands in the other nods' rows, which the nod subtraction removes from the
      trace. Light spread evenly across the slitlet subtracts like background, so nod differences
      cannot measure it, and its amount sets the absolute flux (`debug_design.py`).
    - The wide light measured directly (`mb_star_stack.py`): 232 clean stars, single frames with the
      master background subtracted, profiles stacked about the fitted trace. Share of a star's light
      beyond 0.8 slit units of its trace, five bands: data 0.8 / 0.9 / 1.1 / 1.5 / 2.2 %; shipped
      model 0.4 / 0.3 / 0.05 / 0.2 / 0.6 %; slot-pupil optics 0.7 / 1.1 / 1.9 / 2.8 / 3.7 %. The
      shipped model has almost no wide wing; the slot pupil has twice the real one in the red. The
      excess is diffraction (the hard-edged 1-D pupil's 1/y^2 wing), not the kernel
      (`wing_budget.py`). The faint p1 galaxies are too noisy for this test, and comparing the
      nodded and master-background fluxes at a fixed shape does not separate the models (both move
      together, within 1 %).
    - Changed model (in progress). Telescope pupil amplitude (1 - u^2)^alpha across the slit
      (alpha 0 the slot; 0.5 the chord of a round pupil seen through a narrow strip). The bar shadow
      prefers alpha 0 (misfit 0.0137; 0.25: 0.0161; 0.5: 0.0193); at alpha 0.25 (rho 0.996, blur
      0.034" - 0.0026"/um) the diffraction's wide light falls below the data at every wavelength, so
      a non-negative kernel can supply the rest. The kernel is now a free table over source offset
      (31 nodes, -2..2 slit units), fitted linearly (`fit_kernel_linear.py`): the image is linear
      in the kernel, so per star R = model pixels per unit kernel weight at each offset (nod
      differences and master-background single frames, the fitted spectrum and shape held), normal
      equations summed per detector and band, a non-negative solve with a penalty on the wing's
      second differences; three iterations with the stars refitted. The penalty and the symmetry
      were chosen by fitting half the stars and scoring the other half (`klin_cv.py`): the wing
      smoothed, the core free, not symmetric (forcing symmetry is clearly worse on held-out stars).
      Fitted with the production centre law, the kernel took up the law's centring errors as a
      shoulder one row out and the free-centre star stack failed it (5 of 14 ranges within 1 %).
      So the fit now uses the stack test's own state (`fit_kernel_ranges.py`): every clean star
      in every range where it is clean, the range fitted by `psf_calibration.RangeFit` with the
      optics model (centre and drift free per range, an offset per exposure, no law, the S/N
      floor), R built at that state; one kernel per detector and range, linear in ln(lambda)
      between range centres. Two iterations converge (integrals 1.000 +- 0.006).
    - Result (`klin_r2_DET.json`; alpha 0.25, rho 0.996, blur 0.034" - 0.0026"/um). The kernel: a
      narrow core, a shoulder at 0.2-0.3 slit units (about one detector row; stronger on the + side),
      and a wide wing (2-5 % of the light beyond 0.3 slit units, 0.4-0.9 % beyond 0.8).
      - Star stack, free centres: 12 of 14 ranges within 1 % (median worst residual 0.77 %; shipped
        12 of 14, 0.68 %); per star-range chi2/dof equal to the shipped model's (median ratio 1.000
        over 1,127 star-ranges, better in 50 %; 0.993-0.997 at 2.6-5.3 um).
      - Single frames (master background subtracted): mean wing residual within 0.04 % of the
        peak in every band (shipped +0.05 to +0.16 %).
      - Check 1: 12 / 8 / 8 / 12 / 15 % (shipped 28 / 25 / 22 / 15 / 17 %); edge visits
        5 / 10 / 10 / 11 / 19 % (shipped 38 / 32 / 28 / 15 / 20 %), median edge-visit bias
        +3 / -1 / +1 / -4 / -10 % (shipped +25 / +10 / +21 / +13 / -18 %). The red end's deficit is
        the dispersion-direction loss (above), not this model.
      - p1 PRISM, 40 brightest clean sources: centred compact sources 1.014 / 1.010 / 1.021 /
        1.026 / 1.020 x the shipped flux (the wide light the shipped model lacks); near the edge
        0.94 / 0.99 / 1.03 / 1.09 / 1.19; chi2/dof median 1.092 against 1.095, lower in 55 %.
      - Engine fixes on the way: the tables now span +-5.5 slit units in source and detector
        position (wide galaxies and long slitlets were cut at 2.8 / 3.0), and a wide galaxy is
        summed over source positions at most 0.025 slit units apart (8 Gauss-Hermite nodes left
        ripples at the bars: 925389, width 0.56, chi2/dof 1.31; now 1.098 against 1.101 shipped).
    - New calibration stars (2026-11-03). NGC 2506 (program 1493, 10 PRISM visits, 3-shutter
      nods) reduced in the scratch root: 317 clean stars no fit had seen, 79-162 per range,
      95 / 79 at 4.1-5.3 um (21 / 17 before).
      - Star stack, free centres, `klin_r2` on all 317: 12 of 14 ranges within 1 % (median worst
        0.81 %, rms 0.29); shipped model on the same stars 10 of 14 (0.76 %); 4.1-5.3 um
        0.83 / 0.73 % against 1.46 % on NRS2.
      - Wide light (master-background single frames): the faintest third of either star set has a
        negative wide share (-1 to -10 %), flat in distance: a master background a little too
        high, not a wing. The brightest third agrees between the two sets at 0.7-3 um (1.0-1.2 %
        of the light beyond 0.8 slit units). At 3-4 / 4-5.2 um the old set had S/N 16 / 5 and gave
        1.6 / 2.5 %; NGC 2506's brightest third (S/N 95 / 52) gives 1.3 / 1.5 % (about 1.5 / 1.6 %
        after the background bias seen in the fainter thirds). `klin_r2` predicts 1.5 / 2.2 %.
      - Refit with the new stars (`klin_r4`: old stars + all NGC 2506, plain normals). Held-out
        test first (old stars + NGC 2506 half A, scored on half B): 12 of 14 within 1 %, median
        worst 0.73 % (`klin_r2` on the same stars 11 of 14, 0.80 %; shipped 12 of 14, 0.86 %);
        4.1-5.3 um 0.62 / 0.51 % (`klin_r2` 0.93 / 0.82 %). A free master-background scale per
        frame (`KLIN_SKY_NUISANCE`) scores the same on the stack but raises the red wing (2.5 %
        beyond 0.8 at 4-5.2 um): a slowly falling wing and a background offset trade off across a
        3-shutter slitlet, so it is not used. The red wide light stays too strong (2.1 % against
        about 1.6 %): it is the diffraction's own (alpha 0.25), which the kernel, non-negative,
        cannot remove.
    - Program 4465 (one star stepped across its shutter). MPT "virtual" sources: negative source
      IDs (the pipeline skips them as background) and the default planned position. The open
      column is row 250, columns 1-171, in quadrants 3 and 4. Star A sits in quadrant 4 column 25;
      the data place it at the shutter centre, near -0.33 pitch (5 configurations) and +0.34 to
      +0.43 pitch, against the bar (5). In the scratch root its MSA file is rewritten
      (`msa_rewrite_4465.py`, original kept as `.orig`): a 7-shutter slitlet per star (all 7 are
      open), a positive ID per configuration, the planned y from the measured centroid. Caveat:
      the rewritten slitlet moves with the nod; the pipeline differences nods on the common
      detector frame, so the differences are right. Star B (86-87 shutters along) is 17 x
      fainter and too faint to use. Pipeline fix found on the way: the profile fit's row height
      was NaN on a slit's end rows, which slitlets longer than three shutters reach (every fit
      failed); fixed with a test (commit f5edf189). Fits per range, centre free (`test_4465.py`,
      `test_4465_r2.png`): centred, the two models are equal (chi2/dof ratio 0.98); against the
      bar the optics model is better (median 0.75; at 2-3.3 um 0.87 -> 0.42, 1.34 -> 0.72) but
      worse at 0.7-1 um, a +-4 % residual pair at the core; at -0.33 pitch the same pattern
      (median 1.14). The blue core of an off-centre star is the open shape problem. Fluxes need
      each configuration's position across the shutter (dispersion direction), which the
      program steps too (peak rate varies 3x at the same y); not measured yet.
    - Converged kernel on all stars (`klin_r5`, one more linearisation of `klin_r4`; integrals
      within 0.2 %). Star stack: NGC 2506 14 of 14 within 1 % (median worst 0.59 %), the original
      stars 6 of 14 (1.03 %, up to 1.75 % at 1.0-1.5 um; `klin_r2`, fitted on them alone, 12 of
      14). The original stars' residual has data above the model at the centre and below it one
      row out: their cores are sharper. The fitted core width S per observation (`core_by_obs.py`)
      spreads about +-3 % between visits at 0.7-2 um and NGC 2506 is about 4 % broader than the
      original visits (NRS1 0.7-1 um: 0.365-0.386 against 0.347-0.368 rows); at 4.1-5.3 um every
      visit has 0.65. Pointing jitter or small wavefront changes between visits, most visible in
      the narrow blue core. Open decision (to the user): one kernel for all, a kernel from the
      sharper visits with each source's free width taking up the blur of the others, or a kernel
      per observation. Fluxes do not depend on it: check 1 with `klin_r5` is 11.6 / 8.3 / 8.1 /
      12.0 / 15.3 % (edge visits 5.7 / 10.3 / 10.0 / 10.9 / 18.7 %), as `klin_r2`.
    - Edge stars (`test_edge.py`, 142 NGC 2506 stars with |centre| >= 0.3, `klin_r4`): optics /
      shipped chi2/dof per range at the bar 0.88 / 0.71 / 0.83 / 0.83 / 0.89 / 0.67 / 0.52, better in
      61-96 %; at peak S/N >= 80 0.50 / 0.59 / 0.62 / 0.66 / 0.73 / 0.87 (78-89 %). The blue misfit
      of 4465's star is not seen here: it belongs to that program's setup.
    - A softer spectrograph stop (amplitude (1 - (f / rho f_max)^2)^beta) tried for the uniform bar
      shadow and the red wide light (`fit_fourier_ab.py`). Fitted to the bar shadow with the blur
      free, alpha 0.25 / beta 1 gives misfit 0.0120 (hard stop 0.0161) and little wide diffraction,
      but its blur is 0.044" growing to the red (0.030" before); the kernel, before the shutter,
      cannot sharpen it, and the held-out star stack fails (4 of 14 within 1 %, up to 4.9 %).
      With the blur held at the stars' value a softer stop only worsens the bar shadow (0.0162 ->
      0.0195). So the hard stop stays, and the tension is the blur itself: the uniform bar shadow
      wants about 0.044", point-source cores about 0.03" in the red (perhaps smoothing within the
      empirical bar-shadow tables). With the blur written as fitted (s0 + s1 (lam - 3)), the
      diffraction alone puts 1.52 % of a centred star's light beyond 0.8 slit units at 4.6 um,
      about what the bright stars show; the kernel's 0.3 % wing on top makes the model's 2.1 %:
      compact-source fluxes about 0.5 % high at 4-5 um. Every kernel now carries its diffraction
      numbers beside it (`<prefix>_diffraction.json`, from the normals; `optics.diffraction_params`),
      and the table cache is keyed by the kernel file's content (a review hazard).
    - Review findings (independent review of the prototype, 2026-11-03). No double counting in
      the throughput normalisation. The shipped model in check 1 ran with unscaled positions;
      with 1.15 x SRCYPOS it scores 22.8 / 19.3 / 13.5 / 11.7 / 22.3 % against the optics model's
      11.5 / 8.3 / 8.1 / 12.1 / 15.2 %. Hazards for integration: the table cache keyed by the
      kernel's file path (now keyed by content), `for_placement`, where `scale_positions` is
      applied, long slitlets (the profile fit's end-row bug now fixed), and the PL_REF / MOS1x1
      normalisation.
    - The PSF core broadens with brightness, not with the visit (2026-11-03). Decision (user): a
      kernel for the sharper core with each source's free width taking up the rest. Within every
      visit a brighter star has a broader core: the shipped PSF's fitted core width grows 0.026
      rows per dex of peak S/N at 0.7-2.6 um on NRS1 (0.054 at 0.7-1 um on NRS2), 4-5 sigma, and
      the visit means move together once that trend is removed (`core_vs_snr.py`). NGC 2506's
      stars are 5-8 x brighter than the original ones, which is most of the "4 % broader". The
      optics model's free source width shows the same with the wing in the model
      (`w_vs_snr.py`): at 0.7-2.6 um stars above peak S/N 63 need 0.02-0.03 slit units, stars
      below none; no trend in the red. Charge spreading in bright pixels fits that. Galaxies are
      faint, so the kernel is the faint stars' core.
      - The free width could not move at first: a star needs 0.01-0.03 slit units, under two steps
        of the tables' source grid, and the 8 Gauss-Hermite nodes over the linearly interpolated
        table give a non-smooth function of the width. Compact sources (width <= 0.06) are now
        summed exactly over the interpolated table (a triangle convolved with a Gaussian,
        `_compact_weights`; equal to a brute-force sum within 1e-5).
      - Kernels tried, all scored with each star's width free (`optics_table_test_w.py`): from
        the sharper visits below 2.6 um (`klin_s1`): original stars 10 of 14 within 1 %; from
        stars below S/N 63 alone (`klin_f1`): loses the wide wing (nothing beyond 0.8 slit units
        at 2.3 um), not used; all stars with each star at peak S/N >= 63 fitted with its own width
        carried into its normal equations (`fit_kernel_ranges.py`, `KLIN_FREE_W_SNR=63`; the
        width convolves the kernel, `freew.gauss_matrix`), two linearisations from `klin_s1`
        (`klin_w2`, integrals within 0.3 %, 0.7 % of the light beyond 0.8 slit units in the
        blue).
      - `klin_w2`: NGC 2506 14 of 14 ranges within 1 % (median worst 0.44 %; `klin_r5` 0.59 %;
        shipped 10 of 14, 0.76 %); the original stars 12 of 14 (0.75 %; `klin_r5` 6 of 14,
        1.03 %; shipped, fitted on them, 12 of 14, 0.68 %). The two misses (NRS1 1.0-1.5 um
        1.5 %, 4.1-5.3 um 1.2 % with 21 stars) are single-bin dips the shipped model shares,
        absent from the larger NGC 2506 set. The shipped model's NRS2 red shoulder (-1.5 % at
        +1.8 rows, 3.3-5.3 um) is gone (`w2_stacks_NRS1.png`, `w2_stacks_NRS2.png`).
    - The path-loss normalisation and partial slitlets (review hazard). jwst uses MOS1x1 for a
      slitlet with both neighbours closed and MOS1x3 otherwise, at y -+ 1 when the source sits in
      an end shutter; both tables repeat the same profile in every shutter, so jwst's point path
      loss has no loss to closed neighbours (MOS1x3 at y - 1 / y 0 = 1.0000, MOS1x1 / MOS1x3 at
      the centre = 1.0000; `endshutter_loss.py`). The model takes that loss once, through each
      exposure's own open shutters (0.7-1.2 % for an end shutter, 1.8-3.4 % for a single one):
      nothing is counted twice, no change.
    - G395M. The PRISM diffraction numbers fit the G395M bar-shadow tables with misfit 0.017
      (PRISM 0.016); refitted to G395M, no better (`barshadow_g395m.py`). The same 6642 stars in
      G395M (`test_g395m.py`) are too faint for the kernel: 4-7 stars per range at peak S/N >= 8;
      chi2 equal to the shipped model's (median ratio 0.98-1.00), stacks within their noise. (The
      gratings first kept FIT_VERSION 5; since 2026-11-04 they take the same model, below.)
    - Pipeline (FIT_VERSION 6, approved by the user 2026-11-03). `nirspec/optics.py`: the engine,
      `Optics` per disperser and detector, tables cached under `$CAMPFIRE_ROOT/cache/optics`
      (keyed by the model's numbers and the kernel, built under a lock file, memory-mapped),
      `jwst_point_y_ratio` from the path-loss file the cal file names. `ProfileModel` takes it for
      every exposure whose disperser has a model unless PSF or shutter tables are given
      (`model_optics`; `for_placement` too), so the fit, contamination placement, the
      master-background extraction and the joint pass all use it. PRISM positions in slit units
      at load (`source_position`, `Exposure.src_y_jwst`, `Exposure.pl_ref`). Header card
      `PFOPTIC`. The model file `data/nirspec_optics.json` is written from the scratch kernel by
      `ship_optics.py`; the port reproduces the prototype's tables exactly (`check_port.py`).
      Tests: `test_nirspec_optics.py`; the suite runs the PSF tables unless a test asks for the
      model (`tests/conftest.py`), so no test builds real tables.
      - Check 1 with `klin_w2`: 11.5 / 8.4 / 7.6 / 12.0 / 15.5 % (edge visits 5.2 / 10.5 / 9.3 /
        11.0 / 18.9 %), as `klin_r5`: the kernel choice moves core shapes, not fluxes.
      - The pipeline on real data (`p1_pipeline_v6.py`, the 20 brightest p1 PRISM sources, read
        only, production fit): the pipeline's model equals the prototype's to 2e-7 on every
        exposure (image, throughput, design matrix; the shipped file's rounding); fitted centres
        agree within 0.003, which is the optimiser's stopping point, not the model. FIT_VERSION 6
        over 5, median per band 0.6-1 / 1-1.8 / 1.8-3 / 3-4.2 / 4.2-5.3 um: centred sources
        (|SRCYPOS| < 0.2, 12) 1.006 / 1.011 / 1.023 / 1.024 / 1.016; off centre (8) 0.93 / 0.99 /
        1.06 / 1.12 / 1.25; chi2/dof 0.3-0.7 % lower.
    - One model for every disperser (user, 2026-11-04: "gratings should not be on a different
      version"). `optics.optics_for(detector)`; the model file has one `diffraction` block and a
      kernel per detector (version `2026.11-w2`, same numbers). Removed: the PSF and shutter
      tables, `psf_calibration` and its tests, `PFSHUT`; the FIT_VERSION 4 / 5 CHANGELOG entries
      folded into FIT_VERSION 6's (never released). `nirspec/optics_calibration.py` keeps the star
      loading, the per-range star fit through the model (centre, drift, exposure offsets and width
      free) and the stack test (`stack` / `score`). Tests run a closed-form stand-in
      (`tests/optics_standin.py`) unless they ask for the shipped model.
      - The module's stack test on all 16 PRISM calibration visits (12497 obs 1-5, 6642 obs 2,
        NGC 2506; 1155 star-detectors; `oc_full/`): every range within 1 % of the peak, worst
        |median residual| NRS1 0.41 / 0.37 / 0.26 / 0.40 / 0.42 / 0.60 / 0.46 %, NRS2 0.49 / 0.59
        / 0.45 / 0.53 / 0.70 / 0.78 / 0.43 % over 0.7-5.3 um (84-223 stars per range). Clean
        star-ranges: peak S/N >= 20, |centre| < 0.35, no saturated trace pixel, the star's median
        width <= 0.06 slit units (wider: a galaxy, a pair or a contaminated star) and chi2/dof
        within 5 x the range's median. On the stars it shares with the scratch test the samples
        are identical.
      - Gratings on 6642 obs 2 (`test_gratings.py`, `grat_w2/`, figure `final_gratings.png`):
        the shared model against each grating's FIT_VERSION 5 tables, chi2/dof ratio per
        detector-range 0.95-1.03 in 18 of 20 ranges (3-7 stars each, peak S/N >= 20); worst
        stacked residual 0.8-2.0 % against 0.7-1.9 %. G140H NRS2 above 1.4 um fits worse (1.07,
        1.22; worst residual 3.2 %): those stars' cores are sharper than the PRISM kernel and the
        free width can only broaden it. The FIT_VERSION 5 tables lack the red wings there as on the
        PRISM (G235M NRS2 3 um).
      - Check 1 on NGC 2506 (`check1_ngc2506.py`, `check1_ngc_spec.py`; 93 stars in two or more of
        the ten configurations, 245 visits, each visit against the star's most central one on the
        wavelengths both cover; saturated and gap-split bands left out; figure `final_check1.png`):
        clipped scatter 7.0 / 12.2 / 6.3 / 7.3 / 12.6 % in 0.7-1 / 1-1.5 / 1.5-2.5 / 2.5-3.5 /
        3.5-5 um, no trend with the position along the slit (correlation <= 0.13). FIT_VERSION 5 on
        the same frames: 18.3 / 19.3 / 17.9 / 12.9 / 13.2 %, flux rising with distance from the
        shutter centre by 4-7 % per 0.1 slit units (correlation 0.41-0.49) below 3.5 um.
      - The along-dispersion loss (jwst's point path loss, x dependence). At 3.5-5 um jwst's table
        under-corrects off-centre stars: -47 % per unit SRCXPOS (correlation -0.51; a star 0.3 of
        the x pitch off centre comes out ~14 % low); below 2.5 um no trend. The diffraction model
        in x (one open shutter of half-width HX, the pupil, the stop, the slit-direction kernel
        and an extra blur SX, the acquisition / astrometry error) fits the whole sample best at HX
        0.110", SX ~0.03" (`xfit.py`; clipped scatter 9.3 -> 7.9 %, the red trend gone), but fitted
        on half the stars it predicts the other half worse than jwst's table (9.9 / 9.8 % against
        9.5 / 7.4 %); only 3.5-5 um improves in both halves (12.7 -> 10.4, 12.5 -> 10.8 %). On the
        12497 stars it is neutral. Not adopted: jwst's x dependence stays.
      - jwst's path loss in the fit: stage 2 types every source as a point source
        (`set_stellarity = 1.0`), so jwst's point / uniform choice never applies to a science
        source; the fit divides the applied correction out of the data and keeps only jwst's
        point table's level at the shutter centre (0.98-1.00, paired with the photom calibration)
        and its x dependence. The cal / s2d files and the 3-pixel boxcar still carry jwst's point
        correction at the planned position.
      - Galaxies (`fig_final_galaxies.py`, `final_galaxies.png`): three p1 PRISM sources (compact
        centred, wide, 0.45 off centre) and two G395M sources fit with chi2/dof 0.94-1.14, noise-
        like residuals in the continuum; emission lines leave residuals where the line emission
        sits differently from the continuum (one shape for both).
    - The kernel calibration in the repo (`optics_calibration` `kernel` / `solve`, 2026-11-04).
      On 29 star-ranges it gives the scratch fit's normal equations to rounding, and its solve
      gives the shipped kernel from the scratch normals to 1e-11. A further pass at the shipped
      model on all 16 PRISM calibration visits (547 stars, 2158 star-ranges, the stack run's
      clean selection; `oc_kernel/`) holds 0.996-1.003 of the light per range and changes a
      point source's image by at most 0.37 % of its peak (0.06-0.2 % in most ranges): the shipped
      kernel has converged and stays. Neighbouring core nodes in the red move by up to half
      their value between passes without changing the image (the PSF blurs them together).
    - The centre law and nod law remeasured through the model (`optics_calibration laws`,
      `oc_laws/`; 497 stars). The scatter of each star's centre change from its 1.0-2.6 um
      centre, per range 0.85 ... 4.7 um, NRS1: no law 0.015 / 0.007 / 0.002 / 0.007 / 0.018 /
      0.033 / 0.052, the old law 0.004 / 0.002 / 0.001 / 0.002 / 0.008 / 0.018 / 0.024, the new
      law held out by observation 0.003 / 0.002 / 0.001 / 0.002 / 0.004 / 0.006 / 0.011; NRS2
      at 2.95-4.7 um 0.004 / 0.010 / 0.013 -> 0.004 / 0.007 / 0.010. In sample and held out agree.
      The slope with the star's position (alpha) is near zero now: the old PSF needed -0.11 at
      4.7 um to stand in for what the diffraction does. Adopted (`profile_fit.CENTRE_LAW`). The
      planned nod shifts take the exposure offsets' rms from 0.0137 to 0.0060; the scale measured
      / planned is 0.95-1.10 per visit (one visit 0.68 on 26 star-exposures): scale 1 stays.
    - A correction to jwst's along-dispersion point loss (`Optics.x_correction`, a model-file
      `x_loss`): exp(-b (lambda - 2 um)+ x^2), x = SRCXPOS. Fitted to the NGC 2506 pairs of check 1
      (`optics_calibration.fit_x_loss`, `xcorr_ramp.py`): b 0.592 / 0.559 on the even / odd stars,
      0.572 on all. Held out, per band 0.7-1 / 1-1.5 / 1.5-2.5 / 2.5-3.5 / 3.5-5 um: unchanged
      below 2.5 um; 7.1 -> 6.0 and 5.6 -> 5.3 % at 2.5-3.5 um; 12.7 -> 10.7 and 12.5 -> 10.8 % at
      3.5-5 um; the trend with |x| from -28 / -54 % per unit to within +-13 %. A free b per band
      flips sign between the halves below 2.5 um (noise). On the 12497 repeat stars it is
      neutral: their x barely changes between visits (rms change of x^2 0.04). At 4.5 um and
      x = 0.3 it raises a flux by 13 %. Wired into the throughput; not yet in the shipped file.
    - What the product should report for an extended source (user, 2026-11-04: open to revisiting
      "the light that entered the target's shutter"; no second column, a bounded aperture instead
      of the shutter, 1.5 or 3 shutter lengths). Test on p1 PRISM (`xsize_p1.py`,
      `xsize_analyze.py`, figure `xsize_p1.png`): the 106 clean sources fitted with FIT_VERSION 6,
      92 with COSMOS v1.2 auto fluxes at S/N >= 10 in f150w-f444w; spectrum / photometry per
      source (median over bands, top-hat bands), medians in sextiles of the fitted width at 2 um
      (sigma 0.03 to 0.3 arcsec):
      - in the shutter (today): 0.87 / 0.67 / 0.60 / 0.55 / 0.46 / 0.33, scatter 0.21 dex;
      - total with the slit-direction model only: 0.87 / 0.67 / 0.62 / 0.62 / 0.54 / 0.54;
      - total with the along-dispersion transmission of an extended source as well (the model in
        x: one shutter 0.20 arcsec wide, pupil, stop, kernel; the source round, its x width the
        fitted width): 0.95 / 0.78 / 0.78 / 0.81 / 0.70 / 1.09, scatter 0.145 dex, no trend;
      - inside a circle 0.8 or 1.6 arcsec across (1.5 or 3 shutter lengths) of the fitted
        Gaussian: the same except the widest sextile (1.03, 1.09).
      The level of about 0.8 does not follow SRCXPOS (correlation +0.20, the off-centre half
      higher) or SRCYPOS; it is lower for sources measured in more bands (0.77 against 0.86), so
      it looks like a calibration difference with wavelength between the PRISM and the
      photometry, not a loss the model misses. (Resolved below, review round 2026-11-04: it is
      the comparison, light in 0.8 arcsec against a Kron total.)
    - Adopted: 0.8 arcsec (user, 2026-11-04). End to end with the shipped code (`endtoend_x.zsh`,
      `xsize_pipeline.py`, figure `xsize_pipeline.png`): the p1 sources' reported flux over the
      photometry by size sextile, before 0.87 / 0.65 / 0.58 / 0.55 / 0.44 / 0.32 (scatter 0.20
      dex), now 0.93 / 0.77 / 0.77 / 0.75 / 0.68 / 1.03 (0.16 dex).
    - The first end-to-end run found the along-dispersion ratio unstable at the shutter edge: the
      model's point transmission fell from 0.90 to 0.33 between 0.05 and 0.10 arcsec from the
      centre, and stars planned there (SRCXPOS up to 0.45) with fitted widths of 0.03 arcsec got
      factors up to 2.2 (`xratio_probe.py`; check 1 at 0.7-1 um 7.0 -> 9.9 %). The x model now
      takes the half-width and position scatter the stars' x loss fitted (`xfit.py`: 0.110 and
      0.030 arcsec): at the edge the ratio is 1.02-1.05 for small widths, near the centre about
      as before (0.71 -> 0.73 for 0.09 arcsec).
    - Check 1 on NGC 2506 with the x correction and the ratio in the fit (`check1_ngc_spec_x.log`),
      clipped rms per band 0.7-1 / 1-1.5 / 1.5-2.5 / 2.5-3.5 / 3.5-5 um: 8.0 / 11.5 / 6.5 / 6.1 /
      9.4 % (before 7.0 / 12.2 / 6.3 / 7.3 / 12.6 %); the trend with |x| at 3.5-5 um -47 -> -7 %
      per unit. The 0.7-1 um band is a little worse (MAD-sigma 5.1 -> 5.5 %, about 1.5 sigma):
      stars with fitted widths of 0.02-0.05 arcsec come out 6 % higher there, the width taken
      as the star's size along the dispersion. For a bright star part of that width is the
      core's broadening with brightness, which may not happen before the shutter. Open.
    - Open. The bar shadow of uniform light is the alpha 0.25 diffraction's (misfit 0.016 against
      the FIT_VERSION 5 edge transmission's 0.011), which matters for wide galaxies, and the blur
      it wants is larger than the stars allow; the model's wide light at 4-5.2 um is 2.1 % against
      about 1.6 %. The along-dispersion loss at 3.5-5 um. G140H NRS2 cores (a per-disperser
      after-shutter blur or kernel, needs more bright grating stars). The blue core of 4465's
      star. The fit of a faint star can fall into a second minimum (2401 in check 1). Fit time
      for wide galaxies. Check 1 itself (`check1_ngc2506.py`, `check1_ngc_spec.py`) is still
      scratch. What the product reports for an extended source (below, 2026-11-04).
    - Review round (2026-11-04). An independent read of the model (a separate review agent, asked
      for logical consistency and reliability, given the code and this document, not our
      conclusions) found the slit-direction model coherent and counted once (jwst's y loss moved
      to the centre exactly; its table is separable and repeats per shutter; jwst's bar-shadow
      step skipped for science slits) and the weak side along the dispersion. Its findings and
      what was done:
      - `boxcar_factor` (a neighbour's pass 1 from its 3-pixel boxcar when its own fit failed)
        divided by the model's point transmission at the planned position, while the s2d holds
        the forward model divided by the path loss stage 2b applied; the two differ by jwst's y
        loss against the model's (about 20 % near the shutter edge) times the along-dispersion
        terms. Fixed: it is now the three rows of the model frames in the s2d's units (test
        `test_boxcar_factor_is_what_the_model_frames_hold`).
      - An exposure naming no path-loss reference keeps jwst's y loss in its data while the
        model applies its own: now a warning at load. Docstrings that said `fnu_prof` equals
        `fnu_opt` for a point source corrected (only at the shutter centre).
      - The ~0.8 level against photometry (`cat_extract.py`, `phot_match.py`, figure
        `phot_match.png`; the catalog's native-PSF apertures 0.05-1.0 arcsec,
        `~/cosmos_v1.2/aperate_config.toml`). In the catalog itself the light inside 0.75 / 1.0
        arcsec is 0.75-0.82 / 0.81-0.89 of the Kron total. Against the catalog's own 0.8 arcsec
        aperture flux (between 0.75 and 1.0) the 106 own-fit sources come out at 0.96 +- 0.04
        (median per source; per band f115w-f444w 0.95 / 0.91 / 0.96 / 0.93 / 0.92 / 0.94),
        scatter 0.15 -> 0.12 dex; the sum over sources 0.96 +- 0.02. The aperture flux holds the
        PSF-blurred source and ours the source before the PSF, so a point source would sit a few
        per cent above 1. The level was the comparison, not a calibration difference.
      - Fitted width against the catalog's half-light radius (`size_vs_rh.py`, figure
        `size_vs_rh.png`): rank correlation +0.73 (F200W, 60 sources) / +0.75 (F444W, 33), the
        measured radius 1.6 x what a Gaussian of the fitted width blurred by a Gaussian PSF
        predicts. The fitted width follows the source's size but describes its core, not its
        half-light radius (the PSF approximation without wings explains part of the factor).
      - Default width (finding: the reported flux of a source below the gate scales with the
        assumed width through `x_extended_ratio`, by 19-28 % at 0.15 and 40-50 % at 0.21 over
        0.8-4.5 um). `dwidth_p1.py`: the 43 cleanest default-shape PRISM sources of p1, refitted
        at 0.15, 0.21 and free. Against the 0.8 arcsec aperture flux, median per source 0.87 +-
        0.15 at 0.15 and 1.17 +- 0.20 at 0.21 (the own-fit sources 0.96); too few and too faint
        to decide alone. Own-fit widths at 2 um (`width_by_snr.py`): median 0.143 slit units, no
        trend with S/N (0.160 / 0.135 / 0.139 for gate S/N 25-40 / 40-80 / >80, quartiles about
        0.10-0.22); free fits below the gate 0.133. Nothing in p1 supports 0.21. The spread
        (quartiles 0.10-0.22) is the real uncertainty of a source without a measured size: about
        +-15 % in flux. It stays out of the error column (user, 2026-11-04).
      - Not yet addressed: the x correction was fitted and checked on the same NGC 2506 pairs
        (held out by star, but pairs share reference visits) and never tested on the gratings;
        the FIT_VERSION 5 check-1 numbers in the CHANGELOG mix the SRCYPOS unit fix with the
        model change; `fnu_prof_err` is statistical only by decision (user, 2026-11-04: no
        shape, kernel, centre-law or x-loss term; check 1 measures 6-12 % for the same star at
        different positions); the x
        transmission reuses the kernel's one-sided slit-direction shoulder.
    - Data for the along-dispersion loss (search, 2026-11-04; `xcal_mast_search.py`,
      `xcal_keywords.py`, `xcal_msa_repeats.py`, `xcal_pattern.py`, `xcal_archive.py`; MSA files
      in `scratch/claude/extraction-v2/xcal_msa/`). Sources planned at two or more positions
      across the shutter, from the MSA files:
      - 1133 (commissioning, "Slit Loss Characterization for MSA shutters", PRISM, June 2022):
        34 stars, each at 20 positions on a grid x 0.15-0.85, y 0.3-0.9 of the shutter (0.5 the
        centre); 128 uncal files, 40 GB, not downloaded.
      - 12497 obs 4 (G140H): 45 stars stepped along the dispersion at fixed y, x 0.02-0.94 in steps
        of 0.13, plus one PRISM configuration; on disk and through stage 2 in the scratch root
        (`nrscal_wavecal_c4_p4`). Below 1.9 um only.
      - 1117 (target acquisition tests, three roll angles): G140M 66 stars repeated, 37 over more
        than 0.2 in x; PRISM 13. 22.5 GB.
      - 2136 (slit-stepping galaxies, G140H): 43 galaxies at 14 positions, x 0.2-0.95. 27 GB.
      - 6642: PRISM 46 stars repeated across its seven configurations (22 over more than 0.2; only
        obs 2 is on disk); each grating 6 stars, 2 over 0.2.
      - The local archive (S/N >= 10, same grating in two or more observations, x from each
        product's EXPOSURES table): PRISM 1073 objects (550 over 0.2 in x), G395M 320 (177),
        G235M 92 (51), G395H 49 (23), G140M 28 (16), G235H 20 (4); mostly JADES and RUBIES.
        Galaxies, so a joint test of the x correction and the extended-source ratio; the per-
        exposure files are in the product tarballs.
      - No use: 9278 ("MOS Pathlosses Extension") and 10208 are in MAST but not executed; 4465
        has virtual sources at the default position (x must be measured from the data); 1128,
        1492, 6154, 6643, 2640 and 5437 have no source at two x positions.
      No program steps stars along the dispersion in a red grating. The archive's galaxies are not
      used (user, 2026-11-04): repeat observations are often at different position angles, so an
      extended source crosses the shutter differently between visits. Next: 12497 obs 4 and 1133.
    - 12497 obs 4, G140H (`xscan_12497.py`, `xscan_analyze.py`, `xtilt.py`, `xscan_window.py`,
      `xscan_window_analyze.py`, `xscan_shift.py`; figures `xscan_12497.png`, `xscan_window_12497.png`):
      51 stars, 447 visits (seven configurations, the same y, x stepped by 0.13 shutter units).
      - FIT_VERSION 6 fluxes over each star's most central visit fall linearly with SRCXPOS: +22 % at
        -0.42, -12 % at +0.28 (0.97-1.2 um; the same at 1.2-1.7 um). Each star's own slope is negative
        (median -0.40 to -0.58 per unit x, spread 0.15-0.35). NGC 2506 shows no such term (-0.06 to
        +0.01 +- 0.03 in every band; ten configurations, each acquired separately).
      - Without a model (the calibrated nod difference, stage 2b's path loss removed, summed over a
        window of rows around the star; y is fixed so only x changes): the light in the shutter is
        flat within 3 % over x -0.15 to +0.05 and symmetric about x = -0.050 / -0.045 / -0.050 in the
        three bands (per star, 32 stars, quartiles -0.07 to -0.01); jwst's curve is centred on 0. The
        stars sat about 0.05 shutter units (about 0.013 arcsec) further along +x than planned: one
        offset for the visit (one acquisition), a few hundredths per star on top.
      - jwst's G140H point loss evaluated at the shifted position: rms of the visit ratios 28 -> 15 %
        (0.97-1.2 um), 25 -> 16 %, 22 -> 9 %; a free scale on its log shape gives 1.2 and little
        further gain. jwst's G140H x dependence is about right; the tilt is the pointing. Near the
        edge a 0.05 offset moves a flux by tens of per cent (planned +0.35: light 0.37 of the centre
        against jwst's 0.57), so the planned position's error is the largest along-dispersion term.
      - The same with the model (`xgrid_fit.py`: ln F = A_star + ln P(x + dx) - ln P(x) + ln C(x + dx;
        b) - ln C(x; 0.572), P jwst's MOS1x3 point loss at y = 0 from `pathloss_0010`, C the x
        correction; F the FIT_VERSION 6 flux, whose y the fit measures): one dx +0.0475 for the
        visit takes the residual rms per band from 8.5 / 9.7 / 9.2 % to 3.4 / 3.4 / 3.1 %; per
        configuration +0.033 to +0.065.
    - NGC 2506 with an offset per configuration (`check1_to_scan.py`, `xgrid_fit.py`, 245 visits,
      stars in two or more configurations): offsets -0.015 to +0.038 (one +0.085), rms 0.029;
      residual rms 5.7 / 5.8 / 4.9 / 4.5 / 4.6 % -> 2.6 / 2.5 / 3.2 / 2.4 / 4.3 %. b with the
      offsets free 0.38 (per band 0.22 at 2.5-3.5 um, 0.42 at 3.5-5 um) against 0.48 with one
      offset and 0.572 shipped: part of the shipped b took up the configurations' pointing. Most
      stars have two visits, so offsets and b are not well separated here; 1133 (20 positions per
      star) will be.
    - jwst's MOS1x3 point loss (`pathloss_0010`, PS version 2) is exactly an x part times a y part
      (`ps_separable.py`: |ln P(x, y) + ln P(0, 0) - ln P(0, y) - ln P(x, 0)| is 0.0 % over the
      shutter at every wavelength), so the y = 0 cut `xgrid_fit.py` uses holds at any y.
    - 1133 obs 3 (`nrscal_slitgrid_1133`, PRISM, 20 configurations, 3-shutter slitlets nodded, 34
      stars; 422 visits = star x detector x configuration). The first reduction had almost every
      band flux negative: master background step 1 called 11-85 of 102 open shutters per visit
      stuck closed (fitted amplitudes 0.06-0.28), the per-source metafile dropped those shutters,
      and where a dither's primary shutter was dropped jwst wrote a background slit (SRCXPOS =
      SRCYPOS = 0, shutter state `x`), so the exposure holding the star became the background of
      the others. Rerun with `detect_stuck_shutters = false` for this observation (the wrong calls
      kept as `stuck_closed_shutters_wrong_auto.toml.bak`): the fluxes stayed negative (median
      band S/N -80 to -125), with the fitted width on its upper bound. The cause is the metafile:
      in every visit the telescope nodded the other way from its dither order (`nodsign_1133.py`:
      PATT_NUM 2 at YOFFSET -0.529", PATT_NUM 3 at +0.529"; NGC 2506 has +0.529" / -0.529",
      matching its metafile). So the headers put the star of nods 2 and 3 in the shutter at the
      wrong end of the slitlet, and the model's source term lay on the data's subtracted copies
      (`model1_1133.py`: the data peak at nod-1-frame slit y +0.88 and -1.21, the swap predicts
      +0.80 and -1.20). Fixed in the scratch reduction by swapping dither points 2 and 3 in the
      workspace metafile (`swapnods_1133.py`; original kept as `_msa_original.fits.bak`), stage 2
      rerun. The wrong stuck calls were the same error: with the corrected metafile step 1 calls
      2 of 102 shutters stuck in every visit (11-85 before), and the model of a visit matches its
      data row by row in all three nods (`model1_1133.py`, star 23294 NRS2, configuration 36).
      Any other program whose nods go the other way from its metafile would fail the same way
      (why 1133 does is not known); the sign check is cheap (YOFFSET of PATT_NUM 2 against the
      metafile's dither-2 shutter).
    - 1133 results (680 visits, 34 star-detectors x 20 configurations, band S/N about 1000;
      `xscan_1133.json`). Residual = ln flux minus the star's median over its visits.
      - As shipped, all visits: rms 3.1 / 2.5 / 2.2 / 2.5 / 5.1 %; away from the y edges (|fitted
        centre| < 0.3, 521 visits) 2.3 / 1.9 / 1.5 / 1.6 / 3.3 %, and with one x offset per
        configuration 1.6 / 1.4 / 1.4 / 1.4 / 2.5 % (`xgrid_1133_ymid.png`). The 16 central
        configurations' offsets are -0.002 to -0.027 (mean -0.011): the pointing, consistent to
        0.006. The corner configurations 53 and 54 take +0.15, which no pointing explains.
      - b: 0.80 jointly with one offset; bootstrap over stars (all visits) 0.72 [0.56, 0.87], with
        an offset per configuration 0.74 [0.64, 0.88]. NGC 2506 gave 0.38 with offsets free and the
        shipped value is 0.572: the two calibration sets differ by about 2 sigma, and b moves the
        red flux only beyond |x| = 0.25.
      - x edges (|x| >= 0.4, away from the y edges): 1.5-2.5 um 20-30 % below the model on both
        sides, 2.5-3.5 um -15 % at -0.45, 3.5-5 um +10-13 % on both sides. jwst's table (exactly
        symmetric, ln P(+-0.45) / P(0) = -1.2 to -0.8) is not accurate there to better than 20 %.
      - Blue ramp in x (0.7-1.5 um): -9 % at x = -0.25 and +8 % at +0.25, rising to -15 / +12 % at
        +-0.35; about +-2 % at 2 um. Not a wavelength error: one star's spectra across its 20 visits
        (`spec_vs_x.py`) differ smoothly in wavelength, with no structure where the PRISM response
        is steep, and no wavelength shift follows x. It reads as the blue light sitting about 0.05
        pitches (13 mas) toward -x of the red. NGC 2506 shows no such ramp (within +-4 %), so it
        belongs to 1133 (commissioning, June 2022); not put in the model.
      - y edges (|fitted centre| >= 0.35, 115 visits): antisymmetric, data above the model at the
        -y edge (+1 / +1 / +7 / +15 / +16 % in the five bands) and below at +y (-4 / -2 / -6 / -7
        / -5 %). Refitting those visits with the model's shutters moved by delta along the slit
        relative to jwst's slit frame (`xscan_dy.py`, every pixel's slit y and the planned position
        + delta): delta +0.03 brings the -y edge to within 1 % in every band and the +y edge to
        -3 / -1 / +4 / +5 / +7 %, and lowers the edge visits' summed chi2 by 64,000 (better in 70 of
        115); -0.03 and +0.05 are worse. The central control visits' chi2 rises slightly at any
        delta (+11,000 at +0.03). Every configuration prefers +0.03 over 0 (53: about +0.015) and
        rejects -0.03 (`dy_by_cfg.py`); a parabola through the summed chi2 puts the minimum at
        +0.020. One offset for the whole observation.
      - NGC 2506, the same test (`ngc_dy.py`: 166 visits with |centre| >= 0.3 in ten observations,
        60 central controls, delta -0.03 to +0.03): the observations disagree (`ngc_dy_by_obs.py`,
        `dy_obs.png`). v1a and v2 improve at +0.03, v1b and v5 sit near 0, v3b and v7b near
        -0.015, v3a, v4 and v6 at -0.03 or beyond; over all, 87 of 166 visits are better at -0.015,
        74 at -0.03, 45 at +0.03 (median chi2 ratio at -0.015: 1.006 at -y, 0.999 at +y).
        Controls rise by 0.15-0.4 % in chi2 at any delta.
      - Reading: the model's bars are where the average observation has them, and an observation's
        bars sit within about +-0.03 slit units (+-14 mas) of jwst's slit frame, the same for its
        visits (1133) and perhaps different between stars of one observation (v2 improves both
        ways). 1133's +0.02 is inside that range. The flux a point source loses to an offset of
        0.03, from the model's three-nod transmission (`dy_flux_census.py`): at 1 um under 1 %
        out to |centre| 0.4, then 33 % at 0.45 and 95 % at 0.5; at 3 um about 1 % to 0.3, 5.5 %
        at 0.35, 14 % at 0.4, 25 % at 0.45; at 5 um 1.5 % at 0.2, 3.5 % at 0.25, 6 % at 0.3,
        9-15 % beyond. The central controls (|centre| < 0.12) move by under 0.4 %.
      - How many sources this touches: the planned positions of the 165,000 sources in the 318
        production metafiles (`srcpos_census.py`) are close to uniform in y, median |SRCYPOS|
        0.21; 28 % have |SRCYPOS| >= 0.30 (|centre| >= 0.35) and 6 % >= 0.40. With the table
        above, an offset of 0.03 changes the flux of a point source by more than 5 % for 13 % of
        sources at 1 um, 28 % at 3 um and 41 % at 5 um. Extended sources feel less.
      - The offset measured from the background, with no star (`bg_edge.py`, `bg_edge_fit.py`,
        `bg_vs_tilt.py`, `bg_edge_quads.py`, figure `bg_vs_tilt.png`): in every empty shutter the
        background lights the shutter evenly, so its profile across the slit is the open area
        blurred by the optics. Each pixel's surface brightness over its exposure's background,
        divided by the shutter's own centre level per band, is compared with the shipped prism
        bar-shadow table at the pixel's slit y plus a trial shift. Only the half facing away from
        the star and the steep part (0.2-0.75 pitch from the centre) are used; the two halves are
        fitted apart, so a shift moves both the same way and a width change moves them opposite.
        Moving the data by +0.020 pitch through the table is recovered as +0.022 / +0.023.
        - 1133 +0.017 slit units (the stars said +0.020), the same in all four quadrants (+0.014 to
          +0.020) and the three bands. NGC 2506 between -0.014 and 0 (v3b -0.014, v3a -0.009, v4
          -0.007, v2 -0.006, the rest within 0.003); the stars' spread there (+-0.03) is not the bars.
        - Survey observations (uncover2, eight EMBER pointings, capers_uds_p8, venus_pg004;
          hundreds of empty shutters each, the two halves agree to 0.001, exposures to 0.001-0.003):
          -0.009 (venus_pg004) to +0.004, rms 0.004. The EMBER pointings trained the table, so
          their mean is 0 by construction; their spread is not. Five calibration pointings on one
          day (12497, 2026-01-06) give -0.004, +0.012, +0.009, +0.008: the offset changes between
          observations hours apart.
        - Across the MSA: one number per observation plus a fixed quadrant pattern, the same in
          every survey observation: quadrant 1 -0.004, 2 +0.000, 3 -0.001, 4 +0.005 from the
          observation's value (scatter 0.001). Small next to the per-observation term.
        - Not predicted by the grating-wheel tilt readings or the date: fitting the 27 shifts on
          GWA_XTIL, GWA_YTIL or both leaves the rms at 0.0064 (r = -0.10 and -0.03).
        - What the survey offsets do (`dy_flux_census.py` with 0.009): over 2 % flux change for
          10 / 23 / 32 % of sources at 1 / 3 / 5 um, over 5 % for 8 / 11 / 0 %, the large ones
          on the bar itself (|centre| >= 0.45).
      - So, as the model stands: compact sources inside |x| < 0.3 and |centre| < 0.3 are described to
        1.4-1.6 % (0.7-3.5 um) and 2.5 % (3.5-5 um) with the observation's pointing offset in x;
        near a bar (|centre| >= 0.35) the observation's own bar offset (survey -0.009 to +0.004,
        calibration up to +0.017) adds a few percent and up to 10 % at 3 um, more on the bar; it
        can be measured from each observation's own background to about 0.002. At |x| >= 0.4
        jwst's table misses by up to 20-30 %.
- Outputs and figures: `scratch/claude/psf_cal_law/` (`psf_table_wing3_after_{S,SA,SB}.json`,
  `smooth_joint_wing3_after/`, `smooth_coefs.log`, `smooth_test.log`, figures `psf_stack_smooth.png`,
  `psf_parameters_smooth.png`, `psf_heldout.png`; the alternating-fit runs in
  `superseded_alternating/`); `psf_table_wing3_after_{J,Jn,A,B}.json`,
  `forms_report.log`, `edge_prepost.log`, `table_test.log`, `table_test_J.log`, figures
  `psf_stack_tables_J.png`, `psf_parameters_J.png`, `star_own_psf_<key>.png`), and from the
  previous rounds `psf_table_new.json`, `centre_law_new.json`,
  `offset_law.log`, `kappa_by_obs.log`, `p1_out_of_sample.json`, `summary.log`, `selection.log`,
  `figures/` with `nod_offset_law.png`); the shared-position refit in `scratch/claude/psf_cal/`.

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
  - Fixed (2026-11-02, `claude/bkg-revision`; p1 922323, nod 3 in `jw07076001001_05101`): the
    decision flipped from run to run. Its neighbor 922322 read 0.020 of the background in a
    run that started from canonicals where nod 3 used only exposure 1 (both kept), and 0.021
    in the next, which started from both (exposure 2 dropped). The joint first pass fitted
    every member on its live nod differences, i.e. on the choice the previous run wrote
    (`BKGNODS`), and 922323 is the second member of 922322's neighbourhood. Now, when the
    decisions are redone, that pass fits every member on the nod differences of the override
    files (hand and scan), rebuilt in memory from `PRE_BKGSUB_*` with stage 2b's arithmetic
    (`profile_fit.load_group(pre_bkgsub=True)`), so the same data and settings give the same
    decision. The sources whose own background exposures then differ from the override
    files' get their pass 1 again on their canonicals, since a product takes its neighbors'
    light from its own solve (`add_joint_light`). Second bug: Spec3 ran before the revision
    and, with `combine_method = "1d"` (p1), never again, so a revised source's s2d, the
    drizzles through its crf and its EXPOSURES table kept the old choice (run B: `bkg_nods
    '1,2'`, against the log's [1]). Now per disperser: pass 1, revision, pass 1 again where
    needed, Spec3, the products (every drizzle); then the extraction. Spec3 still runs once
    per product.
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
  Decision (user, 2026-11-02, relayed by the master-background session): failed-open
  shutters are painted with background (master background 1.6, in progress there), and
  the master-background extraction keeps MSA_FAILED_OPEN pixels. Light above the
  background in a failed-open shutter is other-source light, modeled like any
  neighbor's. Today the contamination model only records those pixels
  (`profile_contam.Geometry.failed_open`, `PFFOPEN`): it predicts other light from a
  source's pass 1, and a failed-open shutter has no source. Scope on p1
  (`e2e_failed_open_count.py`): 109 of 681 products have failed-open pixels on the
  source's own shutter (91 G395M, 18 PRISM; up to half of it, 924509 0.51); 921027 has
  none (`PFFOPEN` 0), so its excess is the background level, not this. On 924509 the
  two failed-open shutters are in the slitlet's own MSA rows (155 and 157) but other
  columns (44 and 157, against 170), so their traces run along the slitlet's rows at
  other wavelengths, and the source crosses them as it moves between exposures.
  Built (user, 2026-11-02, `failed_open.py`): per exposure and failed-open shutter on the
  source's trace, a spectrum along detector columns (linear between knots every pass-1
  smoothing length: 15 columns PRISM, 31 gratings) times a flat profile over the
  shutter's pixels, solved together with the source at the nodded chain's shape on the
  master-background frames minus the other sources' predicted light, with its own
  outlier rejection (the source-only fit clips that light as outliers). Added to
  `CONTAM2D` / `CONTERR2D` (statistical error plus `DPRED_FLOOR` of the light, since the
  flat profile is an assumption); cards `PFNFOPEN` / `PFFOSIG` in `SPEC1D_MBKG`.
  Synthetic test: recovers the light off and across the source within 0.03 of a 0.2-0.8
  signal at noise 0.05. The nodded spectrum is unchanged. Open: the background behind a
  failed-open shutter cancels between exposures, but an object behind one moves with the
  nod and need not cancel; not checked for the nodded spectrum.
- Not added (user, 2026-11-02): a fourth `MBKGWHY` reason for another slitlet's light
  entering mainly through the background exposures (629466); the contamination model
  handles it.
- Holding `MBKGPREF` back on G395M: moot (user, 2026-11-02). Nothing ships before the
  master background 1.6 fix, so the mark is set from the fixed master background; the
  PRISM comparison is rerun then.
  The deploy reader on `claude/mbkg-web` accepts all 99 p1 products.
- Master background 1.6 on p1 (2026-11-02; `claude/mbkg-paint` f620b887; `claude/mbkg-paint`
  and `claude/mbkg-web` merged into this branch the same day (user), 309 pipeline and 41
  deploy tests pass; run from a detached worktree with this branch's 165382b7 plus its `master_bkg.py`, 256
  NIRSpec and master-background tests pass). 1.6 paints failed-open shutters at their
  measured transmission (0 to 1, combined over the nods; G395M 12/q3 0.09, 19/q3 0.51,
  14/q3 0.03, 23/q4 0.00, 1/q1 0.17, 5/q1 0.26, as the master-background session
  measured), keeps the margins of crossing traces, and leaves 21/q4 and 24/q4 unpainted
  (closed in the science state). Stage 3 for the 74 sources, no errors. Against PRISM
  (20 G395M sources): beyond 3 sigma 17 master-background spectra against 9 nodded (16
  with 1.5); median 7.3 against 2.8 sigma (8.1 with 1.5); 921027 still 37x. Every
  master-background spectrum moved down by a similar amount: those too high came closer
  (925403 8.0 → 6.1, 950151 6.8 → 3.1, 879928 2.6 → 1.8, 633676 2.6 → 1.4), others are
  now below PRISM (880017 -1.4 → -3.0, 634522 -0.8 → -1.5, 925380 0.53 → 0.40). Why
  (`e2e_mbkg_level.py`, `e2e_mbkg_residual.py`, figure `e2e_slitlet/mb16_g395m_vs_prism.png`):
  on G395M the background left in the source's own slitlet away from the source is
  -7 % to +7 % of the background per exposure (PRISM -3 % to +3 %), and in the 2-D
  master-background spectrum the rows away from the source carry a level as large as the
  source (921027 6.9e-14 against 8.0e-14 on the source, 634522 -1.7e-14 against -1.1e-14,
  632454 2.0e-14 against 1.5e-14; PRISM about 10x below its source). That level, scaled
  to the extraction, accounts for most of the difference from PRISM in 634522, 632454,
  925380 and 634072. 921027 and 925403 are different: bright bands past 4.3 µm (921027
  the band at rows 636-652 near 24/q4, unexplained) that are not flat across the slitlet;
  925403's nodded spectrum is high there too (3.2x). Caveat (user, 2026-11-02): none of
  this measures the master background against a truth. The PRISM comparison compares two
  extractions, each with its own handling of contamination and overlap, with statistical
  errors only; the level "away from the source" depends on which pixels are counted as
  background (here: |ys - source| > 0.75 slit units, other sources' source shutters
  masked, their wings and margins not); and the master-background session's per-shutter
  numbers use yet another selection. What is established is only that, measured this way,
  the G395M master-background spectrum differs from PRISM more than the nodded one does
  (median 7.3 against 2.8 sigma), and that the difference follows the level in its own
  off-source rows. Not established: that the master background is too high or too low.
  8 G395M products carry MBKGPREF, kept as is (user, 2026-11-02). The per-slitlet scale
  was raised again and decided against (user with the master-background session,
  2026-11-02, after its per-shutter nod-repeatability measurement,
  `~/campfire-data/scratch/claude/mbkg_ship/repeat16/`): no per-slitlet and no smooth
  row/column scaling of the master background on any disperser, the opt-in code removed
  (`claude/mbkg-paint` 0831aa2d); on G395M the scatter is specific to each shutter and
  neither predicts it (master-background design doc, 2026-11-02). 925403 (its finding): the 4.4-5.1 µm rows are crossed in all
  three exposures by the source shutters of 630784 and 948429; a bright continuum on the
  lower edge of 925403's source shutter moves with the nods, so nodding cannot remove it.
  The contamination model predicts most of it (`e2e_925403_contam.py`, medians in µJy).
  Nodded: fnu - fnu_contam is 0.113 at 4.4-4.7 µm (PRISM 0.122, fnu 0.80, contam 0.74)
  and 0.35 at 4.7-5.1 µm (PRISM 0.13, fnu 2.48, contam 1.06). Master background: 0.35 and
  0.66, against 1.57 / 4.73 before subtracting. What is left at the red end is a part of
  the neighbors' light the model misses, plus, in the master-background spectrum, the
  background level above.
- uncover2 end to end (2026-11-02; PRISM, 344 sources, two visits of three exposure groups;
  stage 2a, 2b and 3 from the production rate files in an isolated root, 4 h 45 min, no
  errors). The 1-D combination of several exposure groups carries the master-background
  spectrum (20698: 6 groups, 18 exposures, `NEXP` / `NEXPBKG` / `MBKGNEXP` filled). 20698
  (z~2 LRD, the user's case; `e2e_slitlet/uncover2_20698.png`): the optimal extraction on
  the new frames matches the production v0.3 optimal within about 1 %; the profile
  extraction is within about 5 % of it (-4 % at 1.2-1.8 µm, +5 % at 4.0-4.3 µm, +5-10 %
  near 5 µm); the master-background spectrum lies above the nodded ones, about 3 % where
  the source is bright and 10-20 % where it is faint or the background is high, the shape
  of a small additive background difference. A comparison of methods, not of either with a
  truth.
- p1 stage 3 with the drizzle-once code (2026-11-02, before its merge; the 74 sources,
  products saved before in `scratch/claude/extraction-v2/p1_before_drizzle`): nodded `fnu`
  identical to rounding in 98 of 99 products; stage 3 803 s against 1001 s (Spec3 173 → 65
  s, products 224 → 143 s; pass 1 480 s unchanged). The master-background spectra agree
  with the nodded ones as before; three heavily contaminated G395M sources (885305,
  885945, 884644) changed most, because the old master-background Spec3's outlier detection
  clipped other sources' light that moves between exposures, and the new run keeps it
  (`e2e_slitlet/drizzle_885305.png`). 922323 showed two older bugs (fixes on
  `claude/bkg-revision`, user 2026-11-02): (1) the same-slitlet background choice for a
  neighbor at the 0.02 threshold flipped between runs (0.020 kept, 0.021 dropped), the
  measurement apparently depending on the background choice the previous run left in the
  canonicals; (2) Spec3 (s2d, crf, `EXPOSURES`) runs before the revision of background
  exposures and is not rerun, so a revised source's 2-D and `EXPOSURES` show the choice
  from before (922323's product says nods 1,2 while its log says nod 3 used nod 1 only).
  `claude/drizzle-once` merged (user, 2026-11-02).
- PSF table re-derived from the six calibration fields (2026-11-02; stage 2a/2b rerun with
  this branch in `scratch/claude/e2e_nrscal`; module `psf_calibration.py` on
  `claude/psf-table`; outputs and figures `scratch/claude/psf_cal/`). 131 / 133 clean NRS1
  / NRS2 PRISM stars, fitted through the profile model on the nod differences stage 3
  reads. Found: each nod sits off its WCS position by an amount linear in the slitlet's
  detector row, zero near row 1000 and +-0.08 rows at the edges, the two outer nods mirror
  images, NRS1 and NRS2 on one line (`figures/nod_offsets.png`); the model has held the
  per-exposure offsets at zero since 2026-11-01, and with them free the bright stars' chi2
  falls 2-5x and the NRS1 joint wing fraction by 0.05-0.11, the core widths by <= 0.003
  rows. With offsets at zero the refit gives a narrower core (NRS1 0.32 against 0.39 rows
  in the blue) and a larger wing fraction (0.34-0.40 against 0.19-0.30), core and wing
  trading. Decision (user, 2026-11-02): a fixed nod-offset law, measured from these stars
  and built into the model as fixed geometry like the centre law, no free parameter per
  source; then the PSF table is refitted with it. `PSF_TABLES` / `CENTRE_LAW` unchanged
  until the user decides on the refit. The law came out as each nod's own planned position
  (SRCYPOS): the fitted source centre is shared, each exposure shifted by its SRCYPOS
  minus the median (no free parameter; the 2026-11-01 decision against fitted per-nod
  shifts stands). With each star's centre free in every exposure and its own width, a
  symmetric core + wing (wing width 2.2 x core) fits the stack to about 1 % of the peak but
  leaves a one-sided shoulder at +1 to +1.5 rows and a faint tail to +4 rows (about 1 %
  of the light, in 63-86 % of the stars, independent of where the nod partners lie), and
  the table's flat extrapolation beyond 4.70 µm makes the model too narrow above about
  4.8 µm. Decision (user, 2026-11-02): more terms in the parametric PSF, at minimum the
  wing's own width and offset, more for the asymmetry if needed; every parameter smooth
  over the full NIRSpec range (no flat extrapolation); goal: agreement with the stacked
  stars better than 1 % of the peak.
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
  background (`stage2.master_background_on`, `sky_for_slit`), in memory
  (`mbkg_extraction.frame_arrays`; variance plus the master background's). Until
  2026-11-02 they were written as copies of the canonicals in `<workspace>/mbkg/` and read
  back.
- 2-D: the frames drizzled as data through the nodded product's pixel maps
  (`drizzle_map`, below), so on the nodded grid by construction. Outlier flags
  (`outlier_flags`, the one place of that choice; user, 2026-11-02): the nodded Spec3's,
  plus a hot-pixel mask (`hot_pixels`), plus the pixels without a master background; jwst's
  outlier detection is not run on the frames. Until 2026-11-02: one extra Spec3 run per
  product on the copies, with its own outlier detection (and its own grid, checked against
  the nodded one).
- Hot-pixel mask: a pixel that stays high at one detector pixel in every exposure cancels in
  the nod differences, so the nodded detection never flags it. Per frame, `local_contrast` is
  the smaller of (value minus the median of its row neighbors within +-3 pixels) and (value
  minus the median of its column neighbors within +-2), over its error: a single hot pixel
  stands above both, light smooth along the dispersion (a trace, a failed-open band) does
  not stand above its row, light spread over a few rows (an extended line, a band's edge
  rows) not above its column. Flagged where it exceeds 5 sigma in every exposure on that
  detector with a usable value there, at least 2 (`HOT_SIGMA`, `HOT_MIN_EXPOSURES`). Sky,
  source and neighbors move on the detector between nods, so they are not high at one
  detector pixel in every exposure; a cosmic ray hits one. Pure noise passes 5 sigma in
  every one of 2 exposures with probability ~1e-13 per pixel. 5 sigma kept (user,
  2026-11-02) over a higher threshold: 10 sigma would leave steady 5-10 sigma warm pixels
  (629466 det (1794,1537) at +7 to +8, (1797,1517) at +5.5) and half of 924509's 2x2
  hot cluster, whose bottom row reads +5 to +7.5 because its column neighbors include the
  hot top row. With one exposure nothing
  tells a hot pixel from the source, so nothing is flagged (jwst's outlier detection skips a
  single exposure too); a product with no nod between its exposures could have a bright
  compact source feature flagged (not seen; noted).
- Fit: the nodded fit's exposures with the frames in place of the nod differences, as single
  frames (`single_frame_exposures`: geometry, variance and path loss from the nodded load;
  identical to `load_source(single_frames=True)` on the old copies, p1 629466 PRISM), the
  forward model at the nodded chain's shape, the spectrum solved; `PROF2D` / `CONTAM2D` /
  `CONTERR2D` drizzled through the same pixel maps. Other sources' light from their pass 1s,
  once and positive, catalogued same-slitlet neighbors included (`predict(include=...)`, from
  `slitlet_neighbours.scene`): the stored map counts them as the slitlet's own light.

**Drizzle once (2026-11-02, branch `claude/drizzle-once`).** Every 2-D image of a product is
drizzled before any 1-D extraction, through one set of pixel maps per product
(`drizzle_map.DrizzleMap.from_crf`: jwst's output grid from the crf, each exposure's
`calc_pixmap` once; then the drizzle library directly, with stcal's weights and variance
propagation). The nodded data stay Spec3's s2d. Checks on p1 629466 PRISM (3 exposures;
scripts in `scratch/claude/drizzle-once/`):
- The crf's own arrays through the map reproduce Spec3's s2d bit for bit: SCI, ERR, WHT,
  VAR_POISSON, VAR_RNOISE, VAR_FLAT.
- `PROF2D` / `CONTAM2D` / `CONTERR2D` against the per-image `ResampleSpecStep` calls: equal to
  1-2 float32 ulps (max 1.8e-7 of the peak, 155 of 12990 pixels). Cause: jwst builds each
  call's grid with the slit position weighted by the image being drizzled, so a model's grid
  sat 1.6e-9 pixel from the data's; with the grid built from the model the map reproduces
  the old call bit for bit. The map uses the data's grid, the one the extraction reads.
  `fnu` moves by at most 1.5e-7 relative.
- Master-background 2-D against its own Spec3: the grid alone changes it by 2e-9 of the
  peak; the rest is the outlier flags. Of 23058 usable pixels the master background's own
  outlier detection flagged 20, the nodded one 10 (9 shared). Two hot pixels at +69 sigma
  (detector 1792,1501 and 1794,1489) are stationary, cancel in the nod differences and are
  missed by the nodded detection in one exposure each: with the nodded flags alone they
  stayed in `SCI_MBKG` as spikes up to +540 sigma. With the hot-pixel mask (12 pixels: 4
  detector pixels x 3 exposures, both +69 sigma pixels among them) the flags total 18 (14
  shared with the old 20); the brightest `SCI_MBKG` pixel is 7 sigma (old run: 207 sigma,
  its own detection missed one of the two hot pixels in one exposure, which is the largest
  |new-old|/err left, -61 sigma, the new value the clean one; largest the other way +2.6).
  `SPEC1D_MBKG.fnu` moves by at most 1.4 sigma in one column (0.906 um), under 1 sigma in 5
  others. Figure: `scratch/claude/drizzle-once/hot_pixels_629466.png`.
- Fixed smooth light (p1 924509 G395M, `PFFOPEN` 0.54): the mask flags 8 detector pixels in
  each of the 3 NRS1 exposures (+5 to +48 sigma, a 2x2 cluster among them; the nodded crf
  had 2 of the 24). With the master background added back to the frames (sky and
  failed-open bands, 7000-9000 pixels above 5 sigma per exposure, fixed on the detector in
  every one) it flags exactly the same pixels and nothing in the bands.
- Without a crf (a rare named condition) there is no master-background spectrum: the old
  one had its own Spec3.
- Time (machine under load): the master-background Spec3 (7.6 s) is gone; the nodded drizzles
  3.8 s -> 0.06 s plus 1.0 s for the map; the master-background fit 5.5 s -> 2.3 s, of which
  ~0.75 s per exposure is `sky_for_slit` opening the canonical as a datamodel.
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
| V7 | Forward-model shutter edge vs measured bar shadow | the model's response to uniform light against the table | edge, narrowing and floor consistent, or a stated revision | Made 2026-11-03: the hard edge fails in the red. A narrower measured edge passes but edge stars prefer the geometric edge; the geometric shutter with the transmission T capped at 1 and the PSF split evenly (3.7, the default from `FIT_VERSION` 5) passes, with edge stars 0.7 % worse in median chi2 | `scratch/claude/psf_cal_T/figures/v7_T4_*.png` |
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
| Held-out validation of the b0c08026 tables | MB session | |
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
| Nod offset law (3.7, `NOD_OFFSET_LAW`: each nod at its own planned SRCYPOS): built, on by default | extraction session | |
| Centre-law refit with the nod law (3.7, `psf_calibration.py` step `centre_law`): adopt or not (the two-component PSF refit is superseded by table S and its code removed) | user | |
| Three-component PSF (3.7, `wing3_after`, general `psf_components` tables): table rule decided 2026-11-02 (smooth joint fit, table S); S adopted 2026-11-03 (`FIT_VERSION` 4); the self-consistent second round gives S2 (same on NRS1, NRS2 wing/shoulder split moved by up to 2 % of peak at 1.5-3 um, stack test equal overall): keep S or adopt S2. Left: the 3.3-5.3 um ranges (few stars; more calibration stars), and the flux change on p1 | user (S or S2); extraction session | |
| Shutter (3.2, 3.7): the geometric shutter with the smooth transmission T, capped at 1, the PSF split evenly before and after it, is the default (user, 2026-11-03; `FIT_VERSION` 5, card `PFSHUT` `geomT-2026-11-03b`); the H gratings use the M grating with the same filter (user, 2026-11-03). Left: p1 stage 3 rerun with it; the blue end against jwst's point path loss; the held-out check of the b0c08026 grating tables, which T is fitted to | extraction session | |
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
| Profile model, shape chain | `nirspec/profile_fit.py` (PSF: `data/nirspec_profile_psf.json`, general layout), `nirspec/profile_chain.py`, `nirspec/profile_product.py` |
| PSF, centre law and nod offsets from calibration stars | `nirspec/psf_calibration.py` (CLI `python -m campfire_pipeline.nirspec.psf_calibration <step>`) |
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
- **2026-11-02.** Section 3.7: the PSF and centre-law refit through the profile model, with the
  nod-position mismatch and the skewed PSF it found; the nod offset law (each nod at its planned
  SRCYPOS, built and on by default); the refit with the law, pending the user's decision; the
  general PSF and the three-component table candidates. Later the same day: the user's decision
  that the table is a smooth joint fit, and its result (table S, held-out test).
- **2026-11-03.** Section 3.7: table S adopted as the extraction's PSF (`FIT_VERSION` 4), and
  the two tests that had depended on the old PSF. Later: the old two-Gaussian PSF removed from
  the code (`profile_fit`, `psf_calibration`, tests); sections 3.7, 6 and 7 updated. Then the
  second, self-consistent round from S (table S2) and its verdict. Then the measured shutter
  edge (sections 3.2 V7 follow-up, 3.7, 5 V7, 6): its model, the stars refitted with it (SE),
  the checks (stack, V7, edge stars, p1 flux) and the decisions they leave. Then the user's choice
  (d) and the shutter transmission T built and checked the same way (3.2, 3.7, V7 row, 6).
  Then T adopted as the default (`FIT_VERSION` 5, card `PFSHUT`; 3.2, 3.7, V7 row, 6): capped
  at 1, which leaves the form T = 1 - (1 - tedge) x^8, and what the cap costs; the measured edge
  and its file removed; the H-grating fallback recorded as decided; the bar-shadow tables'
  sign-off caveat dropped (settled 2026-10-30). Then the PSF split held at 0.5 in the shutter
  model (`geomT-2026-11-03b`, 3.2, 3.7, V7 row, 6): edge stars 0.7 % worse than the hard edge
  instead of 1.8 %.
