# HST imaging reduction beyond the archive pipeline: what people actually use (as of 2026-11-11)

Scope: ACS/WFC and WFC3/IR imaging, starting from calibrated FLT/FLC. Reader: NIRCam expert, new to HST.
Tags: **[verified]** = read in this session at the cited source; **[inferred]** = memory or indirect evidence; **[unknown]** = could not establish.
grizli line numbers refer to a shallow clone at commit `c8c9d325` (2026-10-26), in
`/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/upstream/grizli/`.

---

## 1. Verdict on the two-lineage hypothesis

The picture "grizli/DJA versus the CANDELS (Koekemoer) lineage" is right about the two largest families but incomplete. I find five families in use, of which only two have public code.

| # | Family | Public code? | Typical users today |
|---|---|---|---|
| A | Koekemoer / STScI "MosaicDrizzle" (COSMOS-ACS 2007, CANDELS 2011, CLASH, Frontier Fields, BUFFALO, CEERS HDR1, COSMOS-Web F814W) | No public repository found | CEERS, COSMOS-Web/COSMOS2025, anyone adopting CANDELS/HFF mosaics |
| B | Brammer: `threedhst` → 3D-HST (Skelton 2014) → grizli → CHArGE → DJA; DASH via `wfc3dash` | Yes (MIT) | UNCOVER, MegaScience, CANUCS, MINERVA, PANORAMIC, DJA users |
| C | Illingworth / Magee / Bouwens: APSIS + WFC3RED → HUDF09 / XDF → Hubble Legacy Fields | Not stated public | JADES (DR5), ASTRODEEP-JWST for GOODS; PRIMER team reductions by D. Magee |
| D | STScI archive Hubble Advanced Products (HAP SVM / MVM) | Yes, inside `drizzlepac` (BSD) | Archive users; I found no deep-field catalog paper that adopts them |
| E | Hand-built `drizzlepac` (TweakReg + AstroDrizzle) per project, including `hst123` for point sources | Yes | RELICS, 3D-DASH, transient / resolved-star work |

3D-HST is a hybrid, not a separate lineage: Brammer-lineage processing of WFC3/IR, drizzled on the CANDELS tangent point, with CANDELS ACS mosaics adopted as-is (Section 3.3).

---

## 2. Comparison table

| Approach | Code availability | Maintenance (dates) | License | Dependencies | Astrometry | Background | CR / artifacts | Weights | Fields / surveys |
|---|---|---|---|---|---|---|---|---|---|
| **grizli / CHArGE / DJA** | github.com/gbrammer/grizli | Last commit 2026-10-26; tag 1.14.3 (2026-10-22); mostly one author | MIT | `stwcs`, `tristars`, `mastquery`, `sep`; extras pin `drizzlepac<3.7.0`, `jwst<1.17`; WFC3/IR needs `reprocess_wfc3` + `hstcal` | Per-visit shift + rotation (scale optional) of a drizzled visit catalog; triangle matching (`tristars`); Gaia DR3 with proper motions, or LS DR9 / PS1 / field catalog; shift-only between exposures in a visit | AstroDrizzle `skysub` (`localmin`) + optional SEP mesh blotted to FLTs; WFC3/IR ramps re-fit from RAW for variable background | AstroDrizzle `driz_cr`; LACosmic for single ACS exposures; STScI persistence model mask; extra WFC3/IR bad-pixel masks; hand-drawn region masks | Visit stage: AstroDrizzle IVM. Mosaic stage for HST: one scalar per chip = median(1/ERR²); separate `_var` from full ERR² | CHArGE, ALCS, UNCOVER, MegaScience, CANUCS, MINERVA, DJA v7 fields |
| **Koekemoer "MosaicDrizzle"** | Not public (none found) | In use through 2025 (CEERS HDR1 v1.9, COSMOS-Web) | n/a | PyRAF/STSDAS MultiDrizzle in 2011; current stack unknown | Hierarchical: cross-correlation within orbit, then each orbit to an external catalog (shift + rotation); Gaia EDR3/DR3 in recent releases; ~5 mas quoted in 2011 | Per-exposure constant offset after masking sources on the visit stack | Two-pass `driz_cr` (S=1.2/SNR=3.5, then 0.7/3.0); ACS destripe + pixel CTE; persistence + warm-pixel flags; satellite trails masked by script after visual ID | Inverse variance from sky + dark + read noise × flat; source Poisson excluded | COSMOS-ACS, CANDELS, CLASH, HFF, BUFFALO, CEERS HDR1, COSMOS-Web F814W |
| **3D-HST (Skelton 2014)** | `threedhst` repo exists, last push 2017-07-07, no license file | Dormant | none stated | calwf3, MultiDrizzle, TweakReg, AstroDrizzle 1.1.9 | TweakReg to SExtractor catalogs of CANDELS mosaics; ~0.03" rms | Median + first-order polynomial per association; time-variable super-sky flats | MultiDrizzle `driz_cr_scale 2.5 0.7`; satellite trails by hand (DS9 polygons); persistence flagged, grown | Inverse variance from flat + dark + sky + read noise; no source Poisson | AEGIS, COSMOS, GOODS-N/S, UDS |
| **HLF / XDF (Illingworth, Magee)** | Not stated public | HLF v2.5 page last updated 2021-10-29; Magee reduced PRIMER (2024) | n/a | APSIS (ACS), WFC3RED (WFC3/IR), MultiDrizzle | Visit stacks cross-correlated to HUDF F850LP (XDF); HLF v2.x on a Gaia DR2 frame | Supermedian-frame subtraction; per-read sky subtraction before CALWF3 when background varies | CTE + bias-striping correction (post-SM4 ACS); persistence mask at 0.2 e-/s | MultiDrizzle inverse variance (1/rms²) | HUDF09, XDF, HLF-GOODS-S v2.0, HLF-GOODS-N v2.5 |
| **Frontier Fields / BUFFALO** | Not public | HFF v1.0 2014–2017; BUFFALO 2020 | n/a | Follows Koekemoer 2011 | Prior HST + ground catalogs (HFF); Gaia DR2 via Nonino catalogs (BUFFALO) | WFC3/IR time-variable sky removed in the ramp fit | ACS self-calibration, destriping; persistence + blob masks; every exposure inspected | Not described in the papers read | 6 HFF clusters + parallels |
| **RELICS** | Not public (uses AstroDrizzle) | 2019 | n/a | DrizzlePac / AstroDrizzle | WISE point-source catalog for absolute frame | Variable F105W sky **not** corrected | ACS CTE + destripe; satellite trails by hand; Brammer bad-pixel list | Inverse-variance maps as drizzle weights | 41 clusters |
| **COSMOS-DASH / 3D-DASH** | github.com/gbrammer/wfc3dash | Last push 2024-02-10 | MIT | calwf3 (CRCORR off), AstroDrizzle, grizli for PSFs | Galaxies in COSMOS ACS F814W catalog | Not described | AstroDrizzle on the per-read difference images | Scales with exposure time and drift | COSMOS F160W, 1.43 deg² |
| **HAP SVM / MVM (archive)** | github.com/spacetelescope/drizzlepac | 3.11.0 on 2026-04-28 | BSD-3 | drizzlepac, stwcs, tweakwcs | Automatic fit to Gaia (GAIAeDR3 default since 3.3.0); falls back to a priori guide-star WCS | AstroDrizzle sky matching | Pipeline DQ flags only at MVM stage; no realignment between visits | Not stated in the pages read | All ACS / WFC3 data in MAST |
| **hst123** | github.com/charliekilpatrick/hst123 | Last push 2026-07-24 | MIT | drizzlepac, vendored stwcs, jhat (optional), DOLPHOT (optional); Python 3.12 only | TweakReg (default) or JHAT; reference image or built from the data | AstroDrizzle options | AstroDrizzle | AstroDrizzle | Transients, progenitors (Kilpatrick 2021, 2022, 2023) |

