# HST ACS/WFC and WFC3/IR imaging and public reductions, per field

Research date: 2026-11-11. Scope: imaging only (ACS/WFC, WFC3/IR); public reductions are wanted as *comparison references* for a new FLT/FLC-based reduction on the NIRCam mosaic grids.

Tags: **[verified]** = read in this session at the cited URL (or computed in this session from a cited public table); **[inferred]** = from memory or indirect evidence, not read in this session; **[unknown]** = could not be established.

Two kinds of numbers in this file were computed here from public metadata, with no data products downloaded:

- **DJA exposure tables.** Every DJA v7 mosaic has a `<root>-<filter>_wcs.csv` listing the exposures that went into it (columns `file, ext, exptime, …, wcsname`). I counted unique `file` names and summed `exptime` per file, and mapped the 3-character program code to a proposal ID with `https://dawn-cph.github.io/dja/data/hst_program_codes.csv`. Script: `research/tools/dja_wcs_count.py`. These counts are for the *DJA mosaic footprint*, not the whole field, and the east/west and north/south PRIMER halves overlap, so do not add them.
- **MAST observation metadata.** One small CAOM cone search each for the three small fields (NEXUS, RXC J0911, PLCK G004.5−19.5), plus the product *listing* for the matching observations to count FLT/FLC rootnames. Scripts: `research/tools/mast_cone.py`, `research/tools/mast_flt_count.py`.

---

## 0. Field identifications

| Project field | Identification | Evidence |
|---|---|---|
| `nep` | **NEXUS** (North ecliptic pole EXtragalactic Unified Survey; PI Yue Shen). JWST 5105 is Cycle 3, 9263 is Cycle 4, same title; a Cycle 5 continuation 12588 exists. | **[verified]** https://www.stsci.edu/jwst-program-info/program/?program=5105 , https://www.stsci.edu/jwst-program-info/program/?program=9263 |
| | APT target `NEXUS-Center` = RA 17 53 51.00, Dec +65 11 57.0 (268.4625, +65.19917). The project tangent point (268.4636, +65.2) is 3.4″ from it. | **[verified]** text extracted from https://www.stsci.edu/jwst-program-info/download/jwst/pdf/5105/ ; separation computed here |
| | It is **not** the JWST NEP Time-Domain Field. Using the TDF centre from memory (17:22:47.9 +65:49:22) the two are 3.3° apart. | **[inferred]** for the TDF centre; separation computed here |
| `rj0911` | **RXC J0911.1+1746** = MACS J0911.2+1746, z = 0.505, a RELICS cluster. JWST 6882 observation 25 is labeled `RXCJ0911`, target (26) `RXCJ0911+1746`, RA 09 11 11.40, Dec +17 46 33.5 (137.7975, +17.77597). Project tangent point is 90″ from the target position. | **[verified]** text extracted from https://www.stsci.edu/jwst-program-info/download/jwst/pdf/6882/ ; RELICS list https://archive.stsci.edu/hlsp/relics ; z from https://ar5iv.labs.arxiv.org/html/1903.02002 |
| `pg004` | **PLCK G004.5−19.5**, z = 0.540, a RELICS cluster. JWST 6882 observation 60 is labeled `PLCKG004 - visit 37 repeat`, target (40) `PLCKG004-19.5`, RA 19 17 04.50, Dec −33 31 28.5 (289.26875, −33.52458). Project tangent point is 88″ from the target; its galactic coordinates come out as l = 4.5, b = −19.5. | **[verified]** same PDF; RELICS list; galactic coordinates computed here |
| JWST 6882 | **VENUS** (Vast Exploration for Nascent, Unexplored Sources), PI Seiji Fujimoto, Cycle 4 GO Treasury; NIRCam 10-filter imaging of 60 lensing clusters. | **[verified]** https://www.stsci.edu/jwst-program-info/program/?program=6882 , https://jwst-venus.github.io/about.html |

Galactic latitudes (computed here from the tangent points): cosmos +42.1, uds −60.0, egs +59.4, a2744 −81.2, nep +30.4, rj0911 +38.7, pg004 −19.5, goods-s −54.5, goods-n +54.8.

---

## 1. `cosmos` — COSMOS

### HST imaging

- **COSMOS ACS F814W** (Scoville; GO 9822, and 10092 **[inferred]** for the second cycle). 583 orbits, 1.64 deg², median 2028 s per pointing, 5σ point-source depth 27.2 AB. **[verified]** https://arxiv.org/abs/astro-ph/0703095 . Observed Cycle 12–13, July 2003 – June 2005 (all pre-2007 ACS), 4 exposures per orbit per pointing. **[verified]** https://irsa.ipac.caltech.edu/data/COSMOS/images/acs_mosaic_2.0/ACS_INFO_2.0.txt . The ACS catalog is "constructed from 575 ACS pointings". **[verified]** https://irsa.ipac.caltech.edu/data/COSMOS/gator_docs/cosmos_acs_colDescriptions.html . So roughly 575 × 4 ≈ 2,300 F814W exposures for the full field **[inferred]** (product of two verified numbers; no source gives the count directly). Scoville et al. 2007 list 581 orbits F814W and 9 orbits F475W. **[verified]** https://ar5iv.labs.arxiv.org/html/astro-ph/0612305
- **CANDELS-COSMOS** (GO 12440, Faber): WFC3/IR F125W + F160W (88 orbits) with ACS F606W + F814W parallels (88 orbits), post-SM4. **[verified]** https://ar5iv.labs.arxiv.org/html/1403.3689 (Table 2). Wide strategy: one ~2000 s orbit per epoch, two epochs; per orbit two F125W and two F160W exposures; ACS parallels in F606W/F814W. **[verified]** https://ar5iv.labs.arxiv.org/html/1105.3754 . 88 orbits ⇒ 44 tiles **[inferred]**.
- **3D-HST** (GO 12328, van Dokkum): F140W direct images taken with the G141 grism, 8 orbits of F140W in COSMOS. **[verified]** same Table 2.
- **COSMOS-DASH** (GO 14114): WFC3/IR F160W drift-and-shift, 456 pointings (8 per orbit), 0.49 deg² (0.66 deg² with archival). **[verified]** https://archive.stsci.edu/hlsp/cosmos-dash
- **3D-DASH** (GO 16259): F160W DASH, 1256 WFC3 pointings, 1.35 deg² (1.43 deg² with archival), median 5σ 24.74 mag. **[verified]** https://arxiv.org/abs/2206.01156 , https://archive.stsci.edu/hlsp/3d-dash . DASH exposures are unguided; they need the per-read shift step, which a standard FLT-based pipeline does not do. **[inferred]**
- **UVCANDELS** (GO 15647, Teplitz): ACS F435W parallels delivered for COSMOS. **[verified]** https://archive.stsci.edu/hlsp/uvcandels
- Other programs present inside the PRIMER-COSMOS footprint (from the DJA exposure tables, **[verified]**): 12461 (Riess), 13792 (Bouwens), 15100 (Cooke, F435W), 12167 (Franx), 12990 (Muzzin), 13657 (Kartaltepe), 13641 (Capak), 14750 (Wang), 15229 (Daddi), 12578 (Förster Schreiber), 14808/15363 (Suzuki), 14719 (Best), 15115 (Silverman), 15862 (Finkelstein).

