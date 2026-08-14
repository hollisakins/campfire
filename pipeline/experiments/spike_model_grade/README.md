# spike_model_grade — mask-grade downsample validation (M2 → G0)

How far can the spike footprint models (WebbPSF PSF+scatlight, see
`docs/design-nircam-spike-masking.md` §3.4, §7 OQ6) be block-downsampled
before threshold isophotes move by more than the mask tolerance
(`grow` = 2 detector px)?

## Method

`downsample_drift.py` — for each (model, factor, isophote level):
block-mean downsample (mean preserves the surface-brightness unit, so one
absolute threshold applies at every grade), nearest-upsample back, and
measure the Hausdorff-style drift between the full-res and round-tripped
binary masks via EDT, both directions:

- **miss** — footprint area the coarse grade fails to cover (the dangerous
  direction: under-masking)
- **over** — extra area the coarse grade adds (costs gated depth only)

Reported as max and p99.9 (the max is dominated by isolated islands at the
model noise floor) in original detector px.

Isophote levels span 1e-11–1e-6 in model units (flux fraction per raw
detector px): `level = threshold_sigma × σ_bkg / F_star`, so lower levels
= brighter stars / deeper data. Companion note: the arm envelope hits the
model's own 240″ FOV edge below ~1e-8, so levels below that mostly probe
the outer halo boundary, and the brightest stars need the capsule fallback
regardless of grade.

## Run

```bash
conda run -n campfire python downsample_drift.py \
    --model-dir /path/to/nircam_psf_scatlight_models --agg max --out ./out
```

Results: `out/drift_results_{mean,max}.json` (0.9 µm + 2.0 µm SW, 4.4 µm LW;
factors 2/4/8/16; levels 1e-11–1e-6).

## Results (2026-07-28)

**Mean-pooling fails at every factor.** Narrow spike ridges (a few raw px
wide above threshold) are diluted below the isophote level by block
averaging, so whole arm segments vanish from the coarse-grade mask: worst
miss p99.9 over the plausible level range is 140–190 px already at f=2,
rising to 400–1600 px at f=4–16. Not a metric artifact — the missing
pixels are contiguous arm segments, exactly what the mask exists to cover.

**Max-pooling never under-masks, by construction and by measurement.**
The coarse cell records "some sub-pixel exceeds L", so the round-trip mask
is a strict superset of the full-res mask at every threshold: measured
miss = 0.00 px at every (model, factor, level). Overshoot is one-sided and
bounded by the block diagonal f·√2: worst over max = 1.41 / 4.24 / 9.90 /
21.21 raw px at f = 2 / 4 / 8 / 16 (p99.9 within ~10% of max).

**Accepted: block-max, f=4 in both channels** — a 4-native-px mask-grade
cell (0.124″ SW / 0.252″ LW), so overshoot is ~4 px in each channel's own
exposure pixels, the unit `grow` uses (safe direction only; the pipeline's
`grow` dilation rides on top). Mask-grade set total ≈ 174 MB vs 2.7 GB
photometric. These are the defaults baked into
`scripts/build_spike_models.py`. (×8 was initially considered for SW —
uniform *angular* cell instead of uniform factor — and dropped: masks are
rasterized and grown in native exposure px, so native-px fidelity is the
right invariant, and 0.25″ SW cells were judged too coarse.)

**Continuity** (`mask_continuity.py`): the full-res isophote is
intrinsically *beaded* — SB oscillates through a fixed threshold along an
arm (218 components, 263 px, in the 0.9 µm validation window at 1e-8).
Block-max bridges sub-cell gaps: ×4 → 33 components. Apparent "holes" in
the coarse mask are inherited beading, already reduced, and closed further
by `grow` + polygon closing at consumption. Consumers must smooth at the
polygon stage (simplify + outward buffer), never by smoothing the pixel
grid — any linear kernel (Gaussian included) is a weighted mean and
re-introduces the under-masking failure.

Companion finding for §7 OQ6 provenance: the raw set is **pre-flight**
WebbPSF 1.0.0 (Feb 2022, requirements OPD RevW), computed at NRCA1/NRCA5
field points — not a post-commissioning ePSF. Fine for packaging;
raises the stakes on G0 validation against real in-flight spikes.