---

## 3. Lineage by lineage

### 3.1 grizli / CHArGE / DAWN JWST Archive

**Repository facts** [verified]
- Last commit 2026-10-26 (`c8c9d325`, G. Brammer); latest tag `1.14.3` on commit `ec61b57` (2026-10-22); GitHub reports 49 open issues, 95 stars.
- License MIT (`LICENSE.txt`, "Copyright (c) 2016-2022 Gabe Brammer").
- Authors in the last ~50 commits and merged branches: Brammer 98, L. Robbins 29, P. Watson 20, others 8. Bus factor is effectively one.
- `setup.cfg`: `python_requires = >=3.9`; base requirements include `stwcs`, `tristars`, `mastquery>=1.8.1`, `sep`, `photutils<1.13`, `pillow<11.3`, `setuptools==67.8.0`, `astro-prospector`, `eazy>=0.8.3`.
- Extras: `[hst]` = `reprocess-wfc3`, `astroscrappy`, `drizzlepac<3.7.0`, `stsci.tools`, `wfc3tools`; `[jwst]` = `jwst<1.17`, `drizzlepac<3.7.0`, `snowblind>=0.2.1`, `crds<13.0`, `grismconf==1.32`.
- `pyproject.toml` build requires `oldest-supported-numpy` and `cython`. `environment.yml` installs `hstcal` from the retired astroconda channel plus git installs of `reprocess_wfc3` and `tristars`.
- CI matrix: Python 3.9–3.12 (`.github/workflows/python-package.yml`).
- `drizzlepac<3.7.0` means at most 3.6.2 (2023-11-27). drizzlepac 3.7.0 (2024-04-02) declared `numpy<2`; numpy-2 fixes appear from 3.9.0 (2024-12-16).

**Install conflict with this repo** [verified from both files]
- `pipeline/pyproject.toml` here requires Python ≥3.12, `jwst>=1.20,<1.21`, `numpy>=2.0`.
- grizli's declared extras cap `jwst<1.17` and `drizzlepac<3.7.0`. The two cannot share one environment as declared. Whether drizzlepac 3.6.2 runs under numpy 2 was not tested [unknown]; its release predates numpy 2.

