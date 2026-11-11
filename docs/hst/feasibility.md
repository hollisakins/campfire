# HST imaging reduction for CAMPFIRE: survey of practice (Stage 1)

Date: 2026-11-11. Scope: ACS/WFC and WFC3/IR imaging, mosaics built from calibrated exposures onto the CAMPFIRE NIRCam grids.

## How to read this document

Evidence tags on substantive claims:

- **[V]** verified: the cited source was read in this session. Most reading was done by four research sub-agents working from briefs I wrote; I read their full notes. Upstream code was read from shallow clones made today. Handbook pages on `hst-docs.stsci.edu` were read through a fetch tool that summarizes pages, so a single number taken from a handbook page should be re-opened before it is hard-coded.
- **[V†]** verified, and I re-checked it myself against the cloned source or the local environment.
- **[I]** inferred: from memory, a secondary source, or reasoning from verified facts. All starting leads in the brief count as inferred unless tagged otherwise.
- **[U]** unknown: looked for and not established.

Sources are cited by short key; section 6 lists each key with its URL and access status. `UP/` means the scratch directory of upstream clones (not in the repo).

Upstream versions as of today **[V]** (`git log`, tags, PyPI):

| Package | Latest release | Role |
| --- | --- | --- |
| `hstcal` | 3.3.0 (tag 2026-11-04); calacs 10.5.0, calwf3 3.8.0 | C calibration programs |
| `drizzlepac` | 3.11.0 (2026-04-28) | AstroDrizzle, TweakReg, archive mosaics |
| `stwcs` | 1.7.7 (2026-03-05) | HST WCS and distortion handling |
| `drizzle` | 3.0.0 (2026-09-23); the `campfire` env has 2.1.1 | resampling core used by `jwst` |
| `acstools`, `wfc3tools` | 3.8.2 (2026-06), 1.6.1 (2026-02) | instrument utilities, wrappers |
| `grizli` | tag 1.14.3 (2026-10-22), last commit 2026-10-26 | community pipeline |

All were committed to within the last three weeks.

---

## a. The archive standard

### a.1 Calibration: calacs and calwf3

The HST counterpart of the `jwst` package's detector-level stages is **HSTCAL**, a set of C programs: `calacs` for ACS and `calwf3` for WFC3. The Python packages `acstools` and `wfc3tools` are thin wrappers that call the executables **[V]** (`UP/hstcal/README.md`; `UP/acstools/acstools/calacs.py`). MAST runs these and serves the results.

**ACS/WFC (a CCD).** calacs, in order: data-quality initialization, bias subtraction, conversion from DN to electrons, overscan level and (for post-2009 data) bias-shift, crosstalk and stripe removal, saturation flagging, error-array initialization, sink-pixel flagging, **pixel-based CTE correction** (`PCTECORR`), cosmic-ray combination of CR-SPLIT pairs (`CRCORR`), dark subtraction, post-flash subtraction, flat-fielding, and writing of photometry keywords (`PHOTCORR`, headers only) **[V]** (ACS DHB §3.4; `UP/hstcal/pkg/acs/lib/acsccd/doccd.c`).

**WFC3/IR (a HgCdTe array read non-destructively, like NIRCam).** calwf3, in order: data-quality initialization, zero-read signal estimate, reference-pixel subtraction, zero-read subtraction, error initialization, non-linearity correction, dark subtraction, photometry keywords, conversion to count rate, **up-the-ramp fit with cosmic-ray rejection** (`CRCORR`, 4 sigma), flat-fielding with gain **[V]** (WFC3 DHB §3.3; `UP/hstcal/pkg/wfc3/lib/wf3ir/doir.c`).

Products **[V]** (ACS DHB Table 2.1; WFC3 DHB Table 2.1, §2.2):

| Suffix | ACS/WFC | WFC3/IR |
| --- | --- | --- |
| `_raw` | DN | DN, all reads |
| `_ima` | does not exist | every read after per-read calibration, e-/s |
| `_flt` | calibrated exposure, **electrons** | calibrated ramp-fit image, **e-/s** |
| `_flc` | FLT plus pixel-based CTE correction, electrons | does not exist |
| `_crj` / `_crc` | CR-SPLIT combination | repeat-observation combination |
| `_drz` / `_drc` | drizzled product, e-/s (`drc` from FLC) | `_drz` only, e-/s |
| `_asn` | association table (which exposures form a dither set) | same |

- **FLT versus FLC.** Identical except that FLC has had charge-transfer losses redistributed back along the columns (section c). FLC is the correct ACS/WFC starting point; WFC3/IR has no CTE and no FLC.
- **Layout.** An ACS/WFC file holds two 4096x2048 chips: `[SCI,1]` is chip WFC2 and `[SCI,2]` is chip WFC1, each with its own `ERR` and `DQ`, followed by distortion lookup tables and stored WCS solutions. A WFC3/IR FLT holds one 1014x1014 image with `SCI`, `ERR`, `DQ`, `SAMP` (reads used per pixel) and `TIME` (effective exposure per pixel) **[V]**.
- **Cosmic rays.** The WFC3/IR FLT has cosmic rays removed by the ramp fit, but its DQ array carries no cosmic-ray flag; those exist only in the IMA **[V]** (WFC3 DHB §3.3). An ACS/WFC FLC still contains every cosmic ray, about 1.5 to 3 percent of pixels per 1000 s **[V]** (ACS DHB §4.3); calacs rejects them only for CR-SPLIT pairs at one pointing, never across dithers **[V]** (ACS DHB §3.2).

### a.2 Reference files

HST uses the same CRDS system as JWST, on a separate server (`https://hst-crds.stsci.edu`) with its own context series; the latest is `hst_1372.pmap`, dated 2026-11-09 **[V]** (HST CRDS). The mechanism differs from JWST in one important way. For JWST, each pipeline step asks CRDS for its reference file at run time under `CRDS_CONTEXT`. For HST, `crds bestrefs` writes reference-file *names* into the RAW header, and calacs/calwf3 then find those files through the `jref` and `iref` environment variables; the calibration code never talks to CRDS **[V]** (CRDS user guide; ACS DHB §3.5).

Consequences:

- MAST keeps calibrated products and reprocesses them when reference files or software change, so what a download contains depends on when it was made **[V]** (Cycle 34 Primer). ACS darks, biases and sink-pixel files are delivered after every monthly anneal, and STScI advises waiting a few weeks after an observation for the right ones **[V]** (ACS DHB §3.1).
- No CRDS context keyword was found in HST product headers or in the open-source code; the record of calibration is `CAL_VER` plus the reference-file name keywords **[V for the code search; U for whether MAST adds one]**.
- To pin calibration exactly, one starts from RAW and runs `crds bestrefs --new-context hst_NNNN.pmap` followed by calacs or calwf3 at a fixed hstcal version **[V]** (CRDS user guide). If we start from archive FLT/FLC, reproducibility rests on keeping the files and recording `CAL_VER`, the reference-file keywords and the retrieval date.

### a.3 Combination: AstroDrizzle

`drizzlepac.astrodrizzle` is the combination stage. Its steps **[V†]** (DrizzlePac Handbook §5.2; `UP/drizzlepac/drizzlepac/pars/astrodrizzle.cfg`):

1. Static mask of persistently low pixels.
2. Sky estimate per exposure (default `localmin`: the lowest chip sky is used for the exposure).
3. Drizzle each exposure separately onto a common frame.
4. Median of those, by default `minmed`: where only two or three valid values exist and one stands out, the minimum is used in place of the median.
5. Blot the median back to each exposure's distorted frame.
6. `driz_cr`: flag pixels where `|data - blot| > scale * |gradient| + SNR * noise`, in two passes (defaults SNR `3.5 3.0`, scale `1.2 0.7`). Flags go into the exposure DQ as bit 4096.
7. Final drizzle with all masks.

