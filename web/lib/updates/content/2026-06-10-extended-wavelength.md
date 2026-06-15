---
title: "Extended-wavelength NIRSpec reductions (G140M/G235M out to 5.3 µm)"
date: 2026-06-10
category: pipeline
summary: "Opt-in re-reductions extend G140M/F100LP and G235M/F170LP coverage redward of the nominal grating cutoffs, recovering 1st-order light out to ~5.3 µm."
links:
  - { label: "NIRSpec reduction docs", href: "/docs/reduction/nirspec" }
---

The F100LP and F170LP long-pass filters pass light well redward of the nominal
G140M and G235M grating cutoffs. The pipeline can now optionally extract that
redder 1st-order light, extending coverage out to roughly **5.3 µm** for these
grating/filter combinations.

This ships a SPURS-derived calibrated `photom` reference and generates the
extended flat-field and wavelength references on the fly. Stage 1's
background-subtraction mask auto-widens for these gratings so the extended-order
flux is preserved rather than subtracted as background.

The feature is **opt-in** (`[nirspec.stage2].extend_g140m_g235m`, default off) and
only affects G140M/F100LP and G235M/F170LP; all other gratings are unchanged.
See the [reduction docs](/docs/reduction/nirspec) for details and caveats.
