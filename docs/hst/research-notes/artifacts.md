# HST instrument artifacts and gotchas: ACS/WFC and WFC3/IR

Research date: 2026-11-11. Scope: imaging only, starting from calibrated FLT/FLC exposures, drizzling our own mosaics.

## How to read this file

Tags on every substantive claim:

- **[verified]**: the source was fetched and read in this session.
  - Repo files and PDFs converted to text (marked "direct" in the source list) were read as raw text.
  - hst-docs.stsci.edu and stsci.edu web pages were read through a fetch-and-summarize tool, so quoted numbers are as that tool reported them. Treat a single such number as good, but re-open the page before hard-coding it.
- **[inferred]**: from memory, a secondary source, or arithmetic on verified numbers.
- **[unknown]**: looked for and not found.

Document versions read:

| Document | Version | URL |
|---|---|---|
| ACS Data Handbook (ACS DHB) | v14.0, January 2026 (Cycle 34) | https://hst-docs.stsci.edu/acsdhb |
| ACS Instrument Handbook (ACS IHB) | v25.0, December 2025 (Cycle 34) | https://hst-docs.stsci.edu/acsihb |
| WFC3 Data Handbook (WFC3 DHB) | v6.0, June 2024 | https://hst-docs.stsci.edu/wfc3dhb |
| WFC3 Instrument Handbook (WFC3 IHB) | v18.0, December 2025 (Cycle 34) | https://hst-docs.stsci.edu/wfc3ihb |
| hstcal (calacs) | `ACS_CAL_VER "10.5.0 (16-Mar-2026)"` | `upstream/hstcal/pkg/acs/include/acsversion.h:5` |

`upstream/` below means `/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/upstream/`.

Corrections to the leads I was given (details in the notes):

1. ACS LED post-flash is **not** common or generally recommended. STScI recommends it "only in very limited circumstances". What changed in January 2015 is that the calibration darks are post-flashed.
2. The satellite tool is the module `acstools.satdet` with the function `detsat`; both names are in use in the docs.
3. "Figure-eight ghost" is the WFC3/UVIS name. The ACS/WFC handbook describes pairs of elliptical annuli instead.
4. IR blob pixels (DQ 512) are **not** dropped by default drizzle settings; they are treated as good and corrected by delta-flats since October 2020.
5. DASH has been unavailable since HST entered reduced-gyro mode in June 2024; only archival DASH data exist.
6. WFC3/IR FLT files carry **no** cosmic-ray flags in DQ (the 8192 flag lives only in the IMA file).

---

## 1. ACS/WFC summary table

| Artifact | How recognized | Handled by calacs / MAST products? | Extra handling by community pipelines / STScI tools |
|---|---|---|---|
| Parallel CTE loss and trails | Trails pointing away from the amplifiers (toward the chip gap) behind CRs, hot pixels, stars; worst at low background, far from amplifier; grows with date | Yes: `PCTECORR` → `_flc`. Generation 3 (parallel + serial) since calacs 10.4.0 (2024-05-07). Reconstruction error ~25% of the correction; fails at background < ~20–30 e⁻ | Use FLC, never FLT. Grow CR masks ~30 px along the trail direction (CANDELS). Self-calibration with > 8 exposures (Frontier Fields). `acstools.acsphotcte` for point-source photometry |
| Serial CTE | 0.005–0.02 mag loss, 0.01–0.035 px centroid shift along x | Yes in Gen 3, full-frame post-SM4 only (subarrays since calacs 10.5.0) | None needed if FLC retrieved after the 2024 reprocessing |
| Bias striping (post-SM4) | Row-correlated stripes, σ ≈ 0.9 e⁻, all rows, both amps of a chip | Yes for full frame (prescan-based, residual ~0.4 e⁻). Not for subarrays | `acstools.acs_destripe_plus` for subarrays or when the prescan fit is insufficient; CANDELS/CLASH/XDF ran the science-pixel destripe |
| Bias shift, crosstalk (post-SM4) | Shift: 0.02–0.3% of previous pixel signal. Crosstalk: faint negative mirror ghosts in the other quadrant | Yes, in `doBlev` for full frame | CANDELS: negligible at GAIN=2 |
| Quadrant (amplifier) offsets | Sky steps at quadrant boundaries, ~0.3 DN random per exposure | **No** (random component not removed) | Per-quadrant sky fit: CANDELS, COSMOS, HUDF; grizli `clean_amplifier_residuals`, `separate_chip_sky` |
| Hot / warm / unstable pixels | DQ 16 (hot > 0.14 e⁻/s), 64 (warm), 32 (unstable) | Dark-subtracted and flagged; superdark per anneal, weeks late | Dither; choose which bits are "good" (grizli keeps 64+32+128+256 for ACS); self-calibration |
| Sink pixels | DQ 1024, data after 2015-01-15 only; 1–3% of pixels flagged | Flagged only (`SINKCORR`) | Treat 1024 as bad (default) or good at high background |
| Cosmic rays | 1.5–3% of pixels per 1000 s; DQ 8192 (acsrej) / 4096 (drizzle) | `acsrej` only for CR-SPLIT / repeat-obs at one pointing. Dithered: AstroDrizzle `driz_cr` in DRC only; FLC flags depend on product generation | Own `driz_cr` pass per visit then per overlap; single exposures: L.A.Cosmic (`astroscrappy`) as in grizli |
| Satellite trails | Linear streak; ~8–10% of full-frame images by 2022 | **No** | `acstools.findsat_mrt` (85% recovery, 2.5–10% false), `acstools.satdet`; manual masks in all legacy surveys |
| Saturation bleed | Column bleed, DQ 256; >64,000 DN flagged A-to-D (2048) | Flagged | grizli has `apply_saturated_mask` (`prep.py:770`, body not read); CANDELS excluded bleed columns from cross-correlation alignment |
| Ghosts, glint, dragon's breath, edge glow, scattered Earth light | See notes | **No** | Manual masks; survey-level "master scattered light" image (COSMOS, HUDF) |
| Dust motes, flat residuals | Rings ~100 px; flats good to ~1% | Flat-fielded | Sky flats for deep fields (GOODS, HUDF) |
| F850LP long-wavelength halo | Extended PSF wings, color-dependent aperture loss past 9000 Å | No (it is the PSF) | Color-dependent aperture corrections (Sirianni et al. 2005); PSF matching |
| Chip gap | 2.5″ (~50 px), no data | n/a | Dither ≥ 3″ in y, or rely on other epochs |
| Time-dependent distortion | Linear skew drifting ~25 mas over 20 yr; X-scale / Y-skew jump at SM4 | Yes, in IDCTAB (run `updatewcs` with current reference files) | Re-fit shifts/rotation (and scale) per visit to Gaia |
| Guiding failure / drift | `EXPFLAG` ≠ NORMAL; `_jif`/`_jit` keywords; smeared PSF | Flagged in headers only; exposure still delivered | grizli drops visits with ≥ 2 bad `EXPFLAG`; hst1pass `qfit` > 0.2 (ACS ISR 2025-01) |

## 2. ACS/WFC detailed notes

### 2.1 Timeline that sets which artifacts apply

- 2002-03 launch; 2006-07-04 WFC set point lowered from −77 °C to −81 °C after the Side-2 switch, cutting hot pixels by almost 50% and requiring new L-flats (0.6% peak-to-peak at F435W, 0.15% at F814W; ACS ISR 2006-06). [verified] ACS IHB §4.3, ACS DHB §4.4.
- June 2006 and January 2007: CCD Electronics Box and power supply failures; WFC lost January 2007. 2009-05-16 (SM4): CEB-R installed with a SIDECAR ASIC; WFC restored, HRC not. [verified] ACS IHB §2.1.
- Pre-SM4 read noise 5.5 e⁻; post-SM4 3.75–5.65 e⁻ (amplifier D degraded in 2013 and 2020). [verified] ACS IHB §2.1, §4.2, §4.3.
- Post-SM4 only: bias striping, bias shift, bias gradient (5–10 DN per quadrant, removed by the superbias). Pre-SM4 only: bias jumps of a few tenths of a DN over hundreds of rows, not detected by calacs. [verified] ACS DHB §4.2.
- Cycle 34: ACS/WFC "may be offered as shared-risk and receive minimal calibration and support". [verified] ACS IHB §2.2. Calibration reference files for 2026+ data may arrive more slowly. [inferred]

### 2.2 CTE

