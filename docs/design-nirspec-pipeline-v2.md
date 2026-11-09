# The NIRSpec pipeline, version 2

This document describes the redesigned CAMPFIRE NIRSpec reduction as it ships on
`claude/extraction-v2`: the master background, stuck-closed shutters, the trace maps, the
same-slitlet background choice, the forward-model extraction with its optics model of the MSA,
the light of other slitlets, the master-background spectrum, and the boundary with jwst's
references. It is written for the reviewers of the pull request and for whoever maintains the
code afterwards.

- The main text (sections 1 to 12) describes what ships and the measurements behind it.
- Appendix A lists what was built or tried and not shipped, and why.
- Appendix B is the record of the user's decisions, with dates.
- Appendix C lists errors found in earlier results and how they were corrected.

**Related documents.**
- The user-facing documentation is on the web docs, under
  `web/lib/docs/content/reduction/nirspec/`: `backgrounds.md`, `slit-losses.md`,
  `extraction.md`, `detector-calibration.md`, `describing.md`, and the FITS reference
  `web/lib/docs/content/data-products/fits-columns.md`. Their figures are made by
  `scripts/docs/nirspec/`. This document gives the implementation and the evidence; the docs give
  the description users read. Where the two differ, one of them is wrong and should be fixed.
- [The master-background design study](design-nirspec-master-background.md) holds the
  master-background measurements in detail (its section 9).
- The [emission-line fitting design](design-emission-line-fitting.md) reads the products this
  pipeline writes.
- `pipeline/CHANGELOG.md`, `## Unreleased`: one MAJOR Algorithm entry for all of this.

**History.** The working version of this document, with every intermediate result and the
scratch paths of each figure, is in git history up to commit 7887b615. Scratch results live under
`~/campfire-data/scratch/claude/` on the reduction machine and are not part of the repository.

**Units.** Positions along a slitlet are in slit units: the open height of one shutter is 1
(0.46 arcsec), the shutter pitch 1.15. Positions across a shutter (along the dispersion) are in
shutter widths (SRCXPOS, 0.20 arcsec open, 0.27 arcsec pitch). jwst's planned position along the
slit (SRCYPOS) is in shutter pitches.

**Datasets.** The observations named below: p1 is `ember_cosmos_p1` (PRISM and G395M), uncover2
a PRISM observation of two visits, spurs_gds a SPURS field (G140M, G235M, G395M). The calibration
stars: NGC 2506 (program 1493, ten MSA configurations), programs 6642 and 12497, and the
commissioning slit-loss program 1133 (stars stepped across their shutters).

---

## 1. Summary

The released pipeline (v1) extracts each source from nod differences with a fixed optimal
profile, treats every source as a point source on jwst's point-source calibration, and ignores
light from other slitlets. On the gratings traces sit edge to edge (38 % of each G395M detector is
under more than one slitlet's light), so many spectra carry other sources' light.

Version 2 changes six things.

1. **Master background.** Every exposure's background is measured from its empty shutters and
   modeled back onto the detector, through every open shutter's footprint, past both ends of the
   calibrated wavelength range, and with failed-open shutters at their measured transmission.
   It gives a second spectrum of every source and a record of the background. It is not used to
   correct the nodded spectra.
2. **Stuck-closed shutters** are found on every disperser by fitting each commanded-open
   shutter's background in each exposure, and kept in a per-observation list keyed by physical
   MSA shutter.
3. **A forward-model extraction.** Each source is a Gaussian whose center and width vary
   smoothly with wavelength, seen through a diffraction model of the MSA, in every exposure and
   in its negative images from the nod subtraction. `fnu` is extracted with this model as the
   weights. The reported flux of an extended source is its light in a circle 0.8 arcsec across.
4. **Other slitlets' light** is solved jointly with each source's neighbors and reported as its
   own column, `fnu_contam`, never subtracted from `fnu`.
5. **Same-slitlet neighbors.** A background exposure that puts a cataloged neighbor on the
   source is left out of the nod subtraction when the neighbor is bright enough. Stage 3 redoes
   these decisions from the modeled light of each neighbor.
6. **CAMPFIRE's own MSA calibrations** (the optics model, each observation's bar offset, the
   empirical bar-shadow tables) sit at an explicit boundary with jwst's path-loss and bar-shadow
   references, each with a switch back to jwst's.

| Component | Where it runs | Main modules | Section |
|---|---|---|---|
| Stage 2a: one `Spec2Pipeline` call per rate file; background-only slitlets | stage 2a | `stage2.py` | 3 |
| Stage 1 mask and geometry follow G140M / G235M traces to 5.3 um | stage 1, step 1 | `stage1.py`, `coverage.py` | 3 |
| Empirical bar-shadow tables (PRISM, G140M, G235M, G395M) | master background | `barshadow_calibration.py`, `data/empirical_jwst_nirspec_barshadow_*.fits` | 4.1 |
| Master background step 1 (count rate, before stage 2a) | before stage 2a | `master_bkg_rate.py`, `coverage.py` | 4.2 |
| Stuck-closed shutter fit and the stuck list | step 1 | `shutter_fit.py`, `stuck_list.py` | 4.3 |
| Trace maps, stored in the rate file | step 1 | `trace_overlap.py` | 4.4 |
| Master background step 2 (calibrated, end of stage 2a) | end of stage 2a | `master_bkg.py` | 4.5 |
| Same-slitlet background choice; errors with the subtracted exposures' noise | stage 2b | `slitlet_neighbours.py`, `stage2.py` | 5 |
| Source model and shape chain | stage 3 | `profile_fit.py`, `profile_chain.py` | 6.2, 6.6 |
| Optics model of the MSA | stage 3 | `optics.py`, `data/nirspec_optics.json`, `optics_calibration.py` | 6.3 |
| Bar offset per observation | stage 3 | `bar_offset.py` | 6.4 |
| Joint first pass and the prediction of other slitlets' light | stage 3 | `joint_first_pass.py`, `profile_joint.py`, `profile_extend.py`, `profile_contam.py` | 6.7 |
| Same-slitlet revision and the neighbor scan | stage 3 | `slitlet_neighbours.py`, `nod_scan.py` | 6.8 |
| Master-background spectrum | stage 3 | `mbkg_extraction.py`, `failed_open.py` | 6.9 |
| Every 2-D image drizzled once per product | stage 3 | `drizzle_map.py` | 6.10 |
| Boundary with jwst's references | stage 3 | `crds_refs.py`, `data/nirspec_calibrations.toml` | 7 |

Versions at the time of writing: `FIT_VERSION` 9, optics model `2026.11-r8` (engine 2), master
background `CFMBKGV` 1.9 (step 1 1.6), `JOINT_PASS_VERSION` 5, `PASS1_VERSION` 11, bar offset
`MEASURE_VERSION` 5, trace map version 3.

---

## 2. Pipeline order

Each step lists what it reads and writes.

1. **Stage 1 (detector).** Reads the uncal files. Writes `<root>_<nod>_<nrsN>_rate.fits` with the
   detector background removed outside every open shutter's footprint (`CFBKG`, `CFBKGMASK`). The
   footprint of G140M/F100LP and G235M/F170LP traces (and the H gratings with those filters)
   reaches 5.3 um (section 3).
2. **Master background, step 1** (count rate, before stage 2a; per exposure group). Reads the rate
   files, the original MSA metafile, the failed-open shutter list and the bar-shadow tables. One
   `assign_wcs` pass per rate file serves everything below. It measures the count-rate background
   from empty shutters lit by one slitlet only, fits every commanded-open shutter's background to
   call stuck-closed shutters, and builds the exposure's trace map. It writes all of it into the
   rate file and merges the stuck calls into the observation's stuck list as `auto` entries.
3. **Stage 2a (calibration).** Reads the rate files and the stuck list. One `Spec2Pipeline` call per
   rate file. Writes one canonical cutout per source (`<root>_<nod>_<nrsN>_<sid>.fits`,
   point-source calibration) and the MSA plan's background-only slitlets
   (`bkgslits/<root>_<nrsN>_bkgslits.fits`).
4. **Master background, step 2** (calibrated, end of stage 2a). Reads step 1's tables, the
   canonicals and `bkgslits/`. Writes `<root>_<nod>_<nrsN>_mbkg.fits`. Stage 2a then rectifies each
   exposure's calibrated frame (the one before stage 2b's subtraction, once 2b has run) with its
   master background subtracted, for the nods plots (`S2D_SCI`, cards `MBKGSUB`, `S2DVER`).
5. **Stage 2b (nod subtraction).** Each exposure minus the mean of its background exposures. A
   background exposure that puts a bright cataloged neighbor on the source is left out
   (section 5). The variance includes the subtracted exposures' noise.
6. **Stage 3**, per observation: the bar offset of each disperser from its empty shutters
   (`bar_offset.json` in the workspace, measured once); the stuck list resolved once; trace maps
   read from the rate files as needed.
7. **Stage 3, per disperser, PRISM first:**
   1. the joint first pass: every source solved with the sources whose light shares its pixels,
      each at a fixed shape, on the nod differences, including its light past its cutout;
   2. the same-slitlet revision: stage 2b's decisions redone from the first pass's model of each
      neighbor; stage 2b again where they change, and the first pass again for the sources whose
      own background exposures changed;
   3. Spec3 (s2d, crf, `EXPOSURES` table) and every drizzle of the product;
   4. per source: the profile fit with the shape chain, the other slitlets' light predicted and
      reported, the neighbor scan, and the master-background spectrum.
8. **Stage 3, after every grating:** a grating product that fell to the default shape and had no
   PRISM shape is redone with the shape of the source's grating that took its own fit
   (section 6.6).
9. **Final extraction.** Extracts the columns from the combined 2-D spectrum, writes
   `<obs>_<grating>_<filter>_<sid>_spec.fits` and deletes the `_pf.fits` intermediate.

Nothing is computed twice: the count-rate background step 1 measures is the master background
before calibration, and step 1's `assign_wcs` pass serves the coverage counter, the stuck-shutter
fit and the trace maps.

---

## 3. Stage 1 and stage 2a

**One `Spec2Pipeline` call per rate file.** Stage 2a used to call `Spec2Pipeline` once per source.
It now calls it once per rate file with an edited full metafile and splits the result into the
canonical cutouts. This is faster, and the same call extracts the MSA plan's background-only
slitlets, which the master background reads. The per-source loop runs only as the fallback when
the single call fails. The output was checked array for array against the per-source loop on
`ember_cosmos_p1`. `bkgslits/` is a regenerable local intermediate (layout kind
`nirspec_bkgslits`). Stage 2a removes stuck-closed shutters from the background-only slitlets too,
and gives them the empirical bar-shadow tables (CRDS for the H gratings, which have none).

**G140M and G235M past their calibrated range.** With the standard wavelength range, jwst gives a
G140M/F100LP or G235M/F170LP slitlet whose nominal range lies wholly on NRS1 no NRS2 WCS, so its
red tail did not exist for the pipeline (on spurs_gds 21 of 116 slitlets; the coverage was wrong
on 17 % of G140M and 13 % of G235M NRS2 pixels). Three things now follow these traces to 5.3 um
whatever `extend_g140m_g235m` says:
- stage 1's slit mask for the detector background fit (before, the red tails on NRS2, to about 3.5
  and 5.05 um, were fitted as detector background);
- the geometry of step 1 (`coverage.py`, `GEOMETRY_VERSION` 3), hence the master background and
  the trace maps;
- the G140M and G235M bar-shadow tables (section 4.1).

`extend_g140m_g235m` only decides what stage 2a calibrates.

---

## 4. Master background, stuck-closed shutters and trace maps

The master background is described in full in the master-background design study (section 9 there
for the measurements). This section summarizes what ships.

### 4.1 Empirical bar-shadow tables

The bar shadow is how the MSA bars and shutter edges dim a uniformly lit shutter, as a function of
position along the slitlet and of wavelength. The CRDS references put the shutter edges a fraction
of a pixel row off and have the wrong width under the interior bars, which left errors of 10 to
23 % in a background modeled with them. CAMPFIRE measures the profile from the background in
empty shutters instead.

- The background spectrum of each disperser is measured from shutter centers alone, where the
  bar shadow is 1 by definition, so no bar-shadow model enters the measurement.
- Each pixel of an empty shutter is divided by that spectrum. The values are binned in
  position along the slitlet and wavelength, and the median per bin defines the profile of a
  slitlet's outer edges and of an interior bar. The median replaced a clipped mean, which kept
  only the bright tail where the profile approaches zero.
- PRISM from seven EMBER pointings; G140M, G235M and G395M from the four SPURS fields. G140M and
  G235M are on an axis widened to 5.3 um (101 columns: a mean profile plus one shape change cubic
  in wavelength, measured to 3.18 and 5.05 um).
- The tables are written in the CRDS reference format.

Use. Stage 2a gives the tables to the background-only slitlets, and the master background models
the background with them. Science frames are never divided by a bar shadow. In the extraction the
bar shadow is part of the optics model (section 6.3), which is fitted to reproduce these tables.
The H gratings keep the CRDS reference.

Code: `nirspec/barshadow_calibration.py`, `data/Generate_Barshadow_Cals.py`, `barshadow_qa.py`;
tables `data/empirical_jwst_nirspec_barshadow_{prism,g140m,g235m,g395m}.fits`, listed with their
hashes in `data/nirspec_calibrations.toml`.

### 4.2 Step 1: the count-rate background

Before stage 2a, from the rate files only, per exposure group:
- one `assign_wcs` pass per rate file; the rate model after it is cached under
  `$CAMPFIRE_ROOT/cache/coverage/`, and geometry is evaluated from it;
- the count-rate background (`MBKG1DR`) from empty shutters lit by one slitlet only, with
  count-rate quadrant scales (`MBKGQSR`) and the shutter selection (`MBKGSHR`);
- the stuck-shutter fit (`SHUTAMP`, `CALLS`; section 4.3);
- the trace map (section 4.4).