### Exposure counts (DJA v7.0, PRIMER-COSMOS footprint only) **[verified]**

| Filter | east: n exp / ks | west: n exp / ks |
|---|---|---|
| F435W | 120 / 147.4 | 146 / 182.5 |
| F475W | 24 / 12.2 | 28 / 14.2 |
| F606W | 162 / 62.8 | 145 / 59.1 |
| F814W | 371 / 163.0 | 361 / 149.6 |
| F850LP | 4 / 2.0 | 4 / 2.0 |
| F105W | 31 / 12.2 | 32 / 13.1 |
| F125W | 139 / 66.2 | 152 / 73.1 |
| F140W | 106 / 25.1 | 114 / 26.8 |
| F160W | 380 / 114.7 | 436 / 127.1 |

Source: `https://s3.amazonaws.com/grizli-v2/JwstMosaics/v7/primer-cosmos-{east,west}-grizli-v7.0-<filter>_wcs.csv`. The F160W lists include DASH programs 14114 and 16259. The two halves overlap. Full-field counts (COSMOS-Web area): **[unknown]** beyond the ~2,300 F814W estimate above.

NIRWL Table 2 gives 212 F160W frames in the CANDELS-COSMOS programs. **[verified]** https://ar5iv.labs.arxiv.org/html/2301.07725

### Public reductions

| Reduction | Filters | Pixel scale | Astrometric frame | Where / version | Terms |
|---|---|---|---|---|---|
| COSMOS ACS v2.0 (Koekemoer+2007) | F814W | 0.03″; 144 tiles of 20,480² pix (README says 81 survey tiles); inverse-variance weights | README says "astrometrically aligned", no catalog named; CANDELS paper says the COSMOS frame is CFHT/MegaCam i* supplemented by Subaru i+ | https://irsa.ipac.caltech.edu/data/COSMOS/images/acs_mosaic_2.0/ ; README dated 2011-10-31 | cite Koekemoer+2007, Massey+2010 **[verified]** |
| COSMOS-Web reprocessed F814W | F814W | 0.03″ | "direct astrometric alignment to Gaia-DR3 as well as the COSMOS2020 catalog", following Koekemoer+2011 methods; used as the NIRCam astrometric reference | Described in Franco+2025 **[verified]** https://arxiv.org/html/2506.03256 . Public serving of the F814W mosaic itself: **[unknown]** — the DR page lists only NIRCam and MIRI (https://cosmos.astro.caltech.edu/page/cosmosweb-dr); a CDS HiPS named "COSMOS-Web DR1 HST ACS F814W" appeared in search results but could not be opened | — |
| CANDELS COSMOS v1.0 (Koekemoer+2011) | F606W, F814W, F125W, F160W | 30 and 60 mas **[inferred]** for this field (the HLSP page states them for GOODS-N) | Subaru/CFHT-based; Treasury F160W mosaic is 0.11 ± 0.04″ from Gaia DR3 (NIRWL Table 1) | https://archive.stsci.edu/hlsp/candels ; V1.0 released 2013-02-07 | CC BY 4.0 **[verified]** |
| 3D-HST v4.0 (Skelton+2014) | F606W, F814W, F125W, F140W, F160W | 0.06″ | tweakreg to CANDELS F160W v1.0 (same frame as CANDELS) | https://archive.stsci.edu/prepds/3d-hst/ | license not stated **[verified]** |
| NIRWL (Finner+2023) | F160W only | 0.05″, Gaussian kernel, pixfrac 0.7 | Gaia DR3 with proper motions, hybrid with HSC-SSP DR3 / PS1 DR2; 0.02 ± 0.03″ in COSMOS | https://archive.stsci.edu/hlsp/candelsnirwl ; v1.0, 2024-03-26 | CC BY 4.0 **[verified]** |
| COSMOS-DASH / 3D-DASH | F160W | **[unknown]** | "external catalogs" (not named) | https://archive.stsci.edu/hlsp/cosmos-dash (v1.2.10), https://archive.stsci.edu/hlsp/3d-dash (v1.0, 2022-06-02, 150 tiles) | CC BY 4.0 **[verified]** |
| DJA / grizli `primer-cosmos-{east,west}-grizli-v7.0`; `cosweb-grizli-v6.0` | F435W…F850LP, F105W…F160W | 40 mas (HST and NIRCam LW) | PRIMER visits aligned to an F444W-derived catalog itself aligned to the HSC ultra-deep i-band catalog; `wcsname` = `USER` in the exposure tables | https://dawn-cph.github.io/dja/imaging/v7/ , `s3.amazonaws.com/grizli-v2/JwstMosaics/v7/` ; v7.0 = July 2023 | cite Valentino+2023; no license stated **[verified]** |
| UVCANDELS | F435W (+F275W) | 30 and 60 mas | aligned to CANDELS 30 mas (F814W for COSMOS), < 45 mas | https://archive.stsci.edu/hlsp/uvcandels ; v1.0 | CC BY 4.0 **[verified]** |
| HAP MVM skycells | all | 0.04″; WFC3/IR also 0.12″ | "most current Gaia catalog"; fallbacks to other catalogs when a visit cannot be aligned | MAST | **[verified]** https://drizzlepac.readthedocs.io/en/latest/mast_data_products/mvm_processing.html |

### Best comparison reference

- F814W: the COSMOS-Web reprocessed mosaic if it can be obtained (Gaia DR3, 30 mas, and COSMOS-Web tiles use CRVAL = 150.1163213, 2.200973097 **[verified]**, which is the project's `cosmos` tangent point). Otherwise IRSA v2.0 for pixel-level flux comparison, with a frame offset to be fitted.
- F160W astrometry: NIRWL. Multi-filter photometry and exposure membership: DJA v7.0 (the `_wcs.csv` tables say exactly which exposures went in).

### Astrometric reference options

Gaia DR3 is sparse: NIRWL found 0–5 usable Gaia stars per CANDELS patch and had to add HSC-SSP DR3 / PS1 DR2 **[verified]**. Options: the project's own NIRCam catalog (Gaia-tied through COSMOS-Web's chain), HSC-SSP UltraDeep **[inferred]**, COSMOS2020, Legacy Surveys DR9/DR10 **[inferred]**, Pan-STARRS.

---

## 2. `uds` — UDS

### HST imaging