1. What: radiation-induced traps capture charge during parallel readout and release it later. Flux is lost and trailed along columns, away from the amplifier. [verified] ACS DHB §4.6.
2. Recognize: trails over 50 px behind CRs, hot pixels and stars; worst next to the chip gap (farthest from the amplifiers) and at low background. In 2023 a 100 e⁻ star at chip center on a 30 e⁻ background loses ~33%; at very low background up to 90% is lost and cannot be reconstructed. [verified] ACS IHB §4.3, §2.6.
3. Standard calibration:
   - Pixel-based correction after Anderson & Bedin (2010), first in calacs in 2012 ("PixelCTE 2012"); Generation 2 in calacs 9.2.0 (2017-06-01, ACS ISR 2018-04); Generation 3 "Par/Serial PixelCTE 2023" in calacs 10.4.0 (2024-05-07, ACS ISR 2024-07), default on all post-SM4 full-frame WFC. Serial correction extended to post-SM4 subarrays in 10.5.0 (2026-03-16). [verified] `upstream/hstcal/pkg/acs/include/acsversion.h:12-24`, `upstream/hstcal/pkg/acs/CHANGELOG.txt:1-17,121-124`; ACS DHB §3.4; ACS IHB §4.3.
   - Products: `_flc` (corrected) beside `_flt` (uncorrected), `_crc`, `_drc`. Darks use the CTE-corrected `DRKCFILE`. [verified] ACS DHB §3.4.
   - Accuracy: "good to about 25%" of the correction amplitude (DHB) / "75% reconstruction accuracy" for backgrounds > 30 e⁻ (IHB). The correction "cannot restore the lost S/N". [verified] ACS DHB §4.6, ACS IHB §2.6.
   - It is a deconvolution: pixel-to-pixel noise in the corrected image is somewhat higher; read noise is treated as a separate component that did not take part in the trailing. [verified] Koekemoer et al. 2011 §5.2.2 (direct).
   - Extended sources: large-aperture fluxes good to ~0.05 mag if background > 20 e⁻/px and source ≥ ~300 e⁻ per exposure; small-scale measurements can be off by > 0.1 mag in recent data unless within ~512 px of the serial register; artificial asymmetries appear. [verified] ACS ISR 2025-02 abstract (STScI ISR listing).
   - FLC reaches perfect-CTE point-source depth only above ~100 e⁻/px background; best faint-source S/N near 30 e⁻/px. [verified] ACS ISR 2024-02 abstract.
4. Extra handling:
   - Post-flash: an LED illuminates the shutter; pattern varies ~50% center to corner (factor of two in ACS ISR 2014-01). "Not recommended for use during science exposures until relatively recently in 2013"; then and now only for faint compact targets with background < 20–30 e⁻ (short or narrow-band exposures). ACS IHB (Dec 2025): "recommended only in very limited circumstances". Calibration darks have been post-flashed since January 2015. `FLSHCORR` subtracts the flash; its noise stays. [verified] ACS ISR 2014-01 (direct), ACS IHB §2.6, §4.3, ACS DHB §3.4.
   - CR trails: CANDELS convolved the CR mask with a 30 px linear kernel opposite to the readout direction and rejected again in that region. [verified] Koekemoer et al. 2011 §5.4 (direct).
   - Point-source photometric formula: ACS ISR 2022-06; `acstools.acsphotcte`; `hst1pass` (ACS ISR 2022-02). [verified] ACS DHB §4.6; module present at `upstream/acstools/acstools/acsphotcte.py`.
   - Forward model for simulations: `acstools.acscteforwardmodel`. [verified] `upstream/hstcal/pkg/acs/CHANGELOG.txt:108-111`.
   - COSMOS (2007) had no pixel correction and corrected catalog quantities instead. Any pre-2012 high-level product is uncorrected. [verified] Koekemoer et al. 2007 §3.1 (direct).

### 2.3 Bias striping, bias shift, crosstalk, quadrant offsets

1. Striping: 1/f noise (1 mHz–1 Hz) on an ASIC reference voltage applied after correlated double sampling; nearly uniform along a row across both amplifiers of a chip. Gaussian σ 0.74 e⁻ with a negative tail, 0.9 e⁻ overall; stable 2009–2023, "less than 20% of the WFC read noise". [verified] ACS DHB §4.2 (ACS ISR 2011-05, 2024-03).
2. calacs: in `doBlev`, per-row residual from 96 prescan pixels (24 per amplifier × 4), stated uncertainty 0.4 e⁻. Subarrays "contain too few prescan pixels in each row", so striping is left in and `PCTECORR` defaults to OMIT for them. [verified] ACS DHB §3.4.
3. Extra: `acstools.acs_destripe_plus` runs calacs with the standalone `acs_destripe` (science-pixel, sigma-clipped row fit) between ACSCCD and ACSCTE; "useful for when the built-in CALACS destriping algorithm using overscans is insufficient or unavailable". [verified] `upstream/acstools/acstools/acs_destripe_plus.py:3-16`. CANDELS reached 0.3–0.4 e⁻ residual with the Grogin et al. 2010 row fit. [verified] Koekemoer et al. 2011 §5.2.2 (direct).
4. Bias shift 0.02–0.30% of the previous pixel's signal (ACS ISR 2012-02) and crosstalk (ACS ISR 2010-02) are removed in `doBlev` for full-frame post-SM4 data. Intra-CCD crosstalk ~0.007% at GAIN=2. [verified] ACS DHB §3.4, §4.2, §4.5; Koekemoer et al. 2011 §5.6 (direct); `CHANGELOG.txt:197-198` (scaling 9.1e-5).
5. Quadrant offsets: prescan-to-image offsets up to 3.5 DN are in the superbias, but a random ~0.3 DN per-exposure component is not: "sky background levels often appear discontinuous across the boundaries of adjacent image quadrants after calacs processing". STScI advice: measure and subtract sky per quadrant. [verified] ACS DHB §4.2.
   - COSMOS: "bias levels often vary by a few tenths of a count ... measured and corrected for each exposure". HUDF: iterative correction of a few tenths of an electron. CANDELS: iterative clipped fit of quadrant differences. [verified] Koekemoer et al. 2007 §3.1 (direct); Beckwith et al. 2006 (ar5iv); Koekemoer et al. 2011 §5.2.2 (direct).
   - grizli: `clean_amplifier_residuals` (2-D polynomial per chip in the detector frame, source-masked) and `separate_chip_sky`. [verified] `upstream/grizli/grizli/prep.py:9042`, `:4936`.

### 2.4 Hot, warm, unstable and sink pixels; darks

- Dark current 6.8 e⁻/px/hr at launch, 53–65 e⁻/px/hr in December 2025; growth ~2.7 e⁻/px/hr/yr. [verified] ACS DHB §4.3, ACS IHB §4.3.
- Thresholds since 2015-01-15 (and applied to all archival data in the 2018 recalibration): warm 0.06–0.14 e⁻/s (DQ 64), hot > 0.14 e⁻/s (DQ 16), unstable (DQ 32). Creation rates: warm stable 0.16%/yr, hot stable 0.10%/yr of the detector. [verified] ACS DHB §4.3, §3.4.
- Monthly anneal heals only 1–4% (DHB) / ~3% (IHB) of WFC hot pixels. One superdark per anneal cycle, available weeks after the observation: "using an old superdark reference file will produce a sub-optimal dark correction". [verified] ACS DHB §4.3.
- "Hot pixels cannot be eliminated in post-observation processing without dithering between exposures." [verified] ACS DHB §4.3.
- Sink pixels: charge traps that swallow charge, with a trail of low pixels upstream and an excess downstream in ~30% of cases. 0.3–0.5% of pixels per anneal; 1–3% of pixels receive DQ 1024 depending on background; flagged only for data after January 2015 (`SINKCORR`, `SNKCFILE`; calacs 9.1.0, 2017-02-21). [verified] ACS DHB §3.4, §4.3; `CHANGELOG.txt:127-128`.
- Frontier Fields: imperfectly corrected hot pixels leave the same residual pattern in every image; "with multiple exposures (> 8), it is possible to self-calibrate out this pattern and regain ~20% in F435W depth". Needs exposures within one anneal cycle. RELICS did not self-calibrate (too few exposures). [verified] Lotz et al. 2017 §5 (direct); Coe et al. 2019 (ar5iv).

ACS/WFC DQ bits (ACS DHB Table 3.4): 1 Reed-Solomon; 2 fill; 4 bad detector pixel; 8 masked by aperture; 16 hot; 32 unstable; 64 warm; 128 bias structure (bad columns); 256 full-well saturation; 512 bad in reference file; 1024 sink; 2048 A-to-D saturation; 4096 CR by AstroDrizzle; 8192 CR by acsrej; 16384 reserved (satellite trail masks). [verified] ACS DHB §3.4.

