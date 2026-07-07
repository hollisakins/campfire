---
title: "Extended-wavelength NIRSpec reductions for G140M/G235M"
date: 2026-06-10
category: pipeline
summary: "Added functionality to extend G140M/F100LP and G235M/F170LP coverage redward of the nominal grating cutoffs using SPURS as calibration."
---

The F100LP and F170LP long-pass filters pass light well redward of the nominal
G140M and G235M grating cutoffs. The pipeline can now optionally extend the 
wavelength range of the extraction for G140M/F100LP and G235M/F170LP. 

The photometric calibration beyond the nominal cutoff was derived using [SPURS](/nirspec/metadata/programs/spurs) (GO#9214) which obtained deep G140M/F100LP, G235M/F170LP, and G395M/F290LP data. A first-pass reduction was done assuming flat calibration in the extended wavelength regime, and line fluxes were measured in each grating. Only emission lines fluxes in the overlapping regions were used for flux calibration to avoid contamination from second-order spectra. The final calibration curve is derived from a smooth polynomial fit and injected into a custom `photom` reference file. See [PR#163](https://github.com/hollisakins/campfire/pull/163) for more details. 

The extension is configurable (`[nirspec.stage2].extend_g140m_g235m`) and is off by default, but has been manually enabled for several programs in the CAMPFIRE database (e.g. [SPURS](/nirspec/metadata/programs/spurs), [C3PO](/nirspec/metadata/programs/c3po)).