The tables go into `<base>_rate.fits` after jwst's extensions, as stage 1 does with `CFBKG`
(`master_bkg_rate.write_into_rate`, atomic by copy). All of them are written every time, empty when
there is nothing to hold, because a jwst save of the rate file records every extension name in its
ASDF tree and a later rewrite must find each one. `masks.restore_pre_bkgsub` drops them together
with the detector background. `MBKG1DR`'s header holds step 1's skip key
(`master_bkg_rate._inputs_cards`): its version, the rate file and its `CFBKGDT`, the metafile name
and id, the bar-shadow table and its stamp, the wavelength cutoffs, the hash of the manual and web
stuck entries, the trace-map and geometry versions, and the wavelength range.

### 4.3 Stuck-closed shutters

A stuck-closed shutter passes no light. Treated as open, it would count as background in the nod
subtraction, get a modeled background where no light arrives, and give a source in it a spectrum of
nothing. Stuck shutters change between MSA configurations (of 10 shutters vetted stuck in an EMBER
PRISM configuration and also in the G395M one, 7 were stuck again and 3 open), so every
configuration is measured from its own data.

**The fit** (`shutter_fit.py`). Each rate frame is fitted as rate = c + sum_s a_s T_s, with one
background template T_s per commanded-open shutter: the count-rate background times the quadrant
scale times the bar shadow, over the shutter's full footprint, overlaps and the light past the
calibrated range included. Failed-open shutters enter as free templates. An open shutter has
a = 1 and a stuck one a = 0. The fit is weighted, clipped (six rounds at 4 sigma) and solved
densely. Because overlapping traces are modeled, it works on the gratings, where the old detector
(the fraction of low pixels in a shutter's rows) failed: on G395M it found 1 of 17 dark shutters at
its configured threshold.

**The call**, per physical shutter of one configuration, over measurements with at least 200
fitted pixels: stuck if the median a over nods and detectors is below 0.3 (at least 2 measurements);
review if it is 0.3 to 0.8 or the detectors differ by more than 0.5; open at 0.8 or above.

**Evidence** (`ember_cosmos_p1`).
- PRISM: all 15 hand-vetted shutters at median a = 0.00-0.02; no other of 561 shutters below 0.92.
- G395M: 1338 shutters; live shutters at 0.92-1.09 (16-84 %). 23 below 0.3: 21 credible (7 also
  vetted on PRISM, 14 new) and 2 artifacts (templates that are short blue fragments at the filter cut-on),
  which the detector-disagreement rule catches. Single measurements are not decisive on G395M; the
  median is.
- Modeling the background again with the 21 shutters closed: in the pixels whose model changes by
  more than 2 %, the median (rate - model)/model goes from between -0.22 and -0.32 to between +0.03
  and +0.10.
- spurs_gds: the same 3 physical shutters called in G140M, G235M and G395M.

**The stuck list** (`stuck_list.py`), `reference/nirspec/<obs>/stuck_closed_shutters.toml`.
- Key `"q{quadrant}r{row}c{column}"`, matching `^q([1-4])r(\d+)c(\d+)$`; row and column are the
  metafile's `shutter_row` and `shutter_column`. Scope: one root (one MSA configuration), shared by
  its nods.
- Value: always a table with `origin` and `stuck`, optional `a` and `note`, for example
  `"q2r195c64" = {origin = "auto", stuck = true, a = -0.02}`.
- Origins `manual` (typed by a person), `web` (the portal's nod review) and `auto` (the fit), with
  precedence manual > web > auto. `stuck = false` from a person vetoes an auto call, so a wrong call
  is not reinstated on the next run.
- Legacy entries (`sid = [ordinals]`) are still read and mapped through the metafile on read
  (`stuck_list.resolve`); ones that cannot be mapped are reported. The local file is never
  rewritten to physical keys. The auto writer (tomlkit) rewrites only auto entries and does not
  write when the merged content is unchanged, because the file time is the canonicals' skip key
  (`STKSHTIM`).
- The database keeps the same keys in its own table, `nirspec_stuck_shutters`: one row per
  observation, exposure root and physical shutter, origin `manual` (pushed from the local file on
  deploy) or `web` (the portal's nod review). Auto calls stay local. It supersedes the per-source
  ordinal lists in `nirspec_source_review.stuck_shutters`, which
  `scripts/backfill_stuck_shutters_physical.py` converts once the migration is live.
- `detect_stuck_shutters = false` keeps the calls in the rate file's `CALLS` table but does not
  write them to the list. Header card `STKSHPHY` lists the physical shutters applied.
- Stage 2a remakes, without `--overwrite`, a canonical whose `STKSHPHY` differs from what the list
  now names for its source, and a background-slitlet file whose `STKSHSET` (a hash of the root's
  stuck set) differs from the root's. So a run that stopped after step 1 changed the list, or a
  hand edit, is caught on the next run.

### 4.4 Trace maps

For every exposure and detector, a map of which slitlet's light lands on which pixel, through which
shutter and at what wavelength. The joint first pass, the prediction of other slitlets' light, the
neighbor scan and the master background need it.
- Built in step 1 from every commanded shutter (failed-open shutters padded like any open shutter),
  following each trace past both ends of the calibrated range, and stored in the rate file
  (`TMSH1`, `TMSH2`, `TMFLAG`, `TMSLITS`, `TMSHUT`; `trace_overlap.ShutterMap`).
- Each slitlet is named by its source at every dither point. Stuck-closed shutters are removed on
  read with the observation's stuck list (`profile_contam.trace_maps`), so a new stuck call needs
  no new map; the last 64 maps are kept in memory per worker.
- A second cataloged source in a slitlet counts as the slitlet's own light: same-slitlet
  neighbors are the job of section 5, not of the joint model.
- A rate file without a stored map (reduced before 2026-11-01) has none; stage 3 logs the
  exposures. Every observation is rerun from stage 1 for the release.

### 4.5 Step 2: the calibrated background and its model on the detector

At the end of stage 2a, per exposure and detector:
- reselects its empty shutters in calibrated units and measures the background spectrum
  (`MBKG1D`, MJy/sr; the only place that unit is used);
- each exposure takes its group's combined background unless it differs from it (eight wavelength
  bands, 2 % and 3 sigma); with fewer than five empty shutters it always takes the combined
  background;
- shutters are weighted equally (inverse-variance weights favored pixels that fluctuated low, about
  2 points on G395M);
- the background is modeled onto the detector through every open shutter's footprint and bar
  shadow, past the calibrated range in count-rate units out to measured cutoffs (where the light
  falls below 2 % of the in-band level: PRISM 0.55 / 5.6 um, G140M/F100LP 0.96 um / detector edge,
  G235M/F170LP 1.55 / 5.05 um, G395M/F290LP 2.60 / 5.7 um);
- failed-open shutters are included at their measured transmission (0 to 1, combined over the nods
  that agree), with the margins of crossing traces kept;
- where two slitlets' footprints light a pixel at the same position along the dispersion (within
  one column, `same_spot_px`), that is the light of one MSA shutter, and it is counted once, not
  once per slitlet.

No per-slitlet scale, no smooth row or column scaling, and no smoothing of the grating background
(appendix A). The settings are fixed in `master_bkg.SETTINGS` and recorded in the header; only
`master_bkg_plot` (the QA PDF) is a config key.

A product is redone when its inputs change: the time stage 2a made each canonical and
background-slitlet file (`CFS2ATIM`, which the later rewrites of those files keep), the rate file's
`CFBKGDT`, the stuck list's file time, the bar-shadow table and step 1's date.

Product `<root>_<nod>_<nrsN>_mbkg.fits` (layout kind `nirspec_mbkg`): `MBKG1D`, `SHUTTERS`, and the
model on the detector `MBKG2D` / `MBKG2DN` / `MBKG2DDQ` (losslessly compressed: 2.8 MB on PRISM,
about 8 MB on G395M). `load_product` follows `MBKGRATE` to read `MBKG1DR` from the rate file.
`sky_for_slit(product, path, extver=1, table=None, mode='total', mask_bits=0)` is the interface the extraction
uses; it warns when it falls back to the slit's own slitlet.

**Accuracy.** PRISM: each exposure within 0.2-0.4 % of the combined background in scale, a quadrant
term of +-2.5 %, and a shutter-to-shutter scatter of +-4 %, of which about 2.7 % is a per-shutter
floor that nodding cancels and an ensemble cannot. On held-back empty shutters after the weighting and
bar-shadow fixes the residual is -0.3 % (G395M) and +0.1 % (PRISM). G395M's background agrees
with PRISM's in the same field to 1-5 %. The background modeled past the calibrated range joins the
in-range model at the percent level (`MBKGRRAT` 0.968-1.007 on p1). On spurs_gds the extended
geometry and the new G140M / G235M tables give NRS1 in range -0.1 to +0.5 %, extensions +0.7 %
(G140M) and +2.9 % (G235M). Section 10 lists what is still being measured.

---

## 5. Stage 2b: nod subtraction and same-slitlet neighbors

Each exposure minus the mean of its background exposures, as before. Two things are new.

**The variance includes the subtracted exposures' noise.** Before, the error came from the
canonical's own variance only, because jwst's background step does not add the subtracted frames'
variance: every nodded spectrum's error was about 20 % low with three nods.

**Same-slitlet neighbors.** With three nods, a cataloged neighbor that a background exposure puts
on the source's position takes half of its light, negative, off the source. The rule
(`slitlet_neighbours.py`):
- Positions come from the MSA plan's `SOURCE_INFO`. A quadratic map from sky offsets to MSA shutter
  coordinates (`SkyToMsa`), fitted per configuration and quadrant with one offset per dither point,
  reproduces the plan's own placements to 0.001-0.003 shutter on p1. Every `SOURCE_INFO` source
  within 0.75 of a row pitch of the slitlet's row and 0.75 slit units of one of its open shutters,
  at any dither point, is a neighbor (one the map cannot place falls back to `SHUTTER_INFO`).
- A background exposure is contaminated for a nod when it puts a neighbor within
  `slitlet_neighbor_max_distance` (0.75 slit units) of the source's position in that nod.
- It is left out when the neighbor's light is at least `slitlet_neighbor_min_background` (0.02) of
  the master background and another background exposure remains. The light is measured in the frame
  before subtraction, over one shutter height around the neighbor, inside the slitlet's open
  shutters, median over the spectrum, in a frame that holds the source and puts the neighbor at
  least 1 slit unit from it. A neighbor too close to measure counts as bright. Both detectors give
  one choice per nod.
- When every background exposure is contaminated, the nod keeps them all and `BKGWHY` says so ("every
  background exposure: neighbor <id> at <f> of background").
- Manual overrides (`nodded_background_overrides.toml`, reason `manual`) win. `BKGNODS` and `BKGWHY` are copied to the
  `EXPOSURES` table (`bkg_nods`, `bkg_why`), and the bkgsub plots label each nod
  `nod - [background nods]` with the reason.

Stage 2b measures the neighbor's light from the data as a first guess. Stage 3 redoes the decisions
from the joint model (section 6.8).

**Evidence** (p1, by position): PRISM 14 sources with a neighbor within reach, 21 nods with a
contaminated background exposure, 4 with none uncontaminated; G395M 39 sources, 57 nods, 11 with none
uncontaminated. In the PRISM check (the light an override
removed from the source's shutter against the neighbor's own PRISM spectrum), 8 of 15 overridden
nods removed light with the neighbor's spectrum, among them negative lines of the neighbor at
3.75 and 2.85 um; the other 7 removed nothing measurable and only cost noise. On average the effect
on G395M against PRISM is within the noise; it is clear where the neighbor is bright.

---

## 6. Stage 3: the extraction

### 6.1 The reported columns

| Column | Content |
|---|---|
| `fnu`, `flam` | the best spectrum: `fnu_prof`, or `fnu_3px` for a named failure (`EXTRACT`, `EXTRWHY`) |
| `fnu_prof` | the combined 2-D spectrum extracted over the aperture rows with the fitted source model (`PROF2D`) as inverse-variance weights, scaled to the reported flux; nothing subtracted |
| `fnu_contam` | other slitlets' predicted light (`CONTAM2D`), extracted the same way; NaN where none is predicted or where the source cannot be separated from a neighbor |
| `fnu_opt` | the v1 optimal extraction, on jwst's point-source calibration |
| `fnu_3px`, `fnu_4px`, `fnu_5px` | boxcars, unchanged, on jwst's point-source calibration |

`fnu_err` is statistical only: it includes the subtracted exposures' noise and leaves out the
prediction's uncertainty (`fnu_contam_err`) and the shape's. Section 8 lists the 2-D extensions
and cards.

### 6.2 The source model

`profile_fit.ProfileModel`. The source is a round Gaussian whose center and width vary linearly
with log wavelength (`profile_fit_chromatic`). It is seen through the optics model (6.3) in every
exposure, and as negative images at the positions of that exposure's background exposures. One
spectrum is shared by all exposures and solved wavelength by wavelength, linearly, given the shape.
The fit sets the shape and the model images (`PROF2D`); the reported columns are then extracted
from the combined 2-D spectrum with those images as weights (6.1), so the 1-D spectrum is read
from the same drizzled data as the other extractions.

- **Each nod at its own planned position** (`NOD_OFFSET_LAW`, scale 1). The shared center is moved in
  each exposure by its SRCYPOS minus the reference. Measured freely on 465 calibration stars, the
  planned differences take the exposure offsets' rms from 0.0100 to 0.0043 slit units with no
  parameter; a fitted law does no better on an observation it was not fitted to. A single exposure,
  frames with a placeholder SRCYPOS, and frames without the source are not shifted.
- **No per-exposure pointing offsets.** The nods land where commanded (achieved steps within 0.001
  slit units of the plan). In crowded G395M, fitted offsets took neighbors' light and held it in
  the joint solve (p1 629668's false 5.05 um line).
- **The center law**: the trace's drift by MSA quadrant and with wavelength, measured through the
  optics model on 497 stars (`profile_fit.CENTRE_LAW`). Held out by observation it leaves a scatter
  of 0.001-0.011 slit units over 0.85-4.7 um on NRS1.
- **Bar offset**: each pixel's slit position and the planned position move by the observation's bar
  offset (6.4).
- **Priors and bounds** on center, width, drift and width change (`prior_*`, `bound_*` in
  `[nirspec.stage3]`). They are marked provisional in the config: calibrated on two observations.

### 6.3 The optics model of the MSA

`nirspec/optics.py`, model file `data/nirspec_optics.json` `2026.11-r8`, header card `OPTICVER`. One
model for every disperser: the light that reaches the MSA does not depend on the disperser.

**Along the slit**, a one-dimensional Fraunhofer model:
1. the telescope's image at the MSA, from a 6.5 m pupil with amplitude (1 - u^2)^0.25 across the
   slit, at each wavelength;
2. an empirical kernel on the sky before the MSA (wavefront error, pointing jitter), one per
   detector, a free table over 31 offsets from -2 to 2 slit units, interpolated in wavelength;
3. the open shutters of each exposure, each exactly its open height, placed exactly on the model's
   grid (`opening_mask`);
4. the spectrograph's pupil stop (0.996 of the pupil's image), which removes part of the light
   diffracted at the shutter edges;
5. a Gaussian detector blur, sigma = 0.0344 arcsec - 0.0026 arcsec/um x (lambda - 3 um), then
   integration over a pixel row.

Tables of point images per open-shutter pattern (wavelength x source position x detector position)
are built on first use and cached under `$CAMPFIRE_ROOT/cache/optics`, keyed by the model's numbers,
the kernel's content and `ENGINE_VERSION`. A Gaussian galaxy is a sum over source positions at most
0.025 slit units apart; a compact one (width <= 0.06) is summed exactly over the interpolated table.

**Normalization.** The absolute level is jwst's point path loss of a source at the shutter center
(paired with jwst's flux calibration). The model sets how the light kept changes with the source's
position along the slit and its width. Each exposure's own open shutters enter, so a source in an
end shutter or a single shutter loses the light that falls on its closed neighbors (0.7-1.2 % and
1.8-3.4 %); jwst's tables repeat one profile in every shutter and count none of it, so nothing is
counted twice.

**Along the dispersion**, jwst's point path loss (its x part; `pathloss_0010` is exactly an x part
times a y part) times a correction fitted to stars, `x_correction` = exp(-b max(lambda - 2 um, 0)
x^2), x = SRCXPOS, b = 0.572 (`x_loss` in the model file). For an extended source, times the model's
ratio of extended to point transmission across a 0.20 arcsec shutter (`x_extended_ratio`, the
source taken as round, with the half-width 0.110 arcsec and position scatter 0.030 arcsec the
stars' x loss fitted). The correction raises a flux by 13 % at 4.5 um and x = 0.3; below 2.5 um it
changes nothing measurable.

**Calibration.** The diffraction numbers (pupil apodization, stop, blur) were chosen to reproduce
the measured PRISM bar shadow without more wide-angle light than the stars show; the kernel was
then fitted linearly to the stars (`optics_calibration` `kernel` / `solve`). Stars: 590
star-detector pairs (a star on both detectors counts twice) from all PRISM calibration visits with
every star used, each weighted once whatever its number of exposures, with each observation's bar
offset: NGC 2506 (program 1493, ten configurations) 390, programs 6642 and 12497 167, the
commissioning slit-loss program 1133 33. A star's wavelength range enters when S/N >= 20,
|center| < 0.35, unsaturated, width <= 0.06 slit units and
chi2/dof <= 2. A further pass changes the light beyond 0.3 slit units by at most 0.2 %: the fit
has converged. The tests below run on the same stars, by dataset; nothing is held out.

**Evidence.**
- *Stack test* (per detector and range, the median over clean stars of data minus model at each
  offset from the trace, % of the peak; a range passes within 1 % at every offset): 14 of 14
  ranges over 0.7-5.3 um, worst 0.69 % (NRS2 3.3-4.1 um). By dataset, the `2026.11-r8` kernel and
  the previous one agree to 0.1 % in every range.
- *Uniform light*: the model's image of a uniformly lit slitlet misses the empirical bar-shadow
  tables by a median 0.018 (PRISM) and 0.019 (G395M) rms of the open level.
- *The same star at different positions* (NGC 2506, 93 stars in two or more of ten configurations,
  245 visits, each against the star's most central visit): clipped scatter 8.0 / 11.5 / 6.5 / 6.1 /
  9.4 % in 0.7-1 / 1-1.5 / 1.5-2.5 / 2.5-3.5 / 3.5-5 um, no trend with position along the slit.
  The previous shutter model (`FIT_VERSION` 5) gave 18.3 / 19.3 / 17.9 / 12.9 / 13.2 %, its fluxes
  rising 4-7 % per 0.1 slit units from the center. These numbers are from the kernel before
  `2026.11-r8`; the repeat on `2026.11-r8` is among the checks still to run (section 10).
- *1133* (34 stars, each at 20 positions, 680 visits, band S/N about 1000): compact sources inside
  |x| < 0.3 and |center| < 0.3 are reproduced to within 1.4-1.6 % (0.7-3.5 um) and 2.5 % (3.5-5 um) once
  each configuration's pointing offset in x is allowed.
- *Physical cross-check*: a two-dimensional calculation with the real JWST pupil (segments, struts,
  three open shutters, the stop) agrees with the model near a bar (0.70-0.73 and 0.54-0.57 of the
  centered light at 0.35 and 0.40 pitch at 5 um; model 0.71 and 0.52; jwst's table 0.85 and 0.73),
  and shows the small rise of a compact source's transmission off center in the red that the model
  also has.
- *Gratings*: the shared model against each grating's own earlier tables on 6642 obs 2 gives chi2
  ratios of 0.95-1.03 in 18 of 20 detector and wavelength-range combinations. G140H on NRS2 above 1.4 um fits worse (cores
  sharper than the PRISM kernel).
- *Galaxies* (p1 PRISM and G395M): chi2/dof 0.94-1.14 with noise-like continuum residuals; emission
  lines leave residuals where the line emission sits differently from the continuum (one shape for
  both).

### 6.4 The bar offset

Where an observation's MSA bars sit against jwst's slit frame changes between observations (by
about +-0.01 slit units, +-5 mas; observations hours apart differ). A source near a bar is
sensitive to it: an offset of 0.03 changes a point source's flux by more than 5 % for 13 / 28 / 41 %
of sources at 1 / 3 / 5 um.

`nirspec/bar_offset.py` measures it per disperser from the observation's own empty shutters, where
the background lights the shutter evenly. Each pixel's surface brightness over its exposure's
background and the shutter's own center level is compared with the model's profile for uniform
light (its point images summed over source positions, without the kernel) at the pixel's slit
position plus a trial offset. Only the steep part of each edge (0.2-0.75 pitch from the center) on
the side facing away from the source is used, and shutters brighter than 1.05 of the background
(`SCALE_CUT`) are left out, because bright stars bias it. A fixed quadrant pattern (-0.0042 /
+0.0003 / -0.0009 / +0.0047) is added per quadrant, after the pattern's mean over the fitted pixels
is taken out (the fit over every quadrant's pixels already holds it, in proportion to where the
pixels sit; zero for a field spread evenly). A disperser with under 3000 usable pixels per half, or
whose best trial offset is the edge of the trial range (+-0.10), gets none. The pixels are binned on
the empirical bar-shadow table's wavelength grid, which for G140M and G235M runs to 5.3 um (CRDS's
stops at 1.495 / 2.49 um).

`profile_fit.load_group` adds the offset to every pixel's slit position and to the planned
position (jwst's SRCYPOS, which only undoes jwst's own path loss, stays as it is); the light past
the cutouts takes the same frame. The value is kept in the workspace's `bar_offset.json` (measured
again when the canonicals change in number or are made again by stage 2a, their `CFS2ATIM`, or when
`MEASURE_VERSION` or the optics model change) and recorded in
`BAROFF`. `bar_offset = "off"` turns it off.

**Evidence.** Moving the data by +0.020 pitch is recovered as +0.022 / +0.023. Survey observations
range from -0.015 to +0.0005 against the model, with the two edge halves agreeing to 0.001 and
exposures to 0.001-0.003. A grating sees the same offset as the PRISM of the same visit to within
0.002-0.005. On 1133 the stars prefer +0.020 and the background measures +0.017 (before the zero
point was set on the model). The zero point is the model's own; the two star sets would move it by
about +-0.01, which stays a known limitation. On p1 galaxies the offset changes no shape; central
sources move by under 0.3 %, default-shape sources near a bar by a median +0.2 to +0.9 %.

### 6.5 The reported flux

`fnu_prof` (and `fnu`) is the fitted source's light inside a circle 0.8 arcsec across (1.5 shutter
pitches), centered on it: the round Gaussian of the fitted width before the PSF
(`profile_fit.REPORT_APERTURE`, card `FLUXAPER`). For a point source this is all of its light. The
aperture bounds how far a wide source is extrapolated past what the shutter saw. The fit's spectrum
is the source's total light (along the slit from the model at the fitted width, along the
dispersion as in 6.3); `reported_factor` scales it to the aperture.

`fnu_opt`, `fnu_3px` and the cal and s2d files stay on jwst's point-source calibration: the light
entering the shutter.

**Evidence** (p1 PRISM against COSMOS photometry).
- Against the catalog's own 0.8 arcsec aperture flux (`FIT_VERSION` 8; 127 sources with their own
  fit; per band, catalog and spectrum S/N >= 10 and the band fully covered; each source's ratio
  the median over its bands): the reported flux reads a median 0.952 +- 0.03 (error of the
  median), 16-84 % 0.70-1.30, scatter 0.124 dex. The light in the shutter (`fnu_opt`) reads 0.620,
  16-84 % 0.41-0.84, 0.149 dex. Per band: F150W 0.84, F200W 0.97, F277W 0.89, F356W 0.92, F444W
  0.95. The ratio sits near 1 for compact sources and rises above 1 for the widest (figure
  `web/public/docs/nirspec/reported_flux.png`, `scripts/docs/nirspec/fig_reported_flux.py`;
  numbers from `scripts/docs/nirspec/reported_flux_stats.py <root> <aperture json>`).