### 2.5 Cosmic rays

- 1.5–3% of pixels per 1000 s (factor ~2 with SAA proximity); median event covers 9 WFC pixels; deposition peaks near 1000 e⁻. [verified] ACS DHB §4.3, ACS IHB §4.3.
- STScI planning advice: one orbit → four 500–600 s exposures; three ~800 s acceptable; two 1200 s "not recommended". Dithering preferred over CR-SPLIT. [verified] ACS IHB §4.3.
- calacs: `acsrej` combines only CR-SPLIT / repeated exposures at one pointing (products `_crj`/`_crc`, DQ 8192). It "will not combine images from multiple positions within an association"; dithered data get CR rejection only in AstroDrizzle. A single exposure gets none. [verified] ACS DHB §3.2, §3.4.
- With N exposures: COSMOS (4 × 507 s) expects ≪ 1 pixel hit in all four, up to ~60 pixels hit in three of four per chip pair; CANDELS (2–4 per filter per orbit): for two exposures ~2000–6000 pixels are hit in both. [verified] Koekemoer et al. 2007 §3.3, Koekemoer et al. 2011 §5.4 (direct).
- `driz_cr` settings used by the legacy surveys: `minmed` median; SNR "3.5 3.0" (COSMOS: 4 and 3), scale "1.2 0.7". Same as the AstroDrizzle defaults. [verified] Koekemoer et al. 2011 §5.4, 2007 §3.3 (direct); `upstream/drizzlepac/drizzlepac/pars/astrodrizzle.cfg:69,88,91`.
- Single-exposure visits: grizli runs `astroscrappy.detect_cosmics` (`sigclip=4.5, sigfrac=0.3, objlim=5.0`, flags DQ 1024) when an ACS/UVIS visit has exactly one exposure, and again via `find_single_image_CRs` for parts of a mosaic covered by one exposure (found from the drizzle context image). [verified] `upstream/grizli/grizli/prep.py:359-389`, `:5615`, `:5813-5816`, `:8857-9040`.
- grizli relaxes the first-pass ACS rejection to SNR "15. 10.0" before alignment is refined, then uses "3.5 3.0". [verified] `upstream/grizli/grizli/prep.py:5691-5694`, `:5733-5735`.

### 2.6 Satellite trails

- ACS/WFC trail rate rose from 0.30 ± 0.02 per hour (2002) to 0.62 ± 0.06 per hour (2022); 97 of 1159 images searched in 2022 had a trail (8.4%). ACS DHB quotes "~10% of full-frame WFC images". [verified] ACS ISR 2022-08 Table 2 (direct), ACS DHB §4.5.
- Kruk et al. 2023 (Nature Astronomy 7, 262): ~2.7% of all HST exposures 2002–2021 crossed by a satellite, increasing with time. [inferred: ADS abstract seen through a search snippet only; the article page was not readable]
- Not run in the pipeline. [verified] ACS DHB §4.5.
- `acstools.findsat_mrt` (Median Radon Transform): sensitive to trails ~0.1× the background noise per pixel, about ten times more sensitive than `satdet`; recovers ~85% of by-eye trails in Frontier Fields data; false rate 2.5% after removing diffraction-spike angles, ~10% in a larger sample; unreliable for trails < 500 px, in image corners, and in dense fields; also picks up diffraction spikes, glint and scattered light. `WfcWrapper` handles binning, background and bad pixels; masks go to DQ 16384 via `update_dq`. [verified] ACS ISR 2022-08 §5 (direct); https://acstools.readthedocs.io/en/latest/findsat_mrt.html
- `acstools.satdet.detsat` (edge detection + probabilistic Hough transform, ACS ISR 2016-01): approximate segments; ~22 s per chip. [verified] https://acstools.readthedocs.io/en/latest/satdet.html
- Every legacy survey masked by hand (CANDELS, HUDF, XDF, Frontier Fields, RELICS). [verified] Koekemoer et al. 2011 §5.6, Lotz et al. 2017 §5.2 (direct); XDF, RELICS (ar5iv).

### 2.7 Optical artifacts

All from ACS DHB §4.5 unless noted; none are removed by calacs. [verified]

- Ghosts: pairs of elliptical annuli along the negative diagonal from bright sources in the amplifier-D quadrant, each up to > 0.2% of the source; F660N double rings.
- Glint: thin diagonal ray when a bright star sits in a band from the chip gap to the middle of WFC1 (ACS ISR 2024-05).
- Dragon's breath and edge glow: light bouncing between CCD and the knife-edge mask from stars just off the field (ACS ISR 2016-06).
- Scattered Earth light: at 14° limb angle the sky rises ~100× with gradients up to ~20%. Survey fix: median "master scattered-light image" scaled and subtracted (COSMOS), or object-masked median smoothed over 100 px, ~2% of sky (HUDF). Two COSMOS tiles had to be re-observed because of scattered light from adjacent stars. [verified] Koekemoer et al. 2007 §2.1, §3.1 (direct); Beckwith et al. 2006 (ar5iv).
- Diffraction spikes along x and y; charge bleed along y with < 10% spilling along x (ACS ISR 2020-07). WFC saturates at ~77,400 e⁻ (72,000–82,000); with GAIN=2 the summed bleed is photometric to < 1% for nearly 4 magnitudes past saturation. [verified] ACS DHB §4.7, ACS IHB §4.3.
- Dust motes: ~100 px rings (CCD window); a 350–400 px mote in F606W. Before April 2004 filter-wheel step errors shifted motes and gave 1–3% flat errors (`FWOFFSET`).
- Long-wavelength halo: red photons scatter from the mounting substrate. WFC has a front-side metallization that removes it below 9000 Å; past 9000 Å (F850LP) "extremely red stars show a progressive loss of flux in small to moderate sized apertures as a function of color"; extra spikes along x beyond 9500 Å. [verified] ACS IHB §5.2, §5.6. Magnitude for WFC F850LP: [unknown] (Sirianni et al. 2005 not read).
- Fringing: "ACS CCDs exhibit fringing longward of ~7500 Å", but "only the F892N filter shows a fringe pattern for white light illumination". No broadband fringe correction exists or is described for F814W/F850LP. [verified] ACS IHB §4.2.
- "Figure-eight" ghosts are a WFC3/UVIS feature. [verified] https://www.stsci.edu/hst/instrumentation/wfc3/performance/anomalies

### 2.8 Chip gap, dithers, flats, distortion

- Gap: "approximately 50 pixels (2.5 arcsec)". COSMOS used a ~3″ y offset plus a few pixels in x so every sky point has ≥ 3 of 4 exposures. HUDF used 3″ and 6″ inter-visit offsets. CANDELS did not cover the gap within an epoch. [verified] ACS IHB §4.2, §10.4; Koekemoer et al. 2007 §2.2, 2011 §5.3.1 (direct); Beckwith et al. 2006 (ar5iv).
- Plate scale varies ±5% across the field, so a pixel-phase dither does not hold across the detector. [verified] ACS IHB §7.4.
- Flats: accurate to better than 1% for most-used modes; "donut" residuals up to 2% at chip center; sky flats from GOODS/Frontier Fields agree to ≤ 1–2%; F435W ~3% for blue sources. FLT/FLC flats do not remove the pixel-area variation (0.88–1.06 of the central value); drizzling does. [verified] ACS DHB §4.4, ACS IHB §10.4.
- Amplifier-to-amplifier sensitivity agrees to 0.4% (F606W, F814W). [verified] ACS ISR 2026-03 abstract.
- Distortion: scale changes ~10% corner to corner. Linear terms drift, ~25 mas from the 2002 solution; "the X-scale and Y-skew changed after SM4, but the reason for the change is not yet understood". Time dependence is in the IDCTAB (ACS ISR 2015-06; Gaia DR2-based ACS ISR 2020-09); `NPOLFILE` (filter-dependent) and `D2IMFILE` (pixel-grid) are also required. [verified] ACS IHB §10.4.
- Filter-to-filter offsets of 10–25 mas and per-dither errors of 3–5 mas were solved empirically by CANDELS. [verified] Koekemoer et al. 2011 §5.3 (direct).

### 2.9 Sky