Weighting choices for the final drizzle **[V]** (`UP/drizzlepac/drizzlepac/astrodrizzle.py`, `imageObject.py`; Handbook §3.3):

| `final_wht_type` | Input weight | Output WHT means |
| --- | --- | --- |
| `EXP` (default, used by the archive) | exposure time | exposure-time map |
| `IVM` | `flat^2 / (dark + sky*flat + readnoise^2)`, or a user map | inverse variance of the background, no source term |
| `ERR` | `(exptime / ERR)^2` | inverse variance including source noise; biases sources faint by 1 to 2 percent |

AstroDrizzle writes `SCI`, `WHT` and `CTX` (context), and no error extension.

What the archive drizzle uses: native pixel scale (0.050 arcsec ACS/WFC, 0.128 arcsec WFC3/IR), `pixfrac` 1, square kernel, detector orientation, `EXP` weights, one association at a time **[V]** (WFC3 DHB §4.2; ACS ISR 2022-03 Table 9). The parameters come from a reference table (`MDRIZTAB`) by filter and number of inputs.

### a.4 Hubble Advanced Products

The **Hubble Advanced Products (HAP)** are a second set of archive mosaics, made by code inside drizzlepac **[V]** (drizzlepac docs, HAP pages; ACS ISR 2022-03):

| | Single-Visit Mosaic (SVM) | Multi-Visit Mosaic (MVM) |
| --- | --- | --- |
| Contents | all exposures of one visit, all filters, one detector | every public exposure in a 0.2 degree sky cell, per detector and filter |
| Since | December 2020 | April or May 2022 (sources give both) |
| Alignment | filters aligned to each other, then one fit to Gaia eDR3, else GSC 2.4.2, else 2MASS | none; uses each visit's SVM solution as is |
| Grid | native scale, north up | 0.04 arcsec for all; 0.12 arcsec also for WFC3/IR; fixed all-sky tessellation |
| Cosmic rays | full AstroDrizzle sequence | none; trusts earlier flags |

STScI's own statements on MVMs: they "may have photometric errors of several percent or systematic alignment errors when combining visits with different catalog solutions", some visits in a sky cell "have global offsets of several pixels", time-dependent sensitivity is not accounted for, and they are recommended "as discovery images ... and not for precise photometry" **[V]** (ACS ISR 2022-03 abstract, §4.2). Whether MAST is still regenerating HAP products is unclear: the code is still being changed (3.11.0, 2026), but the 3.9.0 changelog (December 2024) says that build "does not generate HAP products" **[V for both; current status U]**.

### a.5 Astrometry in the archive

HST pointing was historically limited by guide-star catalog errors. Header astrometry of the original files is wrong by 1 to 2 arcsec for data before October 2005 (GSC 1), about 0.3 arcsec for 2005 to 2017 (GSC 2.3), and 0.1 to 0.2 arcsec after (GSC 2.4, Gaia-based) **[V]** (DrizzlePac Handbook §4.5; ACS ISR 2022-03 §1; sources differ on the last figure between 0.1 and 0.5 arcsec).

Since December 2019 MAST attaches improved solutions to every FLT/FLC **[V]** (ACS ISR 2022-03 §3; drizzlepac `astrometry.rst`):

- **A priori**: guide-star positions recomputed in the Gaia-based catalog, no image fit. Name ends `-GSC240` or `-HSC30`.
- **A posteriori**: sources in the image fitted to a catalog. Name ends `-FIT_REL_<cat>` (same-filter exposures of a visit), `-FIT_SVM_<cat>` (all filters of a visit), or `-FIT_IMG_<cat>`; `<cat>` is `GAIAeDR3`, `GSC242` or `2MASS`.
- The solution in use is named in the `WCSNAME` keyword; all candidates are stored in the file as **headerlets** (small extensions each holding a complete WCS), and `stwcs` can switch between them.
- Proper motions are applied to the reference catalog at the observation epoch **[V]** (`UP/drizzlepac/drizzlepac/haputils/astrometric_utils.py`).

How often it works **[V]** (ACS ISR 2022-03 Tables 1 and 4, data to April 2022):

| | ACS/WFC | WFC3/IR |
| --- | --- | --- |
| Fitted to Gaia eDR3 | 79% | 58% |
| Fitted to GSC 2.4.2 | 7% | 10% (28% in SVM) |
| No fit (a priori only) | 10% | 31% |
| Median fit RMS, Gaia | 16 mas | 24 mas |
| Median fit RMS, GSC 2.4.2 | 38 mas | 111 mas |

In a sparse extragalactic field there are few Gaia stars inside a 2 to 3.4 arcmin field of view, so these fields fall disproportionately into the weaker classes **[I]**. The archive never fits one visit against another or against a deep reference image **[V by absence in the documentation]**.

### a.6 How far to trust each level

| Level | Use as our starting point? | Why |
| --- | --- | --- |
| ACS/WFC **FLC** | Yes | Per-exposure calibrated pixels, error, DQ, distortion in the header. We must redo astrometry, cosmic rays, sky, and add masks for trails and scattered light. |
| WFC3/IR **FLT** | Yes, with a known exception | Same, but exposures with time-variable background are not usable as delivered (section c.2); repairing them needs the IMA or RAW file and calwf3. |
| Pipeline DRZ/DRC | No | "Should generally be regarded as quick-look products" **[V]** (DrizzlePac Handbook §5.3). |
| HAP SVM | No as a mosaic; possibly as a WCS source | Its per-exposure `FIT_SVM` solution is the best automatic starting WCS for ACS (92% Gaia) **[V]**. |
| HAP MVM | No | Discovery images by STScI's own description. |

Why published surveys do not use archive mosaics: no cross-visit registration to one reference; native scale and `pixfrac` 1 with no shared tangent plane; exposure-time weights with no noise map; a per-exposure constant sky that over-subtracts near extended objects; no handling of variable IR background, persistence, satellite trails or residual ACS bias structure; no correction for sensitivity changes between epochs **[V for each archive property; the list as "the usual reasons" is I]**. In a sample of 2023 to 2026 JWST-era catalog papers, none used archive drizzle products for HST photometry (section b.3).

---

## b. What is used in papers

### b.1 Is the two-lineage picture right?

Partly. The brief's picture (grizli and the DAWN JWST Archive, versus the CANDELS lineage) names the two largest families. The survey found five, of which two have public code **[V unless noted]**:

| Family | Members | Public code |
| --- | --- | --- |
| **Koekemoer "MosaicDrizzle"** | COSMOS-ACS (2007), CANDELS (2011), CLASH, Frontier Fields, BUFFALO, CEERS HST release, COSMOS-Web F814W | None found **[I: absence of a repository, not a statement by the authors]** |
| **Brammer** | `threedhst`, 3D-HST, grizli, CHArGE, DAWN JWST Archive (DJA); used by UNCOVER, MegaScience, CANUCS, MINERVA | Yes, MIT |
| **Illingworth / Magee / Bouwens** | HUDF09, XDF, Hubble Legacy Fields; adopted by JADES; one source attributes the PRIMER team's HST mosaics to D. Magee | Not stated public |
| **Archive HAP** | SVM, MVM | Yes, in drizzlepac; no deep-field catalog paper found that adopts it |
| **Project-specific drizzlepac scripts** | RELICS, 3D-DASH, `hst123` (point sources, transients) | Partly |

3D-HST is a hybrid, not a lineage of its own: it re-reduced WFC3/IR with Brammer's tools on the CANDELS tangent point and adopted the CANDELS ACS mosaics unchanged (Skelton et al. 2014).

### b.2 Comparison of approaches