- Against the catalog's total (Kron) flux, in six bins of fitted width (`FIT_VERSION` 6): the
  light in the shutter read 0.87 for the most compact bin down to 0.32 for the widest; the
  reported flux reads 0.93 / 0.77 / 0.77 / 0.75 / 0.68 / 1.03, with the scatter 0.20 -> 0.16 dex.
- The aperture flux holds the source blurred by the PSF and
  `fnu_prof` the source before it, so a point source would sit a few percent above 1. The level
  near 0.8 against the Kron total is the comparison (in the catalog the light inside 0.75 / 1.0
  arcsec is 0.75-0.82 / 0.81-0.89 of the Kron total), not a calibration difference.
- The fitted width follows the source's size (rank correlation +0.73 / +0.75 with the catalog's
  half-light radius in F200W / F444W) but describes its core.

### 6.6 The shape chain

`profile_chain.choose_shape`, `profile_fit_shape = "gated"` (`"fixed"` uses the default shape only).
The fit's detection S/N is measured on the clipped mean square of the residuals.

1. **PRISM shape** (gratings only). When the same source's PRISM product took its own fit at the
   same position angle, its center offset from the planned position and its width are the
   grating's fallback candidate (`profile_fit_prism_transfer`). PRISM and grating visits of one
   configuration share pointing and planned positions, so the offset means the same thing (over
   113 free G395M fits, G395M minus PRISM center +0.006, scatter 0.05).
2. **Own fit**, above the detection gate (`gate_snr_prism` 25, `gate_snr_grating` 70). A grating
   with a PRISM shape keeps the PRISM shape unless its own fit is at detection S/N 100 or more
   (`own_vs_prism_min_gate`) and both better (chi2 by `own_vs_prism_dchi2` + k ln N) and different
   (center by more than 0.08 or ln width by more than 0.3).
3. **Bounds.** An own fit with a width or drift on its bound falls back: that is the fit taking
   up other light (a neighbor in the slitlet). One with only the center on its bound is kept when
   the fallback fits clearly worse (`bound_kept`), since a large offset from the plan can be real.
4. **Default shape**: a Gaussian of width 0.15 slit units (0.16 arcsec FWHM) at the planned
   position. A source whose planned position is inconsistent between exposures takes it too.
5. **Another grating's shape**, after every grating has run (`profile_fit_grating_transfer`,
   `stage3.grating_transfer_plan`): a grating product that fell to the default and had no PRISM
   shape is redone with the center offset and width of the source's grating that took its own fit
   (the highest detection S/N), held over wavelength. `SHAPE` = `grating`, `SHAPESRC` names the product.

`SHAPE` (`own`, `prism`, `grating`, `default`), `SHAPEWHY` and `DETSNR` record the choice.

**Why a chain.** Faint sources do not constrain their own shape, and a joint solve with wrong
shapes moves light between neighbors (6.7). The success metric is delivered S/N, with line width and
centroid (user, 2026-10-22).

**Evidence.**
- p1 G395M sources with a PRISM spectrum: 112 of 156 took the PRISM shape, 4 their own, 40 the
  default.
