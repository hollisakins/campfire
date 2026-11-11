# HST archive standard (ACS/WFC + WFC3/IR imaging): what software produces what MAST serves, and the conventions that differ from NIRCam

Research date: 2026-11-11. Scope: imaging only, ACS/WFC and WFC3/IR.

**Tags.** **[verified]** = I read the cited source in this session (docs page, PDF text, or upstream source file). **[inferred]** = from memory or indirect evidence, not read this session. **[unknown]** = could not be established. **[derived]** = arithmetic I did from verified inputs.

**Path shorthand.** `UP/` = `/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/upstream/` (shallow clones, depth 50, made this session).

**Docs fetch caveat.** hst-docs / stsci.edu pages were read through a fetch tool that summarizes the page; numbers and tables below were requested verbatim, but a page-level re-check is cheap for anything that becomes load-bearing. ACS ISR 2022-03 was read as extracted PDF text (`.../scratchpad/research/isr2203_compact.txt`); its Tables 1 and 4 were reconstructed from column-ordered text and checked by row sums.

## 0. Upstream software currency (recorded 2026-11-11)

| Repo | Latest release tag | Release date | HEAD of default branch (last commit) | Notes |
|---|---|---|---|---|
| spacetelescope/drizzlepac | 3.11.0 | 28-Apr-2026 (`UP/drizzlepac/CHANGELOG.rst` L47; PyPI upload 2026-04-28) | 6d11d26, 2026-10-29 | 3.12.0 "(TBD)" in progress. **[verified]** |
| spacetelescope/hstcal | 3.3.0 | tag commit 858b94f, 2026-11-04 | 733bd3e, 2026-11-10 | calacs `ACS_CAL_VER "10.5.0 (16-Mar-2026)"` (`UP/hstcal/pkg/acs/include/acsversion.h` L5); calwf3 `WF3_CAL_VER "3.8.0 (Aug-24-2026)"` (`UP/hstcal/pkg/wfc3/include/wf3version.h` L7). **[verified]** |
| spacetelescope/stwcs | 1.7.7 | 2026-03-05 | 8b8bcce, 2026-11-11 | 1.7.8 "(TBD)". **[verified]** |
| spacetelescope/drizzle | 3.0.0 | 2026-09-23 (CHANGES.rst; PyPI upload 2026-09-24) | 421ae2c, 2026-11-06 | 3.1.0 unreleased. **[verified]** |
| spacetelescope/acstools | 3.8.2 | PyPI 2026-06-24 | 69339bb, 2026-11-02 | **[verified]** |
| spacetelescope/wfc3tools | 1.6.1 | PyPI 2026-02-06 | 4d51241, 2026-11-09 | **[verified]** |

All six repositories are actively maintained (commits within the last two weeks). **[verified]** (`git log -1`, `git ls-remote --tags`)

---

## 1. Calibration stage per instrument

### 1.1 Who does what

- `calacs` (ACS) and `calwf3` (WFC3) are C programs in the HSTCAL package; Python packages `acstools` / `wfc3tools` are thin `subprocess` wrappers around `calacs.e` / `calwf3.e`. **[verified]** (`UP/hstcal/README.md`; `UP/acstools/acstools/calacs.py` L16–105)
- Reference files are found through environment variables: `jref` (ACS), `iref` (WFC3). **[verified]** (`UP/hstcal/README.md`)
- The drizzle stage is a separate Python step, `drizzlepac.runastrodriz`, gated by the `DRIZCORR` switch. **[verified]** (`UP/drizzlepac/doc/source/mast_data_products/pipeline-astrometric-calibration-description.rst` L116–126)

### 1.2 calacs for ACS/WFC — steps in execution order

Source: ACS Data Handbook §3.4, https://hst-docs.stsci.edu/acsdhb/chapter-3-acs-calibration-pipeline/3-4-calacs-processing-steps **[verified]**; order of the CCD block cross-checked in `UP/hstcal/pkg/acs/lib/acsccd/doccd.c` (doDQI L247, doBias L274, to_electrons L298, doFullWellSat L374, doNoise L389, sink L461) **[verified]**.

| # | Task | Function | Switch | Reference file | What it does |
|---|---|---|---|---|---|
| 1 | acsccd | doDQI | `DQICORR` | `BPIXTAB`, `CCDTAB` | Initialize DQ from bad-pixel table; A-to-D saturation check (2048) |
| 2 | acsccd | doBias | `BIASCORR` | `BIASFILE` | Subtract bias image |
| 3 | acsccd | toElectrons | (none) | `CCDTAB` | **Multiply by per-amp gain: DN → electrons, this early** |
| 4 | acsccd | doBlev | `BLEVCORR` | `OSCNTAB` | Subtract overscan bias level; post-SM4 full-frame also bias-shift, cross-talk, destripe |
| 5 | acsccd | doFullWellSat | (none) | `SATUFILE` | Flag full-well saturation (256) from a saturation image (calacs ≥ 10.3.0, `UP/hstcal/pkg/acs/CHANGELOG.txt`) |
| 6 | acsccd | doNoise | (none) | `CCDTAB` | Initialize ERR |
| 7 | acsccd | doSink | `SINKCORR` | `SNKCFILE` | Flag sink pixels and trails (1024) |
| 8 | acscte | — | `PCTECORR` | `PCTETAB` | Pixel-based CTE correction (parallel + serial, "Generation 3", calacs 10.4.0, May 2024). Default `OMIT` for subarrays per DHB; calacs 10.5.0 (Mar 2026) enables serial CTE for post-SM4 subarrays (`UP/hstcal/pkg/acs/CHANGELOG.txt` L1–2) |
| 9 | acsrej | — | `CRCORR` | `CRREJTAB` | Combine CR-SPLIT sub-exposures → CRJ/CRC (flags 8192) |
| 10 | acs2d | doDark | `DARKCORR` | `DARKFILE` / `DRKCFILE` (CTE-corrected dark) | Subtract scaled dark |
| 11 | acs2d | doFlash | `FLSHCORR` | `FLSHFILE` | Subtract post-flash |
| 12 | acs2d | doFlat | `FLATCORR` | `PFLTFILE`, `LFLTFILE`, `DFLTFILE`, `CFLTFILE` | Divide by flat |
| 13 | acs2d | doShad | `SHADCORR` | `SHADFILE` | Shutter shading; currently `OMIT` |
| 14 | acs2d | doPhot | `PHOTCORR` | `IMPHTTAB` | Write `PHOTMODE`, `PHOTFLAM`, `PHOTZPT`, `PHOTPLAM`, `PHOTBW` (keywords only; pixels untouched) |
| 15 | acs2d | doStat | (none) | — | Header statistics |
| — | acssum | — | `RPTCORR` | — | SBC only; not WFC |
| — | (python) | runastrodriz | `DRIZCORR` | `MDRIZTAB`, `IDCTAB`, `NPOLFILE`, `D2IMFILE` | Distortion/WCS update, alignment, drizzle |

Full switch list in the primary header (ACS DHB Table 2.6): `STATFLAG, WRTERR, DQICORR, ATODCORR, BLEVCORR, BIASCORR, FLSHCORR, CRCORR, EXPSCORR, SHADCORR, GLINCORR, LFLGCORR, PCTECORR, DARKCORR, FLATCORR, PHOTCORR, RPTCORR, DRIZCORR, SINKCORR`. **[verified]** https://hst-docs.stsci.edu/acsdhb/chapter-2-acs-data-structure/2-4-headers-and-keywords

ERR initialization (CCD, non-bias): σ = sqrt((SCI − bias) + σ_RN²), i.e. Poisson of the *observed* signal plus read noise, in electrons. **[verified]** (ACS DHB §3.4)

### 1.3 calwf3 for WFC3/IR — steps in execution order

Sources: WFC3 Data Handbook §3.3, https://hst-docs.stsci.edu/wfc3dhb/chapter-3-wfc3-data-calibration/3-3-ir-data-calibration-steps **[verified]**; call order in `UP/hstcal/pkg/wfc3/lib/wf3ir/doir.c` L206–315: `doDQIIR → doZsigIR → doBlevIR → doZoffIR → doNoisIR → doNlinIR → doDarkIR → photcalc → doUnitIR → cridcalc → doFlatIR` **[verified]**.

| # | Switch | Reference | What it does |
|---|---|---|---|
| 1 | `DQICORR` | `BPIXTAB` | Populate DQ from bad-pixel table |
| 2 | `ZSIGCORR` | `NLINFILE` (DHB: `DARKFILE` no longer used) | Estimate signal already present in the zeroth read; flag 2048 |
| 3 | `BLEVCORR` | `OSCNTAB` | Subtract sigma-clipped mean of reference pixels from every read (`MEANBLEV`) |
| 4 | `ZOFFCORR` | — | Subtract zeroth read from all reads |
| 5 | (`NOISCORR`, not user-settable) | `CCDTAB` | Initialize ERR: `sqrt(rn² + |signal·gain|)/gain` per read, in DN (`UP/hstcal/pkg/wfc3/lib/wf3ir/noiscalc.c` L133–134) |
| 6 | `NLINCORR` | `NLINFILE` | Polynomial non-linearity correction; flag saturated reads (256) |
| 7 | `DARKCORR` | `DARKFILE` | Subtract dark ramp matched read-by-read to the sample sequence (`MEANDARK`) |
| 8 | `PHOTCORR` | `IMPHTTAB` | Write photometry keywords selected by `PHOTMODE` |
| 9 | `UNITCORR` | — | Divide SCI and ERR by TIME → count rate |
| 10 | `CRCORR` | `CRREJTAB` | Up-the-ramp linear fit with iterative outlier rejection (`CRSIGMAS`, default 4); produces the single-imset FLT; sets FLT ERR from the fit uncertainty, fills SAMP and TIME |
| 11 | `FLATCORR` | `PFLTFILE`, `LFLTFILE`, `DFLTFILE` | Divide by flat **and apply gain** (mean over amps): DN/s → e⁻/s |
| 12 | (stats) | — | Header statistics |
| — | `RPTCORR` | `CRREJTAB` | `wf3rej` combination of REPEAT-OBS associations (CR-SPLIT does not exist for IR) |
| — | `DRIZCORR` | `MDRIZTAB`, `IDCTAB` | runastrodriz |

IMA: every read after all read-level calibrations, each read being "that read minus the zeroth read" (not adjacent differences). FLT: the ramp-fit image; "no CR or SPIKE flags" are carried into the FLT DQ — CR hits (8192) are flagged in the IMA only. **[verified]** (WFC3 DHB §3.3, §2.2 Table 2.7)

**Known failure of the default IR FLT.** With time-variable background (He I 1.083 µm airglow in F105W/F110W/G102, up to 6× zodi; scattered earthlight on the leftmost ~200–400 columns) the linear-ramp assumption fails; the CR thresholds trip, and "only one or two reads out of 15 are used to form the flt". The handbook states: "default flt products generated from ramps with time-variable background are not recommended for scientific analysis." Fixes operate on the IMA/RAW (last-minus-first with `CRCORR=OMIT`, flatten-ramp, or excluding reads) and require rerunning calwf3. **[verified]** https://hst-docs.stsci.edu/wfc3dhb/chapter-7-wfc3-ir-sources-of-error/7-10-time-variable-background

### 1.4 Products, units, extensions