| Approach | Code | Maintenance | License | Dependencies | Astrometry | Background | Cosmic rays and artifacts | Weights | Surveys |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **grizli / DJA** | github.com/gbrammer/grizli | Active; last commit 2026-10-26; effectively one maintainer | MIT | `drizzlepac<3.7.0`, `jwst<1.17`, `photutils<1.13` **[V†]**, `stwcs`, `tristars`, `hstcal` for WFC3/IR | Per visit: shift and rotation from a catalog of the drizzled visit, triangle matching; reference Gaia DR3 with proper motions, or Legacy Surveys, or a field catalog. Shift only between exposures in a visit | AstroDrizzle sky, optional mesh background blotted to exposures; WFC3/IR ramps re-fitted from RAW | AstroDrizzle `driz_cr`; L.A.Cosmic for single ACS exposures; STScI persistence model mask; extra IR bad-pixel masks; hand-drawn masks | Mosaic stage for HST: one number per chip, the median of 1/ERR^2 **[V†]** | UNCOVER, MegaScience, CANUCS, MINERVA, DJA fields |
| **Koekemoer** | Not public | In use through 2025 | n/a | MultiDrizzle in 2011; current stack unknown | Cross-correlation within an orbit, then each orbit to an external catalog (shift, rotation); Gaia EDR3/DR3 in recent releases | Constant per exposure after masking sources | Two-pass `driz_cr` with `minmed`; ACS destriping and CTE; CR masks grown 30 px along CTE trails; persistence and warm-pixel flags; satellite trails by hand | Inverse variance from sky, dark, read noise and flat; source noise excluded | COSMOS, CANDELS, CLASH, Frontier Fields, BUFFALO, CEERS, COSMOS-Web |
| **3D-HST** | `threedhst`, dormant since 2017, no license | Dormant | none | calwf3, TweakReg, AstroDrizzle | TweakReg to catalogs of CANDELS mosaics | Median and first-order polynomial | `driz_cr`; trails by hand; persistence flagged | Inverse variance, no source term | AEGIS, COSMOS, GOODS, UDS |
| **HLF / XDF** | Not stated public | HLF v2.5 page updated 2021 | n/a | APSIS, WFC3RED, MultiDrizzle | Visit stacks cross-correlated to a reference; HLF v2 on Gaia DR2 | Super-median subtraction; per-read sky for variable IR background | CTE and striping correction; persistence mask at 0.2 e-/s | 1/rms^2 | HUDF, XDF, GOODS-S, GOODS-N |
| **Archive HAP** | drizzlepac | Active | BSD-3 | drizzlepac, stwcs, tweakwcs | Automatic Gaia eDR3 fit per visit; none between visits | Sky matching | Pipeline flags | Exposure time | all ACS and WFC3 data |
| **RELICS-style scripts** | Not public | 2019 | n/a | AstroDrizzle | WISE catalog | Variable F105W sky not corrected | ACS CTE and destripe; trails by hand | Inverse variance | 41 clusters |
| **hst123** | github.com/charliekilpatrick/hst123 | Active (2026-07) | MIT | drizzlepac, optional JHAT | TweakReg or JHAT | AstroDrizzle | AstroDrizzle | AstroDrizzle | transients |

All rows **[V]** from the papers and repositories listed in section 6, except where marked.

**grizli in more detail**, since it is the only complete, public, currently developed deep-field pipeline **[V]** (`UP/grizli`, commit `c8c9d325`):

- One code path serves JWST and HST: JWST files are given HST-style keywords so that AstroDrizzle can read them (`grizli/prep.py:5629`).
- WFC3/IR always starts from RAW, re-running calwf3 with the ramp flattened (`grizli/pipeline/auto_script.py:1077`, `pipeline/reprocess.py:7-59`). ACS starts from FLC.
- Absolute astrometry is per visit, not per exposure (`prep.py:1051`, `6095-6107`).
- The final mosaic uses grizli's own array-level wrapper around drizzlepac's C drizzle (`utils.py:8577`), not the `drizzle` package that `jwst` uses.
- For HST the mosaic weight is a single number per chip, because HST files lack the `VAR_POISSON` array the JWST branch uses (`utils.py:8300-8325`) **[V†]**. Issue #166 (open since 2023) says the weights are not "statistically robust inverse variance" images **[V]**. UNCOVER notes that the weight maps do not account for correlated noise (Bezanson et al. 2024) **[V]**.
- The DJA mosaic stage reads its exposure list from the author's Postgres database and S3 bucket; a local substitute function exists (`aws/visit_processor.py:2395`, `2752-2771`). Satellite-trail handling for WFC3/IR is an interactive GUI (`pipeline/reprocess.py:75`).
- DJA grid: 40 mas pixels for HST (including WFC3/IR) and NIRCam LW, 20 mas for most NIRCam SW; north up except CEERS; units of 10 nJy per pixel (`aws/define_mosaics.py`; DJA blog 2023-07-18).
- Installation: its declared pins cannot coexist with this repository's `jwst>=1.20`, `photutils>=1.13`, `numpy>=2.0` **[V†]** (`UP/grizli/setup.cfg:32,59,63`; `pipeline/pyproject.toml:8`). `drizzlepac<3.7.0` means a November 2023 release that predates numpy 2. It would need its own environment.

**The Koekemoer recipe**, since it defines the CANDELS-lineage products we would compare against **[V]** (Koekemoer et al. 2011): mosaics at 0.03 arcsec (ACS) and 0.06 arcsec (WFC3/IR), `pixfrac` 0.8, one tangent point per field, units e-/s; weights are inverse variance of sky, dark and read noise only; absolute accuracy quoted at about 5 mas against ground-based catalogs of that era. Recent applications tie directly to Gaia: the CEERS HST release (v1.9; 1,767 exposures; 0.03 arcsec; Gaia EDR3) and the COSMOS-Web F814W mosaic (0.03 arcsec; Gaia DR3; all tiles share `CRVAL = (150.1163213, 2.200973097)`, which is the tangent point of the CAMPFIRE `cosmos` field) **[V]** (CEERS HDR1 page; Franco et al. 2025).

### b.3 What JWST-era catalog papers do for the HST bands

| Survey | HST treatment | Tag |
| --- | --- | --- |
| CEERS | New Koekemoer-style mosaics from exposures, 0.03 arcsec, Gaia EDR3; NIRCam placed on the HST tangent point | V |
| COSMOS-Web / COSMOS2025 | F814W re-mosaicked, Gaia DR3; the only HST band used | V |
| JADES DR5 | Adopts Hubble Legacy Fields v2.0 / v2.5, re-registered to the NIRCam frame and reprojected | V |
| UNCOVER, MegaScience | All archival HST re-drizzled with grizli on the JWST grid, 40 mas | V |
| MINERVA | NIRCam and HST both reduced with grizli, 40 mas | V |
| CANUCS, PANORAMIC | grizli | V from search summaries only |
| ASTRODEEP-JWST | Adopts a different existing mosaic per field, reprojects all to 30 mas, removes residual offsets against NIRCam | V |

Two patterns: teams that own a full pipeline re-drizzle HST from exposures onto the NIRCam grid; the others adopt an existing mosaic, re-register it and reproject **[V per row; the generalization is I]**.

### b.4 Reductions that cover the CAMPFIRE fields

**Where the field list comes from.** The repository holds no canonical field list; fields are defined in `$CAMPFIRE_ROOT/config/fields.toml` and the cloud `fields` table (`nircam-fit.md` section 1.2). I read the section names and tangent points from the local `fields.toml`: `cosmos`, `uds`, `egs`, `a2744`, `nep`, `rj0911`, `pg004`, plus test variants of `rj0911`. `goods-n` and `goods-s` exist only as NIRSpec fields (`python/campfire/deploy/photometry.example.toml:12`, `supabase/seed.sql`), and are included because they are the main HST legacy fields.

**Identifications** **[V]** (STScI program pages and APT files for JWST 5105, 9263, 6882):