- The grating transfer on spurs_gds: 29 of 240 products redone (6 % of the profile fit's time);
  their width 0.15 -> 0.074, flux x 0.80 (16-84 %: 0.66-0.92), delivered S/N x 1.02; the other 211
  bit-identical. The 16-84 % ranges of the line-flux ratios between gratings went from 0.84-1.43
  to 0.90-1.13 across the overlaps measured.
- Own-fit widths on p1 at 2 um: median 0.143 slit units, no trend with S/N, quartiles about
  0.10-0.22. That spread is the uncertainty of a source without a measured size, about +-15 % in
  flux, and it is not in `fnu_err` (user, 2026-11-04).

**Fallbacks.** Named failures fall back to the 3-pixel boxcar (`EXTRACT = boxcar3`, the reason in
`EXTRWHY`); any other error fails the stage.

### 6.7 Other slitlets' light

**The joint first pass** (`joint_first_pass.py`, the linear system in `profile_joint.py`). For each
source, its neighborhood is every source whose source shutter lights the source's data in the trace
maps (its cutouts and its trace past them), plus their direct neighbors (two steps, no size cap).
On p1 G395M that is a median 31 members, at most 46. All members are fitted together
on their nod differences, each at a fixed shape from the chain, so the fit is linear in the spectra
and nothing is masked. Each pixel's model is the sum of every member that lands on it, with the +1
and -1/n terms of the nod subtraction. A member's light past its cutout is a second set of unknowns
per end (count rate, native grid), fitted on nod differences of the rate files
(`profile_extend.py`); the unknowns are scaled to unit diagonal before the solve. Only the target's
spectrum is kept; each source is the target of its own neighborhood. Each solve writes the other
members' light on the target's own pixels, which the product reads. Targets are ordered greedily so
consecutive neighborhoods share members (`contam_joint_cache` members kept per worker).

**Not separable.** A wavelength where the target's spectrum correlates with a member's below -0.9
is not separable; `fnu_contam` is NaN there (`CONTNSEP`, the fraction). The two are not determined
by the data at those wavelengths.

**The prediction** (`profile_contam.py`). Other slitlets' light is predicted from the neighbors'
first-pass spectra over the trace maps. It is subtracted before the shape fit, and the pixels where
the prediction's own uncertainty exceeds `contam_mask_kappa` times the noise are left out of the
fit. In the 1-D spectrum it is reported as `fnu_contam` and never subtracted from `fnu`. A neighbor
whose own fit fell back is predicted from its 3-pixel boxcar, smoothed (`CONTBOX`). A trace the maps
name with no stage-3 s2d is an error (`MissingContaminant`): a contaminant without a spectrum means
something is wrong. `fnu_contam_err` is the error with the other members held, a lower bound, plus a
floor of 0.1 of the prediction (`DPRED_FLOOR`, an assumption).

**Evidence.**
- Why not mask: on p1 G395M a mask of every pixel another source lights left a median 34 % of a
  source's own pixels; 47 % of sources fell below 30 %, and a median 20 % of each spectrum had no
  own pixel at all.
- Simulation with known truth (116 sources in 8 neighborhoods on the real p1 G395M geometry and
  noise): joint rms 0.99-1.06 of its error, bias at most 0.14 sigma with realistic shapes, line
  fluxes mostly 0.95-1.05 of truth. The masked pass was biased low (-0.4 to -0.5 sigma, tail to
  -4.7) and lost lines.
- p1 G395M against the same sources' PRISM (rms deviation over `fnu_prof_err` alone, 138-141
  sources): `fnu_prof` 1.41 median, 3.22 at the 90th percentile; `fnu_prof - fnu_contam` 1.47 and
  2.47. Subtracting the prediction cuts the worst cases (635314 14.1 -> 1.6, 629668 5.7 -> 2.5) and
  adds the neighbors' noise to faint sources with faint neighbors. That is why `fnu_contam` is
  reported and not subtracted.
- Not separable on p1 G395M: none in 358 of 477 sources, over 50 % in 72, over 90 % in 31 (mostly
  pairs on the same detector rows a short distance apart along the dispersion).
- spurs_gds: contamination predicted past the nominal red end in 14 G140M and 15 G235M products.

### 6.8 The same-slitlet revision and the neighbor scan

**Revision** (`stage2.revise_slitlet_overrides`, `slitlet_neighbor_brightness = "model"`). After a
disperser's joint first pass, stage 3 redoes stage 2b's decisions with each neighbor's brightness
from its modeled light, which leaves out other slitlets' light: on p1 G395M, where another slitlet
lights more than 20 % of the band, the data measure read a median 3.1 times the model. The
1-slit-unit separation limit no longer applies, since the model holds none of the source's light.
For the decision, the first pass fits every member on the nod differences of the override files,
rebuilt in memory from the frames before subtraction (`PRE_BKGSUB_*`) with stage 2b's arithmetic,
so the same data and settings give the same decision whatever the previous run wrote. Groups whose
choice changes get stage 2b again; sources whose own background exposures then differ get their
first pass again. Spec3 runs after the revision. On p1 G395M the model dropped fewer exposures than
the data measure (22 sources on 26 exposures against 54 on 65).

**The neighbor scan** (`nod_scan.py`) looks for neighbors that are not in the catalog. It refits the
source at its chosen shape on the nod differences as stage 2b made them, with an extra component
along the slitlet, leaving out every pixel another slitlet's source shutter lights in any frame of
the difference (so it never depends on the joint model). Above `scan_max_masked` (0.3) of such
pixels it reports only. Its proposals are never applied. They go to
`reference/nirspec/<obs>/nodded_background_overrides_proposed.toml` (only what would change), each
proposed source gets a figure `<obs>_<id>_neighbor.pdf` laid out like the bkgsub plots (per nod
the rectified difference with the current and the proposed background exposures, each with its
profile along the slit; the spectrum with each choice), and stage 3 prints the command that reruns
those sources. A proposal stage 2b already applied for a cataloged neighbor is left out. The `NBR*`
cards record the scan. On p1 924883 (PRISM) and spurs_gds 369 (G140M) a proposal raised the S/N
from 10.2 to 11.5 and from 20.4 to 22.3.

### 6.9 The master-background spectrum

`mbkg_extraction.py`. The same extraction on the exposures with their master background subtracted,
so the two spectra differ only in how the background was removed.

- Frames: each exposure's calibrated frame before subtraction (`PRE_BKGSUB_SCI`) minus its master
  background (`sky_for_slit`), in memory; the variance adds the master background's.
- The fit: the nodded fit's exposures as single frames, at the nodded chain's shape, the spectrum
  solved. Other slitlets' light enters once and positive, cataloged same-slitlet neighbors
  included, since nothing is subtracted.
- Failed-open shutters on the source's trace (`failed_open.py`): per exposure and shutter, a
  spectrum along detector columns times a flat profile over the shutter, solved with the source on
  the frames minus the other sources' predicted light, and added to the master-background
  spectrum's contamination and its error (card `NFOPEN`). On p1 109 of 681 products have failed-open pixels on the source's
  shutter. The nodded spectrum is unchanged: the background behind a failed-open shutter cancels in
  the nod difference.
- Outlier flags: the nodded Spec3's, the pixels without a master background, and a hot-pixel mask.
  A pixel that stays high at one detector pixel in every exposure cancels in the nod differences,
  so the nodded detection never flags it. `local_contrast` is the smaller of the value minus the
  median of its row neighbors (+-3) and minus the median of its column neighbors (+-2), over its
  error. A pixel above 5 sigma (`HOT_SIGMA`) in every exposure on that detector, at least 2
  (`HOT_MIN_EXPOSURES`), is flagged. Background, source and neighbors move between nods; a hot
  pixel does not. With one exposure nothing is flagged.