- **CANDELS-UDS** (GO 12064, Faber): WFC3/IR F125W + F160W (88 orbits) with ACS F606W + F814W parallels (88 orbits). **[verified]** Skelton Table 2. Wide strategy as in COSMOS. ACS is all post-2009.
- **3D-HST** (GO 12328): F140W, 8 orbits, with G141. **[verified]**
- **Riess SN follow-up** (GO 12099): F125W + F160W, 18 orbits. **[verified]**
- **CANDELS/UDS B-band Supplemental** (GO/DD 16872, Grogin, Cycle 29, 44 orbits): the F435W exposures in the DJA mosaics all come from this program. **[verified]** https://www.stsci.edu/hst-program-info/program/?program=16872 and DJA tables.
- Other programs in the PRIMER-UDS footprint (DJA tables, **[verified]**): 10876 (Kneib, ACS F606W/F814W, pre-2007 by proposal number **[inferred]**), 9075 (Perlmutter, ACS F775W/F850LP, pre-2007 **[inferred]**), 12265 (Ouchi), 12461 (Riess), 13002 (Williams), 13792 (Bouwens), 14719 (Best), 15137 (Jiang), 15363 (Suzuki), 15862 (Finkelstein).

### Exposure counts (DJA v7.0, PRIMER-UDS footprint) **[verified]**

| Filter | north: n exp / ks | south: n exp / ks |
|---|---|---|
| F435W | 104 / 50.2 | 108 / 52.2 |
| F606W | 141 / 53.3 | 145 / 54.7 |
| F775W | 6 / 2.8 | 4 / 1.8 |
| F814W | 326 / 138.5 | 364 / 160.2 |
| F850LP | 6 / 3.0 | 12 / 6.0 |
| F105W | 26 / 12.4 | 17 / 8.6 |
| F125W | 180 / 76.2 | 163 / 83.2 |
| F140W | 88 / 20.3 | 104 / 22.1 |
| F160W | 180 / 115.3 | 170 / 125.3 |

NIRWL Table 2: 263 F160W frames in the UDS programs. **[verified]**

### Public reductions

| Reduction | Filters | Pixel scale | Astrometric frame | Where / version | Terms |
|---|---|---|---|---|---|
| CANDELS UDS v1.0 | F606W, F814W, F125W, F160W | 30/60 mas **[inferred]** | UKIDSS UDS K-band catalog; Treasury mosaic 0.13 ± 0.05″ from Gaia DR3 | https://archive.stsci.edu/hlsp/candels | CC BY 4.0 **[verified]** |
| 3D-HST v4.0 | + F140W | 0.06″ | CANDELS F160W v1.0 | https://archive.stsci.edu/prepds/3d-hst/ | **[verified]** |
| NIRWL | F160W | 0.05″ | Gaia DR3, 0.01 ± 0.03″ | https://archive.stsci.edu/hlsp/candelsnirwl | CC BY 4.0 **[verified]** |
| DJA `primer-uds-{north,south}-grizli-v7.0` (v7.2 adds NIRCam medium bands) | F435W, F606W, F775W, F814W, F850LP, F105W–F160W | 40 mas | F444W catalog aligned to HSC ultra-deep i band | https://dawn-cph.github.io/dja/imaging/v7/ | **[verified]** |
| MINERVA DR0 (UDS, 2026-01-13) | F435W, F606W, F775W, F814W, F850LP re-reductions of CANDELS data (no WFC3/IR listed) | 0.04″ | not stated | https://minerva.colorado.edu/data-release.html ; grizli-v8.0 / minerva-v2.3; S3 `grizli-v2` | cite Muzzin, Suess, Marchesini+2026 and CANDELS **[verified]** |
| PRIMER team mosaics | — | papers cite a "v2.0.0 PRIMER data release" | — | no MAST HLSP or public page found; Dunlop et al. still "in prep." per search results **[unknown]** | — |
| HAP MVM | all | 0.04″ / 0.12″ | Gaia where it fits | MAST | **[verified]** |

### Best comparison reference

NIRWL F160W for astrometry (Gaia DR3); DJA v7.0 / MINERVA DR0 for multi-filter flux and exposure membership; CANDELS v1.0 for the canonical weight-map convention at 30/60 mas, with a known 0.13″ frame offset to remove.

### Astrometric reference options

Gaia DR3 sparse (b = −60). Project NIRCam catalog; HSC-SSP UltraDeep SXDS **[inferred]**; UKIDSS UDS; Legacy Surveys DR9/DR10 **[inferred]**; Pan-STARRS (Dec −5 is inside PS1 **[inferred]**).

---

## 3. `egs` — EGS / AEGIS

### HST imaging

- **AEGIS ACS** (GO 10134, Davis): 21 × 3 = 63 contiguous tiles, F606W 2260 s and F814W 2100 s per tile, 4-point dither per filter, ~10.1′ × 70.5′ = 710.9 arcmin², June 2004 – March 2005 (pre-2007 ACS). **[verified]** https://ar5iv.labs.arxiv.org/html/astro-ph/0607355 . That is 63 × 8 = 504 exposures **[inferred]** (product of verified numbers).
- **CANDELS-EGS** (GO 12063, Faber): WFC3/IR F125W + F160W 90 orbits, ACS F606W + F814W parallels 90 orbits (post-SM4). **[verified]** Skelton Table 2. 45 tiles **[inferred]**.
- **3D-HST** (GO 12177): F140W 8 orbits with G141. **[verified]**
- CEERS HDR1 combines **1,767 exposures** from eight programs: 10134, 12063, 12099, 12167, 12177, 12547, 13063, 13792. **[verified]** https://ceers.github.io/hdr1.html and https://web.corral.tacc.utexas.edu/ceersdata/HDR1/README.txt
- **UVCANDELS** (15647): F435W parallels delivered for EGS. **[verified]**
- NIRWL Table 2: 226 F160W frames in the EGS programs. **[verified]**

### Public reductions

| Reduction | Filters | Pixel scale | Astrometric frame | Where / version | Terms |
|---|---|---|---|---|---|
| **CEERS HDR1** (Koekemoer) | ACS F606W, F814W; WFC3/IR F105W, F125W, F140W, F160W | 0.03″; all filters pixel-aligned; `drz` (e/s), `wht` (inverse variance), `rms` (free of correlated noise) | Gaia-EDR3 | https://ceers.github.io/hdr1.html ; files `egs_all_<inst>_<filter>_030mas_v1.9_{drz,wht,rms}.fits` at https://web.corral.tacc.utexas.edu/ceersdata/HDR1/ ; files dated 2021-12 to 2022-02 | cite Koekemoer+2011; no license stated **[verified]** |
| CANDELS EGS v1.0 | F606W, F814W, F125W, F160W | 30/60 mas **[inferred]** | DEEP2 CFHT/12k R-band (Coil+2004) with ACS F814W; Treasury mosaic 0.07 ± 0.05″ from Gaia DR3. The 2017 CANDELS-EGS catalog paper reports a spatially varying ~0.2″ offset relative to the CFHT frame (search-result excerpt of arXiv:1703.05768, **[inferred]** — not opened) | https://archive.stsci.edu/hlsp/candels | CC BY 4.0 **[verified]** |
| 3D-HST v4.0 (AEGIS) | F606W, F814W, F125W, F140W, F160W | 0.06″ | tweakreg to AEGIS ACS F814W (Davis+2007) | https://archive.stsci.edu/prepds/3d-hst/ | **[verified]** |
| NIRWL | F160W | 0.05″ | Gaia DR3, 0.03 ± 0.02″ | MAST | CC BY 4.0 **[verified]** |
| DJA | `ceers-full` v7.x lists JWST filters only; `ceers-par-grizli-v7.5` has F435W, F606W, F814W, F105W–F160W (36/43/170/12/23/14/31 exposures) for a parallel area | 40 mas | `USER` | S3 v7 index | **[verified]** from the index and `_wcs.csv`. An EGS-wide grizli HST mosaic may exist under another version path: **[unknown]** |
| UVCANDELS | F435W | 30/60 mas | CANDELS F606W 30 mas | MAST | CC BY 4.0 **[verified]** |
| HAP MVM | all | 0.04″ / 0.12″ | Gaia where it fits | MAST | **[verified]** |