- `nep` is **NEXUS** (PI Shen), centred at (268.4625, +65.199). It is not the JWST North Ecliptic Pole Time-Domain Field, which is about 3.3 degrees away **[I for the TDF position]**.
- `rj0911` is **RXC J0911.1+1746** (MACS J0911.2+1746, z = 0.505) and `pg004` is **PLCK G004.5-19.5** (z = 0.540); both are observations of the VENUS program (JWST 6882) and both are RELICS clusters.

Exposure counts marked "DJA" were counted from the public per-mosaic exposure tables of the DAWN JWST Archive (metadata only) and refer to the DJA mosaic footprint, not the whole field. Counts for the three small fields come from three small MAST metadata queries (observation and product listings; no data products were downloaded).

| Field | ACS/WFC | WFC3/IR | Public reductions | Best comparison reference |
| --- | --- | --- | --- | --- |
| `cosmos` | F814W over 1.64 deg^2 (2003-05, about 575 pointings x 4 exposures **[I for the product]**); CANDELS F606W/F814W; F435W | CANDELS F125W/F160W; 3D-HST F140W; COSMOS-DASH and 3D-DASH F160W (drifting exposures) | COSMOS ACS v2.0 at IRSA (0.03 arcsec, pre-Gaia frame); COSMOS-Web reprocessed F814W (Gaia DR3, 0.03 arcsec, same tangent point as CAMPFIRE; public availability **[U]**); CANDELS v1.0; 3D-HST; NIRWL F160W (Gaia DR3, 0.05 arcsec); DJA v7.0 (40 mas) | COSMOS-Web F814W if obtainable; NIRWL for F160W astrometry; DJA for multi-filter flux |
| `uds` | CANDELS F606W/F814W; F435W (program 16872) | CANDELS F125W/F160W; 3D-HST F140W | CANDELS v1.0 (0.13 arcsec from Gaia DR3); 3D-HST; NIRWL; DJA v7.0; MINERVA DR0 (ACS, 40 mas) | NIRWL (astrometry) with DJA or MINERVA (flux) |
| `egs` | AEGIS F606W/F814W, 63 tiles (2004-05); CANDELS parallels; F435W | CANDELS F125W/F160W; 3D-HST F140W; some F105W | **CEERS HST release v1.9**: 1,767 exposures, six filters, 0.03 arcsec, Gaia EDR3, science, inverse-variance weight and rms maps; CANDELS v1.0; 3D-HST; NIRWL | CEERS HST release |
| `a2744` | Frontier Fields F435W/F606W/F814W; BUFFALO; F775W, F475W | Frontier Fields four filters; GLASS direct images; BUFFALO | Frontier Fields v1.0 (30/60 mas, Subaru frame); BUFFALO v1.0 (30/60 mas, Gaia DR2); UNCOVER DR3 = DJA v7 (40 mas) | BUFFALO (astrometry, matching scale) with UNCOVER/DJA (depth, exposure lists) |
| `nep` | one 1500 s F814W snapshot (2026) | none | none | none: **out of scope for HST** |
| `rj0911` | F555W and F814W (2004-06, 20 exposures); F435W (2017, 4); no F606W | RELICS four filters (16 exposures, 2016); F105W (18, 2023-24) | RELICS (30/60 mas, WISE-based frame) | RELICS |
| `pg004` | RELICS F435W/F606W/F814W, 4 exposures each (2016) | RELICS four filters, 16 exposures on the core (2016); F105W (15, 2022) | RELICS (core and parallel) | RELICS |
| `goods-s` | GOODS, HUDF, CANDELS and others | ERS, HUDF09/12, CANDELS, 3D-HST | Hubble Legacy Fields v2.0 (7,491 exposures including UVIS; 30/60 mas); CANDELS (0.26 arcsec from Gaia DR3); NIRWL; DJA v7.0 | HLF v2.0 |
| `goods-n` | GOODS, CANDELS and others | CANDELS and others | HLF v2.5 (5,817 exposures); CANDELS; NIRWL; DJA v7.3 | HLF v2.5 |

All cells **[V]** unless marked (sources per field in section 6).

Sizes, from the DJA exposure tables **[V]**:

- PRIMER-COSMOS footprint (two overlapping halves, each): about 360 to 370 F814W, 380 to 440 F160W (including DASH), 140 to 150 F125W, 120 to 160 each of F435W and F606W.
- PRIMER-UDS footprint (each half): about 330 to 360 F814W, 170 to 180 each of F125W and F160W, 140 F606W, 105 F435W.
- Abell 2744 cluster footprint: about 950 ACS/WFC plus WFC3/IR exposures in nine filters.
- GOODS-S: about 5,800 ACS/WFC plus WFC3/IR exposures; GOODS-N about 5,200.

Things specific to these fields:

- **Frames of the older public mosaics are not Gaia.** The CANDELS v1.0 mosaics sit 0.05 to 0.26 arcsec from Gaia DR3 (COSMOS 0.11, UDS 0.13, EGS 0.07, GOODS-N 0.05, GOODS-S 0.26), and 3D-HST inherits those frames **[V]** (Finner et al. 2023, Table 1). RELICS is on a WISE frame and Frontier Fields on a Subaru frame; their offsets from Gaia are **[U]**. A comparison against any of these needs a fitted frame offset first.
- **Gaia alone cannot anchor the deep fields.** NIRWL found 0 to 5 usable Gaia stars per CANDELS patch **[V]**. Every modern reduction goes through an intermediate catalog already tied to Gaia. For CAMPFIRE that catalog exists: the field's NIRCam reference catalog or one extracted from the NIRCam mosaic. `pg004` at galactic latitude -19.5 is the exception, with dense Gaia coverage.
- **ACS eras.** COSMOS F814W, AEGIS, GOODS and the early `rj0911` data are 2002 to 2006; CANDELS parallels, Frontier Fields, BUFFALO and RELICS are after 2009, where CTE and striping matter most.
- **COSMOS F160W is mostly DASH**, which needs read-level processing and cannot be reduced from FLT files (section c.2).
- **HST mosaics on the CAMPFIRE COSMOS tile grid already exist outside this repo**: `scripts/cosmos_inspec.py:52-55` reads ACS and WFC3/IR mosaics named for the COSMOS-Web 30 mas tiles **[V†]**. If those files are available to you, they are the most direct comparison reference for COSMOS.

Candidate proof-of-concept datasets (for the Stage 3 plan):

- **`pg004` core**: 12 ACS/WFC and 16 WFC3/IR exposures from one program in 2016, seven filters, dense Gaia, an existing CAMPFIRE NIRCam mosaic. Smallest clean case. Weaknesses: the only external mosaic is RELICS on a WISE frame; intracluster light complicates sky; four ACS exposures per filter.
- **One CANDELS tile in EGS**: roughly 40 to 60 exposures **[I]** across both ACS eras, and the best external reference (CEERS HST release: Gaia-tied, 30 mas, weight and rms maps). Exercises the sparse-Gaia path that COSMOS and UDS need.
- **`rj0911`**: small, mixes 2004 to 2006 and post-2009 ACS and a 20-year baseline; no F606W.

---

## c. Artifacts and differences from NIRCam

### c.1 ACS/WFC