- Extensions `SPEC1D_MBKG` (SPEC1D's columns, same `wave`), `SCI_MBKG`, `ERR_MBKG`, `PROF1D_MBKG`.
- Primary cards `NEXP`, `NEXPBKG`, `MBKGNEXP`, and `MBKGPREF` / `MBKGWHY` when the master-background
  spectrum is likely better, with the first of: `no_clear_background` (every background exposure of
  an exposure holds a bright neighbor), `single_exposure` (an exposure with no background exposure),
  `extended` (the nod subtraction removes more than `mbkg_extended_frac` = 0.10 of the source's own
  light in its own shutter, `SELFSUB`).
- The 1-D combine of several exposure groups carries it as it carries the nodded columns.
- The science hash leaves out `*_MBKG` extensions and `MBKG*` cards, so this spectrum never marks
  line fits stale.

**Evidence.** On uncover2 20698 the master-background spectrum lies about 3 % above the nodded one
where the source is bright and 10-20 % where it is faint or the background high: the shape of a
small additive background difference. On p1 G395M it differs from PRISM more than the nodded one
does (median 7.3 against 2.8 sigma) and the difference follows the level left in its own
off-source rows. Neither comparison measures the master background against a truth (section 10).

### 6.10 Every 2-D image drizzled once

`drizzle_map.DrizzleMap.from_crf` builds jwst's output grid from the crf and each exposure's pixel
map once per product; every 2-D image (`PROF2D`, `CONTAM2D`, the contamination's error, the
master-background 2-D) then goes through the drizzle library with stcal's weights and variance propagation. The nodded
data stay Spec3's s2d. The crf's own arrays through the map reproduce Spec3's s2d bit for bit; the
model images agree with the old per-image `ResampleSpecStep` calls to 1-2 float32 ulps (jwst built
each call's grid weighted by the image drizzled). `fnu` moves by at most 1.5e-7 relative. The
separate master-background Spec3 is gone. Without a crf (a named rare condition) there is no
master-background spectrum.

---

## 7. The boundary with jwst's references

CAMPFIRE replaces parts of two CRDS references: the path loss (along the slit with the optics model,
and a correction along the dispersion) and the bar shadow (the empirical tables, used by the master
background; science slits skip jwst's bar-shadow step). The boundary is explicit, so a better
reference from STScI can be taken up by a setting and measured by a command.

- **One reader.** `nirspec/crds_refs.py` is the only code that reads the path-loss or bar-shadow
  reference or interprets what jwst wrote from them into a cal file. It checks the assumptions the
  fit makes of the path-loss reference (a PS extension with APERTURE = MOS1x3, three axes, y over
  three pitches and x over one, wavelengths in meters) and names the one that fails. Every other
  CRDS read is a calibration used as delivered and stays where it was.
- **One switch per replaced part**, in `[nirspec.stage3]`:
  - `slit_loss_y = "optics"` (the model's loss along the slit for the source's width and fitted
    position) or `"jwst"` (jwst's point path loss at the planned position; the model only shapes
    the image);
  - `slit_loss_x = "corrected"` (jwst's along the dispersion times `x_correction`) or `"jwst"`;
  - `bar_offset = "measure"` or `"off"`.

  The products record `SLOSSY`, `SLOSSX` and `BAROFF`; the path-loss reference the model starts from
  follows from `CRDS_CTX`. The empirical bar-shadow tables have no switch.
- **A manifest.** `data/nirspec_calibrations.toml` lists each shipped calibration (the optics model,
  the four bar-shadow tables, the bar offset): file and sha256, version, what it replaces, its
  inputs, the CRDS context it was fitted on, the references it depends on, its switch, code, tests
  and record. Tests fail when a file changes without its entry, when a version differs from the
  file's own, or when a named switch is not in the default config. When a cal file names a path-loss
  reference other than the one a calibration depends on, the loader warns once.
- **A standing comparison.** `cfpipe nirspec crds-compare` prints, for the active context, which
  references each MOS disperser gets and whether the calibrations were derived against them, how
  far the path-loss reference is from separable, jwst's point loss along y against the model's at
  0.1-0.4 pitch, jwst's dependence along the dispersion against `x_correction`, and the edges of each
  bar-shadow table against its CRDS reference. Run it at every `CRDS_CONTEXT` change (AGENTS.md).
  For `jwst_1481.pmap`: a point source 0.4 pitch off center keeps 0.697 / 0.673 / 0.729 of its
  centered light at 1 / 3 / 5 um in jwst's table and 0.857 / 0.609 / 0.563 in the model.
- In the fit, stage 2 types every source as a point source (`set_stellarity = 1.0`), so jwst's
  point/uniform choice never applies to a science source. The fit divides the applied correction out
  of the data and keeps only jwst's point table's level at the shutter center and its x dependence.

---

## 8. Products

**`_spec.fits`** (`<obs>_<grating>_<filter>_<sid>_spec.fits`). Readers find every extension by
name.
- `SPEC1D` columns: section 6.1, each with `_err` and `flam_*`.
- 2-D: `PROF2D` (the source model per uJy of reported flux, in SCI's units) and `CONTAM2D` (other
  slitlets' predicted light); the full model is `PROF2D x (fnu_prof - fnu_contam) + CONTAM2D`.
  The prediction's 2-D error (`CONTERR2D`) lives only in the `_pf.fits` intermediate and is
  reduced to `fnu_contam_err`. No 2-D image is stored twice.
- Master-background spectrum: `SPEC1D_MBKG`, `SCI_MBKG`, `ERR_MBKG`, `PROF1D_MBKG`.
- Cards: `EXTRACT` / `EXTRWHY`, `CONTSTAT`; the extraction's cards, named by what they hold and
  listed with their descriptions in `nirspec/extraction_cards.py`, the one list the writers, the
  stage-3 copy and the FITS reference follow: versions and calibration (`EXTRVER`, `OPTICVER`,
  `FLUXAPER`, `SLOSSY`, `SLOSSX`, `BAROFF`), the profile (`SHAPE`, `SHAPEWHY`, `SHAPESRC`, `PA_V3`,
  `DETSNR`, `SRCCEN`, `SRCWIDTH`, ...), other sources' light (`NCONTAM`, `CONTFRAC`, `CONTSIG`,
  `CONTNSEP`, `CONTBOX`, `SELFSUB`, `NFOPEN`) and the neighbor scan (`NBR*`); the master-background
  spectrum (`NEXP`, `NEXPBKG`, `MBKGPREF`, `MBKGWHY`, `MBKGNEXP`). A card that does not apply is
  left out. The fit's internal diagnostics (candidate chi2, gate noise, scan statistics,
  per-exposure offsets) are not written (user, 2026-11-08).
- The `EXPOSURES` table gains `bkg_why`, beside the existing `bkg_nods`.

**Per exposure.** `<base>_rate.fits` carries step 1's tables and the trace map;
`<base>_mbkg.fits` (layout kind `nirspec_mbkg`); the canonicals; `bkgslits/` (local, regenerable).

**Deploy and database.** The deploy writes `<base>_spec_mbkg.json` (layout kind
`spectrum_mbkg_json`) beside the spectrum sidecars, and the `spectra` columns `mbkg_preferred`,
`mbkg_reason`, `n_exp` and `n_exp_bkg`. The stuck list's database shape is in section 4.3.

**Gone.** `_mbkgr.fits` (kind `nirspec_mbkgr`), the `tracemaps/` folder, the PSF and shutter tables
and the shutter-table card. The `_pf.fits` intermediate is deleted once the `_spec.fits` is written.

---

## 9. Configuration

New in `[nirspec.stage2]`: `slitlet_neighbor_min_background`, `slitlet_neighbor_max_distance`,
`master_bkg_plot`; `detect_stuck_shutters` now governs whether step 1's calls reach the stuck list.

New in `[nirspec.stage3]`: `profile_fit`, `profile_fit_chromatic`, `profile_fit_shape`,
`profile_fit_prism_transfer`, `profile_fit_grating_transfer`, the gates (`gate_snr_*`),
`default_shape_width`, `own_vs_prism_*`, the priors and bounds, `srcypos_tolerance`, `bar_offset`,
`slit_loss_y`, `slit_loss_x`, the contamination keys (`contam_predict`, `contam_joint_cache`,
`contam_pass1_*`, `contam_mask_kappa`, `contam_min_clean_fraction*`),
`slitlet_neighbor_brightness`, `mbkg_spectrum`, `mbkg_extended_frac`, and the neighbor scan
(`scan_*`).

Removed (`config.REMOVED_OPTIONS`; a config that still sets one is told once that it has no
effect): `single_call`, `extract_bkgslits`, `empirical_barshadow`, 29 `master_bkg_*` thresholds
(now `master_bkg.SETTINGS`), v1's `low_frac` stuck detector keys
(`stuck_shutter_low_frac_threshold*`, `detect_stuck_shutters_grating`), `profile_fit_prism_mode`,
`gate_noise`, partial fits, model-row extraction, `contam_mode`, `contam_dominance*`,
`contam_1d*`, `contam_pair_*` (the similar-brightness pair flag: it reached no product once its
cards went, and it missed jointly solved neighbors; user, 2026-11-08), `scan_noise`,
`scan_close_rule`, `scan_gain_rule`, `scan_action`.

Removed before they reached any release, so not warned about: `profile_fit_shape = "free"`,
`contam_pass1` and the masked first pass's keys, `contam_echo`, `contam_joint*` (but
`contam_joint_cache`), the neighbor scan's flag levels (`scan_flag_dchi2`, `scan_flag_dchi2_low`,
`scan_flag_null_ratio`; section 11, part 4). Appendix A says why each went.

---

## 10. Validation and known limitations

**End-to-end runs**, each in an isolated root:
- p1 (`ember_cosmos_p1`, PRISM and G395M): stage 3 on `FIT_VERSION` 8 and `2026.11-r8`, 681
  products, no fallbacks, 94 min on 12 workers. Against `FIT_VERSION` 7: `fnu_prof` band fluxes within
  0.5 % in median in every band, 16-84 % within +-2 %, no trend with the fitted center.
- uncover2 (PRISM, 344 sources, two visits of three exposure groups): stage 2a to 3, 4 h 45 min, no
  errors (before `FIT_VERSION` 6). The optimal extraction on the new frames matches production v0.3
  within about 1 %; the profile extraction is within about 5 % of it.
- spurs_gds (G140M, G235M, G395M, `extend_g140m_g235m` on): 240 products, no fallbacks, 1 h 47 min.
  Against production v1 on the same exposures (G235M, G395M): noise of the 3-pixel boxcar 1.00-1.01
  of v1's, boxcar fluxes within 1-4 %, delivered S/N of `fnu_prof` against v1's `fnu` 0.99 (16-84 %
  0.88-1.06), stated errors about 20 % larger (the subtracted exposures' noise). The extension's
  calibration still holds on v2 (line-flux ratios between gratings 0.99 / 1.06 / 1.04 / 1.02 in
  `fnu_opt`'s terms by wavelength band).
- uds_p2 (`ember_uds_p2`, PRISM and G395M, the NRSIRS2RAPID readout, one MSA configuration; never
  deployed, v1 products on disk from 2026-07-30), on the code of 2026-11-09: stage 1 to 3 in about
  1 h 30 min, 434 products, no fallbacks. Against v1:
  - the same extraction gives the same flux and larger errors: the 3-pixel boxcar's flux within
    0.2 % in median on both dispersers, its S/N 0.80 of v1's. The canonicals' variances are the same
    before stage 2b and 1.50 times v1's after, the subtracted exposures' noise (section 5);
  - `fnu` is a median 1.46 (PRISM) and 1.51 (G395M) times v1's optimal `fnu` (16-84 % 1.1-3.1),
    the light in 0.8 arcsec against the light in the shutter; 43545 (width 0.57 slit units) reads 4.5
    times;
  - 5 sources have no v2 product: step 1 calls all their shutters stuck closed in every nod, and
    their 10 v1 products are noise (integrated S/N -3.7 to +0.9);
  - every source has both dispersers, which overlap at 2.87-5.25 um. On the 44 sources at v1 S/N
    >= 10 in both, G395M over PRISM (at PRISM sampling, least squares per source) is 0.975 in median
    for v2 `fnu` (scatter 0.019 dex) against 0.985 for v1 (0.027 dex), and the trend from 2.9 to
    5.2 um is 6 % against 9 % (2 % with `fnu_contam` removed). Above G395M S/N 20 both agree within
    5 %. The outliers are other slitlets' light that v2 marks (39341, 34904: half or more of their
    wavelengths not separable; on the separable ones G395M / PRISM is 1.25 and 1.02; 196166, 43281),
    light through a shutter no list knows (below), and the faint G395M deficit (below).

**Runtime.** On p1 (12 workers): stage 3 94 min. Its largest parts are the G395M joint pass (about
30 min with the second pass) and the G395M products (23 min). Stage 2's rectification (25 min) and
stage 2b (about 60 min) were the largest parts of the full run on 6 workers; neither is new in v2.

**Still to run before the release (phase 3, section 11):** the remaining validation plots
(the joint solve against PRISM with 2-D panels first, the inseparable-pair NaN spans, residuals
against overlap count, internal consistency between detectors and nods, delivered S/N against the
archive, injections, shape tests on bright sources, the master-background spectrum against `fnu`),
the master-background residual figures on the current tables, and the optics checks on the shipped
`2026.11-r8` model (`pipeline/scripts/nirspec_optics_checks.py visits` and `repeat`: NGC 2506, and 1133 by
background group, which also refits `x_loss` with 1133).

**Known limitations.**
- *Along the dispersion.* Each visit carries a pointing offset in x of 0.03-0.065 shutter widths
  that the model does not see; the correction's coefficient differs between star sets (NGC 2506
  0.38 with free offsets, 1133 0.74, shipped 0.572); jwst's table misses by 20-30 % at 0.4 of a
  width or more off center.
- *Near a bar.* Refits 0.006 slit units apart move a galaxy's 1.5-2.5 um flux by a median 3.8 %
  (90th percentile 23 %) on p1. The bar offset's zero point is uncertain by about 0.01.
- *Default shape past the shutter edge.* A source planned at |center| >= 0.5 slit units keeps the
  default shape at its planned position, where the transmission changes steeply: `fnu_prof` /
  `fnu_opt` median 0.58 on 6 spurs_gds products (for example 3544, 40-50 % of `fnu_opt`).
- *Default width.* 0.15 slit units; the reported flux of a source below the gate scales with it
  (19-28 % at 0.15 over 0.8-4.5 um), and the population's spread is about +-15 % in flux, not in the
  error.
- *Gratings on the PRISM-fitted kernel.* G140H on NRS2 fits worse; the gratings may sit 0.002-0.005
  higher in bar offset (two observations).
- *Same-row pairs.* Two sources on the same detector rows a short distance apart along the
  dispersion are not separable over much of their spectra; `fnu_contam` is NaN there.
- *A faint source next to a bright neighbor with a wrong shape* is the main risk of the joint solve,
  measured so far only in simulation.
- *Master background.* NRS2 sits high by a constant +5 to +18e-5 DN/s on spurs_gds (also in
  production; cancels in nod differences; touches the master-background spectrum and its 2-D only).
  The +2.5 % in-range offset and the +7 % at the G395M filter cut-on were measured before the median
  fix and are being remeasured. G395M NRS1 empty-shutter scales are bimodal (0.95 / 1.15),
  unexplained.
- *Light from the instrument that nothing models.* In G395M a bright light runs from NRS1 column
  about 450 to NRS2 column 2048 in four strands one shutter pitch apart, brightest up to NRS2 column
  580 (rows 638-646 there). It is the same in all three nods (the nod differences cancel it), and it
  sits at the same detector place, with the same profile, in uds_p2 and in p1: two fields, two MSA
  configurations, two dates. So it follows the instrument, not the sky or the shutters opened. PRISM
  shows a matching six-strand pattern elsewhere (NRS1 columns 1650-2048, rows 643-668), so it is
  dispersed light, not the detector. Through jwst's model it lands like light from quadrant 4 near
  x 20-30, y 35-36, a block the MSA operations reference marks failed closed, with its brightest
  strands on the bars between shutter rows. Light the MSA itself gives off there, such as the glow of
  an electrical short, fits what was measured; the mechanism is not verified. The failed-open
  shutters of the MSA operations reference are in the trace map; this light is not, and the master
  background does not contain it. It enters the master-background spectra of the sources whose
  cutouts it crosses: on uds_p2 38178 at 23 times its PRISM flux, and 39597 at about 20 uJy, where
  its nodded and PRISM spectra are near zero; 39597 is also flagged `MBKGPREF` (`extended`), so its
  master-background spectrum is offered as the likely better one. The same light falls on p1's
  master-background spectra at those rows. A map of light that is the same in every nod and that no
  model explains finds it in both observations, with the fixed slits' light and a few fainter
  traces (design in `scratch/claude/open_shutter_design/design.md`; how to treat it is open).
- *Faint G395M below PRISM.* Between the two dispersers, at faint fluxes, the 3-pixel boxcar
  differs by about -0.005 to -0.014 uJy (G395M lower) in every flux bin, on uds_p2 and on p1, in
  v1 and v2, and with every extraction and both backgrounds; above 0.5 uJy it is invisible. Against
  COSMOS photometry on p1, G395M falls below the catalog in the faintest quartiles and PRISM does
  not (about 2-3 sigma together), but slit losses limit that test. Not traced to a step.
- *Stuck shutters.* Partly open shutters (a = 0.61-0.78) are called "review" and not reported
  otherwise. The 27 G395M shutters flagged for review await the user's verdict.
- *A sidecar from an earlier deploy.* A redeploy whose spec.fits no longer carries a
  master-background spectrum leaves the earlier `_spec_mbkg.json` registered, and the spectrum plot
  still offers it. The redshift-fit and line-fit sidecars have the same gap. Not fixed here: the fix
  is one step in deploy that retires a spectrum's sidecars the new files no longer produce, for
  every kind at once.

**Decisions open with the user:** the default width; the default shape past the shutter edge
(flag, let the center move, or report `fnu_opt`); whether `master_bkg_error_floor` (0.03, the master
background's error floor only) comes back as an option; the stuck-shutter thresholds after the
review packet. Decided: the optics tables' row integration (below) is centered at the next refit
of the optics model, not before (user, 2026-11-08; noted at `optics.row_integrate`).

- *The optics tables sit half a grid step off.* `optics.row_integrate` averages over an even
  number of grid steps (42), so every image table is shifted by +0.0027 slit units (1.25 mas).
  The kernel, the bar offsets, the quadrant pattern and the center law were all fitted with the
  shift in place, so the products are consistent with each other. Centering it means a new
  `ENGINE_VERSION`, rebuilt tables and a refit of the optics model.

---

## 11. Release

The plan (user, 2026-11-06).

**Two PRs.** PR A: pipeline, layout, design docs, Python deploy and the database migrations. PR B:
the web, including the web docs pages. PR B goes live at the switch-over.

Every merge to `main` that touches `web/` rebuilds and deploys the live site (`deploy.yml`), so PR A
carries `main`'s `web/` except a minimal set (user, 2026-11-07):
- the layout mirror (`web/lib/layout.ts`, `layout-products.json`, `layout.test.ts`), which CI's
  golden test compares with `layout/`;
- the server-side retain gate (`web/lib/server/retain.ts`), which knows the new product types;
- the admin stuck-shutter review (`web/app/api/admin/nirspec/stuck-shutters/route.ts`,
  `web/lib/nirspec-shutters.ts` and its test, `web/app/admin/nirspec/nods/[source]/page.tsx`,
  `web/lib/actions/nirspec-nods.ts` and its test) with the types it uses (`web/lib/types.ts`). It
  must go live with the `nirspec_stuck_shutters` table, or the old portal keeps writing the old
  column after the backfill.

Everything else under `web/` waits for PR B: the docs pages and figures, the master-background
spectrum switch, the catalog filters, the metadata page's exposures, and the admin rate view.

Checked in slice 1 (2026-11-07): the migrations only add things (a view and a check constraint are
recreated with new columns and product types; the guards confirm their grants); the nodded
spectrum's sidecars keep their shape, and the master-background spectrum is a new file today's web
never requests. PR A's own `web/` is checked with `npm run typecheck`, `npm test` and
`npm run build` when the branch is cut. The slice's code review (2026-11-07) led to these fixes:
- every spectra row carries the master-background columns, so one unreadable file no longer fails
  its upsert batch on the `NOT NULL` flag;
- `deploy json` updates those columns where the sidecar landed;
- rate-exposure updates are sent in batches of equal columns, so a missing card leaves its column
  as it was;
- `get_program_exposures` lists an observation with no published spectrum only to admins and
  drafts links (new migration, guard extended);
- the `storage_objects` and `spectra` check constraints are added `NOT VALID` and then validated,
  `main`'s shape;
- reverting a background subtraction removes `BKGAUTO` and `BKGWHY` too;
- the web stuck-shutter action can no longer turn a manual row into a web one;
- clearing the last web mark reaches the local list when the database still has the observation's
  manual rows (`--allow-empty` otherwise);
- the deploy snapshot keeps a legacy stuck entry it cannot map.

A bad master-background extension still stops an observation's deploy before anything is uploaded,
as any unexpected error does.

Slice 2 (2026-11-08): v1's stuck detector is removed (below). The read-through and the code review
led to these fixes:
- a shutter holding a secondary source (`background = 'N'`, `primary_source = 'N'`; 304 of the
  archive's 318 metafiles have some) is left out of step 2's empty shutters and counts as a
  source shutter in the stuck-shutter fit, as step 1 already did (`CFMBKGV` 1.8, step 1 1.5);
- step 2 makes all of a group's products together when one is stale, so the pool and the
  failed-open transmissions combined over the nods stay the same for every nod;
- a rate file step 1 cannot process has its earlier tables emptied instead of left for step 2,
  and one without a background gets an empty `MBKG1DR` (step 2 paints it in the calibrated
  range only);
- a root whose step-1 products are current still merges its stored calls into the stuck list,
  read from that root's own rate files;
- the shutter fit's two constants no longer share one header card (`FITCSTS`, `FITCSTA`);
- the failed-open rescale rewrites a product only when a transmission changed.

Checked and left: a failed-open shutter on a commanded-open one would be painted twice, but none of
the 318 metafiles has one; a failed-open shutter measured at zero transmission keeps its footprint
(painted 0, not uncovered), which is correct. The trace map is still built one rate file at a time
after step 1's parallel work.

Slice 3 (2026-11-08), optics and the profile model. The read-through and the code review led to
these fixes:
- a SRCYPOS placeholder (exactly 0 next to real values) was looked for on the position with the
  bar offset added, so with a measured offset it was never found: it entered the prior center and
  could force the default shape. It is now found on the SRCYPOS jwst wrote (`FIT_VERSION` 9);
- an optics-table lock left by a process that died made every later process wait 15 minutes per
  table; it is now taken over, and a process removes only its own lock;
- the bar offset is measured again (`MEASURE_VERSION` 4): its empty shutters now leave out
  secondary sources (slice 2);
- the shape-independent part of the throughput is computed once per exposure instead of at every
  step of the fit;
- `SRCCHROM` (then `PFCHROM`) says whether the shape that was used varies with wavelength (it was always true for a
  held shape);
- the optics calibration keeps the base model's kernel for a detector with no stars (it wrote an
  empty kernel that broke every later run), applies its last clipping to `x_loss` and refuses a
  set of stars that cannot constrain it; the path-loss reference's axes take `CRPIX` into account
  (1 in every reference so far).

Checked and left: a missing path-loss reference fails the product rather than falling back (it is
an environment error); along the dispersion the kernel is applied mirrored relative to the slit
direction, which for a blur taken to be round has no physical meaning (its asymmetry is 0.002 slit
units). The half-step shift of the row integration waits for the next optics refit (section 10).

Slice 4 (2026-11-08), contamination. The read-through and the code review led to these fixes:
- the joint pass weighted its rows past the cutouts as single nod differences, while the cutout's
  rows and the extension solve weight a nod set as a whole (`ProfileModel.diff_variance`). With
  two partners those rows had 1.5 times their weight and their errors came out about 18 % small.
  One helper now gives every nod difference its variance (`JOINT_PASS_VERSION` 5);
- a joint pass 1 was checked against its target's files only. It is now redone when any member's
  canonical or rate file is newer;
- stage 3's second joint pass (sources whose own background exposures differ from the override
  files') wrote over the first pass's file, so every rerun solved both again. It now writes a file
  of its own beside the first, which the products read;
- a source whose pass 1 fails no longer leaves an older pass-1 file for its neighbors to read;
- the light past a cutout is no longer fitted in a group where a frame has no trace map, since
  other sources' light there could not be masked;
- the pixel calibration reads only the cutout's part of the rate file, and the mask past the
  cutout builds each trace map's lookup once;
- the similar-brightness pair flag is removed (section 9);
- smaller: a failed own-shape fit is logged; the clipped-pixel count leaves out pixels that never
  had a usable variance; a trace map's list of a shutter's sources is no longer cut at 64
  characters; the same-slitlet brightness measurement keeps, at each dither position, a frame that
  holds the target.

Checked and left: the error computation's dense matrices (the target's unknowns against every
unknown of the neighborhood) wait for a memory measurement on a crowded field. The floor on a
prediction's uncertainty (`DPRED_FLOOR`, 10 % of the predicted light) is assumed, not measured, and
is an open decision.

Slice 5 (2026-11-08), orchestration. The read-through and the code review led to these fixes:
- the master-background spectrum's `PROF2D`, `CONTAM2D` and `CONTERR2D` were drizzled with the
  nodded crf's masks and weights, its 2-D with the master-background frames' own. At a pixel
  masked in only one of them (a hot pixel, no master background, a bad pixel in a nod partner)
  the profile combined other exposures than the data it weights. They now take the frames' own;
- when stage 2a's single Spec2 call failed, the fallback skipped a source whose canonical from an
  earlier run was on disk, though it was queued to be redone, and did not report the sources the
  call had finished. Its canonicals also kept a one-source metafile name (deleted right after),
  which the same-slitlet check reads;
- the same-slitlet check no longer reads a metafile for a standalone fixed-slit exposure (it has
  none, and stage 2b stopped);
- an override that names only nods missing from a detector's group now stops stage 2b with its
  name, instead of a division by zero;
- the PRISM neighbor scan found earlier runs' grating scans only in `_pf.fits`, which is deleted
  after extraction; it now reads their `_spec.fits` too;
- a grating redone with another grating's shape cross-checked its neighbor scan against that
  grating instead of the PRISM product;
- rewriting the proposed-overrides file dropped the comment (offset, statistic, date) of every
  entry it kept;
- the rectified view for the nods plots is redone when its master background is rewritten, not
  only when one appears or disappears (each view is redone once, to record it);
- smaller: the refit at the chain's shape and the pass-1 lookup are shared by the nodded and
  master-background fits; the combined master-background spectrum takes its profile-fit columns
  only when every group has them, as the nodded one does.

Checked and left: the master-background fit weights its frames without the master background's own
noise; it only sets the spectrum the profile is drawn at, and the reported errors come from the
2-D, which includes it. The drizzle would subtract a background level set by jwst's sky matching;
Spec3 does not run it for MSA spectra, so nothing is subtracted.

Slice 6 (2026-11-08), the web and the Python client (the docs pages and figures are the user's
review). The read-through and the code review led to these fixes:
- the nods overlay drew a reprocessed cell's shutter bands from the marks in the database, which
  never hold the pipeline's automatic calls, so a cell whose shutter the pipeline removed on its own
  had its bands numbered wrong (and a click marked another shutter). The geometry now comes from the
  cell's `STKSHTRS` list, as the cell was made;
- the spectrum plot's master-background switch stayed ticked over the nodded spectrum when the
  master-background file turned out to be missing (a release member not archived yet) or failed to
  load; it now shows off, disabled when the file is missing, and a retry when it failed;
- the rate review kept an earlier view's error after switching to a view that loaded, held its
  decoded images (up to about 100 MB) after leaving the page, and decoded the 2048 rows of a
  master-background image all at once; the error clears, the images are released, the rows are
  decoded 64 at a time;
- a failed exposures query on the program page read as a program without exposures (and was kept
  for 10 minutes); it is now an error, shown in one line;
- `campfire status` counts the master-background files among a NIRSpec observation's intermediates;
- smaller: the exposure-root pattern is shared by the stuck-shutter route and action; the nods page
  computes each root's stuck ordinals once.

The release plan now says when the stuck-shutter backfill runs: right after PR A merges, since the
nods page reads only the new table from then on.

Whole-branch review (2026-11-08). The branch is too large for one `/code-review ultra` (limit 8,000
changed lines), so it was split into six parts, each reviewed as its final code against a local base
branch that holds everything else: 1 data contracts and the web, 2 master background and stuck
shutters, 3 optics and the profile model, 4 contamination and the nod scan, 5 orchestration, 6 the
docs. The bar-shadow tables and the optics table (data) were left out. Part 1 ran as
`/code-review ultra`; the others as `/code-review xhigh`.

Part 1 led to these fixes:
- the web's exposure-root pattern took digits only, but a sequence's activity counts in base 36
  past the ninth (`jw12497004001_0410f`); for such an observation the nods page showed no stuck
  marks and refused every click;
- a redeploy could blank `msa_shutters` on rows whose slitlet did not resolve, and a
  `--supabase-only` deploy wrote the master-background columns' defaults over an unreadable file's
  row (and could blank its thumbnails): a batch upsert sends NULL for any key a row lacks. Rows are
  now sent in batches of the same columns, and an unread file's record carries no
  master-background columns;
- a click on a shutter drawn stuck only by the cell's `STKSHTRS` (the pipeline's own call) marked it
  stuck again and could never veto it; it now sends the veto;