- AstroDrizzle `skymethod='localmin'` measures sky per chip and uses the lowest for the exposure, "based on the premise that for large extended or bright targets" one chip is biased high. `match`/`globalmin+match` exist because "for some image 'true' background may not be present at all". [verified] `upstream/drizzlepac/drizzlepac/sky.py:62-72`, `:409-420`.
- Cluster fields: a constant per-exposure sky from sigma-clipped pixels includes intracluster light, so a NIRCam-style 2-D background model will subtract it. [inferred] Frontier Fields points to separate ICL-subtraction papers (Livermore et al. 2016; Merlin et al. 2016). [verified] Lotz et al. 2017 Table 3 text (direct).
- CANDELS masks 20–30% of pixels (smoothed detection) before the clipped mean. [verified] Koekemoer et al. 2011 §5.7 (direct).

---

## 3. WFC3/IR summary table

| Artifact | How recognized | Handled by calwf3 / MAST products? | Extra handling by community pipelines / STScI tools |
|---|---|---|---|
| Time-variable background (He I 1.083 µm; scattered Earth light) | F105W/F110W (G102/G141): per-read background not constant. FLT has noisy, multimodal pixel histogram; `SAMP`/`TIME` extensions show few reads used | **No**: breaks the up-the-ramp fit. STScI says such FLTs should be reprocessed | Reprocess from RAW/IMA: `CRCORR=OMIT` (last-minus-first) or "flatten-ramp"; excise reads. Notebooks in `hst_notebooks/notebooks/WFC3`; `reprocess_wfc3` (grizli) |
| Persistence | Afterimages of earlier bright sources, incl. previous visits and own dithers; ~0.3 e⁻/s at 1000 s after 10⁵ e⁻ | **No** | MAST persistence products (`_persist.fits`): mask above a threshold and/or subtract; dithers > 10 px |
| Blobs | 153 dark spots, 10–20 px, up to 15–30% deep; DQ 512 | Flagged; corrected by delta-flats in 6 filters since Oct 2020. **Not** excluded by default drizzle bits | Blob dither; decide per filter whether 512 is good; CANDELS/Frontier Fields masked new blobs |
| Bad / unstable / hot pixels, death star, unbonded corners | DQ 4, 8, 16, 32 | Flagged; time-dependent tables since 2019 | Extra bad-pixel masks (grizli, RELICS); warm-pixel flagging from darks (CANDELS) |
| Wagon wheel | Lower-right corner, QE 25–50% low, color-dependent flat | Flat-fielded, not flagged; flat residual ~1% | None; expect lower weight and color residuals |
| Cosmic rays | In IMA: DQ 8192 per read. **FLT DQ carries no CR flag** | Yes, ramp fit at 4σ; residuals remain | `driz_cr` with loose thresholds (grizli "8.0 5.0" / "2.5 0.7") |
| Snowballs | ~1 per hour, 8–25 px, 2–5 saturated | Usually removed as CRs; can leave fuzzy rings | Drizzle rejection |
| Satellite trails | Streak in one read; calwf3 "sometimes makes a trail look blacked out"; possible persistence afterwards | Partly; residuals common | Excise the read from the IMA (ISR 2016-16); manual masks |
| Non-linearity, saturation | DQ 256 only if the zeroth read saturated; otherwise FLT DQ = 0 even when late reads saturated | Yes; pixel-based NLINFILE since Oct 2025 | Use `SAMP`/`TIME` to see how many reads contributed |
| Count-rate non-linearity | Faint sources ~0.04 mag too faint with standard zeropoints | **No** | Apply 0.0075 mag/dex (ISR 2019-01) if needed |
| Crosstalk | Negative mirror image in the paired quadrant, ~1e-6 | No (negligible) | Ignore |
| Ghosts, diffraction spikes | Diffuse donuts quartered by strut shadows; "diamond" above bright stars | No | Manual masks; grizli `mask_IR_psf_spikes` (off by default) |
| Banding | ±3–5 DN band, 64–128 rows, after a subarray exposure | No | Subtract as an extra sky term |
| Undersampling | FWHM 1.0–1.2 native px before pixelation | n/a | 4-point half-pixel dither; 0.06″ output, pixfrac 0.8 |
| Sensitivity loss | 0.06–0.12%/yr, up to 2% since 2009 | Yes, time-dependent IMPHTTAB since 2024-12-17 | Re-read `PHOTFNU`/`PHOTFLAM` from freshly retrieved files |
| DASH (gyro-only) exposures | FLT smeared; archival only | **No** | Split IMA into 25 s read differences, align each, drizzle (`wfc3dash`) |

## 4. WFC3/IR detailed notes

### 4.1 Time-variable background

1. What: zodiacal light is constant within a visit, but two components vary within an exposure. (a) He I 1.083 µm emission from the sunlit upper atmosphere, seen whenever HST is outside Earth's shadow, in F105W and F110W (and G102, G141); it can exceed 4 e⁻/s against ~1 e⁻/s zodiacal, "up to a factor of 6". It is spatially flat. (b) Scattered Earth light at limb angles < ~30°, brightening the leftmost ~200–400 columns by up to 2×, in all filters. [verified] WFC3 DHB §7.10; WFC3 ISR 2016-16 §1 (direct).
2. Why it breaks calwf3: `CRCORR` assumes each pixel's ramp is linear. "Strong time variation in the background can trip the CR thresholds, with most or all of the image identified as a CR at a given read." Result: "perhaps just one or two reads out of 15" used, and "multi-modal non-Gaussian" background pixel distributions. "These FLT products are unusable and should be discarded or reprocessed!" [verified] WFC3 ISR 2016-16 §1 (direct).
3. Frequency: without the SHADOW requirement, "nearly every orbit-long visit using the F105W or F110W filters ... will see the He 1.083 µm emission line at some point during the orbit". [verified] WFC3 ISR 2016-16 §2 (direct).
4. Detection: per-read median background from the IMA (differences of consecutive reads); `SAMP` and `TIME` extensions of the FLT (number of reads and exposure time actually used). [verified] WFC3 DHB §7.10, §3.3; XDF used the TIME array as the trigger (ar5iv).
5. Remedies (all require RAW or IMA, not FLT):
   - Last-minus-first: set `CRCORR=OMIT` in the RAW header and rerun calwf3. No CR flags; drizzle must find them. Loses the dynamic range of sources that saturate mid-ramp. [verified] WFC3 ISR 2016-16 §3.1 (direct).
   - Flatten-ramp: run calwf3 to the IMA without CR rejection, subtract each read's median background, add back the exposure-average rate, resume calwf3 with `CRCORR`. No measurable photometric effect in the HUDF F105W test. Assumes a spatially flat excess, so it suits He I, not scattered light, and "relatively sparse fields". [verified] WFC3 ISR 2016-16 §3.2, §4 (direct); WFC3 DHB §7.10.
   - Scattered light: flag the affected reads and rerun calwf3 (exposure time drops, e.g. 1403 s → 1000 s), or subtract bad reads from the IMA manually. [verified] WFC3 DHB §7.10.
6. Tools: notebooks `ir_ima_visualization`, `calwf3_recalibration`, `tvb_flattenramp`, `ir_scattered_light_calwf3_corrections`, `ir_scattered_light_manual_corrections` under `upstream/hst_notebooks/notebooks/WFC3/` [verified: directories present]; `wfc3tools.calwf3`; https://github.com/gbrammer/reprocess_wfc3 (`make_IMA_FLT`, `show_MultiAccum_reads`).
7. Community practice:
   - grizli downloads WFC3/IR **RAW**, not FLT, and reprocesses every exposure: `inst_products: WFC3/IR: [RAW]`; `reprocess_wfc3ir` calls `reprocess_wfc3.make_IMA_FLT`. An optional GUI (`inspect`) lets a person flag reads with "high backgrounds and/or satellite trails", then calls `make_IMA_FLT(raw, pop_reads=..., flatten_ramp=True)`. [verified] `upstream/grizli/grizli/data/auto_script_defaults.yml:40-45`; `upstream/grizli/grizli/pipeline/reprocess.py:7-72`, `:75-175`.
   - Frontier Fields: "modified IR ramp fitting algorithm"; XDF: scale the sky of each read to the exposure average before CR rejection. [verified] Lotz et al. 2017 §5 (direct); Illingworth et al. 2013 (ar5iv).
   - CANDELS (F125W/F160W mostly) kept all reads; RELICS did "not correct for time-variable sky emission due to Helium line emission". [verified] Koekemoer et al. 2011 §5.7 (direct); Coe et al. 2019 (ar5iv).

### 4.2 Persistence