| Artifact | How recognized | Handled by calacs / archive? | Extra handling by community pipelines and tools |
| --- | --- | --- | --- |
| **Charge-transfer inefficiency (CTE).** Radiation damage traps charge during readout and releases it later | Trails behind cosmic rays, hot pixels and stars, pointing away from the amplifiers; worst far from the amplifier, at low background, in recent data | Yes: pixel-based correction gives the FLC. Third-generation model (parallel and serial) since calacs 10.4.0, May 2024. Accurate to about 25% of the correction; adds noise; cannot recover losses at backgrounds under 20 to 30 e- | Use FLC. CANDELS grows cosmic-ray masks 30 px along the trail. Frontier Fields self-calibrates when more than 8 exposures exist |
| **Bias striping** (post-2009 electronics) | Row-correlated stripes, about 0.9 e- | Yes for full frames (residual about 0.4 e-); no for subarrays | `acstools.acs_destripe_plus` |
| **Amplifier (quadrant) offsets** | Sky steps at the four quadrant boundaries, about 0.3 DN, random per exposure | **No** | Per-quadrant sky fit in every legacy survey; grizli `clean_amplifier_residuals` |
| Hot, warm, unstable pixels | DQ 16, 64, 32 | Flagged; dark per anneal | Dithering; choice of which flags to treat as good |
| Sink pixels | DQ 1024, data after January 2015 | Flagged only | Treat as bad |
| **Cosmic rays** | 1.5 to 3% of pixels per 1000 s | Only for CR-SPLIT pairs; dithered sets get flags only as a by-product of the archive drizzle | Own `driz_cr` per visit, then across visits; L.A.Cosmic (`astroscrappy`) for single exposures (grizli) |
| **Satellite trails** | Linear streaks; 8 to 10% of full-frame images by 2022 | **No** | `acstools.findsat_mrt` (about 85% recovery; 2.5 to 10% false positives); every legacy survey masked by hand |
| Saturation bleed | Column bleed, DQ 256 | Flagged | Exclude from alignment catalogs |
| Ghosts, glint, "dragon's breath", scattered Earth light | Rings, diagonal rays, edge glow, sky gradients | **No** | Manual masks; survey-level scattered-light templates (COSMOS, HUDF) |
| Flat-field residuals | 1 to 2% large-scale | Flat-fielded | Sky flats in the deepest fields |
| F850LP long-wavelength halo | Broad PSF wings for red sources | No (it is the PSF) | Colour-dependent aperture corrections |
| Chip gap | 2.5 arcsec without data | n/a | Dither of 3 arcsec or more, or other epochs |
| Time-dependent distortion | Linear skew drifting about 25 mas over 20 years; a jump at the 2009 servicing mission | Yes, in the distortion reference file | Refit shift and rotation (and scale) per visit |
| Guiding failures | `EXPFLAG` not `NORMAL`; jitter-file keywords; smeared PSF | Header flags only | grizli drops visits with two or more bad flags |

All rows **[V]** (ACS DHB §3.4, §4.2 to §4.7; ACS IHB; ACS ISRs 2022-08, 2024-07, 2025-01; Koekemoer et al. 2007, 2011; Lotz et al. 2017; `UP/hstcal`, `UP/acstools`, `UP/grizli`).

Corrections to the starting leads:

- LED post-flash is not common for ACS/WFC. STScI recommends it "only in very limited circumstances" (faint compact targets on a background under 20 to 30 e-) **[V]** (ACS IHB §2.6; ACS ISR 2014-01).
- "Figure-eight ghosts" belong to WFC3/UVIS, not ACS **[V]** (WFC3 anomalies page).
- ACS/WFC was out of service from January 2007 to May 2009. Read noise, striping and bias behaviour differ before and after **[V]** (ACS IHB §2.1; ACS DHB §4.2).

### c.2 WFC3/IR

| Artifact | How recognized | Handled by calwf3 / archive? | Extra handling by community pipelines and tools |
| --- | --- | --- | --- |
| **Time-variable background.** He I 1.083 micron airglow in F105W and F110W whenever HST is outside Earth's shadow; scattered Earth light at low limb angle in any filter | Background changes between reads; the FLT has a multi-modal noise histogram; the `SAMP` and `TIME` arrays show that few reads were used | **No.** The ramp fit assumes a linear ramp, flags most reads as cosmic rays, and the FLT is "not recommended for scientific analysis" | Re-run from RAW/IMA: either skip the ramp-fit rejection, or subtract the per-read background and refit ("flatten ramp"). STScI notebooks; `reprocess_wfc3` (grizli does this for every exposure) |
| **Persistence.** Charge trapped by earlier bright sources, released as an afterglow | Afterimages of stars from the previous visit or earlier dithers; about 0.3 e-/s at 1000 s after a full-well exposure | **No** | MAST persistence models (`_persist.fits`), masked above a threshold (XDF 0.2 e-/s; grizli 0.5 to 0.6 times ERR, and subtracts the model) |
| **Blobs.** Dust on a mirror; 153 dark spots of 10 to 20 px | DQ 512 | Flagged; corrected by "delta flats" in six filters for data retrieved after October 2020. Default drizzle settings **keep** these pixels | Dither over them; decide per filter whether 512 is good |
| Bad, unstable, hot pixels; "death star"; unbonded corners | DQ 4, 8, 16, 32 | Flagged; time-dependent tables since 2019 | Extra bad-pixel masks (grizli, RELICS) |
| "Wagon wheel" | Lower-right corner, sensitivity 25 to 50% low, colour-dependent | Flat-fielded, not flagged | None |
| Cosmic rays, snowballs | Removed in the ramp fit; residuals remain | Yes, mostly | `driz_cr` with loose thresholds (grizli SNR `8 5`, scale `2.5 0.7`) |
| **Satellite trails** | A streak in one read; often badly fitted | Partly | Remove the affected read and refit; manual masks. No supported automatic finder for IR |
| Saturation | FLT DQ is 0 even when late reads saturated | Yes | Use `SAMP`/`TIME` |
| Count-rate non-linearity | Faint sources about 0.04 mag too faint | **No** | 0.0075 mag/dex correction if required |
| Undersampling | PSF FWHM 1.0 to 1.2 native pixels | n/a | 4-point sub-pixel dither; 0.06 arcsec output with `pixfrac` 0.8 |
| Sensitivity loss | 0.06 to 0.12% per year | Yes, in header zeropoints for files retrieved after December 2024 | Read `PHOTFNU` per exposure from freshly retrieved files |
| **DASH exposures** (gyro-guided, drifting) | Smeared FLT; archival only (mode unavailable since June 2024) | **No** | Split the ramp into 25 s differences, align each, drizzle (`wfc3dash`) |

All rows **[V]** (WFC3 DHB §3.3, §7.4 to §7.11, §8, §9; WFC3 IHB §5.7, §7.9, §7.10; WFC3 ISR 2016-16; Momcheva et al. 2017; `UP/grizli`; `UP/hst_notebooks`).

Points that follow for us:

- **The FLT is not a sufficient starting point for every WFC3/IR exposure.** STScI's report on the effect says that without a shadow constraint nearly every orbit-long F105W or F110W visit sees the helium line at some point **[V]** (WFC3 ISR 2016-16 §2). F125W, F140W and F160W are affected only by scattered Earth light. Treatment in the literature: CANDELS kept all reads; RELICS did not correct F105W; Frontier Fields, XDF and grizli did **[V]**.
- **Retrieval date matters.** Flats and zeropoints changed in October 2020, bad-pixel flags in May 2019, time-dependent zeropoints in December 2024, the non-linearity file in October 2025 **[V]** (WFC3 DHB "What's new", §7.8, §7.9; WFC3 STAN 48).
- Whether MAST still produces persistence models for 2025 to 2026 data is unknown; the documentation says yes, the search page looks unmaintained, and no query was run **[U]**.

### c.3 Both instruments: guiding and gyros

HST has operated in reduced-gyro mode since June 2024 **[V]** (WFC3 IHB §1.5; ACS ISR 2025-01). STScI reports normal image quality for most programs, but "undeclared loss of lock" became more frequent: an exposure delivered with normal flags that is smeared by a slow roll. Drifts up to about one pixel are hard to see by eye **[V]** (ACS ISR 2025-01). Failed exposures stay in the archive unless the PI reported them **[V]** (HST Primer). A pipeline needs a per-exposure quality gate (flags, jitter keywords, and a PSF-width check on post-2024 data).

### c.4 Differences from NIRCam that could break shared code

**Units and zeropoints.**