- `deploy json` reads each spec.fits once (it read each three times), which also checks `MBKGWHY`
  before anything is uploaded;
- smaller: the backfill script uses `stuck_list.shutter_key`; the unused `toggleStuckOrdinal` is
  removed.

Kept as it is: a spec.fits that breaks the master-background contract stops the deploy, before any
upload. Noted (section 10): a sidecar left from an earlier deploy.

Part 2 led to these fixes (`CFMBKGV` 1.9, step 1 1.6):
- the master background was redone on every run: its skip key was the newest modification time of
  the canonicals, which stage 2a's `CFEXPGRP` stamp, stage 2b and the S2D views rewrite. The key is
  now the `CFS2ATIM` card stage 2a writes once on each canonical and background-slitlet file, and
  the stamp no longer rewrites a file that already carries the value;
- an exposure whose step-1 table is empty never recorded step 1's date, so it was redone every run;
- a run that stopped after step 1 changed the stuck list, or a hand edit of the list, left
  canonicals made with the old shutters in place (section 4.3 says how they are now remade);
- the geometry pass painted a slitlet without a canonical with its stuck shutters open; they now
  pass no light;
- two sources in one slitlet name the same empty shutter by different offsets, so a shutter could
  count twice and others be dropped; shutters are now told apart by their index in the slitlet;
- a canonical's extended footprint never got the disperser's wavelength cutoffs (a slit of a
  `MultiSlitModel` has no `meta.instrument`), so it ran to the transform's whole range; the cutoffs
  are now passed;
- step 1 left out of the background the shutters the fit called stuck even when a person had vetoed
  the call, and old automatic calls the new fit reverses; it now uses the list as merged;
- step 1 took a virtual slitlet's shutters (source id 0 or below: 8,636 of the 520,082 rows with
  background `N` in the 318 metafiles of the archive) as holding a source, unlike step 2 and the
  fit; they are now empty shutters;
- a trace map that failed to build counted as done and was never retried; it now is;
- when the geometry cache existed without the coverage counter, the workers crashed until the cache
  was cleared by hand; the counter is now written;
- with `CAMPFIRE_ROOT` unset the coverage cache went to `~/campfire-data`, not the layout's
  `~/campfire`;
- when the stuck fit's clipping ran out of rounds, its errors and pixel counts came from another
  pixel set than its amplitudes; it now solves once more on the final set.

Left for later: each background-slitlet file and metafile is opened once per slit rather than once
per rate file; step 1 starts a process pool per root; the shutter-index geometry is written out in
four places (`geometry_on_block`, `RateGeometry.blocks`, `slit_geometry`, and the column mapping
of `shutter_fit` and step 1). The virtual-slitlet difference above was two of those copies
drifting apart.

Part 3 led to these fixes (bar offset `MEASURE_VERSION` 5):
- the quadrant pattern was counted twice in a field whose pixels sit mostly in one quadrant: the
  offset fitted over every quadrant's pixels already holds the pattern in proportion to where the
  pixels sit, and the pattern was then added in full. Its mean over the fitted pixels is now taken
  out first (section 6.4). Re-measured with the fix, the applied offsets of 1133 and the eleven
  NGC 2506 visits move by 0.0001 to 0.0014 slit units (1133: -0.0003), the same in every quadrant.
  That is a tenth of the zero point's 0.01 uncertainty, so the optics fit is not redone;
- G140M and G235M offsets were measured on CRDS's wavelength grid, which stops at 1.495 / 2.49 um,
  so their redder light was dropped; they use the empirical table's grid (to 5.3 um);
- a best trial offset on the edge of the trial range (+-0.10, a half dominated by other light) was
  applied; it now gives no offset;
- `bar_offset.json` was kept after stage 2a made the canonicals again (a new CRDS context moves
  jwst's slit frame); it is now also keyed on their `CFS2ATIM`;
- the path-loss reference was located in the CRDS cache but never fetched, so stage 3 failed where
  stage 2 had not run; it is fetched when missing. Its reader now also checks that the reference
  repeats one profile in every shutter, which `point_y_ratio` assumes for the nods whose source is
  in an end shutter (jwst moves the source one shutter there); `jwst_nirspec_pathloss_0010` does,
  to 0.00 %;
- `refit_at_shape` (the neighbor scan, the master-background spectrum) took the fit's pivot but its
  own wavelength range, and dropped the fit's optics and laws; it takes all of them;
- with `CAMPFIRE_ROOT` unset the optics tables went to `~/campfire-data`; they follow the layout's
  root;
- calibration tools: `stack` takes one disperser and `laws` PRISM only (ranges are pooled by
  index); one range of `kernel_star` that has no usable single-frame pixel no longer discards the
  star's other equations; the repeat check pairs a star within its program and measures a
  grating's fluxes in fixed bands; `crds-compare` compares the bar-shadow tables inside both
  tables' grids; the manifest names its test file.

Left for later: the bar offset holds every pixel's model at each trial offset (41 x the pixels,
float32; over 1 GB for 800 G395M canonicals), to measure before changing; `RangeFit` builds a model
twice.

Part 4 led to these fixes (`JOINT_PASS_VERSION` 6, `JOINT_LIGHT_VERSION` 2):
- a source whose light reached a member only past the member's cutout never joined the
  neighborhood, since the coupling counted cutout pixels only. Its light there was neither modeled
  nor masked, and went mostly into the member's light past its cutout. The coupling now counts each
  source's whole trace, so such sources are members (user, 2026-11-09: dropping or masking pixels
  is not acceptable in the joint solve);
- neighborhoods were cut at 25 members, and a source needed 100 pixels to couple. A source left out
  by either was unmodeled where it overlaps, with nothing to show it; on p1 G395M 360 of the 477
  neighborhoods were at the cap. Both are removed (user, 2026-11-09). The depth stays two steps
  (user, 2026-11-09): on p1 G395M a median 31 members, 90 % 38, at most 46 (three steps: 45, 56, 68;
  the whole disperser couples into two groups of about 240 and 260). PRISM is nearly unchanged
  (median 2, at most 8);
- one solve per coupled group instead of one per neighborhood was considered and is slower. The
  target's errors need its columns of the inverse matrix, about 3,200 solves against the
  factorization, and each costs in proportion to the factorization's size, which grows faster than
  the members (fill per unknown 39, 89, 167, 194 at depths 1-4). Extrapolated, that is 70,000 to
  180,000 CPU seconds on p1 G395M, against about 19,000 for the whole pass now;
