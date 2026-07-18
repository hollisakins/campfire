# bkg2d synthetic validation

Synthetic-scene harness for the opt-in per-exposure applied 2-D background
(`[nircam.bkg].subtract_2d`, PR that added the Ryan-Endsley-style fine-box fit
+ background-map outlier rejection). Runs the **real** `bkg_step` on jwst
ImageModels built from layered truth scenes and measures oversubtraction
through the correction-error map.

## Design

**Layered truth.** `image = sky + galaxies + icl + noise`, with `galaxies`
and `icl` as separate planes. The galaxy/ICL split is *by construction*
(compact + moderate Sersic components vs the extended envelopes), so galaxy
flux conservation and ICL removal are independent integrals of the same error
map — no BCG/ICL decomposition is ever fit.

**The metric.** `E = correction − sky_truth` is what the step removed beyond
the true background. Because removing ICL is *accepted*, a galaxy sitting on
the envelope must not be charged for the ICL removed under its aperture. The
galaxy-attributable map does that separation per pixel:

    E_gal = Ef − clip(Ef, 0, icl_truth)      (Ef = E − quiet-sky floor)

Removal within the local ICL truth band scores zero; only removal beyond
sky+ICL (galaxy flux absorbed, positive) or below sky (undersubtraction,
negative) counts. Per source, aperture-to-aperture:

- truth = that source's own rendered stamp summed inside the aperture
  (r_ap = max(3 px, 2.5 r_e)) — never an analytic total;
- loss = Σ E_gal over the *same* aperture (the quiet-sky floor removes the
  global DC — the pedestal legitimately absorbs the undetected-source mean);
- `frac = loss / truth` is the metric of record. `frac_raw` (raw Ef, charges
  ICL removal to the galaxy) and `frac_local` (local-annulus reference, where
  a bowl partially cancels — the EGS Moran's-I confound) are recorded for
  reference only.

**ICL absorption** = Σ E over the ICL-dominant region ÷ Σ icl → 1.0 means
fully removed, which is *success* for `subtract_2d`.

**Faint-source scatter floor.** E contains the Background2D mesh's response
to noise, so per-source `frac` for faint sources scatters at the
(mesh wiggle × aperture area) / truth level — random, not systematic. Judge
faint bins by their *median*; the systematic story lives in the bright tail
and the bowl profiles. Per-source rows are only recorded above 30 sigma total.

**Thresholds.** Per-exposure oversubtraction is coherent across dithers (it
does NOT average down in the mosaic), so the compact-galaxy bar is the final
photometry requirement: |frac| < 0.05% (dotted lines in the per-cell PNGs).
Extended galaxies trade against sky flatness — the sweep maps that surface.
ICL removal near 1.0 and quiet-sky rms ≪ the input gradient are the "it
works" criteria.

## Usage

```bash
cd pipeline/experiments/bkg2d_synthetic
conda run -n campfire python run_sweep.py --out out --quick   # smoke, ~5 min
conda run -n campfire python run_sweep.py --out out           # default sweep
conda run -n campfire python run_sweep.py --out out \
    --preset both --channels sw,lw --seeds 3 --reject both    # full grid
```

Arms = `off` baseline + box_size × extra_dilate × reject. `--estimator none`
(default) isolates the 2-D stage from the GP 1/f; `--estimator gp` runs the
full chain. Outputs: `summary.md`, `results_cells.csv`,
`results_sources.csv`, one PNG per cell (E map, bowl profiles of the 5
brightest galaxies, per-source loss vs r_e).

## Scene population (defaults; scene.py)

- ~500 galaxies / SW frame: power-law fluxes (α=1.8, 10–10⁵ σ, sub-threshold
  tail included), log-normal sizes with a size–flux correlation (median
  ~4 px SW, extended tail rare), 70/30 disk/spheroid n mix, brightest biased
  n=3–4.5, Gaussian PSF FWHM 2 px. Truth flux = rendered clipped stamp sum.
- `cluster` preset adds ~20 radially-concentrated bright members, a
  two-component BCG (inner n=4 → galaxy plane; outer envelope → ICL plane)
  and a large ICL envelope (peak ~0.6 σ_pix ≈ 3% of sky).
- Sky = level + linear gradient (0.2 sky units ≈ 4 σ_pix across the frame).
- `lw` halves all angular sizes in px; PSF stays ~2 native px.
- Diffraction spikes deliberately deferred (separable failure mode).