- ACS FLC is in electrons, WFC3/IR FLT in e-/s, never MJy/sr **[V]**.
- The flux scale is in header keywords: `PHOTFLAM` (erg cm^-2 s^-1 A^-1 per e-/s), `PHOTPLAM` (pivot wavelength), and on WFC3 `PHOTFNU` (Jy per e-/s). `ZP_AB = -2.5 log10(PHOTFLAM) - 5 log10(PHOTPLAM) - 2.408` **[V]** (ACS DHB §5.1; WFC3 DHB §9.1). For F160W, 1 e-/s is 152.6 nJy **[derived by the research agent from the published values]**.
- Zeropoints change with time for both instruments. ACS headers have carried date-appropriate values since 2019; WFC3/IR headers only for files retrieved after December 2024 **[V]** (ACS zeropoints page; WFC3 STAN 48). A mosaic that spans years needs each exposure scaled by its own zeropoint before combination. AstroDrizzle does not do this by default, and the archive's multi-visit mosaics do not either **[V]**.
- FLT/FLC pixels are not corrected for the variation of pixel area across the field (ACS: 0.88 to 1.06 of the central value; WFC3/IR: up to 4%); drizzling applies it **[V]** (ACS DHB §5.1; WFC3 DHB §9.1). NIRCam `cal` images in MJy/sr follow the same flat-sky convention **[I]**.

**Pixel scales and sampling** **[V]** (ACS IHB §5.6, §10.4; WFC3 IHB §7.6; WFC3 DHB §4.1):

| | ACS/WFC | WFC3/IR | NIRCam SW / LW **[I]** |
| --- | --- | --- | --- |
| Native scale | 0.050 arcsec | 0.128 arcsec (0.135 x 0.121) | 0.031 / 0.063 arcsec |
| Format | 2 x 4096x2048 | 1014x1014 | 2048x2048 per detector |
| Field of view | 202 x 202 arcsec | 123 x 136 arcsec | |
| PSF FWHM | 0.10 to 0.13 arcsec (2 to 2.6 px) | 0.13 to 0.15 arcsec (1.0 to 1.2 px) | |

Output scales used in the literature: CANDELS and CEERS 0.03 arcsec (ACS) and 0.06 arcsec (WFC3/IR) with `pixfrac` 0.8; HLF 0.03 and 0.06; DJA 0.04 for everything; COSMOS-DASH 0.1 **[V]** (section b). Putting WFC3/IR on a 30 mas grid oversamples it by a factor 4.3, which makes neighbouring pixels strongly correlated (below).

**Distortion and WCS.** HST stores distortion in the FITS headers: a SIP polynomial per chip, plus (ACS only) lookup-table extensions for filter-dependent and pixel-grid residuals. The ACS time-dependent skew and the velocity aberration are already folded into the stored coefficients **[V]** (`UP/stwcs/stwcs/updatewcs/`). `astropy.wcs.WCS(header, fobj=hdulist)` evaluates the complete model; `stwcs` is needed to change a WCS or switch between stored solutions, not to read one **[V from documentation and source; not executed]**. JWST instead stores a gwcs object in an ASDF extension.

**Errors, weights and correlated noise.**

- HST provides one `ERR` array per chip, which includes source Poisson noise estimated from the noisy data. There are no separate read-noise, Poisson and flat variance arrays **[V for the ERR definition; the absence is I]**.
- Weighting by 1/ERR^2 biases source fluxes low by 1 to 2 percent (DrizzlePac Handbook §5.2) **[V]**. Every survey reduction in section b uses a background-only inverse variance instead.
- Drizzling correlates neighbouring pixels. The ratio R between the true noise and the pixel-to-pixel rms depends on `pixfrac` and the output scale (Fruchter & Hook 2002): about 1.8 for ACS at 0.03 arcsec with `pixfrac` 0.8; about 2.1 for WFC3/IR at 0.06 arcsec; about 3.6 for WFC3/IR at 0.04 arcsec with `pixfrac` 1 **[derived by the research agent from the verified formula]**. Any code that turns a weight map into a noise map needs this factor per instrument and grid.

**Data-quality flags** **[V]** (ACS DHB Table 3.4; WFC3 DHB Table 2.7): 16-bit, with instrument-specific meanings that share nothing with JWST's. Examples: 4096 is "cosmic ray found by AstroDrizzle" on both; 512 is "bad in reference file" on ACS and "blob or bad flat" on WFC3/IR; 1024 is "sink pixel" on ACS and unused on WFC3/IR. The archive treats different sets as good per instrument (ACS 336 = hot + warm + saturated; WFC3/IR 528 = stable hot + blob). A shared mosaic step needs a per-instrument translation from DQ to "use / do not use".

**Naming and headers** **[V]** (HST Data Handbook §2.2; ACS DHB Table 2.6): files are named by a nine-character root `ipppssoot` (instrument letter `j` for ACS or `i` for WFC3, three characters encoding the program, two for the visit, two for the exposure, one suffix). The program number is the `PROPOSID` keyword, not readable from the name. ACS has `FILTER1` and `FILTER2` (one is a `CLEAR`); WFC3 has `FILTER`. There is no `S_REGION` in the keyword lists read **[I]**.

**Exposures per pointing** **[V]** (survey papers in section 6):

| Survey | Per pointing and filter |
| --- | --- |
| COSMOS ACS F814W | 4 x 507 s |
| GOODS ACS | 2 per epoch in V and i, 4 in z, 5 epochs |
| CANDELS Wide WFC3/IR | 2 + 2 per epoch, 2 epochs; ACS parallels 2 to 4 |
| CANDELS Wide UVIS F350LP | 1 |
| Frontier Fields | 4 half-orbit exposures per filter per visit; tens of visits |
| RELICS | ACS 4 per orbit; WFC3/IR possibly 1 to 2 per filter **[U]** |
| COSMOS-DASH | 1 (as 10 to 11 short reads) |
| XDF | hundreds |

A per-visit stack often has two exposures. A median of two is a mean; with two ACS exposures thousands of pixels per chip are hit by a cosmic ray in both **[V]** (Koekemoer et al. 2011 §5.4). This is the case `minmed` exists for, and CANDELS rejects first within an orbit and again across epochs once the astrometry is good to a tenth of a pixel.

---

## d. Recommendation

### d.1 The options

**1. Wrap grizli.** Run grizli as the HST reduction engine and ingest its outputs.

- For: complete, used on exactly these fields, handles the WFC3/IR ramp problem, persistence and single-exposure cosmic rays today.
- Against: it cannot be installed with the CAMPFIRE pipeline (pins on `jwst`, `drizzlepac` and `photutils` that conflict with ours), so it would run in a separate environment behind a file-level boundary. One maintainer. The multi-visit mosaic stage is written around the author's database and bucket. HST weights are one number per chip, with a long-open issue on their statistical meaning. Parts are interactive. Its conventions (40 mas north-up grids, 10 nJy units, per-visit astrometry) are not CAMPFIRE's, so we would resample or re-drizzle anyway. Scientifically it would reproduce what DJA already publishes rather than give an independent reduction.

**2. Call the STScI tools for everything.** `stwcs` + TweakReg + AstroDrizzle through to the final mosaic, with `final_refimage` pointing at a NIRCam tile.

- For: reference implementations maintained by STScI, BSD-licensed; drizzlepac 3.11.0's requirements are met by the current `campfire` environment on paper (not test-installed) **[V at the metadata level]**; STScI ships `jwst`, `drizzlepac` and `hstcal` together in its `stenv` environment **[V]**.
- Against: AstroDrizzle is built for one visit or one association in memory, not for 1 to 2.4 gigapixel tiles with hundreds to thousands of inputs (the EGS tile is 44068 x 54881 pixels). Its final product has no error extension and different weight semantics from the NIRCam mosaics. It would be a second, parallel mosaic path with its own grid, provenance and staleness logic, which is the duplication the brief wants to avoid. The NIRCam `align` work found that the `tweakwcs`/`stimage` pair-enumeration matcher failed on clustered extragalactic catalogs (39% of COSMOS LW exposures; `pipeline/campfire_pipeline/data/config_default.toml:681-687`); TweakReg uses a matcher of the same family **[I]**.