**One code path for JWST and HST** [verified]
- `prep.process_direct_grism_visit` (`grizli/prep.py:5305`) handles both. JWST `cal`/`rate` files are given HST-style keywords (`jwst_utils.set_jwst_to_hst_keywords`, `prep.py:5629`) so AstroDrizzle can read them; HST files go through `stwcs.updatewcs` (`prep.py:5638`).
- Instrument branches are flags: `isACS = '_flc' in file` (also UVIS), `isWFPC2`, `isJWST` (`prep.py:5608-5628`).
- The final combine in both cases is grizli's own wrapper `utils.drizzle_from_visit` → `utils.drizzle_array_groups`, which calls `drizzlepac.adrizzle.do_driz` on arrays with an astropy WCS (`utils.py:8577-8578`, `8769-8786`). It does not use the `drizzle` package that `jwst` uses.

**Steps for an HST imaging visit** [verified, `grizli/prep.py` unless noted]
1. Inputs: `auto_script.fetch_files` requests `FLC` for ACS/WFC and WFC3/UVIS, but `RAW` for WFC3/IR (`pipeline/auto_script.py:1077`).
2. WFC3/IR is re-run from RAW with `reprocess_wfc3.make_IMA_FLT` to flatten ramps with variable background (`pipeline/reprocess.py:7-59`; method in WFC3 ISR 2016-16 per the `reprocess_wfc3` README). Needs `hstcal` (calwf3). An interactive Tk GUI (`reprocess.inspect`, `reprocess.py:75`) lets a person flag reads with satellite trails or Earth glow, which are then dropped and the ramp re-fit (`pop_reads`, `reprocess.py:148-167`); `make_masks` (`:178`) draws DS9 trail regions by hand.
3. `fresh_flt_file` (`:122`): copies the exposure, fetches `IDCTAB`/`NPOLFILE`/`D2IMFILE` for FLC (`:232-240`), ORs extra WFC3/IR bad-pixel masks into DQ (`$iref/badpix_spars200_Nov9.fits`, and packaged `wfc3ir_dark_badpix_2019.01.12.fits.gz`, `:301-330`), applies DS9 region masks if present (`:473-475`).
4. Single ACS/UVIS exposure: LACosmic via `astroscrappy.detect_cosmics` (sigclip 4.5, objlim 5, `:359-389`) and `find_single_image_CRs` (`:8857`).
5. Relative alignment inside the visit: `tweak_align` → `tweak_flt`, documented as "Intra-visit shift alignment" (`:6636-6652`), using `tristars` matching. For ACS it runs after a first CR-flagging drizzle (`:5913-5929`).
6. First AstroDrizzle: `skysub=True`, `skymethod='localmin'`, `driz_cr=True`, `final_wht_type="IVM"` (`:5861-5887`).
   - CR thresholds: ACS/UVIS `driz_cr_snr "3.5 3.0"`, `driz_cr_scale "1.2 0.7"` with a relaxed first pass `"15. 10.0"`; WFC3/IR `"8.0 5.0"` and `"2.5 0.7"` (`:5691-5738`).
   - Good DQ bits: ACS `64+32+128+256`, WFC3/IR `576+256`.
7. Absolute alignment: `align_drizzled_image` (`:1051`) on a SEP catalog of the visit mosaic.
   - Default model is `skimage.transform.EuclideanTransform`, i.e. shift + rotation (`:1172-1174`); a scale term is carried if another transform is passed.
   - Three matching iterations, then 8 nearest-neighbor refinements (`:1558-1633`); a fit with rms above `rms_limit` (2 pix) or fewer than 3 matches is discarded and the WCS left unchanged (`:1550-1556`).
   - The solution is applied to every FLT with `drizzlepac.updatehdr.updatewcs_with_shift` (`:6095-6107`). So absolute astrometry is per visit, not per exposure.
8. Reference catalog: `catalog.get_radec_catalog` (`grizli/catalog.py:2171`) tries a list in order. Function default is `GAIA, PS1, SDSS, WISE` (`prep.py:5350`); the DJA processor passes `LS_DR9, PS1, DES, NSC, GAIA` and `master_radec='astrometry_db'` (`aws/visit_processor.py:1853`).
   - Gaia query is DR3 from VizieR (`catalog="I/355/gaiadr3"`, `catalog.py:570`), with positions moved to the exposure epoch by `get_gaia_radec_at_time` using `apply_space_motion` (`catalog.py:458-519`, `2315-2347`).
   - `auto_script.fine_alignment` (multi-visit global fit, shift-only by default) still calls `get_gaia_DR2_vizier` (`pipeline/auto_script.py:4228`, `4336`).
9. Second AstroDrizzle with final CR parameters, `final_pixfrac=0.8` (`:6186-6207`).
10. Optional background: `blot_background` (`:4423`) fits a SEP mesh background on the visit mosaic and blots it back onto each FLT. `separate_chip_sky` (`:4936`) sets per-chip sky, by default only for `F200LP, F350LP, F600LP, F390W`.
11. Persistence: `apply_persistence_mask` (`:485-603`) reads STScI `_persist.fits` products, flags pixels where persistence > 0.6 × ERR, grows by 3 pixels, subtracts the model and adds it in quadrature to ERR. Whether those STScI products are still generated for new data is [unknown].
12. Final visit mosaic: `utils.drizzle_from_visit(pixfrac=1.0, kernel="square", weight_type="jwst")` (`:6378-6393`).