The CEERS NIRCam reduction used an F160W catalog from this 0.03″ Gaia-EDR3 mosaic as its astrometric reference, and the NIRCam mosaics share its tangent point (pixel-aligned). HST-to-NIRCam RMS ~12–15 mas. **[verified]** https://arxiv.org/html/2211.02495

### Best comparison reference

**CEERS HDR1 v1.9.** Gaia-tied, 30 mas, both instruments, six filters, science + inverse-variance weight + RMS maps, and a published exposure list by program. It is the cleanest like-for-like reference in any of the fields.

### Astrometric reference options

Gaia DR3 sparse (b = +59). Project NIRCam catalog; Legacy Surveys DR9 (CEERS used it as an intermediate, per search-result excerpt **[inferred]**); HSC-SSP **[inferred]**; CFHTLS D3; Pan-STARRS.

---

## 4. `a2744` — Abell 2744

### HST imaging

- **GO 11689** (Dupke): ACS on the cluster, 16 orbits, 2009 (post-SM4 **[inferred]** from proposal number). **[verified]** orbits: https://archive.stsci.edu/hlsp/frontier
- **Frontier Fields** (GO/DD 13495, Lotz): 70 orbits per camera per field. Epoch 1 (Oct–Dec 2013): WFC3/IR on the cluster, ACS on the parallel. Epoch 2 (May–Jul 2014): ACS on the cluster, WFC3/IR on the parallel. Survey-wide split ≈ F435W 18, F606W 10, F814W 42, F105W 24, F125W 12, F140W 10, F160W 24 orbits. 5σ point-source depths 28.6–29.1 AB. Epoch-1 F105W was affected by time-variable He I sky. Parallel field at 00:13:53.6 −30:22:54.3. **[verified]** https://ar5iv.labs.arxiv.org/html/1605.06567 , https://archive.stsci.edu/hlsp/frontier
- **GO 13386** (Rodney, FrontierSN) and **GO 13389** (Siana, ACS F435W… on the parallel, 16 orbits). **[verified]**
- **GLASS** (GO 13459, Treu): F105W and F140W direct images with the grisms; appears in the DJA F814W/F105W/F140W lists. **[verified]** DJA tables.
- **BUFFALO** (GO 15117, Steinhardt): 16 orbits per cluster in two orients; ACS F606W (2/3 orbit) and F814W (4/3 orbit), WFC3/IR F105W/F125W/F160W (2/3 orbit each); 160 exposures per cluster; 4× the HFF WFC3/IR area and 3× the ACS area; A2744 visits May–Dec 2019. **[verified]** https://ar5iv.labs.arxiv.org/html/2001.09999
- **GO/DD 17231** (Treu, Cycle 29, 15 orbits): ACS F606W and F775W. **[verified]** https://www.stsci.edu/hst-program-info/program/?program=17231 , https://arxiv.org/html/2212.04026
- Also in DJA lists: 15940 (Ribeiro, F475W), 15936 (Kelly), 16757 (Gonzalez). **[verified]**

### Exposure counts (DJA, `abell2744clu` footprint, which includes BUFFALO flanking) **[verified]**

| Filter | cluster v7.0 (ACS v7.1 in brackets): n exp / ks | parallel v7.0: n exp / ks |
|---|---|---|
| F435W | 104 / 133.6 | 48 / 61.9 |
| F475W | 16 / 20.6 | — |
| F606W | 106 / 98.9 [126 / 116.0] | 46 / 40.6 |
| F775W | 20 / 23.6 | — |
| F814W | 262 / 258.5 [282 / 275.5] | 139 / 135.8 |
| F105W | 154 / 151.5 | 79 / 77.1 |
| F125W | 88 / 81.8 | 40 / 39.8 |
| F140W | 55 / 56.7 | 33 / 29.4 |
| F160W | 145 / 153.0 | 70 / 75.9 |

Cluster mosaic total ≈ 950 ACS/WFC + WFC3/IR exposures.

### Public reductions

| Reduction | Filters | Pixel scale | Astrometric frame | Where / version | Terms |
|---|---|---|---|---|---|
| Frontier Fields HLSP (Koekemoer/STScI) | F435W, F606W, F814W, F105W, F125W, F140W, F160W | 30 and 60 mas | Subaru I-band imaging (Merten+2011 catalogs); not Gaia | https://archive.stsci.edu/hlsp/frontier ; v1.0 epoch 1 2013-12-17, epoch 2 2014-07-23; includes a self-calibrated ACS set | license not stated; cite Lotz+ and Koekemoer+ **[verified]** |
| BUFFALO HLSP | F606W, F814W, F105W, F125W, F160W | 30 and 60 mas (paper: common 0.06″ grid) | "tied directly to the GAIA-DR2 absolute astrometric frame" | https://archive.stsci.edu/hlsp/buffalo ; images v1.0, released 2023-08-01 | CC BY 4.0 **[verified]** |
| UNCOVER DR1 (Bezanson+2024) | ACS F435W, F475W, F606W, F775W, F814W; WFC3/IR F105W–F160W (plus UVIS) | 0.04″ for HST, same WCS as NIRCam | F444W aligned to Legacy Surveys DR9, checked against Gaia DR3 at 12 mas | https://jwst-uncover.github.io/DR1.html ; grizli-v5.4; 2022-12-07; superseded | cite Bezanson+2024 **[verified]** |
| UNCOVER / MegaScience DR3 (Suess+2024) = DJA `abell2744clu` | same | 0.04″ | exposure tables show `wcsname` = `LS_DR9` and `USER` | https://jwst-uncover.github.io/DR3.html ; HST ACS v7.1, WFC3/IR v7.0, NIRCam v7.2; 2024-04-22; `s3.amazonaws.com/grizli-v2/JwstMosaics/v7/`; BCG-subtracted versions on Google Drive; a DR4 exists | cite Suess+2024, Bezanson+2024 **[verified]** |
| HAP MVM | all | 0.04″ / 0.12″ | Gaia where it fits | MAST | **[verified]** |

### Best comparison reference

Two, for different purposes: **BUFFALO v1.0** (Gaia DR2 frame, 30/60 mas, a small well-defined exposure set) for astrometry and pixel-scale-matched comparison; **UNCOVER DR3 / DJA v7.x** for depth and photometry with every program included and a per-exposure list. The Frontier Fields v1.0 mosaics remain the reference for deep-stack noise properties, on a non-Gaia frame.