1. What: charge trapped in the HgCdTe diode depletion region when a pixel is filled past roughly half full well (thresholds quoted: ~35,000 e⁻ IHB App. D.2; ~40,000 e⁻ DHB §8.1; full well ~78,000–80,000 e⁻), released later as a power-law afterglow (slope ≈ −1). 10⁵ e⁻ gives ~0.3 e⁻/s at 1000 s and ~0.03 e⁻/s at 10,000 s, against a dark of ~0.048 e⁻/s. Depends on fluence and on how long the pixel stayed full; the upper-left quadrant is worse. [verified] WFC3 DHB §8.1, WFC3 IHB §5.7, §7.9, App. D.2.
2. Recognize: afterimages of stars from the previous visit (any field), from earlier dither positions of the same visit ("self-persistence often dominates"), from grism spectra, from spatial scans (model fails by orders of magnitude), from satellite trails. A 100 s broadband exposure of an ~18th mag star is enough. Worst recorded case: ~30% of the detector for > 24 h after scanned grism data (Frontier Fields MACS0416 parallel). [verified] WFC3 IHB §7.9.4; WFC3 DHB §8.3; Lotz et al. 2017 §5 (direct); Koekemoer et al. 2011 §5.6 (direct).
3. Standard calibration: none. Schedulers block only the worst "bad actors" (two IR-free orbits). "Significant persistence does not trigger automatic notification." [verified] WFC3 DHB §8.1, WFC3 IHB §7.9.4.
4. MAST persistence products:
   - Per FLT: `_persist.fits` (total model), `_extper.fits` (external only), `_flt_cor.fits` (FLT minus model), plus an HTML summary. Not in the normal archive retrieval; search form at https://archive.stsci.edu/prepds/persist/search.php, one tar.gz per visit. Software: WFC3 ISR 2015-15, https://github.com/kslong/Persistence, version 3.0.1 with a spatial correction. "Normally available within a few weeks." Model removes "about 90%". [verified] WFC3 DHB §8.2, §8.3, §8.1.
   - Currency in 2026: the WFC3 persistence page (last updated 2026-07-13) still says the software is run "on all WFC3/IR data" and calls it a "prototype"; WFC3 IHB v18 (Dec 2025) still points to the same archive URL. The search form page itself loads but shows "Last Modified: Oct 06, 2011" and unrendered template text. I did not run a query, so whether 2025–2026 exposures have products is [unknown]. [verified for the page states]
   - Note the persistence page quotes a dark of 0.015 e⁻/s; the handbooks say 0.048 e⁻/s. [verified discrepancy]
5. What pipelines do:
   - STScI notebook: flag model > 0.005 e⁻/s (suggests 0.01–0.05 e⁻/s as a more reasonable start) with DQ 16384 and redrizzle. [verified] `upstream/hst_notebooks/notebooks/WFC3/persistence/wfc3_ir_persistence.ipynb` §3.2.
   - XDF: mask where model > 0.2 e⁻/s, grown by 2 px. [verified] Illingworth et al. 2013 (ar5iv).
   - grizli `apply_persistence_mask`: flag where `persist > err_threshold * ERR` (function default 0.6; pipeline default 0.5), dilate by 3 px, set DQ 1024, and also subtract the model and add it in quadrature to ERR. Needs the `_persist.fits` files downloaded beforehand. [verified] `upstream/grizli/grizli/prep.py:485-600`; `upstream/grizli/grizli/data/auto_script_defaults.yml:185-190`.
   - CANDELS: 5σ pixels in the calibration darks taken just before a visit, and bright pixels of the preceding exposures within a visit; dither sized so 2.5 px persistence spots do not overlap. [verified] Koekemoer et al. 2011 §4.3, §5.1.2 (direct).
   - Self-persistence biases photometry: sources appear ~2% brighter on average; undithered sequences rise 0.02–0.04 mag. [verified] WFC3 DHB §7.11, WFC3 IHB §7.9.4.

### 4.3 Blobs

1. What: defocused particulates on the Channel Select Mechanism mirror. [verified] WFC3 DHB §7.5.
2. Recognize: 10–20 px diameter dark spots (radius ~4 px upper right to ~13 px lower left); central absorption "up to ~30%" (DHB) or "up to 15%" (IHB); stronger in bluer filters. Count: 19 (March 2010), 40 (July 2012), 127 (March 2014), 153 (May 2024 and still 153 in October 2025), 58 strong. None has disappeared. Position shifts up to ~1 px with CSM angle. [verified] WFC3 DHB §7.5, WFC3 IHB §7.9.6.
3. Calibration:
   - DQ 512. Since July 2014 time-dependent bad-pixel tables flag the strongest; since the October 2020 delta-flats all cataloged blobs are flagged, raising flagged pixels from 1.1% to 2.3%. [verified] WFC3 DHB §7.5, §7.8.
   - Delta-flats (`*_dfl.fits`, WFC3 ISR 2021-10) for F098M, F105W, F110W, F125W, F140W, F160W at 49 appearance dates, 148 of 153 blobs, applied by calwf3 with the 2020 P-flats. Other filters: flagged only. [verified] WFC3 DHB §7.5, §7.8.
   - Drizzle default: "AstroDrizzle's default treats as bad all pixels with any flag except 512 and 64." HAP configuration uses `final_bits = 528` (512 + 16). So blob pixels go into the mosaic. grizli uses `bits = 576+256` for WFC3/IR visits and 576 elsewhere, also keeping 512. [verified] WFC3 DHB §7.9; `upstream/drizzlepac/drizzlepac/pars/astrodrizzle_filter_hap.cfg:49,100`; `upstream/grizli/grizli/prep.py:5705`, `:9524`. The classic MAST `MDRIZTAB` values were not checked. [unknown]
4. Why dithers matter: only `WFC3-IR-DITHER-BLOB` (±14.25 px, 5.18″) or ≥ 5″ custom dithers step over a blob; the standard 4-point box (≤ 4 px) does not. Photometric errors up to 0.1 mag were measured on blobs before delta-flats. [verified] WFC3 IHB App. C.2, §7.9.6.

### 4.4 Flats, bad pixels, cosmetic regions

- Flats: ground flats (2008), sky-flat update December 2010 (0.5% accuracy, 0.8% in the wagon wheel and at the edge), filter-dependent sky P-flats October 2020 (`4*pfl.fits`, WFC3 ISR 2021-01) correcting ±0.5% center and up to 2% edge residuals. Early (2009–2010) flats had 3–4% large-scale errors, which CANDELS corrected itself. Any FLT retrieved before October 2020 is on the old flats and zeropoints. [verified] WFC3 DHB §7.8, §7.11; Koekemoer et al. 2011 §5.1.2 (direct).
- Wagon wheel: lower-right corner, QE 25–50% low, "color-dependent structure which is not corrected by the existing flat fields"; not flagged. [verified] WFC3 DHB §7.9; Lotz et al. 2017 §5 (direct).
- Death star: ~50 px (6.4″) circle of dead pixels at lower left (DQ 4 and 8); unbonded pixels in the upper corners and top edge (DQ 4). A 4-point sub-pixel dither does not fill them. [verified] WFC3 DHB §7.9; Koekemoer et al. 2011 §4.3 (direct).
- Time-dependent pixels: ~3.5% of pixels changed behavior since 2009; ~200 new stable hot pixels per year; unstable ~1–1.5%. Since May 2019 flags are per cycle: stable hot = 16 (usable, dark-calibrated), unstable = 32 (do not use). Data retrieved before May 2019 lack this. [verified] WFC3 DHB §7.4, §7.9; WFC3 IHB §5.7.
- Extra masks used by others: grizli ORs in `badpix_spars200_Nov9.fits` and `wfc3ir_dark_badpix_2019.01.12.fits.gz`; RELICS used updated bad-pixel lists; CANDELS flagged 5σ warm pixels from contemporaneous darks. [verified] `upstream/grizli/grizli/prep.py:301-330`; Coe et al. 2019 (ar5iv); Koekemoer et al. 2011 §5.1.2 (direct).
- Banding: ±3–5 DN band centered vertically, as tall as a preceding smaller subarray (WFC3 ISR 2011-04). [verified] WFC3 DHB §7.4.
- Crosstalk: negative mirror image at ~1e-6 between amplifiers 1↔2 and 3↔4. [verified] WFC3 IHB §5.7.
- Ghosts: large diffuse donuts quartered by strut shadows (filter–detector); rare "diamond" above a bright star (WFC3 ISR 2007-16, 2017-22). [verified] https://www.stsci.edu/hst/instrumentation/wfc3/performance/anomalies
- Scattered-light "dragon's breath" is listed for UVIS only, not IR. [verified] same page.

WFC3/IR DQ bits (WFC3 DHB Table 3.3): 1 Reed-Solomon; 2 fill; 4 bad detector pixel; 8 unstable zero read; 16 hot; 32 unstable; 64 unused; 128 bad reference pixel; 256 saturated; 512 bad in flat (blobs); 1024 negative CR spike in ramp (IMA only); 2048 signal in zeroth read; 4096 CR by AstroDrizzle; 8192 rejected in ramp fit (IMA only); 16384 unused. [verified] WFC3 DHB §3.3.