**Weights in the mosaic stage** [verified, `grizli/utils.py`]
- `drizzle_from_visit` (`:7644`) builds `wht = 1/ERR²` with DQ-masked pixels set to zero (`:8291-8293`).
- For `weight_type` starting with `jwst`, exposures that have `VAR_POISSON` get `1/(VAR_RNOISE + median(VAR_POISSON))`. HST FLT/FLC files have no such extension, so the code falls back to one number per chip, the median of `1/ERR²` (`:8300-8325`). HST ERR includes source Poisson noise; the median makes the weight nearly source-independent.
- With `weight_type='jwst_var'` (default of `cutout_mosaic`), a separate variance image from the full `ERR²` is drizzled alongside (`:8295-8298`, `8408-8413`).
- Output weights are rescaled by (input scale / output scale)⁴ (`:8483`).
- Open issue #166 (2023-06-30, still open) states the weighting fix "does not necessarily produce statistically robust inverse variance `wht` images".

**How DJA defines output grids** [verified unless noted]
- Field WCS are hard-coded in `grizli/aws/define_mosaics.py:make_wcs`: every call uses `pixscale=0.04` and most `theta=0` (north up); CEERS uses `theta=-310`. Dimensions are multiples of 2048 pixels (e.g. CEERS `NX, NY = 18, 6`, `:129-135`; Abell 2744 expanded from a fixed header, `:171-232`). It returns `half_sw`, whether NIRCam SW gets 2× smaller pixels (`:80-81`).
- DJA blog (2023-07-18): NIRCam LW, NIRISS and HST mosaics have 40 mas pixels; most SW mosaics 20 mas, subsampling the LW grid 2×2; the large PRIMER SW mosaics are 40 mas. Units are 10 nJy per pixel (AB zeropoint 28.9). Products are `_drc_sci`, `_drc_wht`, `_drc_exp` (exposure time, 4×4 binned) and `_wcs.csv`.
- `utils.half_pixel_scale` (`utils.py:6929`) makes the fine grid so that 2×2 blocks coincide with coarse pixels.
- `visit_processor.cutout_mosaic` (`aws/visit_processor.py:2595`) is the mosaic builder. It treats WFC3/IR and MIRI filters as "IR" and everything else (ACS, UVIS, NIRCam, NIRISS) as "optical" on the half-scale grid when `half_optical=True` (`:2819-2836`); it names outputs `drc` or `drz` accordingly (`:2944-2947`) and rescales to 10 nJy (`:2955-2967`). The driver that applies `half_sw` to SW only is not in the files I read [unknown].
- Exposures come from a Postgres table (`exposure_files`, `assoc_table`) and S3 (`grizli-v2/HST/Pipeline/{assoc}/Prep`) (`:2752-2771`, `2888-2891`). `res_query_from_local` (`:2395`) is the local substitute.
- "Tiles": `aws/field_tiles.py` cuts a field into tiles of 2304 or 4608 pixels at 80 mas (`:90-98`, root `{field}-080-{tile}`, `:795`) with `pixfrac=0.75`, `kernel='square'` (`:811-821`); these feed the map viewer.
- WFC3/IR at 40 mas is a factor 3.2 finer than the 0.128" native pixel. Earlier CHArGE mosaics used 0.1" (IR) and 0.05" (ACS/UVIS) with `pixfrac=0.33` (Kokorev et al. 2022).

**DJA versions and HST content** [verified]
- v7 is the current imaging release (from July 2023; entries through abells1063 v7.5 on 2025-01-09). Listings include HST filters (f435w … f160w, UVIS with a `u` suffix). The page states "Updated HST mosaics included here on the new COSMOS pixel grid" and, for GOODS-N, "new HST mosaics were generated as well".
- The DJA summary page says v6 "includes some HST mosaics that were not regenerated for v7".
- MINERVA DR0 (2026-01-13) labels its files `grizli-v8.0` at 40 mas, with HST re-reduced by the same pipeline.
- Astrometric reference varies by field: PRIMER visits aligned to a catalog from the F444W mosaic (DJA v7 page); UNCOVER F444W aligned to Legacy Surveys DR9, checked against Gaia DR3 at ~12 mas (Bezanson et al. 2024); Weaver et al. 2024 describe Gaia DR3.

**Reported limitations** [verified]
- Weight maps do not account for correlated noise from resampling (Bezanson et al. 2024).
- Weights exclude source Poisson noise; the user must rebuild full variance from the exposure map (DJA blog).
- Issue #166 on weights, above. Open install issues #116 (2022), #84, #287 "Missing GRIZLI_PATH" (2026-03).
- Weaver et al. 2024: HST/ACS fluxes differ by 10–20% from the GLASS team's photometry, attributed there to PSF handling, not to the mosaics.

**Could a third party run it on a Linux cluster today?**
- Yes in a dedicated environment, with work. Hard parts: the old `drizzlepac` pin; `hstcal` from conda for WFC3/IR; `$GRIZLI`, `$iref`, `$jref` trees and `fetch_default_calibs`; a visit-directory convention (`RAW/`, `Prep/`, `Persistence/`); the mosaic stage assumes the author's database and S3 unless `res_query_from_local` is used; satellite-trail handling is interactive; documentation says "more documentation is coming soon". [verified for each fact; overall judgment inferred]