### Astrometric reference options

Gaia DR3 very sparse (b = −81). Project NIRCam catalog (UNCOVER chain: LS DR9 → Gaia); Legacy Surveys DR9/DR10 (used by grizli here, **[verified]** from `wcsname`); no HSC-SSP at Dec −30 **[inferred]**; Pan-STARRS stops at Dec −30, so the field (Dec −30.4) is at or just beyond its edge **[inferred]**.

---

## 5. `nep` — NEXUS

### HST imaging

**Effectively none.** A MAST cone search of radius 0.3° around the NEXUS centre returns 5 HST image observations: one ACS/WFC F814W 1500 s pointing (SNAP 18022, Nedkova, "SHIP^3: Snapshot HST Imaging of Pure-Parallel Programs", target `PARALLEL-FIELD-627-C7`, 2026-03-16) and its HAP products, plus WFC3/IR darks. **[verified]** MAST query run here; program title https://www.stsci.edu/hst-program-info/program/?program=18022 . No WFC3/IR science imaging.

The NEXUS papers list Euclid (Deep Field North / self-calibration field), Subaru HSC (HEROES), eROSITA, WISE, WFST, YSE as ancillary data and do not mention HST imaging. **[verified]** https://arxiv.org/html/2408.12713v1 , https://arxiv.org/abs/2411.06372

The deep HST ACS F435W/F606W imaging near the pole (TREASUREHUNT; GO 15278, 16252, 16793, ~88 arcmin², F606W to ~29.5) is in the JWST NEP Time-Domain Field, a different field ~3.3° away. **[verified]** programs/filters from https://arxiv.org/abs/2401.04944 and the DJA `jwst-nep-tdf-nrc-grizli-v7.5` tables (F435W 196 exposures, F606W 184).

### Public reductions

None for HST. DJA has `nexus-nep-grizli-v7.4` (JWST only: F090W, F115W, F200W, F356W, F444W, MIRI). **[verified]** NEXUS team EDR at https://ariel.astro.illinois.edu/nexus/edr/ **[verified]** as cited in the abstract.

### Astrometric reference options

b = +30, so Gaia DR3 is denser than in the other deep fields; the NEXUS team ties NIRCam to Gaia DR3 when ≥ 15 stars match and otherwise to the HEROES Subaru HSC catalog. **[verified]** https://arxiv.org/html/2509.19585v1 . Euclid Deep Field North catalogs also cover it **[inferred]**.

**Conclusion: `nep` is out of scope for HST reduction.**

---

## 6. `rj0911` — RXC J0911.1+1746 (MACS J0911.2+1746), z = 0.505

### HST imaging (MAST metadata, cone 0.05°) **[verified]**

| Program | Instrument / filter | Exposures (FLT/FLC) | Total time | Date |
|---|---|---|---|---|
| 9722 (Ebeling) | ACS/WFC F555W | 6 | 4470 s | 2004-03-26 |
| 9722 (Ebeling) | ACS/WFC F814W | 6 | 4560 s | 2004-03-26 |
| 10493 (Gal-Yam) | ACS/WFC F814W | 4 | 2183 s | 2005-10-14 |
| 10793 (Gal-Yam) | ACS/WFC F814W | 4 | 2082 s | 2006-12-12 |
| 14096 (RELICS, Coe) | ACS/WFC F435W | 4 | 2009 s | 2017-02-04 |
| 14096 (RELICS) | WFC3/IR F105W | 4 | 1412 s | 2016-02-16, 2016-04-05 |
| 14096 (RELICS) | WFC3/IR F125W | 4 | 737 s | same |
| 14096 (RELICS) | WFC3/IR F140W | 4 | 712 s | same |
| 14096 (RELICS) | WFC3/IR F160W | 4 | 2012 s | same |
| 17504 (Kelly) | WFC3/IR F105W | 18 | 2874 s | 2023-12-15 – 2024-10-21 |
| 17504 (Kelly) | WFC3/UVIS F606W, F814W | 3, 15 | 1110 s, 5090 s | 2023-12 – 2024-10 (not ACS) |

Totals: 24 ACS/WFC exposures (20 pre-2007, 4 post-2009) and 34 WFC3/IR exposures (16 RELICS + 18 later F105W). There is **no ACS F606W**. The RELICS paper's archival table lists 4470 s and 8825 s for this cluster, which match F555W and the F814W sum. **[verified]**

### Public reductions

- **RELICS HLSP**: images, color images, catalogs, lens models for `rxc0911+17`; 30 and 60 mas grids; absolute astrometry from the **WISE point-source catalog** (not Gaia); released 2016-08-18, updated 2022-09-30; CC BY 4.0. **[verified]** https://archive.stsci.edu/hlsp/relics , https://ar5iv.labs.arxiv.org/html/1903.02002 . Whether the RELICS ACS mosaics here include the 2004–2006 archival exposures: **[unknown]** (expected, since RELICS only topped up missing depth).
- **HAP SVM/MVM**: skycell products exist for F435W, F555W, F814W, F105W–F160W and UVIS (2 per filter in the MAST listing). **[verified]**
- **DJA**: not in the v6/v7 index. VENUS clusters appear there with names like `rj0018p1626-grizli-v7.5`, so one may be added. **[verified]** absence as of an index whose newest file date is 2026-07-06.
- **VENUS DR1** (images with and without BCG+ICL subtraction) is "anticipated in late 2026"; all links are TBD and HST is not mentioned. **[verified]** https://jwst-venus.github.io/data.html

### Best comparison reference

RELICS 30/60 mas mosaics for flux; HAP SVM for a Gaia-tied astrometric check. No Gaia-tied third-party deep mosaic exists yet.

### Astrometric reference options

b = +38.7: moderate Gaia DR3 density, proper motions matter across the 2004–2024 baseline. Project NIRCam catalog; Legacy Surveys DR9/DR10 and Pan-STARRS cover Dec +17.8 **[inferred]**.

---

## 7. `pg004` — PLCK G004.5−19.5, z = 0.540

### HST imaging (MAST metadata, cone 0.13°) **[verified]**

| Program | Instrument / filter | Exposures | Total time | Date |
|---|---|---|---|---|
| 14096 (RELICS) | ACS/WFC F435W | 4 | 2206 s | 2016-08-15 |
| 14096 | ACS/WFC F606W | 4 | 2180 s | 2016-09-25 |
| 14096 | ACS/WFC F814W | 4 | 2377 s | 2016-08-15 |
| 14096 | WFC3/IR F105W, F125W, F140W, F160W on the cluster | 4 each (16) | cluster + parallel sums: 2868, 2068, 1993, 3468 s | 2016-08-15, 2016-09-25 |
| 14096 | WFC3/IR same four filters, parallel field | 2 each (8) | included above | 2016 |
| 14096 | WFC3/UVIS F350LP, parallel | 4 | 2484 s | 2016-08-15 |
| 16729 (Kelly, SNAP) | WFC3/IR F105W | 15 | 2505 s | 2022-04-02 – 2022-06-04 |
| 16729 | WFC3/UVIS F606W | 15 | 5320 s | 2022 (not ACS) |