**3. Hybrid: STScI tools for what is specific to HST detectors, CAMPFIRE machinery for alignment and mosaicking.**

- Inputs: archive FLC and FLT, with the retrieval date, `CAL_VER` and reference-file names recorded.
- HST-specific per-exposure and per-visit work with STScI tools: `stwcs` for the distortion model and WCS bookkeeping; the AstroDrizzle separate-drizzle, `minmed`, blot and `driz_cr` sequence run per visit only to put cosmic-ray flags into each exposure's DQ; `acstools` for satellite trails and, where needed, destriping; `wfc3tools`/calwf3 for WFC3/IR exposures with variable background.
- Astrometry with the method already built for NIRCam (`nircam/align`: offset-histogram matching, pooled coarse fit, gated fine fit), through `tweakwcs`' FITS-WCS corrector, against the same reference catalog the field's NIRCam data used or a catalog extracted from the CAMPFIRE NIRCam mosaic (`nircam/refcat`). HST then lands in the NIRCam frame by construction.
- Final mosaic with CAMPFIRE's own drizzle (`nircam/drizzle.py`, generalized) on the exact NIRCam tile WCS, so that HST and NIRCam mosaics share grid, error and weight definitions, manifests, version stamps, background subtraction and deploy path.
- Methods that only grizli has in public form (L.A.Cosmic for single exposures, the persistence-mask recipe, blotting a mosaic background back to exposures, extra IR bad-pixel masks) are re-implemented from its MIT-licensed source or the underlying STScI notebooks, without depending on grizli.

### d.2 Recommended: option 3

Reasons, in the order of the brief's priorities (data quality, correctness, provenance):

1. **Same frame and same grid as NIRCam, by construction and not by reprojection.** The output WCS and the astrometric reference are the ones CAMPFIRE already controls. Both JWST-era patterns in section b.3 either do this (CEERS, COSMOS-Web, grizli teams) or pay for not doing it with a re-registration and a second resampling (JADES, ASTRODEEP).
2. **One definition of ERR, WHT and exposure maps across HST and NIRCam.** With AstroDrizzle or grizli as the final step, the HST weight maps would mean something different from the NIRCam ones.
3. **Cosmic-ray rejection is the one combination step where the HST tool is clearly better suited than ours**, because of the two-to-four-exposure regime and undersampled WFC3/IR cores. Using AstroDrizzle for that step alone keeps the well-tested algorithm and its parameters from the literature.
4. **The shared core is plausible, with evidence.** The `drizzle` package that our drizzle already calls has upstream tests that resample an ACS FLT through a FITS WCS **[V†]** (`UP/drizzle/drizzle/tests/test_resample.py:322,393`), and its documentation covers HST units and per-exposure zeropoint scaling **[V]**. `stwcs` is already in the `campfire` environment **[V†]**. Not yet shown: an actual run on an FLC compared with AstroDrizzle. That is the first experiment of the proof of concept.
5. **Maintenance risk sits with STScI packages and with us**, not with a single external maintainer.

Costs and risks of option 3:

- More code of our own than option 1: an HST exposure adapter (units, variance model, DQ translation, per-chip WCS), a generalization of the NIRCam mosaic modules (`nircam-fit.md` section 2), and HST alignment I/O.
- The NIRCam refactor has no golden-pixel test to protect it today (`nircam-fit.md` section 4); one has to be created first.
- Re-running calwf3 needs `hstcal`, which is distributed through conda-forge only (Linux x86-64 and macOS; not on PyPI) **[V]**, plus an HST CRDS cache. The pipeline's CRDS environment variables are currently global and JWST-only (`nircam-fit.md` section 3.8).
- We take on validation that grizli and the Koekemoer products have accumulated over years: weights, correlated noise, zeropoints across epochs, residual artifacts. The validation plan in Stage 3 has to carry that.

A reasonable variant, which I would adopt for the proof of concept: also produce the final mosaic of the test pointing with AstroDrizzle on the same grid, purely as a cross-check of our drizzle and unit handling. It is cheap at the scale of one pointing and it separates "our combination is wrong" from "our inputs are wrong".

### d.3 How each option fits a pipeline that wraps `jwst`

The NIRCam pipeline already has both patterns side by side: it wraps `jwst` pipelines for detector-level calibration and offers either the `jwst` or its own implementation for outlier detection and resampling (`nircam-fit.md` section 1.4). Option 3 is the same division for HST: upstream code for the instrument physics, CAMPFIRE code for combination. Option 2 corresponds to the `implementation = "jwst"` path extended to HST, and option 1 has no counterpart in the current design.

---

## 5. Open questions from Stage 1

1. Is MAST still regenerating HAP SVM products, and do their per-exposure `FIT_SVM` solutions exist for all our exposures? (Useful as a starting WCS only.)
2. Do MAST persistence models exist for recent WFC3/IR data, and can they be fetched in bulk?
3. Does the standalone `drizzle` package reproduce AstroDrizzle on an ACS FLC to rounding, including the pixel-area flux factor? Not run.
4. Does drizzlepac 3.11.0 install cleanly next to `jwst 1.20.2` in practice? Metadata says yes; not test-installed.
5. What are the numeric `final_bits` and `driz_cr` settings in the current WFC3/IR `MDRIZTAB`?
6. Do HST FLT/FLC headers carry any footprint keyword usable in place of `S_REGION`?
7. Is the archive IMA flat-fielded and gain-corrected, so that a ramp could be re-fitted in Python without calwf3? (Determines whether `hstcal` is a hard dependency.)
8. Koekemoer and HLF pipelines: confirmed not obtainable as code? Only absence of a repository is established.

## 6. Sources

Access status: "read" means the text was read in this session; "summarized" means read through a fetch tool that returns a summary; "abstract" means only the abstract or a search excerpt was seen.

**Upstream source (shallow clones made 2026-11-11, read directly)**

| Key | Location | Status |
| --- | --- | --- |
| `UP/hstcal` | https://github.com/spacetelescope/hstcal (tag 3.3.0) | read |
| `UP/drizzlepac` | https://github.com/spacetelescope/drizzlepac (3.11.0; docs under `doc/source/mast_data_products/`) | read |
| `UP/stwcs` | https://github.com/spacetelescope/stwcs (1.7.7) | read |
| `UP/drizzle` | https://github.com/spacetelescope/drizzle (3.0.0) | read |
| `UP/acstools`, `UP/wfc3tools`, `UP/hst_notebooks` | https://github.com/spacetelescope/acstools , `/wfc3tools` , `/hst_notebooks` | read (selected files) |
| `UP/grizli` | https://github.com/gbrammer/grizli (commit `c8c9d325`, 2026-10-26) | read |
| other repositories | https://github.com/gbrammer/reprocess_wfc3 , `/tristars` , `/wfc3dash` , `/threedhst` ; https://github.com/charliekilpatrick/hst123 | README and metadata |

**STScI documentation**