- past each end of its cutout, a member's grid was the source row's own samples, starting at the
  first one past its fit pixels. Pixels of other rows and nods between that boundary and the first
  sample had no unknown and were left out. The grids now reach every pixel past the end with the
  member's light (`profile_extend.end_grids`): 100-150 pixels per p1 G395M member, 0.4-0.6 % of its
  light past the cutout. The boundary stays at the fit pixels' range: the spectrum grid runs on
  over the cutout's pixels without a flat field (about 3,700 per member, all but 3 of 11,295 on
  three members without calibration), which the in-range spectrum cannot reach, so they stay the
  light past the cutout's. The same extension guards the profile fit's spectrum grid, which no
  source of p1 needed (0 of 31.6 million fit pixels outside it, PRISM and G395M), so `FIT_VERSION`
  stays 9;
- a member whose light on the target fell below 1 % of the noise was left out of the stored member
  list, so the product predicted it from its own neighborhood's solve. Every solve member is now
  recorded (`solved`) and taken from this solve;
- the first and last rows of a slit past the cutout were about 4 times too tall (the height was
  differenced against the 0 past the slit's end). One helper (`profile_fit.pixel_height`) serves
  both places;
- the neighbor scan's possible-neighbor flag (`NBRFLAG`) and its three levels (`scan_flag_dchi2`,
  `scan_flag_dchi2_low`, `scan_flag_null_ratio`) are removed (user, 2026-11-09): hand-set levels on
  a statistic whose scale any change to the scan shifts. `NBRDET`, the proposals and their figures
  stay, and a person reviews every proposal. The scan's docstring now says what its noise scale
  holds: the nuisance terms' drop stays in it, recorded as `dchi2_nuisance`;
- a nod's NRS1 and NRS2 proposals overwrote each other. A partner is now dirty for the nod when it
  is on either detector, at its larger fraction;
- a neighbor too close to separate cleared the proposals but stayed listed as unresolved;
- memory: the error computation's dense block (the target's unknowns against every unknown) is built
  in blocks of at most 64 MB (the item left in slice 4), and a worker no longer
  loads every exposure's trace map, only a boxcar target's own;
- a boxcar pass 1 with no usable wavelength raised an `IndexError` and failed the phase; it is now
  the named fallback;
- smaller: the pass-1 signature reads the gates from the chain's settings (its PRISM fallback was 50
  where the gate's is 25); the nod difference's tag is built in one place (`profile_contam.nod_tag`);
  a trace map's list of a slitlet's sources is as wide as its longest (it was cut at 256
  characters); the docstrings of `predict` and of `profile_joint` say what the code does.

Left for later: the scan's proposal level (`scan_dchi2`) is still fixed in the configuration; it
could follow each observation's null (the negative-amplitude statistic) instead.

After part 4, three changes make the joint first pass faster without changing its results (user,
2026-11-09). On p1 G395M with 12 workers and 60 members cached per worker, the pass builds 1,059
members where 477 would do, and two thirds of a build was the member's shape (its own fit above
the gate or the gate's check). The shapes are now fixed once per source before the workers start
(`member_shape`), only for the targets whose pass 1 is not current. The design assigned each pixel
by re-sorting the pixels already claimed once per member and looked every member up for every
other member's pixels; one sort now assigns them (`profile_joint.first_occurrence`) and members
whose pixels lie elsewhere are skipped. The target's correlations with the other members took a
second pass over the target's columns of the inverse; the other members' own columns, which the
pass already solves for their variances, hold the same numbers since the matrix is symmetric. On
two depth-2 neighborhoods (31 and 38 members) against the part 4 code: a member build 14-15 s to
4.4 s, the design 5.4-7.7 s to 2.2-2.8 s with an identical matrix, data and noise, the solve
17-28 s to 10-14 s with identical spectra, errors and not-separable flags, and the correlations
within 4e-15 (measured on the 38-member one).

Part 5 led to these fixes:
- the nods plots' rectified view (`S2D_SCI`) rectified the canonical's live frame, which after
  stage 2b is the nod difference: any view made after 2b (a master background written again, or the
  first stage 2a run after the master background came in) showed a nod difference with the master
  background subtracted on top, in the nods plots and in stage 3's review figures. It rectifies the
  frame before the subtraction (`PRE_BKGSUB_*`) once 2b has run; `S2DVER` 2 redoes older views. On
  a p1 canonical rebuilt with the master background off, the view's median is 3.8e-13 MJy against
  the calibrated frame's 4.8e-13 (the rest is the path loss taken out for the view), where the nod
  difference's is 3e-15;
- Spec3 names a fixed-slit product's crf `{product}_{slit}_s{id}_crf.fits`, but stage 3 looked
  only for the MSA name, so every fixed-slit product fell back to the 3-pixel boxcar (`EXTRWHY`
  `no_crf`) and had no master-background spectrum. One helper (`drizzle_map.crf_path`) tries both,
  as the s2d and x1d renaming does;
- the master-background refit and its failed-open and contamination fits used the frames without
  the outlier and hot-pixel flags their own 2-D leaves out; they take the flagged frames;
- the neighbor scan's and the master-background fit's trace maps kept the stuck-closed shutters
  when `contam_predict` was off; the stuck list is always read;
- the neighbor review figure was drawn unguarded after the whole profile-fit phase, so a failing
  figure stopped stage 3; it is now logged;
- a rerun of one disperser removed the source's proposals for its other disperser, since entries
  were kept only for sources not run at all. They are kept per exposure root and source not scanned
  in the run, with the figure, unless this run proposes again;
- the nods each product combines were fixed before the same-slitlet revision reran stage 2b, which
  also applies a hand override edited since; a nod it no longer subtracts is now left out of the
  product and its Spec3 inputs, and one it now subtracts is reported with the command that adds it;
- an exposure whose slits all project off the detector wrote no background-slitlet file, so every
  stage 2a run called Spec2 on it again; the empty file is now written, as when there is nothing to
  extract;
- a master-background 2-D or profile fit left by an interrupted run could be attached to a later
  product; a product's master-background files are cleared before its Spec3;
- smaller: one reader of the proposed-overrides file (`stage2.proposal_entries`, it was parsed in
  two places, one leaving the file open); the 1-D-combined master-background spectrum shares the
  2-D read and the extensions with the others; removed code no flow ran (`run_profile_fit_single_source`
  and the v1 `profile_fit.run_single_source`, `_prism_scan_agrees`, `FALLBACK_NO_MBKG`).

Checked and left: `fix_units` was said to rewrite every canonical on each stage 2a run, which would
make every pass 1 stale; a canonical it has fixed keeps its bytes and modification time. The
relabel of a background reason after the revision does rewrite the canonicals, so the next run
recomputes the pass 1 of their neighborhoods once.

Left for later: the caches keyed on modification times (pass 1, the joint light) could key on
content instead, as the master background does with `CFS2ATIM`; the master background is read once
per canonical rather than once per exposure, to measure before changing. A source seen in a single
exposure (no background exposure) gets no product at all, so `MBKGWHY` `single_exposure` never
applies. Such a source could get a product from the master background alone: Spec3 on its
calibrated frames for the grid and pixel maps, the profile fit on the frames less their master
background, `SPEC1D` from those, and single-frame rows in its neighbors' joint solves, where its light
is not modeled now. None of the data at hand has such a source (p1, spurs_gds, uncover2, the 1133
visit, and by file name the 4,290 sources of the 33 archive observations with canonicals on disk),
so it waits for an observation that needs it (user, 2026-11-09). They would come from nods lost to
stuck-closed shutters or the detector gap, an observation without nods, or a hand override that
leaves every nod out.

**Phase 0, freeze.** Done. `origin/main` merged 2026-11-06. Failing tests that fail on `main` alone
(NIRCam `test_align_apply`, `test_linefit`'s program-slug check against the local `programs.toml`,
`test_dr_publish::test_cli_json_prompts_stay_off_stdout`) are fixed on `main`, not here. Still to do
in slice 1: rebuild the database from the merged migration chain and run the drift check and SQL
guards.

**Phase 1, cleanup.** Done: the configuration audit (section 9), the neighbor scan made
proposal-only with its review figure, the master-background prototype script removed, the
`local_contrast` warning fix, one CHANGELOG entry, this document rewritten, the docs branch merged.

**Phase 2, review in slices** (read-through, `/code-review` at high effort and fixes by the extraction
session; then a review of the whole branch in six parts, user 2026-11-08):
1. data contracts: FITS products and cards, rate-file extensions, layout kinds, migrations, deploy
   and sidecars;
2. master background and stuck shutters (`master_bkg`, `master_bkg_rate`, `coverage`,
   `barshadow_calibration`, `shutter_fit`, `stuck_list`); v1's stuck detector
   (`stuck_shutters.detect_stuck_shutters`, `merge_stuck_shutters`, `write_stuck_shutters_toml`,
   `plots.plot_stuck_shutter_diagnostics`) had no caller outside tests and is removed: the legacy
   tag reader moved to `stuck_list.legacy_tags`, the shutter-row helper of the nods plots to
   `plots.py`;
3. optics and the profile model (`optics`, `profile_fit`, `profile_chain`, `crds_refs`, `bar_offset`,
   `optics_calibration`);
4. contamination (`trace_overlap`, `profile_contam`, `joint_first_pass`, `profile_extend`,
   `slitlet_neighbours`);
5. orchestration (`stage2`, `stage3`, `profile_product`, `mbkg_extraction`, `drizzle_map`);
6. web and the Python client.

**Phase 3, science validation on the release candidate**, criteria written before the runs: p1,
uncover2, spurs_gds, an H-grating observation, a fixed-slit observation (go2285), a crowded G395M
field, one with many stuck shutters. Against production: delivered S/N, flux against aperture
photometry, redshifts and line fluxes, fallback and contamination-flag rates, runtime and memory per
exposure; the plots of section 10.

**Phase 4, release and reprocessing.**
1. DR 2026.1: freeze the current (v1) catalog as a data release (`campfire dr freeze`) before
   anything of v2 is deployed, with the v1 documentation preserved for it (open: in what form).
2. Merge PR A after review and tag `pipeline-vN.0.0` (MAJOR).
3. Reprocess the archive from stage 1 with the tag and `campfire push` (bytes only). Stage 2a is
   the minimum for most observations (step 1 writes into the existing rate file); G140M/F100LP,
   G235M/F170LP and the H gratings with those filters need stage 1 for the extended mask. The
   whole archive is rerun from stage 1 so that every rate file is made the same way.
4. Fixes during reprocessing: small PRs to `main`; PATCH when science output is unchanged, MINOR when
   it changes; only the affected observations rerun.
5. Switch-over in one step: `campfire deploy` over everything, and PR B merged the same day.

**Other:** run `scripts/backfill_stuck_shutters_physical.py` right after PR A merges (dry run first;
user). From then on the nods page reads only `nirspec_stuck_shutters`, so until the backfill runs, the
marks made with the old ordinal buttons do not show there.

---

## 12. Implementation map

| Area | Files (under `pipeline/campfire_pipeline/` unless noted) |
|---|---|
| Geometry, cutoffs, coverage counter | `nirspec/coverage.py` |
| Master background step 1, step 2 | `nirspec/master_bkg_rate.py`, `nirspec/master_bkg.py` |
| Bar-shadow tables | `nirspec/barshadow_calibration.py`, `nirspec/barshadow_qa.py`, `data/Generate_Barshadow_Cals.py`, `data/empirical_jwst_nirspec_barshadow_*.fits` |
| Stuck shutters | `nirspec/shutter_fit.py`, `nirspec/stuck_list.py` |
| Trace maps | `nirspec/trace_overlap.py` |
| Same-slitlet neighbors | `nirspec/slitlet_neighbours.py`, `nirspec/stage2.py` |
| Source model, shape chain, products | `nirspec/profile_fit.py`, `nirspec/profile_chain.py`, `nirspec/profile_product.py` |
| Optics model and its calibration | `nirspec/optics.py`, `data/nirspec_optics.json`, `nirspec/optics_calibration.py`, `scripts/nirspec_optics_checks.py` (under `pipeline/`) |
| Bar offset | `nirspec/bar_offset.py` |
| Joint first pass, other slitlets' light | `nirspec/joint_first_pass.py`, `nirspec/profile_joint.py`, `nirspec/profile_extend.py`, `nirspec/profile_contam.py` |
| Neighbor scan | `nirspec/nod_scan.py`, `nirspec/plots.py` (`plot_neighbour_proposal`) |
| Master-background spectrum | `nirspec/mbkg_extraction.py`, `nirspec/failed_open.py`, `metadata/science_hash.py` |
| Drizzle once | `nirspec/drizzle_map.py` |
| Boundary with jwst | `nirspec/crds_refs.py`, `data/nirspec_calibrations.toml` |
| Orchestration | `nirspec/stage2.py`, `nirspec/stage3.py` |
| Config | `data/config_default.toml`, `config.py` (`REMOVED_OPTIONS`) |
| Stuck list storage | `supabase/schemas/tables.sql` (`nirspec_stuck_shutters`), `web/lib/nirspec-shutters.ts`, `python/campfire/deploy/nirspec_flags.py` |

Running the pipeline on the reduction machine: stage 1 at most 6 workers, stage 2a-sized work up to
12 when nothing else heavy runs, one heavy chain at a time; isolated roots set `CRDS_PATH`
explicitly.

---

## Appendix A. Built or tried, and not shipped

Each item says what it was, what showed it was wrong or unnecessary, and what replaced it. The code
of each is in git history.