Cluster core: **12 ACS/WFC + 16 WFC3/IR exposures from one program, 5 orbits, two epochs six weeks apart**; all post-SM4. No archival ACS before RELICS. **[verified]** (RELICS Table 4/5 agree: 1 orbit each F435W/F606W/F814W, 2 orbits WFC3/IR, 3 parallel orbits.)

### Public reductions

- **RELICS HLSP** `plckg004-19`: cluster core (images, color, catalogs, lens models) and a parallel field (images, catalogs); 30/60 mas; WISE-based absolute astrometry; CC BY 4.0. **[verified]**
- **HAP SVM/MVM** products exist for all filters. **[verified]** MAST listing.
- **DJA**: not in the v6/v7 index. **[verified]**
- VENUS DR1: TBD, as above.

### Best comparison reference

RELICS 30/60 mas. Same caveat on the WISE frame.

### Astrometric reference options

b = −19.5: **Gaia DR3 is dense here**; direct Gaia alignment of every visit should work, with proper motions for the 2016 → 2025 baseline. Legacy Surveys coverage at this latitude: **[unknown]**. Crowding and bright stars (persistence, spikes) are the cost.

---

## 8. `goods-s`

### HST imaging

HLF-GOODS-S v2.0 combines **7,491 exposures, ~6.37 Ms, 33 programs, July 2002 – October 2016**, over 25′ × 25′, in 13 filters (WFC3/UVIS F225W, F275W, F336W; ACS F435W, F606W, F775W, F814W, F850LP; WFC3/IR F098M, F105W, F125W, F140W, F160W). **[verified]** https://archive.stsci.edu/prepds/hlf/ . Whitaker+2019 gives 7475 exposures / 2635 orbits / 6.3 Ms. **[verified]** https://ar5iv.labs.arxiv.org/html/1908.05682 . Main programs (Skelton Table 2, **[verified]**): GOODS ACS 9425 (199 orbits, pre-2007), CANDELS 12060–12062 (ACS 229 orbits, WFC3 173), HUDF09 11563 (149 orbits WFC3), ERS 11359/11360, 3D-HST 12177, plus HUDF 9978/10086, HUDF12 12498, etc. (DJA lists).

DJA `gds-grizli-v7.0` counts **[verified]**: F435W 528, F606W 490, F775W 691, F814W 1185, F850LP 1224, F105W 392, F125W 499, F140W 202, F160W 579 exposures (≈ 5,800 ACS/WFC + WFC3/IR). NIRWL: 644 F160W frames.

### Public reductions

| Reduction | Pixel scale | Frame | Where / version |
|---|---|---|---|
| HLF v2.0 (Illingworth, Magee, Whitaker…) | 30 mas (ACS, UVIS) and 60 mas (all) | **Sources disagree.** The HLSP page says the grid is "tied to the absolute Gaia DR2 reference frame" with an alternate system on the original GOODS tangent point. Whitaker+2019 says the standard astrometry matches CANDELS/3D-HST and the catalog adds a Gaia-corrected column; offsets vs Gaia DR2: ΔRA +0.011 ± 0.08″ in the text (Table 2 applies +0.113″), ΔDec −0.26 ± 0.10″. | https://archive.stsci.edu/prepds/hlf/ ; catalog v2.1 2020-05-28; license not stated **[verified]** |
| CANDELS GOODS-S v1.0 | 30/60 mas **[inferred]** | ESO 2.2 m WFI R band; 0.26 ± 0.05″ from Gaia DR3 | https://archive.stsci.edu/hlsp/candels **[verified]** |
| 3D-HST v4.0 | 0.06″ | ACS F814W reference | **[verified]** |
| NIRWL | 0.05″, F160W | Gaia DR3, 0.02 ± 0.02″ | **[verified]** |
| DJA `gds-grizli-v7.0` (also `gds-sw`, `ngdeep`, `gds-miri-hudf`) | 40 mas | `LS_DR9` / `USER` | **[verified]** |
| JADES | 0.03″ (0.0299947) | JADES registers to "HST images that have been registered to Gaia DR2" (G. Brammer) and matches the HLF effective pixel size | https://arxiv.org/html/2306.02466 **[verified]**; the HLF version JADES adopted is not stated there **[unknown]** |

### Best comparison reference

HLF v2.0 (30/60 mas, full depth) for flux and depth, after checking which frame the downloaded files carry; NIRWL F160W or the JADES-registered frame for astrometry.

---

## 9. `goods-n`

### HST imaging

HLF-GOODS-N v2.5: **5,817 exposures, ~3.64 Ms, 29 programs, July 2002 – November 2018**, 20.5′ × 20.5′, 11 filters (no F225W, no F098M). **[verified]** HLF page. Main programs (**[verified]** Skelton Table 2): GOODS ACS 9583 (199 orbits, pre-2007), CANDELS 12442–12445 (ACS 219, WFC3 173 orbits), AGHAST 11600 (F140W with grism), 12461, plus 13063 (Riess), 13420 (Barro), 13779 (Malhotra, FIGS), 14227 (Papovich, CLEAR), 15647 (UVCANDELS) from the DJA lists.

DJA `gdn-grizli-v7.3` counts **[verified]**: F435W 586, F606W 320, F775W 599, F814W 1030, F850LP 1232, F105W 346, F125W 440, F140W 192, F160W 462. NIRWL: 463 F160W frames.

### Public reductions

HLF v2.5 (30/60 mas; page says Gaia DR2 grid; updated 2021-10-29); CANDELS GOODS-N v1.0 (Subaru Suprime-Cam R-band frame; 0.05 ± 0.03″ from Gaia DR3); 3D-HST v4.0; NIRWL (0.02 ± 0.02″); DJA `gdn-grizli-v7.3` (Feb 2024; `wcsname` = `LS_DR9`, `PS1`, `USER`); UVCANDELS. **[verified]** at the URLs above.

### Best comparison reference

HLF v2.5.

### Astrometric reference options (both GOODS fields)

Gaia DR3 sparse; HLF/JADES Gaia-registered catalogs; Legacy Surveys DR9; PS1 (GOODS-N); HSC-SSP does not cover GOODS-S **[inferred]**.

---

## Summary table