Sources: ACS DHB Table 2.1 (https://hst-docs.stsci.edu/acsdhb/chapter-2-acs-data-structure/2-1-types-of-acs-files), WFC3 DHB Table 2.1 (https://hst-docs.stsci.edu/wfc3dhb/chapter-2-wfc3-data-structure/2-1-types-of-wfc3-files), ACS DHB §2.2, WFC3 DHB §2.2. **[verified]**

| Suffix | ACS/WFC | WFC3/IR |
|---|---|---|
| `_raw` | DN | DN (all reads) |
| `_ima` | — | calibrated MULTIACCUM stack, **e⁻/s** |
| `_blv_tmp` / `_blc_tmp` | overscan-trimmed, **electrons** (ACS converts to e⁻ before this point) | — (UVIS only; there DN) |
| `_flt` | fully calibrated single exposure, **electrons** | fully calibrated ramp fit, **e⁻/s** (scan mode: e⁻) |
| `_flc` | FLT + pixel-based CTE correction, **electrons** | **does not exist** (no CTE in HgCdTe) |
| `_crj` / `_crc` | CR-SPLIT combination, electrons; `crc` = CTE-corrected | `_crj` only (REPEAT-OBS), e⁻/s |
| `_drz` / `_drc` | AstroDrizzle product, **e⁻/s**; `drc` built from FLC | `_drz` only, **e⁻/s** |
| `_asn` | association table: columns `MEMNAME`, `MEMTYPE` (`EXP-CR1`, `EXP-DTH`, `PROD-CRn`, `PROD-DTH`), `MEMPRSNT` | same |

- **FLT vs FLC**: identical except FLC has the pixel-based CTE correction applied (ACS/WFC, WFC3/UVIS). Dark reference for the CTE branch is the CTE-corrected dark (`DRKCFILE`). **[verified]**
- `BUNIT` records the unit (`ELECTRONS`, `ELECTRONS/S`, `COUNTS`, `COUNTS/S`). **[verified]** (WFC3 DHB §3.3 Table 3.4; ACS DHB Table 2.7)
- **ACS/WFC layout** (one file, two chips): `[SCI,1]/[ERR,1]/[DQ,1]` = **WFC2** (lower chip), `[SCI,2]/[ERR,2]/[DQ,2]` = **WFC1** (upper chip); each 4096 × 2048, SCI/ERR float32, DQ int16; followed by `D2IMARR`×4, `WCSDVARR`×4, `WCSCORR` table and `HDRLET` extensions. **[verified]** (ACS DHB §2.2; listing in `UP/drizzlepac/doc/source/mast_data_products/astrometry.rst` L32–54)
- Interchip gap 2.5″; FOV 202″ × 202″; gain default 2.0 e⁻/DN; read noise 3.75–5.65 e⁻ by amp. **[verified]** (https://hst-docs.stsci.edu/acsihb/chapter-10-imaging-reference-material/10-4-geometric-distortion-in-acs, https://hst-docs.stsci.edu/acsihb/chapter-4-detector-performance/4-2-the-ccds)
- **WFC3/IR layout**: single 1024 × 1024 detector with a 5-pixel reference-pixel border; calibrated arrays 1014 × 1014. FLT has one imset of five extensions: `SCI` (e⁻/s), `ERR`, `DQ` (int16), `SAMP` (number of valid samples per pixel), `TIME` (effective integration time per pixel). IMA holds N+1 imsets in **reverse time order** (`[SCI,1]` = last read, last imset = zeroth read); header `NSAMP` counts the zeroth read. **[verified]** (WFC3 DHB §2.2)
- DRZ/DRC: `PRIMARY`, `SCI`, `WHT`, `CTX` (int32 bit-mask of contributing inputs), `HDRTAB` (per-input keyword table). **[verified]** (ACS DHB §2.2)

---

## 2. Reference file selection (HST CRDS)

| Topic | HST | Source / tag |
|---|---|---|
| Server | https://hst-crds.stsci.edu | **[verified]** |
| Current context | `hst_1372.pmap`, flagged "latest", start date 2026-11-09; `hst_acs_0579.imap`, `hst_wfc3_0664.imap`; delivery note "new ACS WFC bias, dark, CTE-dark, and sink pixel reference files" | **[verified]** https://hst-crds.stsci.edu/ |
| Terminology | "operational" context is now called "latest" | **[verified]** same page |
| How references reach the calibration code | Not at run time. `crds bestrefs` writes reference file *names* into header keywords of the RAW file (`BIASFILE`, `DARKFILE`, `PFLTFILE`, `IDCTAB`, `IMPHTTAB`, `MDRIZTAB`, …, prefixed `jref$` / `iref$`); calacs/calwf3 then resolve the `jref` / `iref` environment variables. "HST calibration software accesses reference files indirectly through environment variables." | **[verified]** https://hst-crds.stsci.edu/static/users_guide/environment.html ; ACS DHB §3.5 |
| Commands | `crds bestrefs --files *raw.fits --update-bestrefs` ; with download: `--sync-references=1` ; with pinned context: `--new-context hst_NNNN.pmap` | **[verified]** https://hst-crds.stsci.edu/static/users_guide/basic_use.html |
| Env | `CRDS_SERVER_URL=https://hst-crds.stsci.edu`, `CRDS_PATH=$HOME/crds_cache`; flat cache: `jref=$CRDS_PATH/references/hst/`; by-instrument cache (default for new personal HST caches): `jref=$CRDS_PATH/references/hst/jref/`, `iref=.../iref/` | **[verified]** environment.html; ACS DHB §3.5 |
| `CRDS_CONTEXT` | Works for HST too (docs example `export CRDS_CONTEXT=hst_1008.pmap`; default `{observatory}-latest`); "does not override command line switches" | **[verified]** environment.html |
| Does MAST reprocess? | Yes, the archive is a store of calibrated products that is reprocessed: "Data will be reprocessed, as needed when updated calibration reference files" are received or algorithms improve; a request for a dataset queued for reprocessing can take the existing copy or wait. | **[verified]** Cycle 34 Primer, https://hst-docs.stsci.edu/hsp/the-hubble-space-telescope-primer-for-cycle-34/hst-primer-data-processing-and-the-hst-data-archive |
| "On-the-fly" wording | The DrizzlePac Handbook §5.3 still says "Only ACS and WFC3 data are currently being processed using On-the-Fly Calibration (OTFR)". The Primer and data handbooks describe stored products that are reprocessed. Sources disagree in wording; operationally a download returns whatever the last (re)processing produced. | **[verified]** both pages |
| Churn | ACS/WFC dark, CTE-dark, bias, sink files are delivered after every anneal; users retrieving data soon after observation are "advised to wait a few more weeks" for the right darks. | **[verified]** ACS DHB §3.1 |
| Does a product header record the CRDS context? | Not found: no `CRDS_CTX` / `CSYS_VER` string in hstcal, drizzlepac, stwcs, acstools, wfc3tools sources. The reference-file keywords and `CAL_VER` are the record. | grep of `UP/` **[verified]**; whether the DMS layer adds a context keyword **[unknown]** |

**Pinning for reproducibility.** (a) Record the header reference-file names plus `CAL_VER` of what MAST delivered (reproducible only as long as you keep the files), or (b) start from RAW, run `crds bestrefs --new-context hst_NNNN.pmap --update-bestrefs --sync-references=1`, then calacs/calwf3 at a pinned hstcal version. ACS DHB §3.5: "By default, the Archive provides calibrated images processed with the latest available reference files at processing time." **[verified]**

**Comparison with JWST.** JWST: "CRDS is fully integrated with the CAL code and operates transparently"; each step asks CRDS for its reference at run time under `CRDS_CONTEXT`. HST: selection is a separate header-rewriting step, and the pipeline itself never talks to CRDS. **[verified]** (basic_use.html) A second difference: HST reference selection is strongly date-dependent (per-anneal darks/bias), so a context pin matters most for *recent* ACS data. **[verified]** (ACS DHB §3.1)

---

## 3. Drizzle / combination stage (AstroDrizzle)

### 3.1 Steps

Sources: DrizzlePac Handbook §5.2 https://hst-docs.stsci.edu/drizzpac/chapter-5-drizzlepac-software-package/5-2-astrodrizzle-the-new-drizzle-workhorse **[verified]**; software defaults in `UP/drizzlepac/drizzlepac/pars/astrodrizzle.cfg` **[verified]**.

| Step | What | Key parameters (software default) |
|---|---|---|
| 0 | Set-up; reset old CR bits | `resetbits=4096`, `crbit=4096`, `proc_unit=native` |
| 1 Static mask | Flag pixels abnormally low in each image (below mode − `static_sig`·rms), combine into a per-chip mask | `static=True`, `static_sig=4.0` |
| 2 Sky | Per-exposure sky (`MDRIZSKY`); subtraction is applied to copies during drizzling, not to the FLT | `skysub=True`, `skymethod=localmin` (options `localmin`, `globalmin`, `match`, `globalmin+match`), `skystat=median`, `skywidth=0.1`, `skyclip=5`, `skylsigma=skyusigma=4.0` |
| 3 Drizzle separate | Each input drizzled alone onto the common output frame | `driz_sep_kernel=turbo`, `driz_sep_pixfrac=1.0`, `driz_sep_bits=0`, `driz_sep_wt_scl=exptime` |
| 4 Median | Median of the single-drizzled images | `combine_type=minmed`, `combine_nsigma="4 3"`, `combine_maskpt=0.3`, `combine_grow=1` |
| 5 Blot | Median resampled back to each input's distorted frame | `blot_interp=poly5`, `blot_addsky=True` |
| 6 driz_cr | Flag where `|data − blot| > scale × deriv + SNR × noise`, two passes | `driz_cr_snr="3.5 3.0"`, `driz_cr_scale="1.2 0.7"`, `driz_cr_grow=1`, `driz_cr_ctegrow=0` |
| 7 Final drizzle | Combine originals with DQ + static + CR masks | `final_wht_type=EXP`, `final_kernel=square`, `final_pixfrac=1.0`, `final_bits=0`, `final_units=cps`, `final_wt_scl=exptime`, `final_scale=None` (native), `final_rot=None` (detector orientation) |

`skymethod='match'` computes only *relative* offsets between overlapping exposures (sky is equalized, not removed). **[verified]** (`UP/drizzlepac/drizzlepac/sky.py` L292–363; ISR ACS 2022-03 §6.2)

### 3.2 Weight types (`final_wht_type`)

`UP/drizzlepac/drizzlepac/astrodrizzle.py` L843–894 and `UP/drizzlepac/drizzlepac/imageObject.py` L737–850 **[verified]**:

| Type | Per-pixel input weight | Meaning | Caveat |
|---|---|---|---|
| `EXP` (default; what the archive uses) | `EXPTIME × mask` (a scalar per chip, not the IR `TIME` array: `buildEXPmask` uses `_exptime`) | Output WHT ≈ effective exposure-time map | Optimal only for source-Poisson-dominated data; ignores sky, read noise, flat |
| `ERR` | `(EXPTIME / ERR)² × mask`, `wt_scl = 1/scale⁴` | Inverse variance from the calibrated ERR array, which includes source Poisson noise | "minimum-variance estimator but not unbiased, typically biasing sources faint by about one to two percent" (Handbook §5.2); docstring advises "extreme caution" |
| `IVM` | user map, or auto: `flat² / (dark + sky·flat + RN²) × mask`, `wt_scl = EXPTIME²/scale⁴` | Background-only inverse variance (no source term) — the SExtractor-style weight | Auto-generation needs flat and dark reference files on disk |

- Handbook variance model for ACS/WFC3: `Var = [f(D+B)/g + σ²f²]/t²`, weight `W = 1/(Var × scale⁴)`. **[verified]** https://hst-docs.stsci.edu/drizzpac/chapter-3-description-of-the-drizzle-algorithm/3-3-weight-maps-and-correlated-noise
- Output SCI is in e⁻/s for both instruments (`final_units=cps`); WHT with `EXP` is in seconds. **[verified]** (ACS/WFC3 DHB tables; ISR ACS 2022-03 §4.1 "weight extension … effective exposure time map")

### 3.3 What the archive actually uses

| Product | Scale | Orientation | pixfrac / kernel | Weight | `final_bits` | Sky | Source |
|---|---|---|---|---|---|---|---|
| Standard pipeline DRZ/DRC (per association or singleton) | native: ACS/WFC 0.050″, WFC3/IR 0.128″ | detector frame | "native detector plate scale and a PIXFRAC of 1.0"; square | `EXP` | ACS/WFC: 336 (=16+64+256) since `16r12191j_mdz.fits`, June 2017. WFC3/IR: stable hot pixels good since `3562021pi_mdz.fits` (May 2019); numeric value **[unknown]** | ACS: `localmin` (as of Aug 2022); WFC3: `match` since 2019 | WFC3 DHB §4.2; ISR ACS 2022-03 §4.1, §6.2, Table 9; DrizzlePac notebook https://spacetelescope.github.io/hst_notebooks/notebooks/DrizzlePac/Initialization/Initialization.html ; WFC3 STAN 29 **[verified]** |
| HAP SVM | native (same table) | **north up** (`final_rot=0.0`) | ACS/WFC 1.0 (n1, n2, total), **0.8** for ≥6 inputs; IR 1.0; square | `EXP` | ACS/WFC 336 (65535 for single-image products); IR **528** (=512+16) | `match` | `UP/drizzlepac/drizzlepac/pars/hap_pars/svm_parameters/acs/wfc/*.json`, `.../wfc3/ir/*.json` **[verified]** |
| HAP MVM | **0.04″** (all three detectors) and, IR only, **0.12″** | north up, projection-cell tangent plane | 1.0; square | `EXP` | ACS/WFC 80 (=16+64) for n2; IR 528 | `match` only; `driz_separate/median/blot/driz_cr = false`, `resetbits=0` | `.../mvm_parameters/...json`; `UP/drizzlepac/drizzlepac/haputils/cell_utils.py` L40 **[verified]** |

- Pipeline parameters come from the `MDRIZTAB` reference file: one row per filter and per input-count range (`numimages`); the code picks the last row with `numimages ≤ N`. **[verified]** (`UP/drizzlepac/drizzlepac/mdzhandler.py` L55–68; Handbook §5.3)
- The DrizzlePac Handbook §5.3 says associations with more than six images "are drizzled to a finer scale with a different pixfrac"; the WFC3 DHB §4.2 says pipeline products are at native scale with pixfrac 1.0. Sources disagree; the SVM JSON (pixfrac 0.8 at ≥6, native scale) is the only one I could check against code. **[verified both statements; which holds for current MDRIZTABs is unknown]**

---

## 4. Hubble Advanced Products (HAP)

| | Single-Visit Mosaic (SVM) | Multi-Visit Mosaic (MVM) |
|---|---|---|
| Definition | All exposures of one visit (same guide stars), one detector; products at exposure, filter and "total" (detection) level | All public exposures in a 0.2° sky cell per detector+filter "layer", any date or program |
| Software | `drizzlepac.runsinglehap` → `hapsequencer` | `drizzlepac.runmultihap` → `hapmultisequencer.run_mvm_processing` |
| Start | 17 Dec 2020; source catalogs from 23 Nov 2021; WFPC2 added Dec 2023 | 26 Apr 2022 per MAST newsletter and Outerspace page; **26 May 2022** per ISR ACS 2022-03 §4.2 (sources disagree) |
| Alignment | relative fit across all filters of the visit, then one global fit to the first catalog that works: GAIAeDR3 → GSC242 → 2MASS; `WCSNAME` ends `-FIT_SVM_<cat>` | **none**: uses each visit's SVM WCS as-is |
| Grid | native scale, north up, common "metawcs" per detector per visit | PS1-style tessellation: 2644 projection cells (0–2643, south pole to north pole), 4.2° × 4.2° on 4° declination rings; sky cells ~0.2° on a side; all sky cells of a projection cell share one tangent-plane WCS |
| Scale | ACS/WFC 0.050″, WFC3/UVIS 0.040″, WFC3/IR 0.128″ | 0.04″ fine; WFC3/IR also 0.12″ coarse ("much less noisy than the super-sampled data") |
| CR rejection | full AstroDrizzle sequence | none; trusts 4096 flags from SVM FLT/FLC |
| Detectors | ACS WFC/HRC/SBC, WFC3 UVIS/IR, WFPC2 | ACS/WFC, WFC3/UVIS, WFC3/IR ("Not produced for ACS HRC, ACS/SBC"); only CTE-corrected inputs for the CCDs; grism, ramp, moving-target, non-public excluded |
| File names | `hst_<propid>_<visit>_<instr>_<det>_<filter>_<ipppss[oo]>_dr[cz].fits` (+ `_fl[ct].fits`, `_hlet.fits`, `_point-cat.ecsv`, `_segment-cat.ecsv`) | `hst_skycell-p<PPPP>x<XX>y<YY>_<instr>_<det>_<filter>_<all|coarse-all>_dr[cz].fits` |
| MAST | `Project = HAP` | same; cutout API https://mast.stsci.edu/hapcut/ |

Sources: `UP/drizzlepac/doc/source/mast_data_products/hap_introduction.rst`, `svm_processing.rst`, `mvm_processing.rst`, `mvm_products.rst`; ISR ACS 2022-03 / WFC3 2022-06 §4, Table 3, Table 9; https://archive.stsci.edu/contents/newsletters/may-2022/multi-visit-mosaics-from-hubble-now-available **[verified]**

**Sky-cell numbers disagree between sources**: drizzlepac docs say 21 × 21 sky cells per projection cell, ~18000 px (example WCS NAXIS 21954); the ISR says 20 × 20 cells spaced 0.2°, oversized to 0.244° (21953 px at 0.04″). **[verified both]**

**Stated limitations (quotes).**
- MVMs "may have photometric errors of several percent or systematic alignment errors when combining visits with different catalog solutions. We therefore recommend these to be used as 'discovery images' … and not for precise photometry." (ISR abstract) Summary: "photometric errors up to 5% … due to temporal changes in the inflight sensitivity … not accounted for when combining each layer." **[verified]**
- "Initial testing shows that some visits in the same sky cell have global offsets of several pixels … slightly smeared drizzled data products when combining images with different types of WCS solutions." (ISR §4.2) **[verified]**
- Proper motion: each visit is fit at its own epoch, so high-PM stars smear or double in MVMs. Trailed (loss-of-lock) exposures are not reliably excluded. (`mvm_products.rst` L175–238) **[verified]**
- SVM: "Drizzled products may not align with other visits"; north-up rotation "may add correlated noise"; `EXP` weighting compromises saturated stars when short and long exposures are mixed. (ISR Table 3, §4.1) **[verified]**

**Current status (2024–2026).**
- Cycle 34 Primer (holdings figure dated 1 Nov 2025) lists SVM (Dec 2020) and MVM (Apr 2022) as available, says HAP "includes improvements to astrometry and exposure alignment by using the Gaia DR3 catalog" and that HAP products are regenerated when inputs change; "HLA has been replaced by HAP". No statement that MVM production stopped. **[verified]**
- MVM code is still being changed: 3.11.0 (Apr 2026) "Added Proposal ID to MVM header keywords [#2099]"; 3.12.0 (unreleased) "Improved MVM guide star failure check to reintroduce exposures that were incorrectly excluded [#2101]". **[verified]** (`UP/drizzlepac/CHANGELOG.rst` L35, L54)
- One contrary hint: drizzlepac 3.9.0 (16-Dec-2024) is annotated "**This version used by operations but does not generate HAP products (SVM/MVM).**" (`CHANGELOG.rst` L232). Whether later operational builds resumed HAP generation, and how current any given sky cell is, I could not establish. **[unknown]**
- No MAST announcement about MVM changes in 2024–2026 was found in two web searches. **[unknown]**
- A community project produced its own 38,027 single-visit and 7,893 multi-visit ACS/WFC3 mosaics "using nonstandard drizzling methods" (SKYSURF X, arXiv:2507.05323; HLSP released 2026-05-15) rather than using HAP. **[verified]** (abstract)

---

## 5. Astrometry in the archive

### 5.1 History and solution classes

| Era / class | `WCSNAME` | Absolute accuracy quoted | Source |
|---|---|---|---|
| GSC 1.1 guiding (to Oct 2005) | `OPUS` (no distortion model) or `IDC_<idctab rootname>` | rms 0.5″/coordinate, up to 1–3″ at plate edges; ~1″ total | DrizzlePac Handbook §4.5; ISR ACS 2022-03 §1 **[verified]** |
| GSC 2.3.x guiding (Oct 2005 – Oct 2017) | `IDC_…` | ~0.3″ rms per coordinate | same **[verified]** |
| GSC 2.4.0 guiding (after Oct 2017; Gaia DR1 positions, < 30 mas) | `IDC_…` (labelled `-GSC240` when recomputed for a new IDCTAB) | Handbook: ~0.1″ total; ISR: "~200 mas" including ~100 mas instrument-to-FGS alignment; drizzlepac docs: "can be up to 0.5 arcseconds" | Handbook §4.5; ISR §1, §3.1; `pipeline-astrometric-calibration-description.rst` L399–403 **[verified; sources disagree]** |
| *A priori* (applied to all pre-Oct-2017 data): guide-star coordinates corrected to GSC 2.4.0 | `IDC_…-GSC240` | as above (~0.1–0.2″, ≤0.5″) | same **[verified]** |
| *A priori* from Hubble Source Catalog v3 (Gaia DR1 frame), applied blindly | `IDC_…-HSC30` | "typically smaller" than GSC240, no number; takes precedence over GSC240 | Handbook §4.5 **[verified]** |
| *A posteriori*, per exposure | `IDC_…-FIT_IMG_<cat>` | see 5.3 | astrometry.rst L141–170 **[verified]** |
| *A posteriori*, same-filter exposures of a visit aligned relatively then to catalog (standard pipeline) | `IDC_…-FIT_REL_<cat>` | ~10 mas typical for Gaia | same **[verified]** |
| *A posteriori*, all filters of a visit (SVM) | `IDC_…-FIT_SVM_<cat>` | "Typically the BEST WCS" | same **[verified]** |
| Single exposure fit during visit processing | `IDC_…-FIT_EVM_<cat>` | — | astrometry.rst L164 **[verified]** |
| Relative fit worked, catalog fit failed | `…-FIT_REL_NONE` etc. | treat as unverified | astrometry.rst L170 **[verified]** |

`<cat>` ∈ `GAIAeDR3`, `GSC242`, `2MASS` (older files: `GAIADR2`, `GAIADR1`). Example: `IDC_0461802ej-FIT_SVM_GAIAeDR3`. Handbook prose uses hyphens (`FIT-REL-cat`); actual keyword values use underscores. **[verified]**

### 5.2 Mechanics

- Introduced 3 Dec 2019 (a priori for all ACS/WFC3 + a posteriori where possible). All data were reprocessed when Gaia eDR3 came out (Dec 2020) and "nearly every prior Gaia DR2 or DR1 WCS solution was replaced with Gaia eDR3". **[verified]** (ISR §3.1)
- **Gaia release in use now.** Code default catalog list is `["GAIAeDR3", "GSC242", "2MASS"]` (`UP/drizzlepac/drizzlepac/pars/hap_pars/svm_parameters/acs/wfc/acs_wfc_alignment_all.json` L11, L17; "As of v3.3.0, the GAIAeDR3 catalog serves as the default", astrometry.rst L173). Cycle 34 Primer and WFC3 STAN 48 (Jan 2025) say "Gaia DR3". ISR: DR3 astrometry "is identical to eDR3 so the HST alignment results are not expected to change". **[verified]**
- **Proper motion is applied** to the reference catalog, not to the image: the catalog is requested from the STScI GSSS web service at the observation epoch (`create_astrometric_catalog(..., user_epoch='match')` → `&EPOCH=<decimal year of DATE-OBS>`), with a silent retry without `EPOCH` if the service errors. **[verified]** (`UP/drizzlepac/drizzlepac/haputils/astrometric_utils.py` L221–339, L454–510) The ISR attributes the 80–92% eDR3 success for ACS/WFC to "including proper motion information", and shows fit RMS minimized near the eDR3 epoch J2016.0. **[verified]**
- Fit geometry by number of matches (ISR Table 2, Aug 2022): `rscale` N ≥ 6; `rshift` N = 4–5; `shift` N = 3; no fit N ≤ 2. Higher-order terms are never fit "to preserve the integrity of the distortion calibration". Current code docs give different thresholds: `mosaic_fitgeom_list` default `{"rshift": 10, "rscale": 10, "general": 6}`, pipeline products always `rscale` (`UP/drizzlepac/doc/source/mast_data_products/hap-parameters.rst` L123–124). **[verified both; disagree — thresholds have changed since the ISR]**
- Quality keywords in SCI headers: `NMATCHES`, `FITGEOM`, `RMS_RA`, `RMS_DEC` (mas). WCS solutions with fewer than ~10 matches "should be treated with caution". **[verified]** (ISR §3.2, §5)
- Selection: the pipeline builds default, a priori and a posteriori drizzle products and keeps the best that passes a "focus index" and "similarity index" check; a successful a posteriori fit always replaces the active WCS. **[verified]** (`pipeline-astrometric-calibration-description.rst`)
- **Headerlets.** Every candidate WCS is appended to the FLT/FLC as a `HDRLET` extension (a small FITS file holding SIP WCS + NPOL/D2IM tables); only one is "active" (the SCI header keywords). Switch with `stwcs.wcsutil.headerlet.restore_from_headerlet(file, hdrname=..., force=True)`; list with `headerlet.get_headerlet_kw_names(file, kw='WCSNAME')`. `stwcs.updatewcs.updatewcs(files, use_db=True)` queries the public astrometry database and appends solutions; `use_db=False` yields the plain `IDC_<rootname>` WCS. **[verified]** (astrometry.rst L56–67, L257–274; `UP/stwcs/stwcs/updatewcs/__init__.py` L32–33)
- Control env vars for `runastrodriz`: `ASTROMETRY_COMPUTE_APOSTERIORI`, `ASTROMETRY_APPLY_APRIORI`. **[verified]**

### 5.3 What fraction gets an a posteriori fit, and how good

ISR ACS 2022-03 / WFC3 2022-06 (data through April 2022; full-frame only; fractions are of "alignable" images — grism, ramp, moving target, DASH etc. excluded). **[verified]** (text extraction; row sums check)

Table 1:

| Detector | Datasets | Alignable (fraction) | Aligned to any catalog (fraction of alignable) | Fit to Gaia eDR3 |
|---|---|---|---|---|
| ACS/WFC | 106,000 | 93,200 (0.88) | 84,200 (0.90) | 0.79 |
| WFC3/IR | 141,800 | 61,600 (0.43) | 42,400 (0.69) | 0.58 |
| WFC3/UVIS | 107,000 | 51,500 (0.48) | 31,000 (0.60) | 0.53 |

Table 4, fraction of alignable images by active solution, standard pipeline ("HST") vs SVM ("HAP"), and median fit RMS (quadrature sum of `RMS_RA`, `RMS_DEC`):

| Solution | ACS/WFC HST / HAP | WFC3/IR HST / HAP | RMS ACS/WFC (HST, HAP) | RMS WFC3/IR (HST, HAP) |
|---|---|---|---|---|
| Gaia eDR3 | 0.79 / 0.92 | 0.58 / 0.53 | 16 ± 11, 17 ± 13 mas (0.3, 0.4 pix) | 24 ± 15, 32 ± 17 mas (0.2, 0.3 pix) |
| GSC 2.4.2 | 0.07 / 0.05 | 0.10 / **0.28** | 38 ± 39, 56 ± 55 mas | **111 ± 36, 119 ± 25 mas** (~0.9 pix) |
| 2MASS | 0.05 / – | 0.01 / – | 57 ± 21 mas | 92 ± 44 mas |
| a priori only (no fit) | 0.10 / 0.03 | **0.31 / 0.20** | — | — |

The Handbook's headline: "approximately 70% of the WFC3 and ACS frames have been aligned successfully". **[verified]**

Relative astrometry: distortion solutions allow combination "with an accuracy of better than 5 milli-arcseconds" (astrometry.rst L9); ACS/WFC alignment within a visit "~0.05 pixels or better" (ACS DHB §5.2). **[verified]**

### 5.4 Sparse extragalactic deep fields

- The code falls through Gaia → GSC 2.4.2 → 2MASS and takes the first catalog that yields a fit. GSC 2.4.2 = "Guide Star Catalog II plus PanSTARRS, SMSS, WISE, GALEX, and ultraVISTA, with astrometric data updated to match Gaia DR2" — i.e. in a field with few Gaia stars the reference is ground-based galaxies/stars with ~50–100 mas scatter. **[verified]** (ISR §3.1; `svm_processing.rst` L450–457)
- For WFC3/IR about one image in ten (HST) to one in four (SVM) is on GSC 2.4.2 with ~110 mas fit RMS, and 20–31% have no fit at all (a priori, 0.1–0.5″). **[verified]** (table above)
- Different visits (or different filters in the standard pipeline) of the same field can end on different catalogs or fit geometries, giving visit-to-visit offsets "on the order of a few pixels". **[verified]** (`mvm_products.rst` L151–160, L212–217; ISR Table 3)
- The pipeline never refits against a deep reference image; that is exactly the step surveys add. **[inferred]** from the absence of any such path in the docs above.

---

## 6. Trustworthiness as a starting point, and why surveys redo it

| Level | Good for | Not good for | Evidence |
|---|---|---|---|
| **FLC (ACS/WFC), FLT (WFC3/IR) from MAST** | The intended starting point: per-exposure calibrated pixels, ERR, DQ, distortion model in the header, all candidate WCS solutions attached; "time-dependent photometry with < 1% errors" | (a) WCS is only as good as §5 — re-alignment needed; (b) 4096 CR flags come from the association-level pipeline drizzle (single-exposure ACS singletons have none); (c) IR FLT is wrong for ramps with variable background (§1.3); (d) calibration vintage = last MAST processing, headers must be checked (`CAL_VER`, reference files, `PHOTFLAM` vintage); (e) ACS FLC still has residual CTE trails, satellite trails (bit 16384 "reserved"), and no persistence/blob masks beyond bit 512 for IR | ISR Table 3; WFC3 DHB §7.10; WFC3 STAN 48 **[verified]** for (a), (c), (d); the singleton remark in (b) and all of (e) are **[inferred]** |
| **Standard DRZ/DRC** | Quick look of one association | Native scale, pixfrac 1, detector orientation, one association only, `EXP` weights, sky handling per MDRIZTAB; "WCS 'a posteriori' solutions for multiple filters in a visit may have small systematic offsets" | Handbook §5.3: pipeline drizzle products "should generally be regarded as quick-look products"; WFC3 DHB §4.2: defaults "may not be optimal", check "alignment, sky subtraction, cosmic-ray rejection, and final resolution" **[verified]** |
| **HAP SVM** | Best *automatic* cross-filter alignment within a visit; its `_flc`/`_flt` + `_hlet.fits` carry the `FIT_SVM` WCS and are a better per-exposure starting WCS than the standard files (Gaia fraction 0.92 vs 0.79 for ACS/WFC) | Does not tie visits together; north-up resampling at native scale; IR is the exception (SVM Gaia fraction 0.53 < 0.58, RMS 30% larger) | ISR Table 3, §5.1 **[verified]** |
| **HAP MVM** | Discovery / footprint finding | Science photometry or morphology: no cross-visit alignment, no time-dependent sensitivity, no PSF or epoch separation, IR oversampled 3× at 0.04″ | ISR abstract, Table 3, Summary **[verified]** |

**Usual reasons published surveys do not use archive mosaics directly.**
1. Cross-visit / cross-filter registration to a single deep reference, to a few mas — not attempted by any archive level (§5.4). **[verified]** from the docs above
2. Output grid: a common tangent plane, finer scale and pixfrac < 1 to recover sampling; archive is native/pixfrac 1 or fixed 0.04″. WFC3 DHB §4.2: "Resolution can be improved by adjusting the output scale and PIXFRAC." **[verified]**
3. Inverse-variance weights and matched rms maps instead of `EXP` weights. **[inferred]** (survey practice)
4. Background: `localmin` sky "tends to overestimate the sky background" for extended targets (ISR §6.2); surveys do their own sky/gradient handling. **[verified]** for the quote
5. Extra calibration the pipeline does not do: IR variable-background ramp repair, persistence masking, blob and satellite-trail masks, ACS bias-striping/CTE refinements. CANDELS abstract: "We have paid special attention to correcting a range of instrumental effects, including CTE degradation for ACS", bias striping, and WFC3/IR persistence (Koekemoer et al. 2011, arXiv:1105.3754). SKYSURF: "preserving the lowest sky-level of each visit/group, applying wider apertures for cosmic-ray rejection, correcting effects caused by charge transfer efficiency degradation, and removing potential light gradients" (arXiv:2507.05323). **[verified]** (abstracts)
6. Depth: archive combines by association or visit; MVM combines everything but without quality control. **[verified]**
7. Time-dependent zeropoints across epochs: must be normalized per exposure before combination (§7.1); MVM does not. **[verified]**

Hubble Legacy Fields GOODS-S (Whitaker et al. 2019, arXiv:1908.05682) recombined "7475 exposures taken in 2635 orbits" with "Gaia astrometric corrections". **[verified]** (abstract). Its and CANDELS'/3D-HST's exact scales are in §7.2 as **[inferred]**.

---

## 7. Conventions that differ from JWST NIRCam

### 7.1 Units and zeropoints

**Pixel units.** ACS FLT/FLC: electrons (divide by `EXPTIME` for a rate). WFC3/IR FLT: e⁻/s. DRZ/DRC: e⁻/s. Never MJy/sr. **[verified]** (§1.4)

**Keywords** (SCI extension header for ACS; ACS DHB Table 2.7) **[verified]**:

| Keyword | Meaning | ACS/WFC | WFC3/IR |
|---|---|---|---|
| `PHOTFLAM` | inverse sensitivity, erg cm⁻² s⁻¹ Å⁻¹ per e⁻/s | yes | yes |
| `PHOTFNU` | inverse sensitivity, Jy per e⁻/s | not listed in ACS DHB Table 2.7 | yes (WFC3 DHB §3.3) |
| `PHOTPLAM` | pivot wavelength, Å | yes | yes |
| `PHOTBW` | rms bandwidth, Å | yes | yes |
| `PHOTZPT` | ST magnitude zero point, −21.10 | yes | yes |
| `PHOTMODE` | observing-mode string used to look up `IMPHTTAB` (can be parameterized by MJD for time dependence) | yes | yes |

**Zeropoints** (identical formulas in ACS DHB §5.1, the ACS zeropoints page, WFC3 DHB §9.1) **[verified]**:
- `ZP_ST = −2.5 log10(PHOTFLAM) − 21.10`
- `ZP_AB = −2.5 log10(PHOTFLAM) − 5 log10(PHOTPLAM) − 2.408` (= `ZP_ST − 5 log10(PHOTPLAM) + 18.692`)
- These apply to count rates in e⁻/s measured in an "infinite" aperture: ACS 5.5″ radius (ACS DHB §5.1), WFC3/IR 6.0″ (WFC3 IR photometric calibration page; ISR ACS 2022-03 Table 9).

**To nJy / MJy sr⁻¹** **[derived]** from the definitions:
- `PHOTFNU[Jy s/e⁻] = 3.33564e4 × PHOTFLAM × PHOTPLAM²` (use this for ACS, read the keyword for WFC3).
- flux density per pixel [nJy] = rate[e⁻/s] × `PHOTFNU` × 1e9; equivalently `10^((31.4 − ZP_AB)/2.5)` nJy per e⁻/s.
- surface brightness [MJy/sr] = rate × `PHOTFNU` × 1e-6 / Ω_pix, with Ω_pix = (scale″/206264.806)² for the *drizzled* pixel.
- Check with the published F160W values (`PHOTFLAM` 1.9370e-20, `PHOTPLAM` 15369.18 → ZP_AB 25.941 as tabulated): 1 e⁻/s = 152.6 nJy; on a 0.04″ pixel = 4.06 MJy/sr.

Reference values, infinite aperture, MJD 55008 (https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/photometric-calibration/ir-photometric-calibration, page dated 09/18/2025) **[verified]**:

| Filter | PHOTPLAM (Å) | ZP_AB | PHOTFLAM |
|---|---|---|---|
| F105W | 10551.05 | 26.266 | 3.0479e-20 |
| F125W | 12486.06 | 26.229 | 2.2512e-20 |
| F140W | 13922.91 | 26.454 | 1.4713e-20 |
| F160W | 15369.18 | 25.941 | 1.9370e-20 |

**Time dependence.**
- **ACS/WFC.** Sensitivity declines slowly: 0.061 ± 0.007 %/yr (ISR 2016-03) and 0.0574 ± 0.0066 %/yr (ISR 2020-08), averaged over filters. Discontinuities: CCD set-point lowered from −77 °C to −81 °C on 2006 July 6 (2006.50), and the SM4 electronics replacement (2009.4). "Beginning in 2019, calibrated FLT images from MAST contain date-appropriate inverse sensitivity values in their headers"; ISR 2020-08 values in the pipeline from 12 Oct 2020. Independent lookup: https://acszeropoints.stsci.edu and `acstools.acszpt.Query(date=…, detector='WFC', filt=…).fetch()`. No ACS/WFC flux-calibration ISR after 2020-08 is listed on the zeropoints page (last updated 04/16/2026). **[verified]** (https://www.stsci.edu/hst/instrumentation/acs/data-analysis/zeropoints ; Bohlin 2016, arXiv:1606.01838 via ar5iv). Size of the 2006 step per filter: order 1–2% **[inferred]** (summarizer's comparison of the paper's Table 7; not read directly). The −77 → −81 °C values: an older handbook copy says −80 °C; ACS IHB §4.2 says the WFC was "lowered to −81 °C". **[verified; minor disagreement]**
- **WFC3/IR.** Loss of ~1–2% since 2009, filter dependent: 0.12 %/yr F105W, 0.06 %/yr F160W (ISR 2024-06); −0.07 ± 0.01 %/yr F160W and −0.13 ± 0.02 %/yr F110W in the 2026 update (WFC3 ISR 2026-01, arXiv:2607.12908), which states "The IMPHTTAB reference file that contains the correction to the time-dependent sensitivity remains unchanged." **calwf3 ≥ 3.7.2** (15-Apr-2024: "Allow MJD as a parameterized variable for the PHOTMODE keyword to enable a time-dependent photometric correction for the IR detector", `UP/hstcal/pkg/wfc3/CHANGELOG.txt` L11–14) with IMPHTTAB `8bq13281i_imp.fits` (delivered 17 Dec 2024, ISR 2024-13) writes time-dependent `PHOTFLAM`/`PHOTFNU` automatically; headers are correct "for WFC3/IR images downloaded after December 2024"; "Previously-retrieved data should either be re-downloaded … or reprocessed"; pixel values are unchanged. **[verified]** (WFC3 STAN 48; IR photometric calibration page; arXiv abstract)
  - Stale doc: WFC3 DHB §9.1 still says users "must apply the recommended corrections manually until a new IMPHTTAB is released", and the calibration page cites "calwf3 3.1.0" in one place and v3.7.2 in another. The STAN and changelog are the reliable sources. **[verified; disagreement noted]**
- Consequence for mosaics spanning years: scale each exposure by its own `PHOTFLAM` (or `PHOTFNU`) to a common reference before combining — the drizzle library documents exactly this use of the per-image `iscale` (`UP/drizzle/drizzle/resample.py` L74–94). AstroDrizzle itself does not do it unless `drizzlepac.photeq` is run first. **[verified]** for the docstring; photeq usage **[inferred]**
- **WFC3/IR count-rate non-linearity**: 0.0077 ± 0.0008 mag/dex (Riess et al. 2019); faint (~23 mag) sources ~4.5% too faint without correction; not applied by calwf3. No NIRCam analogue of this size. **[verified]** (WFC3 DHB §9.1) / "not applied by calwf3" **[inferred]**

**Pixel area map.** FLT/FLC are flat-fielded so that a uniform sky is flat, while pixels subtend different solid angles: point-source fluxes on an FLT vary with position until multiplied by the PAM; `drz = flt × PAM` (rates). Drizzling does the equivalent, so DRZ needs no PAM. ACS/WFC PAM ≈ 1.00 at WFC2 centre, ≈ 0.95 near WFC1 centre; pixel area 0.88–1.06 of central (18% corner to corner). WFC3/IR: maximum deviation 4.1%. ACS PAMs do not include NPOLFILE/D2IMFILE effects. **[verified]** (ACS DHB §5.1; ACS IHB §10.4; WFC3 DHB §9.1)

Comparison: NIRCam `cal` images are surface brightness (MJy/sr) with `PIXAR_SR` and an `AREA` extension; the flat-sky convention is the same, only the unit and bookkeeping differ. **[inferred]**

### 7.2 Scales, FOV, PSF and sampling

| | ACS/WFC | WFC3/IR | Source |
|---|---|---|---|
| Detector | 2 × (4096 × 2048) CCD | 1014 × 1014 active HgCdTe | **[verified]** Primer instrument comparison |
| Scale (table value) | 0.049″/pix | 0.13″/pix | **[verified]** same |
| Scale at chip centre, x / y | WFC1 0.0493 / 0.0486; WFC2 0.0498 / 0.0503 | 0.135 / 0.121 | **[verified]** ACS IHB §10.4; WFC3 DHB §4.1 |
| Axis angle / shape | 84.9° (WFC1), 86.1° (WFC2): rhombus | ~24° tilt → ~10% rectangular elongation | **[verified]** same |
| Drizzled native scale used by archive | 0.050″ | 0.128″ | **[verified]** ISR ACS 2022-03 Table 9 |
| FOV | 202″ × 202″ | 123″ × 136″ (DHB: 138″ × 123″) | **[verified]** |
| PSF FWHM | 0.10–0.13″ in F550M (varies 20% over field) ≈ 2.0–2.6 native pix (my division by 0.05″); charge diffusion kernel central value 0.40 / 0.48 / 0.64 at 4000 / 5500 / 8000 Å; F850LP red halo | model core 0.124″ (0.8 µm), 0.130″ (1.1), 0.141″ (1.4), 0.151″ (1.6 µm) = 0.97–1.18 native pix; "extremely under-sampled"; > 40% of flux in the central pixel | **[verified]** ACS IHB §5.6; WFC3 IHB §7.6 Table 7.5 |
| Read noise / dark | ~4 e⁻; 22 e⁻/pix/hr | 21 e⁻ (CDS) | **[verified]** Primer |

Usual survey output grids **[inferred]** (from memory of the survey papers; the abstracts fetched this session did not state them): CANDELS and 3D-HST 0.06″ for WFC3/IR (pixfrac 0.8), 0.03″ for ACS; Hubble Legacy Fields 0.03″ / 0.06″; COSMOS ACS 0.03″; grizli / DAWN JWST Archive 0.04″ common grid for JWST LW and HST (0.02″ for NIRCam SW in some fields). Verified in this session only: DJA v7 PRIMER mosaics "All mosaics still have 40mas pixels" (https://dawn-cph.github.io/dja/imaging/v7/), HAP MVM 0.04″/0.12″. A shared 0.04″ (or 0.03″/0.06″) grid oversamples WFC3/IR by 3.2× (2.1×); the ISR notes the 0.04″ IR layer is noticeably noisier (more correlated) than 0.12″.

NIRCam for comparison: 0.031″ (SW) and 0.063″ (LW) native. **[inferred]**

### 7.3 Distortion model and WCS storage

- Reference files: `IDCTAB` (polynomial, per chip and filter; ACS page describes the SIP as "5th order"; WFC3 2009 solution 4th order), `NPOLFILE` (filter-dependent residual lookup table, 64 × 32 per chip), `D2IMFILE` (filter-independent detector pixel-grid / lithography correction). ACS/WFC uses all three; **WFC3/IR uses the IDCTAB only** (NPOLFILE and D2IMFILE are described for UVIS only). **[verified]** (https://www.stsci.edu/hst/instrumentation/acs/data-analysis/distortion ; WFC3 DHB §4.1, §4.2) Current order of the IR IDCTAB **[unknown]**.
- Size: ACS/WFC uncorrected positions can be off by "~120 pixels (~6 arcsec)"; WFC3/IR non-linear part ~10 pixels (1.4″). Solution accuracy "better than 0.05 pixel" (WFC3: ~5 mas IR). **[verified]**
- **ACS time-dependent distortion.** The linear terms (scale, skew) of ACS/WFC drift with time; modelled as linear-in-time corrections whose coefficients live in the IDCTAB primary header. Three generations exist in stwcs: `TDDALPHA`/`TDDBETA` (pre-2014 IDCTAB; fallback `alpha = 0.095 + 0.090·(t−2004.5)/2.5`, `beta = −0.029 − 0.030·(t−2004.5)/2.5`), `TDD_CXA/CXB/CYA/CYB` (2014 IDCTAB), and `TDD_CTA/CTB` (2015+ IDCTAB), applied directly to the IDC linear coefficients. Applied by `updatewcs` only for ACS/WFC when `TDDCORR == PERFORM`. Separate pre-/post-SM4 calibrations; the ACS page says an update of the SIP coefficients and TDD model is "currently underway" (page dated 04/16/2026); corrected positions drift ~25 mas from the 2002-based solution after ~20 years. **[verified]** (`UP/stwcs/stwcs/updatewcs/corrections.py` L19–272; `apply_corrections.py` L126–165; ACS distortion page; ACS IHB §10.4)
- **Velocity aberration**: `VAFACTOR` in the SCI header; `updatewcs` scales the CD matrix and CRVAL offsets by it. **[verified]** (`corrections.py` L275–300)
- **How it is stored.** `updatewcs` order per instrument (`UP/stwcs/stwcs/updatewcs/apply_corrections.py` L17–21): ACS `DET2IMCorr, TDDCorr, MakeWCS, CompSIP, VACorr, NPOLCorr`; WFC3 `DET2IMCorr, MakeWCS, CompSIP, VACorr, NPOLCorr`. Result in the FLT/FLC: SIP polynomial keywords in each SCI header (IDCTAB polynomial incl. TDD converted to SIP), NPOL lookup tables as `WCSDVARR` image extensions (FITS distortion-paper convention), D2IM tables as `D2IMARR` extensions, first-order IDC terms as `OCX10, OCX11, OCY10, OCY11, IDCSCALE, IDCTHETA, IDCXREF, IDCYREF, IDCV2REF, IDCV3REF`, a `WCSCORR` history table and `HDRLET` extensions. **[verified]** (`UP/stwcs/stwcs/wcsutil/hstwcs.py` L951–970; astrometry.rst L32–54)
- **Can `astropy.wcs` alone evaluate it? Yes.** `WCS(header, fobj=hdulist).all_pix2world` "Performs all of the following in series": detector-to-image correction, SIP, distortion-paper table lookup, core wcslib. `fobj` "is needed when header keywords point to a distortion paper lookup table stored in a different extension." `all_world2pix` inverts numerically. Because TDD and velocity aberration are already folded into the stored coefficients, no HST-specific code is needed to *evaluate* an archive FLT WCS. **[verified]** (https://docs.astropy.org/en/stable/api/astropy.wcs.WCS.html ; stwcs source above). Not executed in this session.
- **What `stwcs.wcsutil.HSTWCS` adds** (it subclasses `astropy.wcs.WCS`; `hstwcs.py` L101): instrument attributes read from the headers (`idctab, offtab, date_obs, ra_targ, dec_targ, pav3, detector, ltv1, ltv2, parity, binned, vafactor, chip, naxis1, naxis2, filter1, filter2`; `UP/stwcs/stwcs/wcsutil/mappings.py`), `pscale` and `orientat` from the CD matrix, `idcscale` and the IDC model (`readModel`, `readModelFromIDCTAB`), `wcs2header(sip2hdr=…)`, alternate-WCS and headerlet management (`altwcs`, `headerlet`), and `stwcs.distortion.utils.output_wcs` (builds the undistorted output frame AstroDrizzle uses). It is required to *change* a WCS the HST way (updatewcs, tweakback, headerlets) and by drizzlepac internals; it is not required to read one. **[verified]**
- JWST contrast: gwcs object serialized in the ASDF extension, not FITS keywords; `drizzle.utils.calc_pixmap` accepts either through the common `pixel_to_world_values` / `world_to_pixel_values` API. **[verified]** for calc_pixmap (`UP/drizzle/drizzle/utils.py`); gwcs statement **[inferred]**

### 7.4 Error and weight conventions

- **ACS ERR** (electrons): initialized as sqrt((SCI − bias) + RN²) and propagated; includes source Poisson noise from the *measured* (noisy) counts. For CRJ since calacs 10.3.5 the ERR is "propagated from the usable input ERR extensions versus being computed by a model". **[verified]** (ACS DHB §3.4; `UP/hstcal/pkg/acs/CHANGELOG.txt`)
- **WFC3/IR ERR** (e⁻/s): per-read ERR initialized as sqrt(RN² + |signal·gain|)/gain; the FLT ERR "is populated by the CRCORR step" from the uncertainty of the up-the-ramp fit (read noise + Poisson + dark, with flat-field uncertainty). `TIME` and `SAMP` vary per pixel (CR-truncated or saturated ramps have shorter effective time). **[verified]** (noiscalc.c; WFC3 DHB §3.3)
- Neither instrument provides separate variance components (no analogue of `VAR_POISSON`, `VAR_RNOISE`, `VAR_FLAT`). **[inferred]** (absence in the extension lists above; JWST side from memory)
- **AstroDrizzle WHT vs inverse variance**: only with `IVM` or `ERR` is WHT an inverse variance (per output pixel, units (e⁻/s)⁻², scaled by 1/scale⁴); with the archive's `EXP` it is exposure time. AstroDrizzle writes no drizzled ERR/variance extension. **[verified]** (§3.2) for the weights; "no ERR extension" **[verified]** from the DRZ extension list.
- **Correlated noise** (Fruchter & Hook 2002, eqs. 9–10; r = pixfrac/scale with scale in input pixels): single-pixel rms is suppressed relative to the uncorrelated value by 1/R, with `R = r / (1 − 1/(3r))` for r ≥ 1 and `R = 1 / (1 − r/3)` for r ≤ 1; p = 0.6, s = 0.5 gives R = 1.662. **[verified]** (https://ar5iv.labs.arxiv.org/html/astro-ph/9808087). The current hst-docs rendering of DrizzlePac Handbook §3.3 shows the r ≤ 1 case as `r/(1 − r/3)`, which contradicts the paper and has the wrong limit as p → 0; treat the handbook page as a typo. **[verified that the two differ]** Casertano et al. (2000) write the same thing as the ratio √F_A = (s/p)(1 − s/(3p)) for s < p and 1 − p/(3s) for s > p. **[inferred]** (memory; it is the reciprocal of R)
- Examples **[derived]**: ACS 0.05″→0.03″, pixfrac 0.8: r = 1.333, R = 1.78. WFC3/IR 0.128″→0.06″, pixfrac 0.8: r = 1.707, R = 2.12. WFC3/IR →0.04″, pixfrac 1: r = 3.2, R = 3.57. Any shared code that turns a weight map into an rms map needs this factor per instrument/grid.

### 7.5 DQ bits

ACS (ACS DHB Table 3.4) and WFC3/IR (WFC3 DHB Table 2.7) **[verified]**:

| Bit | ACS/WFC | WFC3/IR |
|---|---|---|
| 1 | Reed-Solomon decoding error | Reed-Solomon decoding error |
| 2 | Data replaced by fill value | Data replaced by fill value |
| 4 | Bad detector pixel or vignetted pixel | Bad detector pixel |
| 8 | Masked by aperture feature | Deviant zero-read (bias) value |
| 16 | Hot pixel (dark > 0.14 e⁻/s) | Stable hot pixel |
| 32 | Unstable dark current (RTS, fading hot pixels) | Unstable pixel (after 8 Nov 2012; before, both kinds were 16) |
| 64 | Warm pixel (0.06–0.14 e⁻/s) | (obsolete: warm pixel) |
| 128 | Bias structure (bad columns) | Bad reference pixel |
| 256 | Full-well saturation | Full-well saturation |
| 512 | Bad pixel in reference file | Bad or uncertain flat value, **including "blobs"** |
| 1024 | Sink pixel / sink trail | (unused) |
| 2048 | A-to-D saturation | Signal in zero read |
| 4096 | CR flagged by AstroDrizzle | CR flagged by AstroDrizzle |
| 8192 | CR rejected by acsrej (CR-SPLIT) | CR found in up-the-ramp fit (**IMA only**) |
| 16384 | Reserved (satellite-trail masks etc.) | Ghost/crosstalk (not used) |
| 32768 | not listed | not listed |

`*_bits` parameters list the bits to treat as **good** (sum); everything else non-zero is rejected. Software default `final_bits=0` (any flag is bad). **[verified]** (astrodrizzle.py L962–968)

| Context | ACS/WFC | WFC3/IR |
|---|---|---|
| Standard pipeline (MDRIZTAB) | 336 = 16+64+256: hot, warm, saturated kept | stable hot (16) kept since May 2019; full value **[unknown]** |
| SVM | 336 (65535 = everything good, for single-exposure products) | 528 = 16+512: stable hot and blob/flat-flagged pixels kept |
| MVM | 80 = 16+64 (saturation rejected) | 528 |
| Sky statistics (`sky_bits`, SVM) | 80 | 16 |

(`UP/drizzlepac/drizzlepac/pars/hap_pars/...json`; DrizzlePac Initialization notebook) **[verified]**

Bit meanings share nothing with JWST's DQ scheme (where bit 0 = DO_NOT_USE, 1 = SATURATED, 2 = JUMP_DET, …) and are int16 rather than uint32; a shared mask needs a per-instrument translation table, and "good bits" policy differs even between ACS and WFC3/IR (bit 8, 128, 512, 1024, 2048 all mean different things). **[inferred]** for the JWST side.

### 7.6 Header keywords and naming

| Item | ACS | WFC3 | Note |
|---|---|---|---|
| Filter | `FILTER1`, `FILTER2` (two wheels; the unused wheel holds a `CLEAR…` element, e.g. poller line `F850LP;CLEAR2L`) | `FILTER` | **[verified]** ACS DHB Table 2.6; `svm_processing.rst` L200, L287 |
| Detector | `DETECTOR` = `WFC` / `HRC` / `SBC` | `DETECTOR` = `UVIS` / `IR` | **[verified]** |
| Instrument | `INSTRUME` = `ACS` | `WFC3` | **[verified]** |
| Chip | `CCDCHIP` in each SCI header (1 or 2) — not the same as `EXTVER` (SCI,1 = chip 2) | single | **[verified]** ACS DHB §2.2, Table 2.7 |
| Aperture / subarray | `APERTURE`, `SUBARRAY` (T/F) | same | **[verified]** |
| Time | `EXPTIME` (s, as executed), `EXPSTART` / `EXPEND` (MJD), `DATE-OBS`, `TIME-OBS` — primary header | same; IR also `SAMP_SEQ`, `NSAMP`, per-pixel `TIME` ext | **[verified]** ACS; IR `SAMP_SEQ` **[inferred]** |
| Identity | `ROOTNAME`, `PROPOSID`, `LINENUM`, `TARGNAME`, `ASN_ID`, `ASN_TAB` | same | **[verified]** |
| Gain / noise | `CCDGAIN`, `CCDAMP`, `ATODGNA–D`, `READNSEA–D` | `ATODGNA–D`, `READNSEA–D` | **[verified]** |
| Pointing | `PA_V3` (primary), `ORIENTAT` (SCI; PA of image y axis), `VAFACTOR` | same | **[verified]** |
| WCS provenance | `WCSNAME`, `IDCTAB`, `NPOLFILE`, `D2IMFILE`, `MDRIZTAB`, `MDRIZSKY`, `NMATCHES`, `RMS_RA`, `RMS_DEC`, `FITGEOM`, `SKYCELL` | same (no NPOL/D2IM for IR) | **[verified]** |
| Software | `CAL_VER` (calacs / calwf3 version), `OPUS_VER` | same | **[verified]** |

**Rootname `ipppssoot`** (HST Data Handbook §2.2, https://hst-docs.stsci.edu/hstdhb/2-hst-file-names/2-2-rootnames) **[verified]**: `i` instrument (`j` = ACS, `i` = WFC3, `u` = WFPC2, `n` = NICMOS, `o` = STIS, `l` = COS); `ppp` program ID (base-36, one-to-one with the proposal ID but not equal to it); `ss` observation set (usually the visit); `oo` observation number; `t` transmission source (`q` solid-state recorder, `s` retransmitted SSR, `r` real time, `t` tape, `m`/`n`/`o`/`p` merged or retransmitted) or, for association products, `0` = primary product and `1`–`8` = sub-products. HAP names use the first 6 (`ipppss`) or 8 (`ipppssoo`) characters. The `PROPOSID` keyword, not `ppp`, gives the program number.

**Associations.** `_asn.fits` is a one-extension binary table (`MEMNAME`, `MEMTYPE`, `MEMPRSNT`). Members are dither patterns / CR-SPLITs / sub-exposures of one visit and one filter; singletons have no ASN. Running `runastrodriz` needs the ASN, RAW (for the `DRIZCORR` switch) and FLT/FLC in one directory. **[verified]** (WFC3 DHB §2.1; `pipeline-astrometric-calibration-description.rst` L99–114)

---

## 8. `drizzle` (jwst/stcal) vs the drizzle inside `drizzlepac`

- **Two separate C forks with a common origin.** `drizzlepac` builds its own extension `drizzlepac.cdriz` from `src/*.c` (`arrdrizmodule.c`, `cdrizzlebox.c`, `cdrizzleblot.c`, `cdrizzlemap.c`, `cdrizzlewcs.c`, …) and does **not** depend on the `drizzle` package. **[verified]** (`UP/drizzlepac/setup.py`; `UP/drizzlepac/pyproject.toml` dependencies; `UP/drizzlepac/drizzlepac/adrizzle.py` L54, L1607)
- The standalone `drizzle` package states: "This library is derived from code used in DrizzlePac … The biggest change from DrizzlePac is that this code passes an array that maps the input to output image into the C code, while the DrizzlePac code computes the mapping by using a Python callback." **[verified]** (`UP/drizzle/README.rst`)
- In drizzlepac the default mapping is `cdriz.DefaultWCSMapping(input_wcs, output_wcs, nx, ny, stepsize)` — a wcslib-based transform evaluated on a coarse grid (`stepsize=10`) and interpolated; a Python `wcsmap` callback is the alternative. **[verified]** (`adrizzle.py` L1571–1591; `astrodrizzle.cfg`)
- The forks have diverged: standalone `drizzle` 2.x/3.x added DQ propagation by bitwise OR, squared-weight resampling for variance arrays, a fixed Lanczos kernel, `iscale` / `pixel_scale_ratio` split, removal of `sinc` blot, a segfault fix in the square kernel polygon scan, NaN-tolerant blot. None of these are in `drizzlepac/src`. **[verified]** (`UP/drizzle/CHANGES.rst`)
- Both square kernels use the same geometry: overlap area of the projected input quadrilateral with the output pixel, divided by the Jacobian (`dover /= jaco` in `UP/drizzlepac/src/cdrizzlebox.c` L978–1012; `w = … / jaco` in `UP/drizzle/src/cdrizzlebox.c` L1377–1396). drizzlepac multiplies each input value by `scale²` (output/input linear pixel ratio; L989); standalone multiplies by caller-supplied `iscale` (L1381). **[verified]**

**Versions in use today.**

| Stack | drizzle core | Source |
|---|---|---|
| campfire env now: jwst 1.20.2, stcal 1.15.2 | `drizzle` 2.1.1 (pin `drizzle<2.2.0,>=2.1.1` in jwst 1.20.2) | `conda run -n campfire` metadata query; https://pypi.org/pypi/jwst/1.20.2/json **[verified]** |
| jwst 3.0.0 (2026-07-09), stcal 1.21.0 | `drizzle<3.1.0,>=2.2.0` (via stcal; jwst 3.0.0 has no direct drizzle pin) → 3.0.0 | PyPI JSON **[verified]** |
| drizzlepac 3.11.0 | bundled `drizzlepac.cdriz` (no separate version) | repo **[verified]** |

`stcal.resample.resample` imports `drizzle.utils.calc_pixmap` and `drizzle.resample.Drizzle` and resamples the error image with a second `Drizzle` object. **[verified]** (source of installed stcal 1.15.2)

**Can the standalone `drizzle` resample an HST FLT?** Yes in principle, and upstream tests do it:
- `drizzle.utils.calc_pixmap(wcs_from, wcs_to)` only needs `pixel_to_world_values` / `world_to_pixel_values`, which `astropy.wcs.WCS` (and therefore `HSTWCS`) implements with full distortion. **[verified]** (`UP/drizzle/drizzle/utils.py`)
- The drizzle test-suite resamples an ACS/WFC FLT (`j8bt06nyq_flt.fits`, `j8bt06nyq_sip_flt.fits`) through a FITS WCS and through a gwcs. **[verified]** (`UP/drizzle/drizzle/tests/test_resample.py` L322, L393; `test_utils.py` L35–105)
- The `Drizzle` docstring discusses HST explicitly: FLT/FLC units, the need to rescale by `PHOTFLAM` when epochs or chips differ, and `iscale = s²` for flux-unit images. **[verified]** (`UP/drizzle/drizzle/resample.py` L64–100)
- Not done in this session: an actual run, or a numerical comparison against AstroDrizzle output. **[unknown]** whether results match to rounding (mapping is exact per pixel in `drizzle` vs interpolated every 10 pixels in drizzlepac, so small differences are expected). 

**What a JWST-style resample step would still have to supply for HST** **[inferred]** from the above: (1) build the WCS with the HDUList so NPOL/D2IM tables are used, per chip; (2) convert pixels to a common surface-brightness-like unit first (ACS e⁻ → /EXPTIME; × per-exposure `PHOTFNU`; ÷ nominal pixel solid angle), after which the JWST convention `iscale = 1` applies; (3) translate DQ to a weight mask with the per-instrument good-bit set; (4) weights: `EXP` (seconds; IR `TIME` array available) or inverse variance built from ERR minus the source term, or from sky/RN/flat as in AstroDrizzle's IVM; (5) no blot/outlier step is needed from drizzlepac if the existing JWST-style outlier detection is reused, but the HST CR problem is different in kind for ACS (a single CCD read per exposure, so every cosmic ray survives into the FLC — of order a percent of pixels in a long exposure — and often only 2–4 exposures per pointing) than for up-the-ramp IR data.

---

## 9. Installation / packaging (late 2026)

| Package | Latest | Where | Python / numpy | Wheels | License | Notes |
|---|---|---|---|---|---|---|
| `drizzlepac` | 3.11.0 (2026-04-28) | PyPI; in stenv | `>=3.11`; `numpy>2.0`, `astropy>=6.1`, `photutils>=2.0.0`, `tweakwcs>=0.8.7`, `stwcs>=1.7.4`, `stsci.skypac>=1.0.11`, `spherical_geometry>=1.2.22`, `stsci.imagestats>=1.8.2`, plus bokeh, pandas, scikit-learn, astroquery, astrocut, lxml, pypdf, stregion, simplify-polyline | cp311–cp314; macOS x86_64 + arm64, manylinux x86_64. **No Windows, no linux-aarch64** | BSD-3-Clause (AURA) | C extension. Docs still call pip "experimental" (`UP/drizzlepac/doc/source/getting_started/installation.rst`). Heavy dependency tail from HAP catalog/graphics code. **[verified]** |
| `hstcal` (calacs, calwf3) | 3.3.0 (tag 2026-11-04) | **conda-forge only** (`conda install -c conda-forge hstcal`); platforms linux-64, osx-64, osx-arm64. **Not on PyPI** (404). Source build: cmake, cfitsio, gcc + gfortran, OpenMP; Apple clang "is not sufficient" | n/a (C executables) | n/a | BSD-3-Clause (AURA) | **[verified]** (`UP/hstcal/README.md`, `INSTALL.md`; https://api.anaconda.org/package/conda-forge/hstcal) |
| `stwcs` | 1.7.7 (2026-03-05) | PyPI | `astropy>=6.0`, `numpy>=2.0`, `stsci.tools>=3.6`, requests, lxml | pure Python | BSD | **[verified]** |
| `stsci.skypac` | 1.0.12 (2026-02-09) | PyPI | `>=3.10`; astropy≥5.0.4, numpy, spherical_geometry≥1.2.2, stsci.imagestats, stsci.tools, stwcs | pure Python | not stated in PyPI metadata **[unknown]** | **[verified]** otherwise |
| `acstools` | 3.8.2 (2026-06-24) | PyPI | PyPI metadata `>=3.10`, repo main `>=3.11`; numpy, astropy, requests, PyYAML | pure Python | BSD-3-Clause | needs `calacs.e` on PATH for calibration; `acszpt` needs network. **[verified]** |
| `wfc3tools` | 1.6.1 (2026-02-06) | PyPI | PyPI `>=3.9`, repo main `>=3.11`; astropy, numpy, matplotlib, stsci.tools, scipy | pure Python | BSD-style (AURA) | needs `calwf3.e` on PATH. **[verified]** |
| `crds` | 14.0.6 | PyPI | `>=3.7`; astropy, numpy, filelock, asdf>3, requests, parsley | pure Python | (LICENSE file) | one client for HST and JWST. **[verified]** |
| `drizzle` | 3.0.0 (2026-09-24) | PyPI | PyPI `>=3.10` (main: `>=3.11`, numpy ≥ 2.0.1 to build); runtime dep numpy only | abi3 (cp310+) and cp314t: macOS arm64, manylinux/musllinux x86_64, win32/amd64. **No macOS x86_64, no linux-aarch64 wheel** | BSD-style (AURA) | **[verified]** |
| `stenv` | 2026.10.21 | conda env YAML from GitHub releases | `python>=3.12` | — | — | conda: `fitsverify`, `hstcal>=3.0.3`, numpy, python, uv; pip: astropy, **acstools**, asdf, calcos, ccdproc, costools, **crds<14**, dask, **drizzlepac**, ginga, h5py, ipython, jupyter, **jwst**, reftools, romancal, scipy, **stcal**, stdatamodels, stistools, stsynphot, stregion, synphot, **wfc3tools**, wfpc2tools. **[verified]** (https://raw.githubusercontent.com/spacetelescope/stenv/main/environment.yaml; https://github.com/spacetelescope/stenv/releases). Asset OS/Python matrix **[unknown]** (8 assets per release) |

**Can drizzlepac and jwst share an environment?**
- STScI ships exactly that: `stenv` installs `jwst`, `stcal`, `drizzlepac`, `acstools`, `wfc3tools` by pip plus `hstcal` by conda in one environment. **[verified]**
- Against the project's current pin (`jwst>=1.20,<1.21`; installed jwst 1.20.2, stcal 1.15.2, drizzle 2.1.1, numpy 2.2.0, astropy 7.2.0, photutils 2.3.0, tweakwcs 0.8.12, spherical_geometry 1.4.0, Python 3.13.12): every drizzlepac 3.11.0 requirement is already satisfied by the installed versions, and drizzlepac does not touch the `drizzle` pin. The env already contains `stwcs 1.7.7`, `stsci.skypac 1.0.12`, `stsci.tools 4.3.0`, `stsci.imagestats 1.8.4`, `stsci.stimage 0.3.0` (drizzlepac itself is not installed). No pin conflict on paper. **[verified]** at the metadata level (`pipeline/pyproject.toml`; `importlib.metadata` query; PyPI JSON). Not test-installed (per the no-install rule), so resolver behaviour on the long optional tail (bokeh, astrocut, scikit-learn, pypdf, stregion) is **[unknown]**.
- Against jwst 3.0.0: jwst needs `photutils>=3.0,<4`, `tweakwcs>=0.9.2,<0.10`, `astropy>=7.2`, Python `<3.15,>=3.11`. drizzlepac 3.11.0 allows these ranges, but "Added code to handle deprecations in photutils v3.0.0" is only in unreleased 3.12.0 (`CHANGELOG.rst` L45), so 3.11.0 with photutils 3 may warn or break in the HAP catalog code. Core AstroDrizzle/TweakReg exposure to that is **[unknown]**.
- Python: jwst 1.20.2 requires `<3.14`; drizzlepac has cp311–cp314 wheels; hstcal is Python-independent.

**Practical packaging consequence.** The only piece that cannot be pip-installed is `hstcal` (needed only if we recalibrate from RAW: re-running calwf3 on IR ramps with variable background, or pinning a CRDS context). Everything needed to *consume* archive FLT/FLC (astropy WCS, stwcs, drizzle, drizzlepac) is pip-installable on macOS arm64/x86_64 and Linux x86_64.

---

## 10. Items not verified / open

1. Numeric `final_bits` / `driz_cr_*` in the current **WFC3/IR pipeline MDRIZTAB** (`3562021pi_mdz.fits` or successor) and whether ACS's MDRIZTAB has moved from `localmin` to `match` since Aug 2022. Would need the reference files (small tables) or WFC3 ISR 2019-05 / ACS ISR 2017-02 text.
2. Whether MAST currently regenerates **MVM** (and SVM) products on every reprocessing, given the drizzlepac 3.9.0 note "used by operations but does not generate HAP products"; no 2024–2026 MAST announcement found.
3. Whether HST products carry a CRDS context keyword (none found in open-source code; a DMS-level keyword is possible).
4. A real end-to-end run: `astropy.wcs`/`HSTWCS` + standalone `drizzle` on an FLC, compared against AstroDrizzle. Source and tests say it works; not executed.
5. Survey output scales and pixfrac (CANDELS, 3D-HST, HLF, COSMOS, DJA/grizli for HST) are from memory except DJA 40 mas and HAP.
6. Casertano et al. (2000) √F_A form and the size of the 2006 ACS sensitivity step per filter are from memory / secondary summary.
7. stenv release asset matrix (OS × Python) and whether a linux-aarch64 path exists for hstcal (conda-forge lists none).
8. Current polynomial order of the WFC3/IR IDCTAB; whether IR FLTs ever carry NPOL/D2IM extensions (docs describe them for UVIS only).
9. JWST-side statements used for contrast (DQ bit names, `VAR_*` arrays, MJy/sr + `PIXAR_SR`, gwcs in ASDF, NIRCam pixel scales) are from memory, tagged [inferred].
10. Pages that could not be accessed:
    - Unable to access this page: https://www.stsci.edu/files/live/sites/www/files/home/hst/instrumentation/wfc3/documentation/instrument-science-reports-isrs/_documents/2022/WFC3-ISR-2022-06.pdf (over fetch size limit; the identical ACS ISR 2022-03 PDF was downloaded and read instead)
    - Unable to access this page: https://hst-docs.stsci.edu/acsdhb/chapter-3-acs-calibration-pipeline/3-1-on-the-fly-reprocessing-otfr (404; correct page is §3.1 "Mikulski Archive for Space Telescopes - MAST", read)
    - Unable to access this page: https://hst-docs.stsci.edu/wfc3dhb/chapter-4-wfc3-images-distortion-correction-and-astrodrizzle/4-1-wfc3-distortion (404; correct slug `4-1-wfc3-geometric-distortion`, read)
    - Unable to access this page: https://www.stsci.edu/hst/instrumentation/acs/performance/geometric-distortion (404; `.../acs/data-analysis/distortion` read instead)
    - Unable to access this page: https://stenv.readthedocs.io/en/latest/getting_started.html and `.../getting-started.html` (404; landing page and `environment.yaml` read instead)
    - Unable to access this page: https://hst-docs.stsci.edu/hstdhb/5-observation-logs-and-file-names (404; `hstdhb/2-hst-file-names/2-2-rootnames` read instead)
    - Unable to access this page: https://pypi.org/pypi/hstcal/json (404 — taken as "not on PyPI")

---

## 11. Source list

### Upstream source read (shallow clones under `UP/`)
- drizzlepac @ 6d11d26 (2026-10-29): `pyproject.toml`, `setup.py`, `CHANGELOG.rst`, `drizzlepac/pars/astrodrizzle.cfg`, `drizzlepac/pars/hap_pars/{svm,mvm}_parameters/{acs/wfc,wfc3/ir}/*.json`, `drizzlepac/astrodrizzle.py`, `adrizzle.py`, `imageObject.py`, `wfc3Data.py`, `acsData.py`, `sky.py`, `mdzhandler.py`, `runastrodriz.py`, `haputils/astrometric_utils.py`, `haputils/cell_utils.py`, `haputils/product.py`, `src/cdrizzlebox.c`, `doc/source/mast_data_products/{astrometry,pipeline-astrometric-calibration-description,hap_introduction,svm_processing,mvm_processing,mvm_products,hap-parameters}.rst`, `doc/source/getting_started/installation.rst`, `LICENSE.txt`
- hstcal @ 733bd3e (2026-11-10): `README.md`, `INSTALL.md`, `pkg/acs/CHANGELOG.txt`, `pkg/wfc3/CHANGELOG.txt`, `pkg/acs/include/acsversion.h`, `pkg/wfc3/include/wf3version.h`, `pkg/acs/lib/acsccd/doccd.c`, `pkg/wfc3/lib/wf3ir/doir.c`, `pkg/wfc3/lib/wf3ir/noiscalc.c`
- stwcs @ 8b8bcce (2026-11-11): `pyproject.toml`, `CHANGES.rst`, `stwcs/wcsutil/hstwcs.py`, `stwcs/wcsutil/mappings.py`, `stwcs/updatewcs/{__init__,apply_corrections,corrections}.py`
- drizzle @ 421ae2c (2026-11-06): `pyproject.toml`, `README.rst`, `CHANGES.rst`, `drizzle/resample.py`, `drizzle/utils.py`, `drizzle/tests/{helpers,test_resample,test_utils}.py`, `src/cdrizzlebox.c`, `LICENSE.rst`
- acstools @ 69339bb (2026-11-02): `pyproject.toml`, `acstools/calacs.py`
- wfc3tools @ 4d51241 (2026-11-09): `LICENSE.txt`; `pyproject.toml` via raw GitHub
- Local (read-only): `pipeline/pyproject.toml` of this repository; package metadata of the `campfire` conda env

### Web pages read (all accessed 2026-11-11)
- ACS Data Handbook: https://hst-docs.stsci.edu/acsdhb/chapter-2-acs-data-structure/2-1-types-of-acs-files ; `/2-2-acs-file-structure` ; `/2-4-headers-and-keywords` ; https://hst-docs.stsci.edu/acsdhb/chapter-3-acs-calibration-pipeline/3-1-mikulski-archive-for-space-telescopes-mast ; `/3-4-calacs-processing-steps` ; `/3-5-manual-recalibration-of-acs-data` ; https://hst-docs.stsci.edu/acsdhb/chapter-4-acs-data-processing-considerations/4-7-generic-detector-and-camera-properties ; https://hst-docs.stsci.edu/acsdhb/chapter-5-acs-data-analysis/5-1-photometry ; `/5-2-astrometry`
- ACS Instrument Handbook: https://hst-docs.stsci.edu/acsihb/chapter-4-detector-performance/4-2-the-ccds ; https://hst-docs.stsci.edu/acsihb/chapter-5-imaging/5-6-acs-point-spread-functions ; https://hst-docs.stsci.edu/acsihb/chapter-10-imaging-reference-material/10-4-geometric-distortion-in-acs
- ACS web: https://www.stsci.edu/hst/instrumentation/acs/data-analysis/zeropoints (updated 04/16/2026) ; https://www.stsci.edu/hst/instrumentation/acs/data-analysis/distortion (updated 04/16/2026)
- WFC3 Data Handbook: https://hst-docs.stsci.edu/wfc3dhb/chapter-2-wfc3-data-structure/2-1-types-of-wfc3-files ; `/2-2-wfc3-file-structure` ; https://hst-docs.stsci.edu/wfc3dhb/chapter-3-wfc3-data-calibration/3-1-the-calwf3-data-processing-pipeline ; `/3-2-uvis-data-calibration-steps` ; `/3-3-ir-data-calibration-steps` ; https://hst-docs.stsci.edu/wfc3dhb/chapter-4-wfc3-images-distortion-correction-and-astrodrizzle/4-1-wfc3-geometric-distortion ; `/4-2-distortion-corrections-and-image-combination` ; https://hst-docs.stsci.edu/wfc3dhb/chapter-7-wfc3-ir-sources-of-error/7-10-time-variable-background ; https://hst-docs.stsci.edu/wfc3dhb/chapter-9-wfc3-data-analysis/9-1-photometry
- WFC3 Instrument Handbook: https://hst-docs.stsci.edu/wfc3ihb/chapter-7-ir-imaging-with-wfc3/7-6-ir-optical-performance
- WFC3 web: https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/photometric-calibration/ir-photometric-calibration (updated 09/18/2025) ; https://www.stsci.edu/contents/news/wfc3-stans/wfc3-stan-issue-48-january-2025 ; https://www.stsci.edu/contents/news/wfc3-stans/wfc3-stan-issue-29-june-2019
- DrizzlePac Handbook: https://hst-docs.stsci.edu/drizzpac/chapter-3-description-of-the-drizzle-algorithm/3-3-weight-maps-and-correlated-noise ; https://hst-docs.stsci.edu/drizzpac/chapter-4-astrometric-information-in-the-header/4-5-absolute-astrometry ; https://hst-docs.stsci.edu/drizzpac/chapter-5-drizzlepac-software-package/5-2-astrodrizzle-the-new-drizzle-workhorse ; `/5-3-astrodrizzle-in-the-pipeline`
- DrizzlePac notebook: https://spacetelescope.github.io/hst_notebooks/notebooks/DrizzlePac/Initialization/Initialization.html
- HST Data Handbook: https://hst-docs.stsci.edu/hstdhb/2-hst-file-names/2-1-file-name-format ; `/2-2-rootnames`
- HST Primer (Cycle 34): https://hst-docs.stsci.edu/hsp/the-hubble-space-telescope-primer-for-cycle-34/hst-primer-data-processing-and-the-hst-data-archive ; `/hst-primer-scientific-instrument-comparisons`
- CRDS: https://hst-crds.stsci.edu/ ; https://hst-crds.stsci.edu/static/users_guide/basic_use.html ; https://hst-crds.stsci.edu/static/users_guide/environment.html
- Astrometry / HAP: ISR ACS 2022-03 = WFC3 2022-06 (Mack et al., 23 Aug 2022), https://www.stsci.edu/files/live/sites/www/files/home/hst/instrumentation/acs/documentation/instrument-science-reports-isrs/_documents/isr2203.pdf (downloaded, text-extracted) ; https://outerspace.stsci.edu/spaces/HAdP/pages/54558799/Improvements+in+HST+Astrometry (updated Jan 10, 2025) ; https://outerspace.stsci.edu/x/24HZDQ (older version of the same page) ; https://archive.stsci.edu/contents/newsletters/may-2022/multi-visit-mosaics-from-hubble-now-available
- astropy / stwcs: https://docs.astropy.org/en/stable/api/astropy.wcs.WCS.html ; https://docs.astropy.org/en/stable/wcs/index.html ; https://stwcs.readthedocs.io/en/latest/hstwcs.html
- Papers: Fruchter & Hook 2002 via https://ar5iv.labs.arxiv.org/html/astro-ph/9808087 ; Bohlin 2016 via https://ar5iv.labs.arxiv.org/html/1606.01838 ; https://arxiv.org/abs/2607.12908 (WFC3 ISR 2026-01) ; https://arxiv.org/abs/2507.05323 (SKYSURF X) ; https://arxiv.org/abs/1908.05682 (HLF GOODS-S) ; https://arxiv.org/abs/1105.3754 (CANDELS) ; https://dawn-cph.github.io/dja/imaging/v7/
- Packaging: https://pypi.org/pypi/{drizzlepac,drizzle,drizzle/3.0.0,jwst,jwst/1.20.2,stcal,stwcs,stsci.skypac,acstools,wfc3tools,crds}/json ; https://api.anaconda.org/package/conda-forge/hstcal ; https://anaconda.org/conda-forge/hstcal ; https://github.com/conda-forge/hstcal-feedstock ; https://stenv.readthedocs.io/en/latest/ ; https://github.com/spacetelescope/stenv/releases ; https://raw.githubusercontent.com/spacetelescope/stenv/main/environment.yaml ; https://raw.githubusercontent.com/spacetelescope/hstcal/main/LICENSE.txt ; https://raw.githubusercontent.com/spacetelescope/wfc3tools/main/pyproject.toml

### Working files left in the scratchpad
- `/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/research/pdf_isr2203/isr2203.pdf` (ISR PDF)
- `/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/research/isr2203.txt` and `isr2203_compact.txt` (extracted text)
- `/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/research/scripts/pdf2txt.py` (pdfminer extraction helper)
