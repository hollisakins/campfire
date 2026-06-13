# NIRCam 1/f noise correction around bright sources — experiments

Experimental, **non-shipping** testbed for improving the per-amp-row 1/f
("striping") correction in
`campfire_pipeline.nircam.steps.striping.fit_residual_striping`, specifically
its weak behaviour around bright/extended sources.

## The problem

The production correction estimates one additive offset per *amp-row* (4 amps ×
2048 rows) as a 2σ-clipped median of the ~508 unmasked pixels in that amp-row.
When a bright source fills most of an amp-row, the per-amp-row median is biased,
an asymmetry guard trips, and the code **falls back to the full-row median** —
the median across all four amps in that row. Because the four amps carry
*independent* 1/f offsets, that fallback is a poor proxy exactly where a good
local estimate is hardest to make.

## What's here

- `oneoverf_experiments.ipynb` — the experiment notebook (committed with
  executed outputs). Builds purely-synthetic NIRCam frames with model-matched
  1/f (constant per amp-row + constant per column), injects a controllable
  bright source, and benchmarks alternative per-amp-row estimators against the
  **canonical** production `fit_residual_striping` for recovery of the known
  intrinsic image as a function of amp-row fill fraction.
- `_build_notebook.py` — editable source of truth for the notebook. Edit here
  and regenerate; do not hand-edit the JSON:

  ```bash
  conda run -n campfire python experiments/oneoverf_noise/_build_notebook.py --run
  ```

  (`--run` executes and embeds outputs; omit it to rebuild structure only.)

## Scope (MVP, by design)

Pure-synthetic scenes, model-matched 1/f, notebook deliverable. This isolates
the one failure the production code has — robustness of the per-amp-row
estimator to source contamination — from any noise-model mismatch. The notebook
exposes knobs (`common_mode_frac`, `row_corr`) and documents the natural
follow-ups (realistic 1/f power spectrum, reference-pixel priors, NSClean-style
Fourier fits) that a later iteration would add.

Nothing here changes pipeline behaviour; the production algorithm is imported
and tested, not modified.