| Field | ACS/WFC coverage | WFC3/IR coverage | Public reductions | Best comparison reference | Astrometric reference options |
|---|---|---|---|---|---|
| cosmos | F814W over 1.64 deg² (2003–05, pre-SM4, ~575 pointings × 4); CANDELS F606W/F814W (2011–13); F435W (UVCANDELS, 15100); F475W strip | CANDELS F125W/F160W; 3D-HST F140W; COSMOS-DASH / 3D-DASH F160W (unguided) | IRSA ACS v2.0 (30 mas); COSMOS-Web reprocessed F814W (Gaia DR3, 30 mas, availability unknown); CANDELS v1.0; 3D-HST; NIRWL; DASH HLSPs; DJA v7.0 (40 mas); UVCANDELS; HAP | COSMOS-Web F814W if obtainable, else IRSA v2.0; NIRWL for F160W astrometry; DJA for multi-filter | NIRCam catalog; HSC-SSP UD; COSMOS2020; LS DR9/10; Gaia sparse |
| uds | CANDELS F606W/F814W (post-SM4); F435W (16872, Cycle 29); small pre-2007 pieces (10876, 9075) | CANDELS F125W/F160W; 3D-HST F140W; 12099 | CANDELS v1.0; 3D-HST; NIRWL; DJA v7.0; MINERVA DR0 (ACS only, 40 mas); HAP | NIRWL (astrometry) + DJA/MINERVA (flux) | NIRCam catalog; HSC-SSP SXDS; UKIDSS; LS; PS1; Gaia sparse |
| egs | AEGIS F606W/F814W, 63 tiles (2004–05, pre-SM4); CANDELS parallels (post-SM4); F435W (UVCANDELS) | CANDELS F125W/F160W; 3D-HST F140W; F105W (13792) | **CEERS HDR1 v1.9 (Gaia EDR3, 30 mas, 6 filters, wht + rms)**; CANDELS v1.0; 3D-HST; NIRWL; UVCANDELS; HAP | CEERS HDR1 | NIRCam catalog; LS DR9; HSC; CFHTLS D3; PS1; Gaia sparse |
| a2744 | F435W/F606W/F814W deep (11689, HFF 2013–14), BUFFALO F606W/F814W (2019), F775W (17231), F475W | HFF F105W/F125W/F140W/F160W (70 orbits), GLASS direct images, BUFFALO | HFF v1.0 (30/60 mas, Subaru frame); BUFFALO v1.0 (30/60 mas, Gaia DR2); UNCOVER DR1–DR4 = DJA v7.x (40 mas, LS DR9/Gaia); HAP | BUFFALO (astrometry, pixel scale) + UNCOVER DR3/DJA (depth, exposure lists) | NIRCam catalog; LS DR9/10; Gaia very sparse |
| nep (NEXUS) | one 1500 s F814W snapshot (2026) | none | none | none | Gaia DR3 (b = +30), HEROES HSC, Euclid |
| rj0911 | F555W + F814W (2004–06, pre-SM4, 20 exp); F435W (2017, 4 exp); no F606W | RELICS 4 filters (16 exp, 2016); F105W (18 exp, 2023–24) | RELICS HLSP (30/60 mas, WISE frame); HAP | RELICS | Gaia DR3 moderate; NIRCam catalog; LS; PS1 |
| pg004 | RELICS F435W/F606W/F814W, 4 exp each (2016) | RELICS 4 filters, 16 exp on core + 8 parallel (2016); F105W 15 exp (2022) | RELICS HLSP (core + parallel); HAP | RELICS | Gaia DR3 dense (b = −19.5); NIRCam catalog |
| goods-s | GOODS (2002–03), HUDF, CANDELS, many others; ~4,100 ACS exposures in DJA | ERS, HUDF09/12, CANDELS, 3D-HST, CLEAR; ~1,670 exposures in DJA | HLF v2.0 (30/60 mas); CANDELS; 3D-HST; NIRWL; DJA v7.0; HAP | HLF v2.0 | HLF/JADES Gaia-registered catalogs; LS DR9; Gaia sparse |
| goods-n | GOODS (2002–03), CANDELS, others; ~3,770 ACS exposures in DJA | CANDELS, AGHAST, CLEAR, others; ~1,440 in DJA | HLF v2.5; CANDELS; 3D-HST; NIRWL; DJA v7.3; HAP | HLF v2.5 | same, plus PS1 |

Known frame offsets of the CANDELS Treasury v1.0 mosaics from Gaia DR3 (NIRWL Table 1, **[verified]**): UDS 0.13″, GOODS-S 0.26″, GOODS-N 0.05″, EGS 0.07″, COSMOS 0.11″. 3D-HST and HLF v2.0 (standard astrometry) inherit the CANDELS frames. RELICS (WISE) and Frontier Fields (Subaru) frames versus Gaia: **[unknown]**.

Catalog-level products: Hubble Source Catalog v4 was released 2026-06-30, built on HAP source lists, 88% matched to Gaia DR3, covering ACS and WFC3 data public as of 2026-03-17. **[verified]** https://outerspace.stsci.edu/spaces/hsc/overview

---

## Things that are unusual and will matter to a pipeline

- **ACS eras.** COSMOS F814W, AEGIS, GOODS and the RXC J0911 F555W/F814W are 2002–2006 data; CANDELS parallels, HFF, BUFFALO, RELICS and UDS F435W are post-SM4, where CTE correction (FLC) and bias-striping removal are needed. Koekemoer+2011 lists exactly these as the ACS corrections applied. **[verified]** abstract.
- **DASH.** COSMOS F160W wide-area coverage is unguided drift-and-shift; skip it or treat it separately. **[inferred]** for the processing detail.
- **Grism-paired direct images.** F140W (3D-HST, AGHAST, GLASS) and F105W (GLASS, CLEAR, FIGS) direct images are short and sit next to grism exposures; persistence risk. **[inferred]**
- **Time-variable F105W sky** (He I) affected A2744 epoch 1. **[verified]** Lotz+2017.
- **CANDELS ACS parallels are offset from the WFC3 prime tile**, so one "tile" does not give both instruments on the same sky; coverage in both comes from the mosaic pattern plus AEGIS/COSMOS ACS. **[inferred]**
- **Gaia sparsity.** 0–5 usable Gaia stars per CANDELS patch. **[verified]** NIRWL. Every modern reduction goes through an intermediate catalog (Legacy Surveys DR9, HSC, or a NIRCam/F160W catalog already tied to Gaia).

---

## Proof-of-concept candidates

### A. PLCK G004.5−19.5 (`pg004`) — smallest clean case

- 12 ACS/WFC FLC (F435W, F606W, F814W; 4 each) + 16 WFC3/IR FLT (F105W, F125W, F140W, F160W; 4 each) on the core, one program (14096), two epochs in 2016, 5 orbits. An 8-exposure WFC3/IR parallel and 15 more F105W from 2022 are optional extras.
- Pros: tiny and uniform; both instruments and seven filters; dense Gaia DR3 so absolute alignment is direct; it is a field the project already mosaics in NIRCam; RELICS 30/60 mas mosaics match the project pixel scales; no pre-SM4 ACS, no DASH, no grism neighbors.
- Cons: the only third-party reference is RELICS on a WISE frame, so astrometric validation must rest on Gaia and the project's NIRCam catalog, not on the reference mosaic; low galactic latitude means crowding and bright stars; BCG/ICL makes sky estimation harder than in a blank field; single-orbit ACS depth gives weak cosmic-ray rejection (4 exposures).

### B. One CANDELS-EGS WFC3 tile inside the CEERS NIRCam footprint — best validation

