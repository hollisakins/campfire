# amprow_halo — eye-first harness for the amp-row halo-oversubtraction artifact

Synthetic testbed for the row-wise artifacts around bright multi-amp galaxies
on `subtract_2d` fields (issue context: bright-galaxy halos leak into the
per-amp-row 1/f estimate and get broadcast across the amp — oversubtracted
amp-blocks with hard edges at cols 512/1024/1536 and at the source's
top/bottom rows).

**The metric is the eye.** The harness renders PNGs and nothing else — no
summary tables, no scores. A mitigation succeeds when the artifact visibly
shrinks in `compare_error.png` (and the corrected frames stay clean in
`compare_after.png`) *as judged by a human looking at the images*. Derived
statistics have already misled once here (the fit_order='first' reorder
looked good numerically on synthetics, was rejected on real frames by eye) —
do not reintroduce them as the decision criterion.

## Scene

`scene.py` (shared with `../bkg2d_synthetic`), preset `brightfield`:

- a few very bright extended ellipticals (n≈3–4.5 bodies → galaxy plane),
  the first few pinned near amp boundaries so they span amps, each with a
  broad n=1 halo envelope (→ ICL plane — an *accepted loss* under
  `subtract_2d`; the artifact under study is its row-wise misattribution,
  not its removal);
- ~450 fainter field galaxies (power-law fluxes, sub-threshold tail);
- sky = level + linear gradient + a smooth *complex* component
  (`sky_patch_amp`, a Gaussian random field at ~400 px) — what
  `subtract_2d` exists to remove;
- injected detector systematics (`inject_1f`): per-amp DC offsets, two-scale
  per-amp row banding, column stripes.

The real `bkg_step` runs on a jwst ImageModel of the scene with
`subtract_2d = true` and the shipped defaults (matching the production
user-config), so mask building, channel scaling, iteration, remask and
provenance are all exercised.

## Arms

Config-override dicts in `run_harness.py::ARMS` — edit to add levers:

| arm | override |
|---|---|
| `baseline` | shipped defaults (artifact reproduction) |
| `strp_d40/80/150` | `[nircam.bkg.striping].extra_dilate` = 40/80/150 — grow the source tiers for the **1/f fit mask only**; the amp-row anchors move off the halos and the GP bridges the widened gaps |
| `ideal_1f` | truth arm: injected stripes subtracted exactly, `estimator='none'` — the floor any 1/f estimator could reach; the eye's reference |

## Outputs (per run, under `--out`)

- `input.png`, `target.png` — the scene and the goal (galaxies + noise).
- `{arm}_after.png` — corrected frame, real-image asinh stretch.
- `{arm}_error.png` — **the diagnostic**: `after − target` where target has
  sky + stripes + halo plane removed; diverging map, sources whited out.
  Amp-blocky red/blue structure with hard edges at 512/1024/1536 = the
  artifact.
- `{arm}_hledger.png` — the accumulated amp-row term; where row artifacts
  live before they hit the frame.
- `compare_after.png`, `compare_error.png` — all arms side by side on
  identical stretches.

## Usage

```bash
# campfire conda env, from this directory
python run_harness.py --out out                 # full 2048², ~1–2 min/arm
python run_harness.py --out out --quick         # 1024-row smoke
python run_harness.py --out out --arms baseline,strp_d80,ideal_1f
python run_harness.py --out out --seed 3 --halo-peak 3 --sky-patch-amp 0.15
```

## History

- 2026-08-13: harness created after the `fit_order='first'` reorder was
  rejected on real frames (sometimes better, often worse, by eye — with
  `reject=false`). First lever under test: `striping.extra_dilate`.
