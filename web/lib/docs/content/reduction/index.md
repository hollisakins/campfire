# Data Reduction Overview

The CAMPFIRE pipeline wraps the standard STScI [JWST calibration pipeline](https://jwst-pipeline.readthedocs.io/) with custom processing steps to better handle backgrounds, detector noise, and spectral extraction. The pipeline is fully automated, but all products undergo [visual inspection](/docs/inspection) — automated fitting provides a starting point, and human vetting of individual exposures and redshift solutions is essential for building a reliable catalog.

## Pipeline Architecture

The pipeline is organized into sequential stages for each instrument:

| Stage | NIRSpec | NIRCam |
|-------|---------|--------|
| **Stage 1** | Detector processing + background subtraction | Detector processing + artifact removal |
| **Stage 2** | WCS assignment (2a), stuck shutter masking, nodded background subtraction (2b) | Flat fielding + sky subtraction |
| **Stage 3** | Spectral extraction + 1D combination | Alignment + mosaicking |
| **Redshift Fitting** | Template-based chi-squared fitting | — |

## Heritage

The CAMPFIRE pipeline builds on the work of several teams:

- **NIRSpec** — Largely based on the CAPERS pipeline developed by A. Taylor and P. Arrabal-Haro
- **NIRCam** — Largely based on the CEERS and COSMOS-Web pipelines developed by M. Bagley and M. Franco, respectively

## Instrument Pipelines

- [NIRSpec Pipeline](/docs/reduction/nirspec) — Multi-object spectroscopy: detector processing through 1D extraction and redshift fitting
- NIRCam Pipeline — Imaging: detector processing through mosaic assembly *(documentation coming soon)*