- Per tile: 8 WFC3/IR exposures from CANDELS (2 × F125W + 2 × F160W per epoch, two epochs), a few F140W from 3D-HST where present, 4 + 4 ACS exposures per overlapping AEGIS tile (2004–05), plus overlapping CANDELS ACS parallels (2011–13). Expect roughly 40–60 exposures for a region with all of F606W, F814W, F125W, F160W. **[inferred]** — the exact count needs a footprint query for the chosen tile.
- Pros: **CEERS HDR1 v1.9** is Gaia-EDR3-tied at 30 mas with science, inverse-variance weight and RMS maps and the program list, and the CEERS NIRCam mosaics are pixel-aligned to it; exercises both ACS eras and the sparse-Gaia alignment path that COSMOS and UDS will need; blank-field sky.
- Cons: more bookkeeping (several programs, parallels at different orients); few Gaia stars, so the alignment reference has to be the NIRCam catalog or Legacy Surveys; the reference mosaics are ~8 GB per file, so a cutout step is needed.

### C. RXC J0911.1+1746 (`rj0911`) — small, mixed-era

- 24 ACS/WFC (F555W 6, F814W 14 from 2004–06; F435W 4 from 2017) + 16 WFC3/IR RELICS exposures (+18 F105W 2023–24).
- Pros: small; tests pre- and post-SM4 ACS together and a 20-year proper-motion baseline; project field.
- Cons: no F606W; reference is RELICS only; UVIS data present that are out of scope.

### Not recommended as a first target

- **Abell 2744 core**: ~950 exposures in the DJA cluster footprint. The best-referenced field (HFF, BUFFALO, UNCOVER) but too heavy for a first pass. A BUFFALO-only subset (160 exposures per cluster, Gaia DR2-tied reference at 30/60 mas) is a reasonable second-stage test.
- **PRIMER-UDS CANDELS tile**: equivalent to B but without a Gaia-tied 30 mas multi-filter reference (CANDELS v1.0 is 0.13″ off; DJA/MINERVA are 40 mas).

Suggested order: A to prove the mechanics end to end, then B to validate astrometry, photometry and weights against CEERS HDR1.

---

## Sources and access status

| URL | Status |
|---|---|
| https://www.stsci.edu/jwst-program-info/program/?program=5105 , 9263 , 6882 | read |
| https://www.stsci.edu/jwst-program-info/download/jwst/pdf/5105/ , /6882/ | PDFs; text extracted locally with a zlib script (`research/tools/pdftxt.py`); target tables read |
| https://jwst-venus.github.io/about.html , /data.html | read |
| https://arxiv.org/html/2408.12713v1 , https://arxiv.org/abs/2411.06372 , https://arxiv.org/html/2509.19585v1 (NEXUS) | read |
| https://arxiv.org/abs/2401.04944 (TREASUREHUNT) | abstract read |
| https://archive.stsci.edu/hlsp/relics | read |
| https://ar5iv.labs.arxiv.org/html/1903.02002 (RELICS paper) | read (tables partly garbled; cross-checked with MAST metadata) |
| https://relics.stsci.edu/clusters.html | read; no per-cluster detail |
| https://archive.stsci.edu/hlsp/frontier | read |
| https://ar5iv.labs.arxiv.org/html/1605.06567 (Lotz+2017) | read |
| https://archive.stsci.edu/hlsp/buffalo , https://ar5iv.labs.arxiv.org/html/2001.09999 | read |
| https://jwst-uncover.github.io/DR1.html , /DR3.html , /DR4.html | read |
| https://arxiv.org/html/2212.04026 (Bezanson+) | read |
| https://archive.stsci.edu/hlsp/candels | read |
| https://ar5iv.labs.arxiv.org/html/1105.3754 (Koekemoer+2011) | first 100k characters read; mosaic pixel-scale section not reached |
| https://archive.stsci.edu/prepds/3d-hst/ , https://ar5iv.labs.arxiv.org/html/1403.3689 (Skelton+2014) | read (first 100k characters) |
| https://archive.stsci.edu/prepds/hlf/ , https://ar5iv.labs.arxiv.org/html/1908.05682 | read |
| https://arxiv.org/html/2306.02466 (Rieke+2023), https://arxiv.org/html/2306.02465 (Eisenstein+2023) | read |
| https://ceers.github.io/hdr1.html , https://web.corral.tacc.utexas.edu/ceersdata/HDR1/ , /README.txt | read |
| https://arxiv.org/html/2211.02495 (Bagley+) | read |
| https://archive.stsci.edu/hlsp/candelsnirwl , https://ar5iv.labs.arxiv.org/html/2301.07725 | read |
| https://archive.stsci.edu/hlsp/uvcandels | read |
| https://archive.stsci.edu/hlsp/cosmos-dash , https://archive.stsci.edu/hlsp/3d-dash , https://arxiv.org/abs/2206.01156 | read |
| https://arxiv.org/abs/astro-ph/0703095 , https://ar5iv.labs.arxiv.org/html/astro-ph/0612305 | read |
| https://irsa.ipac.caltech.edu/data/COSMOS/images/acs_mosaic_2.0/ and ACS_INFO_2.0.txt | read |
| https://irsa.ipac.caltech.edu/data/COSMOS/gator_docs/cosmos_acs_colDescriptions.html | read |
| https://arxiv.org/html/2506.03256 (Franco+2025) | read |
| https://cosmos.astro.caltech.edu/page/cosmosweb-dr | read |
| https://ar5iv.labs.arxiv.org/html/astro-ph/0607355 (Davis+2007) | read |
| https://dawn-cph.github.io/dja/imaging/v7/ , /imaging/v6/ , /imaging/summary , /blog/2023/07/18/image-data-products/ , /data/hst_program_codes.csv | read |
| https://s3.amazonaws.com/grizli-v2/JwstMosaics/v7/index.html , /v6/index.html and `*_wcs.csv` exposure tables | index HTML and CSV tables read; no images fetched |
| https://minerva.colorado.edu/data-release.html | read |
| https://drizzlepac.readthedocs.io/en/latest/mast_data_products/mvm_processing.html | read |
| https://outerspace.stsci.edu/spaces/hsc/overview | read |
| https://www.stsci.edu/hst-program-info/program/?program=16872 , 17231 , 16729 , 18022 | read |
| https://mast.stsci.edu/api/v0/invoke (CAOM cone + product listings, metadata only) | queried for three small fields |
| https://cosmos2025.iap.fr/ | Unable to access this page (connection refused / timeout) |
| https://exchg.calet.org/cosmosweb-public/DR1/ | Unable to access this page (connection refused) |
| https://alasky.cds.unistra.fr/COSMOS-Web/CDS_P_COSMOS-Web_DR1_HST_ACS_F814W/properties | Unable to access this page (connection refused / timeout) |
| https://dawn-cph.github.io/dja/imaging/ | Unable to access this page (404) |
| https://grizli.readthedocs.io/en/latest/api/grizli.prep.get_radec_catalog.html | Unable to access this page (404) |
| https://archive.stsci.edu/hlsp/hsc | Unable to access this page (404); the HSC page above was used instead |
| https://archive.stsci.edu/missions/hlsp/relics/rxc0911+17/ | Unable to access this page (404) |
| https://arxiv.org/pdf/1903.02002 | PDF not readable through the fetch tool; ar5iv version used |