**Point-source and shutter models before the optics model.**
- *A two-Gaussian PSF from stars* (October; lower-quartile core width, "err on the side of a
  narrower PSF"). Fitted on nod differences built with the path loss still applied; with the
  profile model's own frames the stars did not prefer the narrow-side table, and a refit found the
  nods misplaced (which became the nod offset law).
- *A three-component smooth PSF* (`FIT_VERSION` 4, 2026-11-02/03). One PSF per detector,
  seven parameters as splines in ln(lambda), fitted jointly to the stars; under 1 % of the peak in
  11-13 of 14 ranges. It raised bright fluxes by about 2 % (light 2.5-4 rows out). A
  self-consistent second round moved the wing/shoulder split by up to 2 % of the peak on NRS2
  without a better fit. Replaced by the optics model: the split did not matter, the part before the
  shutter and the shutter edge did.
- *The hard shutter edge* (+-0.50 slit units, blur split evenly). Its uniform-light image stayed
  0.435-0.439 pitch wide at every wavelength against a measured 0.429 (PRISM 0.6 um) to 0.388
  (5 um): too much light near the edges in the red.
- *A measured, narrower edge* per disperser. It fixed uniform light but made edge stars worse
  (median chi2 1.015-1.051 of the hard edge's): uniform light and point sources disagreed about
  where the edge is.
- *A geometric shutter with a smooth transmission* T = 1 - (1 - tedge) x^8, capped at 1
  (`FIT_VERSION` 5). It fitted the bar shadow (median misfit 0.011-0.018) with edge stars about
  neutral. Replaced by the optics model, which explains the same loss physically (light diffracted
  at the edges and lost at the stop) and also fixes the same-star test (18 % -> 7-12 % scatter).
- *Diffraction choices*: a slot pupil (alpha 0) puts twice the stars' wide light in the red; a
  round-pupil chord (alpha 0.5) fits the bar shadow worse; a softer stop fits the bar shadow better
  but needs a blur the stars do not allow (held-out star stack 4 of 14 ranges). Alpha 0.25 with a
  hard stop shipped.
- *Kernel variants*: one fitted with the production center law took up the law's errors as a
  shoulder; one from faint stars only lost the wide wing; a kernel per observation was not needed
  once the core's broadening was traced to brightness (charge spreading in bright pixels) rather
  than epoch.
- *Weighting each star by its pixels.* One 1133 star (60 exposures) weighed as much as 20 NGC 2506
  stars, and eight faint 1133 stars with a neighbor in the slitlet set the wing. Each star is now
  weighted once.
- *A diffraction model across the shutter* (along the dispersion) fitted the whole NGC 2506 sample
  best but predicted the other half of the stars worse than jwst's table. jwst's x dependence with
  the empirical correction shipped instead.
- *Fitting a shift and widening of the openings to 1133's edge stars.* Most of the one-sided
  residual was the dispersion direction leaking in through 1133's diagonal steps; with one level
  per configuration the dependence along the slit is flat to 1-3 % within 0.35 pitch. No edge is
  moved.
- *A second galaxy component* (gated two-Gaussian shape), rejected by the user (2026-10-24).
- *Galaxy shapes from imaging*: "never reliable" (user, 2026-10-22).

**The first pass and contamination.**
- *A masked first pass* (each neighbor fitted alone with every pixel another source lights masked)
  and its variants (*pass 1b*, *the echo correction*, pass-1 smoothing, *a gated group joint solve*
  of up to 6 members). The mask is circular: the masked pixels are those the prediction needs. Replaced by the
  joint first pass; about 2,800 lines removed, products identical to rounding.
- *A first pass on single frames less the master background.* A single frame holds each source once
  and positive, which looked easier. But a residual in the master background is taken up as source
  light: faint isolated sources sat 0.15-0.2 uJy above PRISM, shape fits ended on a bound for 81 % of
  sources, and no master-background version (1.2-1.4) brought single frames to the nod differences.
  The per-shutter background that repeats between nods is removed only by the nod difference.
- *Reporting the target's own joint-solve spectrum* as its answer. Identical to the subtracted
  answer when the neighbor comes from the same solve, but noisier and with errors too small (median
  rms against PRISM 2.22 against 1.44 for `fnu_prof`).
- *Subtracting the prediction in `fnu`* above 3 sigma (`contam_1d = "subtract"`). The user decided
  `fnu` is never subtracted (2026-10-30): joint modeling of overlapping spectra is reported for users
  to evaluate.
- *Iterating per source instead of solving jointly*: equal to the joint answer where it converges
  (4 of 8 neighborhoods in simulation), stalled 2.5-14 sigma away where sources are tightly coupled;
  iterating on own pixels only diverged.
- *Per-exposure pointing offsets* (prior 0.015, cap 0.1). In crowded G395M 12 % sat at the cap,
  absorbing neighbors' light. Removed (2026-11-01); it cost about 1.5 % more bias of the default shape
  on isolated injections.
- *"Unpredicted light"* (light on a source's pixels no cataloged trace explains). Its causes were
  trace-map bugs (a slitlet mislabeled at a dither point, stuck shutters in maps) and traces not
  followed past the range. A contaminant without a spectrum is now an error.
- *Ending the extension grids where the count-rate background falls below a few percent.* Withdrawn:
  real sources can be much brighter than the background.
- *Predicting a grating neighbor from its PRISM spectrum*: careful modeling, out of scope for the
  archive (user, 2026-10-30).
- *Smoothness priors on other members*: would push a contaminant's lines into the target.
- *Masking and dominance rules* in the shape chain (`contam_mode`, `contam_dominance*`): removed in the
  audit; the ratio is computed but not written.
- *The jwst-built trace maps and the `tracemaps/` folder*: replaced by the maps step 1 stores. On p1
  the stored maps equal the last `tracemaps/` copies in all 12 exposures, and they fix stuck source
  shutters that the jwst build kept lit.

**Same-slitlet neighbors.**
- *Automatic background choice from estimated light* (`partner_choice.py`, from the
  master-background products). It helped where a neighbor's image was strong but failed where the
  own frame shared the light or the light was not a source. Superseded by the rule that overrides
  are only for neighbors in the same slitlet (user, 2026-11-01).
- *A rule by shutter* (a background exposure is contaminated when the neighbor is in the source's
  shutter). Wrong test: pairs one nod
  step apart often put the neighbor 0.5-0.65 slit units away in every exposure, and others land
  exactly on the source. Replaced by the rule by position from `SOURCE_INFO`.
- *Keeping only the farther background exposure* when both are contaminated. Worse against
  PRISM in all 5 sources tried: one background exposure costs more noise than the bias it removes.
- *The data measure of the neighbor's brightness as final.* It counts other slitlets' light; the
  model measure replaced it after the joint pass. Leaving out other slitlets' pixels from the data
  measure left too many neighbors unmeasured.
- *The scan applying its own proposals* (`scan_action = "auto_override"`): never applied, review only
  (user, 2026-11-06).

**Master background and stuck shutters.**
- *Per-slitlet scale* ("that's not master background at that point", user, 2026-10-30) and *smooth
  row/column scaling* of the master background: on G395M neither predicts the shutter-specific
  scatter and both add noise.
- *Smoothing the per-exposure grating background*: not approved; some master-background noise is
  expected.
- *A per-exposure background-difference term in the nodded products*: the master background is 3-5 %
  noisier than the nodded background and would add noise everywhere.
- *Dividing science frames by a bar shadow*: never; the bar shadow enters through the optics model.
- *Reusing step 1's shutter selection in step 2*: the user chose reselection in calibrated units.
- *The `low_frac` stuck-shutter detector*: fails on the gratings, where other traces fill a dark
  shutter's rows.
- *Source x ordinal keys for stuck shutters*: physical shutter keys everywhere instead.
- *`_mbkgr.fits`* (step 1's tables in their own file): the tables moved into the rate file to keep the
  number of file types small.
- *`pipeline/experiments/master_bkg_prototype`*: the feasibility script, replaced by `master_bkg.py`.
- *A fourth `MBKGWHY` reason* for another slitlet's light entering only through the background
  exposures (p1 629466): the contamination model handles it.

**Calibration data.**
- *The archive's repeated galaxies* to calibrate the loss along the dispersion: repeat visits are often
  at other position angles, so an extended source crosses the shutter differently.
- *Holding calibration data out* (1133, or half the stars) as a test: calibrations are fitted on all
  data and validated per dataset (user, 2026-11-05).

---

## Appendix B. Decisions

The user's decisions that shaped what ships, by date.

- **2026-10-16** PSF from calibration stars, on the narrow side. (Superseded by the optics model.)
- **2026-10-17** The galaxy is one Gaussian.
- **2026-10-22** Gratings take the PRISM shape; imaging-based shapes never. Success is delivered S/N,
  with line width and centroid. Nothing that degrades the wavelength solution. Handle the obvious
  contamination; dropping nods is too extreme as a default.
- **2026-10-24** The instrument model is fixed, never fitted. The flux convention goes in the docs,
  not the header. No "pf" or "stage3" in user-facing names. The merge is a MAJOR release.
- **2026-10-29** Profile fit on by default. Columns `fnu` (best), `fnu_opt`, the boxcars, `fnu_prof`,
  `fnu_contam` with its own error. `_pf.fits` deleted; `PROF2D` and `CONTAM2D` kept. The master
  background is the last part of stage 2a, per exposure x detector with the group's combined
  background as fallback,
  never used to correct the nodded spectra; no background-difference term. Science frames are never
  divided by a bar shadow; the measured table is used where one exists. Traces fully extended. A
  contaminant without a spectrum is an error. Master-background spectra as a secondary product. With
  `--source-ids`, neighborhoods form from all sources but only the requested products are written.
- **2026-10-30** Master background split into step 1 and step 2; step 2 reselects in calibrated units.
  No masking; background modeled past both ends. Per-slitlet scale off. The template fit replaces the
  stuck detector, every disperser; the call rule and precedence manual > web > auto; physical shutter
  keys and the stuck-list format. The joint local solve replaces the mask. `fnu` is not
  contamination-subtracted; `fnu_contam` NaN where not separable. A neighbor's grating light from its
  PRISM spectrum is out of scope. Nothing computed twice.
- **2026-10-31** The joint solve covers the full range, light past the cutouts included. Build the
  joint pass on nod differences.
- **2026-11-01** No per-exposure pointing offsets. Overrides are only for neighbors in the same
  slitlet; other slitlets' light is the joint model's, reported. The rule by position with
  `SOURCE_INFO`, threshold 0.02 of the background, R 0.75; when every background exposure is contaminated
  keep them all and flag. Neighbor brightness from the joint model. "Background", not "sky". The
  `MBKGPREF` rule and its reasons; 2-D in the FITS; exposure counts.
- **2026-11-02** No smooth or per-slitlet master-background scaling on any disperser. Failed-open
  shutters modeled at their measured transmission; the master-background spectrum keeps their pixels.
  The hot-pixel threshold stays at 5 sigma. Each nod at its planned position (no free parameter).
  `drizzle-once` merged.
- **2026-11-03** Adopted and then replaced the smooth PSF and the shutter transmission (appendix A);
  removed the old PSF code. The optics model approved (`FIT_VERSION` 6). G140H / G235H / G395H use
  the M grating with the same filter.
- **2026-11-04** One optics model for every disperser. The reported flux is the light in 0.8 arcsec;
  no second column. `fnu_prof_err` is statistical only (no shape, kernel, law or x-loss term). The
  archive's galaxies are not used to calibrate the loss along the dispersion.
- **2026-11-05** Calibrations are fitted on all calibration data, 1133 included; nothing held out.
- **2026-11-06** Release plan (two PRs, phases 0-4). An own fit on a bound is kept only when the
  center alone is on its bound. G140M / G235M geometry and stage 1's mask always to 5.3 um. Every
  observation rerun from stage 1; `tracemaps/` retired. Configuration audit: the opt-in paths not
  chosen are removed. The neighbor scan's proposals are never applied. The grating-to-grating shape
  transfer built.
- **2026-11-07** The neighbor review figure laid out like the bkgsub plots, spectrum unbinned. The
  master-background prototype dropped. DR 2026.1 is the v1 freeze. PR A carries only a minimal set
  of `web/` changes (section 11).

---

## Appendix C. Errors found and corrected

Kept because each one changed a conclusion recorded at the time.

- **A PRISM comparison biased toward the subtraction** (2026-10-31). The rms of `fnu_prof -
  fnu_contam` against PRISM was computed with the prediction's error added in quadrature, so a noisy
  prediction enlarged its own error bar. With `fnu_prof_err` alone subtraction helped in 52 of 131
  sources, not most. Every comparison since uses the spectrum's own error.
- **The bar-shadow derivation's edge rows** were 2 to 10 times too bright: the derivation never
  admitted those pixels, and a clipped mean kept only the bright tail. Fixed with a median per cell.
- **Master-background weights and the bar shadow applied twice** made the G395M model +2.6 % low
  on held-back shutters; fixed in 1.3 (equal weights, bar shadow once).
- **`sky_for_slit` fell back silently** to the slit's own slitlet when a products folder had no rate
  files (40 % of the background). It now warns.
- **The one-sided shutter edge** (2026-11-05). The optics model drew the open shutters on its grid
  with a strict inequality; floating-point rounding opened some edges one grid step too far, and the
  tables were cached under a key that did not change with the mask code. Edges are now exact
  (`FIT_VERSION` 8, engine 2) and the cache key includes `ENGINE_VERSION`.
- **1133's nods ran the other way from its metafile.** The fluxes came out negative and step 1 called
  11-85 of 102 shutters stuck. With dither points 2 and 3 swapped in the scratch metafile, 2 shutters
  are called and the model matches the data row by row. Any program whose nods go the other way
  from its metafile would fail the same way; the YOFFSET sign of dither point 2 against the metafile
  shows it.
- **The optimal extraction's 3-pixel fallback** (`CMPFROPT = 'boxcar-3px'`) reported about a third of
  the flux and understated the S/N by about the square root of 3; `extract_with_profile` now
  normalizes the profile.
- **The same-slitlet decision flipped from run to run** (p1 922323): the first pass fitted members on
  the previous run's choice. Now the decision is made on the override files' nod differences.
- **Spec3 ran before the same-slitlet revision**, so a revised source's s2d and `EXPOSURES` kept the
  old choice. Spec3 now runs after it.
- **The extension's red end was never solved** for spectra across the detector gap (the grid came from
  the first exposure's row, NRS1). Fixed.
- **`boxcar_factor`** (a neighbor's prediction from its 3-pixel boxcar) divided by the model's point
  transmission where the s2d holds jwst's; it now uses the model frames in the s2d's units.
- **Exposure sequences past 9** (`0410b`, ...) were not found, so later sequences got no master
  background and no contamination keys.
- **The profile fit's row height** was undefined on a slit's first and last rows, which slitlets longer
  than three shutters reach.