### 3.2 Koekemoer et al. 2011 (CANDELS) and descendants

**What the 2011 paper did** [verified, ar5iv 1105.3754]
- Software: "MosaicDrizzle", built on MultiDrizzle and Drizzle, in PyRAF/STSDAS on an STScI cluster. The paper does not say it is public, and a search for a repository found none.
- ACS: row-by-row bias destriping (to ~0.3–0.4 e-); pixel-based CTE correction (Anderson & Bedin 2010); amplifier bias offsets fitted.
- WFC3/IR: calwf3 with 4σ up-the-ramp CR rejection; persistence and warm pixels (>5σ) flagged and excluded; residual flat structure corrected with sky flats. All reads kept; excluding high-background reads was "investigated but not adopted".
- Satellite trails: found visually, masked by a script in the undistorted frame.
- CR rejection: `driz_cr` with a minmed median, two passes (S=1.2, SNR=3.5; then S=0.7, SNR=3.0 in a 1-pixel ring); ACS masks extended 30 pixels along the readout to catch CTE trails.
- Astrometry: within an orbit by cross-correlation; then each orbit matched to an external catalog, brightest first, tolerance tightened to 0.1", solving shift and rotation. Scale change from breathing was not solved.
- References (Table 4): GOODS-N Subaru R; GOODS-S ESO WFI R; COSMOS Subaru i+; EGS CFHT R; UDS UKIRT K; each with existing HST ACS where available.
- Quoted accuracy: relative "better than a few milliarcseconds"; absolute ~5 mas (~0.1 ACS pixel).
- Background: sources masked on the smoothed visit stack (20–30% of pixels), sigma-clipped mean offset subtracted per exposure.
- Weights: inverse variance of background noise only, `(f·t)² / [(D + f·B) + σ_read²]` with f the inverse flat, t exposure time, D dark, B background. Source Poisson noise excluded on purpose.
- Grid: 0.06" for WFC3/IR, 0.03" for ACS and UVIS; `pixfrac=0.8`; one tangent point per field (GOODS-S 53.122751, −27.805089; UDS 34.406250, −5.2000000). Units e-/s.
- Kernel and correlated-noise treatment: not in the text I could read [unknown].
- Products: archive.stsci.edu/prepds/candels (v0.5 per epoch, v1.0 combined, 2013).

**Predecessor: COSMOS ACS (Koekemoer et al. 2007)** [verified]
- MultiDrizzle in IRAF/STSDAS; scattered-light master image subtracted; amplifier offsets corrected; CTE corrected in catalogs, not pixels.
- Astrometry: CFHT i* on a frame tied to VLA; offset + rotation per tile from 300–600 sources; ~5 mas relative to CFHT.
- Products at 0.03" (Gaussian kernel, for lensing) and 0.05" (square kernel, `pixfrac=0.8`).

**Descendants**
- CLASH (Postman et al. 2012): names MosaicDrizzle; FLT input; Subaru catalogs as reference; ACS destriping + CTE first [verified]. Pixel scales and any APSIS use were beyond the text retrieved [unknown].
- Frontier Fields (Lotz et al. 2017): 30 and 60 mas; alignment to prior HST + ground catalogs; v1.0 adds ACS destriping, ACS "self-calibration", WFC3/IR time-variable sky removed in the ramp fit, masks for blobs and persistence, visual inspection of every exposure [verified]. Details deferred to "Koekemoer et al. in prep." [verified]; I did not find that paper [unknown].
- BUFFALO (Steinhardt et al. 2020): "similar procedures to … Koekemoer et al. 2011"; Gaia DR2 via catalogs from M. Nonino; time-variable F105W sky handled per read [verified].
- RELICS (Coe et al. 2019): "similar to … Frontier Fields" but with AstroDrizzle; ACS CTE + destripe; no self-calibration; variable F105W sky not corrected; satellite trails by hand; WISE as absolute reference; 0.03" and 0.06" [verified].
- CEERS HDR1 (v1.9 mosaics): "follow procedures from Koekemoer et al. (2011)"; contact A. Koekemoer; 1,767 exposures from 8 programs; F606W, F814W, F105W, F125W, F140W, F160W; 0.03"; Gaia EDR3; `drz` in e-/s, `wht` inverse variance, `rms` "free from correlated noise" [verified]. No release date given.
- CEERS NIRCam (Bagley et al. 2023): NIRCam drizzled to 0.03" on "the same tangent point as the HST mosaics", reference catalog from the HST F160W mosaic tied to Gaia EDR3; HST–NIRCam rms 12–15 mas [verified].
- COSMOS-Web (Franco et al. 2025): reference catalog from a new 0.03" F814W mosaic, reprocessed following Koekemoer 2011 with "direct astrometric alignment to Gaia-DR3"; stars removed before use; JHAT for NIRCam alignment; 30 and 60 mas products; all tiles share CRVAL = (150.1163213, 2.200973097) [verified]. The paper does not name who reprocessed F814W [verified absence].
- COSMOS2025 (Shuntov et al. 2025): only F814W from HST, rebuilt with updated flats, biases and darks, Gaia DR3 [verified].
- PRIMER: the only statement I found is in ASTRODEEP-JWST (Merlin et al. 2024): "The images have been reduced by D. Magee, with ancillary HST data re-reduced from the CANDELS imaging, UVCANDELS … and supplemental programme 16872" [verified quote]. That places PRIMER team HST mosaics in family C, not A. No PRIMER reduction paper was found [unknown].