### 4.5 Ramp fitting, saturation, non-linearity

- MULTIACCUM: zeroth read then up to 15 reads. SPARS (uniform; standard for faint work) or STEP (log-linear). SPARS50/SPARS100 at NSAMP 15 = 703 s / 1403 s. Read noise ~20–21 e⁻ per CDS pair, ~12 e⁻ effective for a 15-read SPARS200 ramp. Minimum ~5–6 samples for a reliable fit. [verified] WFC3 IHB §7.7, §5.7, §7.10.
- Fit: Fixsen et al. (2000) weighting, 4σ outlier rejection, separate fits before and after a hit. CR rate 11 ± 1.5 per second over the array. Pixels with ≥ 4 CRs are flagged unstable (32). [verified] WFC3 DHB §3.3, WFC3 IHB §7.9.2.
- "The CR and SPIKES DQ flags are only present in the ima file." A DQ value reaches the FLT only if present in all reads. [verified] WFC3 DHB §3.3.
- What remains for drizzle: CRs between the zeroth and first read, missed faint hits, warm pixels, snowball rings, satellite trails. "Occasional cosmic rays that are not fully removed, or warm pixels that are not accounted for" (CANDELS ran `driz_cr` on IR too). [verified] Koekemoer et al. 2011 §5.4 (direct); Momcheva et al. 2017 §3 (direct).
- Saturation: reads after saturation are dropped (256 in IMA); the FLT DQ is 0 if earlier reads were usable. If the first read is saturated the FLT value is an estimate from the zeroth read with no 256 flag; only zeroth-read saturation sets 256 in the FLT. [verified] WFC3 DHB §3.3.
- Non-linearity: quadrant-average polynomial from ground tests 2009–2025; pixel-based `NLINFILE` delivered October 2025 (WFC3 ISR 2025-08, 2025-09): up to 7% improvement near saturation, fewer false CR flags in ramp fitting, zeropoints change 0.1–0.2%. [verified] WFC3 IHB §5.7; WFC3 ISR listing.
- Count-rate non-linearity: 0.0075 ± 0.0006 mag/dex (WFC3 ISR 2019-01; IHB writes 0.75%/dex), no wavelength dependence; faint sky-dominated sources come out "0.04 ± 0.01 mag too faint" with published zeropoints. "The calwf3 pipeline does not correct WFC3 IR data for count-rate non-linearity." [verified] WFC3 DHB §7.7, WFC3 IHB §5.7.
- Units: WFC3/IR FLT is in e⁻/s; ACS/WFC FLC is in electrons (total). calwf3 does not apply a pixel-area map. [verified] WFC3 DHB §3.3; Koekemoer et al. 2011 §5.8.1 (direct).

### 4.6 Satellite trails and transients in IR

- A trail affects one read. "calwf3 struggles to identify these types [of] transient events that can affect many adjacent pixels, and with standard processing residuals of the short event will often be seen in the final calibrated image." Fix: build last-minus-first from the IMA, subtract the affected read and its time, then let drizzle find CRs. Bright trails leave persistence along the track. [verified] WFC3 ISR 2016-16 §5 (direct).
- No automated IR trail finder is supported: the `acstools` code "should work on WFC3 data as well" but was developed and tested only on ACS. The WFC3 team keeps a visually flagged anomaly database. [verified] WFC3 DHB §9.5.
- Snowballs: ~1–1.2 per hour, 200,000–500,000 e⁻, alpha particles from the detector; rate constant over 11 years. [verified] WFC3 DHB §7.9, WFC3 IHB §7.9.2.

### 4.7 Sampling, dithers, drizzle parameters

- 0.128″/px; FWHM 0.128″ (1.0 µm) to 0.151″ (1.6 µm) before pixelation, i.e. 1.0–1.18 px; > 40% of a star's flux in the central pixel; "undersampled by about a factor of 2". [verified] WFC3 IHB §7.6, §7.10.
- Consequence for CR flagging: the blotted median cannot follow a stellar core, so `driz_cr` needs the derivative ("scale") term; grizli uses SNR "8.0 5.0", scale "2.5 0.7" for IR versus "3.5 3.0", "1.2 0.7" for ACS. [verified] `upstream/grizli/grizli/prep.py:5691-5707`. The ramp fit already removed most CRs, so loose thresholds cost little. [inferred]
- Patterns: `WFC3-IR-DITHER-BOX-MIN` 4-point (0, 0), (4.0, 1.5), (2.5, 4.0), (−1.5, 2.5) px; CANDELS used its own slightly wider 4-point box; persistence advice is steps > 10 px, which conflicts with uniform sub-pixel phase across the distorted field. [verified] WFC3 IHB App. C.2, §7.10; Koekemoer et al. 2011 §4.3 (direct).
- Output: CANDELS 0.06″ with pixfrac 0.8 (best achievable PSF FWHM ~0.17–0.19″); XDF 0.06″, pixfrac 0.8, square kernel; COSMOS-DASH 0.1″. ACS: 0.03″ with pixfrac 0.8 (COSMOS, CANDELS) or point kernel pixfrac 0 only with ≫ 100 exposures (HUDF, XDF). [verified] Koekemoer et al. 2011 §5.8.2, 2007 §4 (direct); XDF, HUDF (ar5iv); Momcheva et al. 2017 §3 (direct).
- Pointing drifts thermally by up to 0.15 px over two orbits. [verified] WFC3 IHB §7.10.

### 4.8 Photometric calibration over time

- 2020 zeropoints (WFC3 ISR 2020-10) changed 0.5–2% with new CALSPEC models and the 2020 flats; in headers of data retrieved after October 2020. [verified] WFC3 DHB §7.11.
- Sensitivity loss: 0.06%/yr (F140W, F160W) to 0.12%/yr (F098M, F105W, F110W), "up to 2% since installation" (WFC3 ISR 2024-06). Time-dependent `IMPHTTAB` `8bq13281i_imp.fits` delivered 2024-12-17 (WFC3 ISR 2024-13). Latest (WFC3 ISR 2026-01): −0.07 ± 0.01%/yr F160W, −0.13 ± 0.02%/yr F110W; IMPHTTAB unchanged. [verified] https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/photometric-calibration/ir-photometric-calibration; WFC3 ISR listing.
- Repeatability of staring-mode photometry ±1.5% (1σ). [verified] WFC3 DHB §7.11.
- "Burn-in": no handbook or ISR found using that term for WFC3/IR. IHB §7.9.3 reports minimal on-orbit degradation and stable dark current. [unknown as a named effect]

### 4.9 DASH (drift-and-shift)

- Method: one guide-star acquisition per orbit, first pointing guided, then up to 7 more pointings under gyro control. Drift expected 0.001–0.002″/s, so ≲ 0.05″ (under half a pixel) per 25 s read. COSMOS-DASH: 8 pointings per orbit, SPARS25, NSAMP 11 or 12, 253 s or 278 s per pointing. [verified] Momcheva et al. 2017 §2 (direct); WFC3 IHB §7.10.6.
- The FLT of an unguided pointing is smeared and is "scientifically unusable" as is. Reduction: take the IMA, difference consecutive reads into 10–11 independent 25 s images, align each (tweakreg, shifts only, rms ~0.4 native px), drizzle. The ramp fit flags drifting point sources as CRs, so CR flags inside object footprints are reset and L.A.Cosmic on the FLT plus `driz_cr` across reads is used instead. Noise per unguided pointing is ~15% higher (up to 28%) at native scale. [verified] Momcheva et al. 2017 §3–4 (direct).
- Surveys: COSMOS-DASH, 456 pointings, 0.49 deg² (0.66 with archival), F160W 5σ 25.1; 3D-DASH, 1256 pointings, 1.35 deg² (1.43 with archival), F160W 5σ 24.74. [verified] arXiv:1808.04379, arXiv:2206.01156 abstracts. Orbits and per-pointing exposure for 3D-DASH: [unknown] (full text too large to fetch).
- Each DASH pointing is a single exposure: nothing to reject against except its own reads and neighbors' overlaps. [inferred]
- Tools: `wfc3dash` (called by grizli when `is_dash`); the STScI DASH pipeline (WFC3 ISR 2021-02) "is no longer actively maintained". DASH is "no longer available" since June 2024. [verified] `upstream/grizli/grizli/pipeline/auto_script.py:569-572`; WFC3 IHB §7.10.6.