| Key | URL | Status |
| --- | --- | --- |
| ACS DHB (v14.0, Jan 2026) | https://hst-docs.stsci.edu/acsdhb | summarized |
| ACS IHB (v25.0, Dec 2025) | https://hst-docs.stsci.edu/acsihb | summarized |
| WFC3 DHB (v6.0, Jun 2024) | https://hst-docs.stsci.edu/wfc3dhb | summarized |
| WFC3 IHB (v18.0, Dec 2025) | https://hst-docs.stsci.edu/wfc3ihb | summarized |
| DrizzlePac Handbook | https://hst-docs.stsci.edu/drizzpac | summarized |
| HST Data Handbook §2.2 (rootnames) | https://hst-docs.stsci.edu/hstdhb/2-hst-file-names/2-2-rootnames | summarized |
| Cycle 34 Primer | https://hst-docs.stsci.edu/hsp/the-hubble-space-telescope-primer-for-cycle-34 | summarized |
| Reduced-gyro mode tips | https://hst-docs.stsci.edu/hsp/hst-general-science-policies/reduced-gyro-mode-tips-and-resources | summarized |
| HST CRDS, CRDS user guide | https://hst-crds.stsci.edu/ ; https://hst-crds.stsci.edu/static/users_guide/ | summarized |
| ACS zeropoints page | https://www.stsci.edu/hst/instrumentation/acs/data-analysis/zeropoints | summarized |
| WFC3/IR photometric calibration | https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/photometric-calibration/ir-photometric-calibration | summarized |
| WFC3 persistence page; anomalies page | https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/ir-persistence ; https://www.stsci.edu/hst/instrumentation/wfc3/performance/anomalies | summarized |
| ACS ISR 2022-03 (= WFC3 ISR 2022-06; archive astrometry and HAP) | https://www.stsci.edu/files/live/sites/www/files/home/hst/instrumentation/acs/documentation/instrument-science-reports-isrs/_documents/isr2203.pdf | read (PDF text) |
| ACS ISR 2022-08 (satellite trails), 2025-01 (roll drift), 2014-01 (post-flash) | same directory, `isr2208.pdf`, `isr2501.pdf`, `isr1401.pdf` | read (PDF text) |
| WFC3 ISR 2016-16 (variable IR background) | https://www.stsci.edu/files/live/sites/www/files/home/hst/instrumentation/wfc3/documentation/instrument-science-reports-isrs/_documents/2016/WFC3-2016-16.pdf | read (PDF text) |
| Other ISRs cited through the handbooks (ACS 2018-04, 2024-07, 2025-02; WFC3 2019-01, 2021-01, 2021-10, 2024-06, 2024-13, 2025-08) | ISR listing pages | abstract or handbook citation only |
| WFC3 STAN 48 | STScI newsletter | summarized |
| stenv | https://github.com/spacetelescope/stenv | read (`environment.yaml`) |

**Papers and survey pages**

| Key | URL | Status |
| --- | --- | --- |
| Koekemoer et al. 2007 (COSMOS ACS) | https://arxiv.org/abs/astro-ph/0703095 | read |
| Koekemoer et al. 2011 (CANDELS) | https://arxiv.org/abs/1105.3754 | read |
| Grogin et al. 2011 | https://arxiv.org/abs/1105.3753 | read in part |
| Skelton et al. 2014 (3D-HST) | https://arxiv.org/abs/1403.3689 | read in part |
| Illingworth et al. 2013 (XDF); XDF and HLF pages | https://arxiv.org/abs/1305.1931 ; https://archive.stsci.edu/prepds/xdf/ ; https://archive.stsci.edu/prepds/hlf/ | read |
| Illingworth et al. 2016 (HLF) | https://arxiv.org/abs/1606.00841 | abstract only (PDF could not be parsed) |
| Whitaker et al. 2019 (HLF GOODS-S) | https://arxiv.org/abs/1908.05682 | read |
| Lotz et al. 2017 (Frontier Fields) | https://arxiv.org/abs/1605.06567 | read |
| Steinhardt et al. 2020 (BUFFALO) | https://arxiv.org/abs/2001.09999 | read in part |
| Coe et al. 2019 (RELICS); RELICS page | https://arxiv.org/abs/1903.02002 ; https://archive.stsci.edu/hlsp/relics | read in part |
| Postman et al. 2012 (CLASH) | https://arxiv.org/abs/1106.3328 | read in part |
| Momcheva et al. 2017; Mowla et al. 2019, 2022 (DASH) | https://arxiv.org/abs/1603.00465 ; https://arxiv.org/abs/1808.04379 ; https://arxiv.org/abs/2206.01156 | read; abstracts for the last two in part |
| Kokorev et al. 2022 (CHArGE); Valentino et al. 2023 (DJA) | https://arxiv.org/abs/2207.07125 ; https://arxiv.org/abs/2302.10936 | read in part |
| DJA pages | https://dawn-cph.github.io/dja/imaging/v7/ ; https://dawn-cph.github.io/dja/blog/2023/07/18/image-data-products/ | read in part |
| Bagley et al. 2023 (CEERS); CEERS HST release | https://arxiv.org/abs/2211.02495 ; https://ceers.github.io/hdr1.html | read |
| Franco et al. 2025 (COSMOS-Web); Shuntov et al. 2025 | https://arxiv.org/abs/2506.03256 ; https://arxiv.org/abs/2506.03243 | read |
| Bezanson et al. 2024; Weaver et al. 2024; Suess et al. 2024 (UNCOVER, MegaScience) | https://arxiv.org/abs/2212.04026 ; https://arxiv.org/abs/2301.02671 ; https://arxiv.org/abs/2404.13132 | read |
| Rieke et al. 2023; JADES DR5 papers | https://arxiv.org/abs/2306.02466 ; https://arxiv.org/abs/2601.15954 ; https://arxiv.org/abs/2601.15956 | read in part |
| Merlin et al. 2024 (ASTRODEEP-JWST) | https://arxiv.org/abs/2409.00169 | read in part |
| Finner et al. 2023 (NIRWL) | https://arxiv.org/abs/2301.07725 ; https://archive.stsci.edu/hlsp/candelsnirwl | read |
| Davis et al. 2007 (AEGIS); Giavalisco et al. 2004 (GOODS); Beckwith et al. 2006 (HUDF) | https://arxiv.org/abs/astro-ph/0607355 ; https://arxiv.org/abs/astro-ph/0309105 ; https://arxiv.org/abs/astro-ph/0607632 | read |
| Fruchter & Hook 2002 (drizzle) | https://arxiv.org/abs/astro-ph/9808087 | read |
| NEXUS; VENUS; JWST program pages | https://arxiv.org/abs/2408.12713 ; https://jwst-venus.github.io/ ; https://www.stsci.edu/jwst-program-info/program/?program=5105 (also 9263, 6882) | read |
| CANDELS, Frontier Fields, BUFFALO, UVCANDELS, COSMOS-DASH, 3D-DASH archive pages | https://archive.stsci.edu/hlsp/candels (and `/frontier`, `/buffalo`, `/uvcandels`, `/cosmos-dash`, `/3d-dash`) | read |
| COSMOS ACS v2.0 at IRSA | https://irsa.ipac.caltech.edu/data/COSMOS/images/acs_mosaic_2.0/ | read |
| MINERVA DR0 | https://minerva.colorado.edu/data-release.html | read |
| SKYSURF X | https://arxiv.org/abs/2507.05323 | abstract |

**Unable to access**

- Unable to access this page: https://www.nature.com/articles/s41550-023-01903-3 (Kruk et al. 2023, satellite trails; login redirect). Its numbers are not used here.
- Unable to access this page: https://arxiv.org/pdf/1606.00841 as text (Illingworth et al. 2016).
- Unable to access this page: https://pypi.org/project/hst123/ (load error; GitHub used instead).
- Unable to access this page: https://acstools.readthedocs.io/en/latest/acs_destripe_plus.html (404; repository source used instead).
- Unable to access this page: the WFC3 ISR 2022-06 PDF (over the fetch size limit; the identical ACS ISR 2022-03 was read).
- Unable to access these pages: `cosmos2025.iap.fr` and the CDS HiPS entry for the COSMOS-Web F814W mosaic.
- The MAST persistence search form (https://archive.stsci.edu/prepds/persist/search.php) loads but its working state is unclear; no query was run.

The full research notes behind this document (per-claim tags, line references into the upstream clones, and longer source tables) are kept unedited in `docs/hst/research-notes/`: `archive_standard.md`, `community_pipelines.md`, `artifacts.md`, `fields.md`. They are working notes written by the research sub-agents, not reviewed line by line; paths in them point at a scratch directory that no longer needs to exist.