**Is any of the code public?** No repository or download for MosaicDrizzle was found in search; MultiDrizzle's successor AstroDrizzle is public in drizzlepac. Absence of a public release is [inferred] from the search, not a statement by the authors.

### 3.3 3D-HST (Skelton et al. 2014)

[verified, ar5iv 1403.3689]
- WFC3/IR only was re-reduced: FLTs from MAST, flat re-applied with time-variable super-sky flats; satellite trails masked by hand; Earth-limb exposures dropped; persistence flagged from STScI persistence images and grown.
- Background: median then first-order polynomial per association after masking sources.
- Alignment: TweakReg (shift, rotation, scale) to SExtractor catalogs from CANDELS ACS F814W or WFC3 F160W mosaics; rms ~0.03".
- Mosaics: AstroDrizzle 1.1.9, 0.06", square kernel, `pixfrac=0.8`, center and tangent point copied from CANDELS reference images.
- ACS: "obtained from the CANDELS program", not re-reduced.
- Weights: inverse variance from flat, dark, sky, read noise; no source Poisson. Units e-/s.
- The paper names no package. `gbrammer/threedhst` exists ("3D-HST grism analysis software", last push 2017-07-07, no license detected) [verified]. That grizli replaced it is [inferred].

### 3.4 Hubble Legacy Fields, XDF, HUDF09

- XDF (MAST page, v1.0, 2013-03-29) [verified]: ACS with APSIS (Blakeslee et al. 2003) plus supermedian subtraction, CTE and bias-striping correction for post-SM4 data; WFC3/IR with WFC3RED (Magee, Bouwens & Illingworth) plus per-read sky subtraction before CALWF3 when the time array disagrees with EXPTIME by >2%, persistence mask at 0.2 e-/s, visit alignment by cross-correlation to HUDF F850LP; 30 and 60 mas; MultiDrizzle inverse-variance weights.
- HLF (MAST page, v2.5, last updated 2021-10-29) [verified]: GOODS-S at v2.0 (7,491 exposures, 13 filters), GOODS-N at v2.5 (5,817 exposures, 11 filters); 30 mas (ACS, UVIS) and 60 mas (all); `pixfrac=0.8`; weight = 1/rms²; grid "tied to the Gaia DR2 reference frame", with an alternate grid on the original GOODS tangent point.
- Whitaker et al. 2019 [verified]: reduction details deferred to Illingworth et al. (2016, 2019 in prep.); catalog uses 60 mas mosaics; notes residual false clipping of ACS central pixels by CR rejection.
- Illingworth et al. 2016 full text could not be read (PDF only) [unknown]; that HLF uses the same APSIS/WFC3RED software as XDF is [inferred].
- Pipelines are not stated to be public [verified absence on the pages read].

### 3.5 DASH

[verified, Mowla et al. 2019 and 2022]
- calwf3 with `CRCORR=False`; each ramp split into NSAMP−2 difference images with `wfc3dash` so drift is integrated over 25 s; internal alignment per visit, then to galaxies in the COSMOS ACS F814W catalog; AstroDrizzle for CR masking and the mosaic; 0.1", square kernel, `pixfrac=0.8`.
- 3D-DASH: 1.43 deg² including all archival F160W; HLSP at archive.stsci.edu/hlsp/3d-dash.

### 3.6 Archive HAP products used directly

[verified, drizzlepac docs]
- SVM: one visit, all filters on one north-up grid, aligned to Gaia where possible (WCSNAME contains `FIT_SVM_<catalog>`); in production since 2020-12-17. GAIAeDR3 is the default catalog since drizzlepac 3.3.0.
- MVM: all exposures in a 0.2° × 0.2° sky cell (PanSTARRS-style projection cells); 0.04" "fine" scale for all, plus 0.12" "coarse" for WFC3/IR.
- Stated limits: MVM uses each input WCS as is, with no realignment between visits; exposures aligned only to 2MASS-class catalogs can be offset by a fraction to several pixels; CR rejection relies on flags set earlier.
- Entry points `runsinglehap` and `runmultihap` ship with drizzlepac (seen in its `pyproject.toml`).
- I found no 2023–2026 deep-field catalog paper that adopts HAP mosaics for photometry [inferred from the papers read].

### 3.7 hst123

[verified, GitHub README and API]
- Download from MAST, align with TweakReg or JHAT, AstroDrizzle, optional DOLPHOT. "Optimized for point-source photometry across multiple visits and filters."
- Inputs: ACS/WFC `flc`, WFC3/IR `flt`, WFC3/UVIS `flc`, WFPC2.
- Python pinned to 3.12; MIT; last push 2026-07-24; one main author.
- Not designed for deep extragalactic mosaics on a fixed external grid [inferred from the stated purpose].