---

## 5. Guiding, gyros and astrometry (both instruments)

### 5.1 Reduced-gyro (one-gyro) mode

- HST entered reduced-gyro mode in June 2024 (NASA announcement 2024-06-04; science resumed mid-June per news reports). [verified] WFC3 IHB §1.5; ACS ISR 2025-01 (direct); https://hst-docs.stsci.edu/hsp/hst-general-science-policies/reduced-gyro-mode-tips-and-resources. Resumption date: [inferred, news only]
- No gyro-only observing: no DASH, no guide-star handoffs, one acquisition per orbit. [verified] RGM tips page.
- Image quality: STScI states the same quality for most programs. Two-guide-star jitter 0.008″ rms (design 0.007″). [verified] ACS IHB §2.2; HST Primer (Cycle 34).
- New failure mode: "undeclared loss of lock events" became more frequent after June 2024. The exposure is delivered with normal flags but is smeared ("roll-drift"). Drift up to ~1 px is hard to see by eye; hst1pass `qfit` exceeds ~0.2 beyond ~1 px (normal 0.02–0.075); 0.1–0.4 px is indistinguishable from normal. The ACS team screens new data daily; rate "seems to be quite low". [verified] ACS ISR 2025-01 (direct).
- Single guide star (one FGS + gyro for roll): drift typically ≤ 0.2 px per orbit in four of five cases, 0.4 px in the fifth (WFC3 ISR 2025-07); "typical RMS drift over the orbit is about 0.2 pixel (8 mas)" (Primer); no PSF degradation found in 1,669 ACS F606W exposures 2024–2026 (ACS ISR 2026-04). Older statements: roll drift 1.5 to ≤ 17 mas/s, giving up to 60 mas (0.45 IR px, ~1.2 ACS px) in 1000 s (WFC3 DHB §9.2). [verified] sources as named. The 0.45 IR px and 60 mas figures are quoted; the ACS conversion is mine. [inferred]

### 5.2 Spotting bad guiding in any epoch

- `EXPFLAG` in the primary header (anything but `NORMAL`); grizli moves a whole visit aside when ≥ 2 exposures are not `NORMAL` (the first exposure of a visit is sometimes `INDETERMINATE` with good guiding). [verified] `upstream/grizli/grizli/pipeline/auto_script.py:1582-1645`.
- Jitter files `_jif.fits` (per-exposure header keywords) and `_jit.fits` (3 s samples): `T_GSFAIL`, `GUIDEACT`, `GSACQ`, `GSFAIL` (e.g. `NOLOCK`), `GSSEPRMS`, `V2_RMS`, `V3_RMS` (failed exposures show −32767 mas or 1.6e38). Normal RMS ~1.5–2 mas. [verified] `upstream/hst_notebooks/notebooks/WFC3/exception_report/wfc3_exception_report.ipynb` §4–5; ACS equivalent at `upstream/hst_notebooks/notebooks/ACS/acs_exception_report/` (not read).
- Failed exposures stay in the archive unless the PI filed a problem report; 5–10% of HST time goes to repeats. [verified] HST Primer; exception-report notebook.

### 5.3 Absolute astrometry

- Data from Cycle 14 and earlier (GSC 1; "improved guidestar positions were incorporated into the system after Cycle 14"): header WCS wrong by "~1–2″, or more", plus roll errors of 0.01–0.03° (a few ACS pixels across the field). This applies to the original headers; see the 2020 MAST update below. [verified] Koekemoer et al. 2007 §3.2 (direct).
- GSC 2 era: ~0.3–0.5″. [verified] Koekemoer et al. 2011 §5.5 (direct).
- Since 2020 MAST headers carry updated solutions: guide stars re-referenced to the newest catalog (still up to 200 mas), or a Gaia fit (< 20 mas) when enough sources match; the Gaia fit "can slightly worsen relative alignment between exposures". [verified] WFC3 DHB §9.2.
- Within one visit on the same guide stars, relative offsets are good to 2–5 mas; filter changes add 10–25 mas; guide-star reacquisition adds shifts and small rotations. [verified] Koekemoer et al. 2007 §3.2, 2011 §5.3 (direct).

---

## 6. Exposures per pointing in the legacy surveys

| Survey | Instrument / filter | Exposures per pointing per filter | Exposure time | Notes | Source, tag |
|---|---|---|---|---|---|
| COSMOS | ACS F814W | 4 | 4 × 507 s = 2028 s, 1 orbit | 583 orbits; gap covered by ≥ 3 | Koekemoer et al. 2007 §2.2 (direct) [verified] |
| GOODS | ACS F606W, F775W | 2 per epoch, 5 epochs | 1050 s per epoch | "some cosmic rays remain in single-epoch V and i" | Giavalisco et al. 2004 (ar5iv) [verified] |
| GOODS | ACS F850LP | 4 per epoch, 5 epochs | 2100 s per epoch | | same [verified] |
| GOODS | ACS F435W | 6, one epoch | ~7200 s | | same [verified] |
| CANDELS Wide | WFC3/IR F125W, F160W | 2 + 2 per epoch, 2 epochs → 4 per filter | 450–1000 s each, SPARS50 (NSAMP 9–14) or SPARS100 (6–10) | Within one epoch only 2 exposures per filter | Koekemoer et al. 2011 §4.1, §4.3, §4.4 (direct) [verified] |
| CANDELS Wide | ACS F606W, F814W parallels | 2–4 per filter per orbit | 225–807 s | Overlaps roughly double the depth; no gap coverage within an epoch | same §4.4, §5.4 [verified] |
| CANDELS Wide | WFC3/UVIS F350LP | 1 per visit | 434 s, no CR-SPLIT | single exposure | same §4.4 [verified] |
| CANDELS Deep | WFC3/IR F105W, F125W, F160W | 2 + 2 per orbit; ~10 epochs | ≥ 4 orbits J, H; 3 orbits Y | ACS F814W ≥ 28–32 ks | same §4.1; Grogin et al. 2011 (ar5iv) [verified] |
| HUDF | ACS 4 filters | 112, 112, 288, 288 (2 per orbit; 56/56/144/144 orbits) | ~1200 s each | counts are 2 × orbits [inferred from verified orbit numbers] | Beckwith et al. 2006 (ar5iv) |
| XDF | ACS F435W / F606W / F775W / F814W / F850LP | 164 / 286 / 460 / 362 / 700 | 152 / 174 / 378 / 51 / 422 ks | | Illingworth et al. 2013 (ar5iv) [verified] |
| XDF | WFC3/IR F105W / F125W / F140W / F160W | 248 / 289 / 118 / 336 | 267 / 113 / 87 / 236 ks | | same [verified] |
| Frontier Fields | ACS F435W / F606W / F814W | four half-orbit exposures per filter per 2-orbit visit | 45 / 25 / 105 ks | 70 orbits per camera per field; ≈ 18 / 10 / 42 orbits at 2500 s [inferred] | Lotz et al. 2017 Table 3, §5 (direct) [verified] |
| Frontier Fields | WFC3/IR F105W / F125W / F140W / F160W | same structure | 60 / 30 / 25 / 60 ks | IR-DITHER-BLOB within visit; Sobol inter-visit | same [verified] |
| CLASH | 16 filters, ACS + WFC3 | ≥ 4 per filter | 1–2 orbits per filter (~2000–5000 s) | 20 orbits per cluster; two orients | Postman et al. 2012 (ar5iv) [verified] |
| RELICS | ACS F435W, F606W, F814W | 4 dithered per ~1 orbit | ~2000 s per filter | 2 epochs ~1 month apart | Coe et al. 2019 (ar5iv) [verified] |
| RELICS | WFC3/IR 4 filters | 2 orbits split over 4 filters | e.g. 1361 / 686 / 686 / 1861 s | exposures per filter not confirmed; F125W, F140W may have only 1–2 [unknown] | same |
| COSMOS-DASH | WFC3/IR F160W | 1 per pointing (10–11 reads of 25 s) | 253 or 278 s | 8 pointings per orbit | Momcheva et al. 2017 (direct) [verified] |
| 3D-DASH | WFC3/IR F160W | 1 per pointing | not confirmed | 1256 pointings | arXiv:2206.01156 abstract [verified]; time [unknown] |

Implications for outlier logic written for many NIRCam dithers:

- A per-visit, per-filter stack often has 2 exposures (CANDELS Wide IR per epoch, GOODS V and i per epoch) or 1 (F350LP, DASH, some RELICS IR). A median of 2 is a mean; `minmed` exists for this case ("enables the minimum to be used instead of the median in cases where valid pixels from only two or three exposures are present, if one of them exceeds the others by > 4σ"). [verified] Koekemoer et al. 2011 §5.4 (direct).
- CANDELS rejected within an orbit first, then again across epochs once absolute astrometry was good to < 0.1 px. [verified] Koekemoer et al. 2011 Fig. 2, §5.5 (direct).
- With 2 ACS exposures expect thousands of doubly-hit pixels per chip pair. [verified] same §5.4.

---

## 7. Could not verify

- Kruk et al. 2023 numbers by instrument and period: Nature page redirected to a login; only the ADS abstract via a search snippet was seen.
- Whether MAST persistence products exist for 2025–2026 WFC3/IR exposures: the search page loads but looks unmaintained; no query was run.
- Classic MAST `MDRIZTAB` bit settings for ACS/WFC and WFC3/IR drizzle products (only the HAP `.cfg` and the WFC3 DHB statement were read).
- Magnitude of the F850LP halo for ACS/WFC and any broadband fringing amplitude in F814W/F850LP.
- Intracluster-light oversubtraction: no STScI source read; statement is inferred.
- A WFC3/IR effect named "burn-in": nothing found.
- 3D-DASH orbits and per-pointing exposure time (full text exceeded the fetch limit); Grogin et al. 2011 exposure tables (PDF too large; ar5iv partially read).
- RELICS number of exposures per IR filter; CLASH mosaic pixel scales.
- RGM Primer PDF (not read); exact date science resumed in June 2024; rate of undeclared loss-of-lock events.
- ACS equivalents of the jitter-keyword recipe (ACS exception-report notebook not read).
- WFC3 ISR 2014-03, 2017-22, 2019-01, 2021-01, 2021-10 and ACS ISR 2018-04, 2024-07 full texts (abstracts or handbook citations only).
- `acs_destripe_plus` documentation page (404); the repo source was read instead.

---

## 8. Sources and access status

Read directly as text (PDF converted with Ghostscript, or repo file):

| Source | URL / path | Status |
|---|---|---|
| Koekemoer et al. 2011, CANDELS data products | https://arxiv.org/pdf/1105.3754 | read, §4–5.8 |
| Koekemoer et al. 2007, COSMOS ACS | https://arxiv.org/pdf/astro-ph/0703095 | read, §2–4 |
| Lotz et al. 2017, Frontier Fields | https://arxiv.org/pdf/1605.06567 | read, §5 |
| Momcheva et al. 2017, DASH | https://arxiv.org/pdf/1603.00465 | read, §2–4 |
| ACS ISR 2022-08 (Stark et al., findsat_mrt) | https://www.stsci.edu/files/live/sites/www/files/home/hst/instrumentation/acs/documentation/instrument-science-reports-isrs/_documents/isr2208.pdf | read, abstract, §5–6, Table 2 |
| ACS ISR 2025-01 (Cohen, roll-drift) | .../acs/.../_documents/isr2501.pdf | read in full |
| ACS ISR 2014-01 (Ogaz et al., post-flash) | .../acs/.../_documents/isr1401.pdf | read, abstract, intro, recommendations |
| WFC3 ISR 2016-16 (Brammer, variable backgrounds) | https://www.stsci.edu/files/live/sites/www/files/home/hst/instrumentation/wfc3/documentation/instrument-science-reports-isrs/_documents/2016/WFC3-2016-16.pdf | read, §1–6 |
| hstcal | `upstream/hstcal/pkg/acs/include/acsversion.h`, `upstream/hstcal/pkg/acs/CHANGELOG.txt` | read |
| acstools | `upstream/acstools/acstools/acs_destripe_plus.py` (module list checked) | read |
| drizzlepac | `upstream/drizzlepac/drizzlepac/sky.py`, `pars/astrodrizzle.cfg`, `pars/astrodrizzle_filter_hap.cfg` | read (selected lines) |
| grizli | `upstream/grizli/grizli/prep.py`, `pipeline/auto_script.py`, `pipeline/reprocess.py`, `data/auto_script_defaults.yml` | read (selected functions) |
| hst_notebooks | `upstream/hst_notebooks/notebooks/WFC3/persistence/wfc3_ir_persistence.ipynb`, `.../WFC3/exception_report/wfc3_exception_report.ipynb` | read (markdown cells) |

Extracted PDF text is in `/private/tmp/claude-501/-Users-hollis-codes-campfire--claude-worktrees-hst-redux/9cacaece-a785-41f3-9482-121bbc1046c1/scratchpad/research/artifacts_pdftext/`.

Read through fetch-and-summarize:

| Source | URL | Status |
|---|---|---|
| ACS DHB §3.2, §3.4, §4.2–4.7 | https://hst-docs.stsci.edu/acsdhb/chapter-4-acs-data-processing-considerations (and chapter-3 pages) | fetched |
| ACS IHB §2.1, §2.2, §2.6, §4.2, §4.3, §5.2, §5.6, §7.4, §10.4 | https://hst-docs.stsci.edu/acsihb | fetched |
| WFC3 DHB §3.3, §7.4–7.11, §8.1–8.3, §9.2, §9.5, What's New | https://hst-docs.stsci.edu/wfc3dhb | fetched |
| WFC3 IHB §1.5, §5.7, §7.6, §7.7, §7.9, §7.10, App. C.2, App. D.2 | https://hst-docs.stsci.edu/wfc3ihb | fetched |
| ACS ISR listing (2024–2026 abstracts) | https://www.stsci.edu/hst/instrumentation/acs/documentation/instrument-science-reports-isrs | fetched, first page only |
| WFC3 ISR listing (2025–2026 abstracts) | https://www.stsci.edu/hst/instrumentation/wfc3/documentation/instrument-science-reports-isrs | fetched, first page only |
| WFC3 ISR 2025-07 (single-star guiding) | https://arxiv.org/abs/2511.16411 | fetched (abstract) |
| WFC3 persistence page | https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/ir-persistence | fetched |
| MAST persistence search form | https://archive.stsci.edu/prepds/persist/search.php | fetched; functional status unclear |
| WFC3 anomalies page | https://www.stsci.edu/hst/instrumentation/wfc3/performance/anomalies | fetched |
| WFC3 IR photometric calibration | https://www.stsci.edu/hst/instrumentation/wfc3/data-analysis/photometric-calibration/ir-photometric-calibration | fetched |
| RGM tips | https://hst-docs.stsci.edu/hsp/hst-general-science-policies/reduced-gyro-mode-tips-and-resources | fetched |
| HST Primer, guiding performance | https://hst-docs.stsci.edu/hsp/the-hubble-space-telescope-primer-for-cycle-34/hst-primer-optical-performance-guiding-performance-and-observing-efficiency | fetched |
| acstools docs | https://acstools.readthedocs.io/en/latest/findsat_mrt.html, .../satdet.html | fetched |
| reprocess_wfc3 | https://github.com/gbrammer/reprocess_wfc3 | fetched (README only) |
| Giavalisco et al. 2004 (GOODS) | https://ar5iv.labs.arxiv.org/html/astro-ph/0309105 | fetched |
| Beckwith et al. 2006 (HUDF) | https://ar5iv.labs.arxiv.org/html/astro-ph/0607632 | fetched, first 100k characters |
| Illingworth et al. 2013 (XDF) | https://ar5iv.labs.arxiv.org/html/1305.1931 | fetched |
| Postman et al. 2012 (CLASH) | https://ar5iv.labs.arxiv.org/html/1106.3328 | fetched, first 100k characters |
| Coe et al. 2019 (RELICS) | https://ar5iv.labs.arxiv.org/html/1903.02002 | fetched, first 100k characters |
| Grogin et al. 2011 (CANDELS design) | https://ar5iv.labs.arxiv.org/html/1105.3753 | fetched, first 100k characters |
| Mowla et al. 2019, 2022 (COSMOS-DASH, 3D-DASH) | https://arxiv.org/abs/1808.04379, https://arxiv.org/abs/2206.01156 | abstracts only |

Unable to access:

- Unable to access this page: https://www.nature.com/articles/s41550-023-01903-3 (redirect to login).
- Unable to access this page: https://eprints.soton.ac.uk/476918 (HTTP 403).
- Unable to access this page: https://acstools.readthedocs.io/en/latest/acs_destripe_plus.html (HTTP 404).
- Unable to access this page: https://arxiv.org/pdf/1105.3753 and https://arxiv.org/pdf/2206.01156 (over the 10 MB fetch limit).
- https://drizzlepac.readthedocs.io/en/latest/drizzlepac_api/sky.html returned only an overview; the repo source was used instead.