---

## 4. What JWST-era catalog papers do for the HST bands (2023–2026)

| Paper / survey | HST treatment | Tag |
|---|---|---|
| CEERS (Bagley 2023; HDR1) | New Koekemoer-style mosaics from FLT/FLC, 0.03", Gaia EDR3; NIRCam put on the HST tangent point | verified |
| COSMOS-Web / COSMOS2025 | F814W re-mosaicked (Koekemoer 2011 method), Gaia DR3; only HST band used | verified |
| JADES DR5 (Robertson 2026; Johnson 2026) | Adopts HLF GOODS-S v2.0 and GOODS-N v2.5, "re-registered to the JWST NIRCam mosaic astrometric frame" and reprojected to the JADES pixel scale. Earlier JADES reference catalogs came from CHArGE HST mosaics; DR5 ties NIRCam directly to Gaia DR3. JADES pixel scale (0.02999476" GOODS-S) was chosen to match the HST mosaics | verified |
| JADES 2023 (Rieke) | Aligned to HST images registered to Gaia DR2 (Brammer, priv. comm.); HLF for PSFs | verified |
| UNCOVER (Bezanson 2024; Weaver 2024), MegaScience (Suess 2024) | All archival HST re-drizzled with grizli on the JWST grid, 40 mas; DJA v7.2 | verified |
| CANUCS DR1 (Sarrouh 2025) | HST and JWST both processed with grizli; 40 mas | verified via search summary and MAST page only |
| MINERVA DR0 (2026) | NIRCam and HST "reduced consistently using GRIZLI", 0.04", includes CANDELS re-reductions | verified |
| PANORAMIC II (2025) | Builds on DJA grizli mosaics | verified via search summary only |
| ASTRODEEP-JWST (Merlin 2024) | Adopts per field: Brammer grizli mosaics (Abell 2744), CANDELS EGS + CEERS HDR1 F105W, HLF (JADES, NGDEEP), Magee re-reductions (PRIMER); all reprojected to the 30 mas NIRCam grid; residual HST offsets removed by shifting F160W to NIRCam F150W | verified |
| EPOCHS | HST provenance not established | unknown |

Pattern: nobody in this sample runs the default archive drizzle products. Groups either (i) own a full pipeline and re-drizzle HST from exposures onto the NIRCam grid (grizli teams; Koekemoer for CEERS / COSMOS-Web), or (ii) adopt an existing HLSP mosaic, re-register it to the NIRCam frame and reproject (JADES, ASTRODEEP). [verified per row; the generalization is inferred]

---

## 5. What is realistically reusable

- **drizzlepac** (BSD-3, STScI, 3.11.0 on 2026-04-28, numpy-2 work from 3.9.0): AstroDrizzle, TweakReg, `updatehdr`, HAP runners. This is the maintained base every public approach sits on. [verified]
- **grizli** (MIT): the only complete, public, currently developed deep-field HST imaging pipeline. Reuse as a whole is blocked in this repo's environment by its pins (Section 3.1). The individual methods are short and readable and can be re-implemented or vendored under MIT:
  - Gaia DR3 query + proper motion to epoch (`catalog.py:458-519`, `565`).
  - Per-visit shift + rotation fit with triangle matching and refinement (`prep.py:1051-1675`; `tristars`, MIT, last push 2022-11-20).
  - Single-exposure ACS CR flagging with LACosmic (`prep.py:359-389`, `8857`).
  - Persistence mask from STScI products (`prep.py:485-603`).
  - Mesh background blotted to exposures (`prep.py:4423`), per-chip sky (`prep.py:4936`).
  - Packaged WFC3/IR bad-pixel masks under `grizli/data/`.
  - Array-level drizzle onto an arbitrary astropy WCS (`utils.py:8492-8821`).
- **reprocess_wfc3** (MIT, last push 2023-07-11): WFC3/IR ramp flattening for variable background; only needed if starting from RAW, which is outside the stated FLT/FLC scope. [verified]
- **wfc3dash** (MIT, last push 2024-02-10): only for DASH data. [verified]
- **hst123** (MIT, active): useful as a current example of TweakReg / JHAT + AstroDrizzle orchestration on Python 3.12. [verified]
- **Koekemoer and Magee pipelines**: not obtainable as code; only their papers (recipes) and products (reference mosaics and astrometric catalogs) can be reused. [inferred]

---

## 6. Not verified

- Illingworth et al. 2016 full text (PDF could not be parsed here); HLF processing details are taken from the XDF and HLF MAST pages.
- Whether MosaicDrizzle, APSIS or WFC3RED were ever released publicly; only the absence of a findable repository is established.
- Koekemoer 2011 drizzle kernel and correlated-noise treatment; CLASH pixel scales.
- The Frontier Fields pipeline paper ("Koekemoer et al. in prep.").
- A PRIMER team paper describing its HST mosaics; the Magee attribution rests on one sentence in Merlin et al. 2024.
- The DJA driver script that applies `half_sw`, and the astrometric reference used for HST visits in each DJA field.
- HAP SVM pixel scales per detector, and whether the archive has moved from GAIAeDR3 to Gaia DR3.
- Whether STScI WFC3/IR persistence products still exist for recent data.
- Whether drizzlepac 3.6.2 works under numpy 2; grizli was not installed or run (not permitted in this task).
- EPOCHS HST provenance; CANUCS and PANORAMIC details come from search summaries and a MAST page, not the papers.
- hst123 PyPI page did not load; facts are from its GitHub README and API.

---

## 7. Sources

| URL | Status |
|---|---|
| https://github.com/gbrammer/grizli (clone at `c8c9d325`; `setup.cfg`, `pyproject.toml`, `LICENSE.txt`, `environment.yml`, `grizli/prep.py`, `grizli/utils.py`, `grizli/catalog.py`, `grizli/pipeline/auto_script.py`, `grizli/pipeline/reprocess.py`, `grizli/aws/visit_processor.py`, `grizli/aws/define_mosaics.py`, `grizli/aws/field_tiles.py`) | read |
| GitHub API: repos gbrammer/grizli, issues list, issue #166 | read |
| GitHub API: gbrammer/reprocess_wfc3 (README, metadata), gbrammer/tristars, gbrammer/wfc3dash, gbrammer/threedhst | read |
| GitHub API: charliekilpatrick/hst123 (README, commits, metadata) | read |
| GitHub API: spacetelescope/drizzlepac releases; `pyproject.toml` at tag 3.6.2 | read |
| https://raw.githubusercontent.com/spacetelescope/drizzlepac/main/CHANGELOG.rst | read (summarized) |
| https://grizli.readthedocs.io/en/latest/grizli/install.html | read |
| https://dawn-cph.github.io/dja/imaging/v7 | read, first 100k of 802k characters |
| https://dawn-cph.github.io/dja/imaging/summary | read |
| https://dawn-cph.github.io/dja/blog/2023/07/18/image-data-products/ | read (summarized) |
| https://ar5iv.labs.arxiv.org/html/1105.3754 (Koekemoer 2011) | read |
| https://ar5iv.labs.arxiv.org/html/astro-ph/0703095 (Koekemoer 2007) | read |
| https://ar5iv.labs.arxiv.org/html/1403.3689 (Skelton 2014) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/2207.07125 (Kokorev 2022) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/2302.10936 (Valentino 2023) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/1605.06567 (Lotz 2017) | read |
| https://ar5iv.labs.arxiv.org/html/2001.09999 (Steinhardt 2020) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/1903.02002 (Coe 2019) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/1106.3328 (Postman 2012) | read, first 100k characters; image-processing section truncated |
| https://ar5iv.labs.arxiv.org/html/1908.05682 (Whitaker 2019) | read |
| https://ar5iv.labs.arxiv.org/html/1808.04379 (Mowla 2019) | read |
| https://ar5iv.labs.arxiv.org/html/2206.01156 (Mowla 2022) | read |
| https://ar5iv.labs.arxiv.org/html/2211.02495 (Bagley 2023) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/2211.07865 (Casey 2023) | read, first 100k characters; nothing on HST reprocessing found |
| https://arxiv.org/html/2506.03256 (Franco 2025) | read |
| https://arxiv.org/html/2506.03243 (Shuntov 2025) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/2212.04026 (Bezanson 2024) | read |
| https://ar5iv.labs.arxiv.org/html/2301.02671 (Weaver 2024) | read, first 100k characters |
| https://ar5iv.labs.arxiv.org/html/2404.13132 (Suess 2024) | read |
| https://ar5iv.labs.arxiv.org/html/2306.02466 (Rieke 2023) | read |
| https://arxiv.org/html/2601.15954 (JADES DR5 imaging) | read, first 100k characters |
| https://arxiv.org/html/2601.15956v1 (JADES DR5 catalog) | read, first 100k characters |
| https://arxiv.org/html/2409.00169v3 (Merlin 2024) | read, first 100k characters |
| https://ceers.github.io/hdr1.html | read |
| https://web.corral.tacc.utexas.edu/ceersdata/HDR1/README.txt | read (summarized) |
| https://archive.stsci.edu/prepds/candels/ | read |
| https://archive.stsci.edu/prepds/hlf/ | read |
| https://archive.stsci.edu/prepds/xdf/ | read |
| https://minerva.colorado.edu/data-release.html | read |
| https://drizzlepac.readthedocs.io/en/latest/mast_data_products/hap_introduction.html | read |
| https://drizzlepac.readthedocs.io/en/latest/mast_data_products/mvm_processing.html | read |
| https://drizzlepac.readthedocs.io/en/latest/mast_data_products/astrometry.html | read |
| https://arxiv.org/abs/1606.00841 (Illingworth 2016) | abstract only |
| https://arxiv.org/pdf/1606.00841 | Unable to access this page as text: PDF could not be parsed |
| https://ar5iv.labs.arxiv.org/html/1606.00841 | Unable to access this page: redirects to the abstract |
| https://pypi.org/project/hst123/ | Unable to access this page: returned a load error |
| Web searches (MosaicDrizzle code; WFC3RED/APSIS; PRIMER HST; JADES HST; CANUCS; EPOCHS; DJA documentation) | search summaries only; used as leads, marked where relied upon |
