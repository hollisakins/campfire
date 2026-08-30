# Real-frame validation: anisotropic conditioning detrend on A2744

**Branch:** `claude/nircam-bkg-amp-row-oversubtraction-4t12t6`
**Date:** 2026-08-14
**Verdict:** the anisotropic conditioning detrend with `reject = false` was
preferred over production by eye (HA). The `fit_order` reorder was rejected on
the same data. Neither default is flipped by this PR.

Companion to `docs/handoff-aniso-detrend.md` (the plan) — this is what the
plan produced. Everything below is measured on real frames unless labelled
synthetic.

## 1. Test setup

Six A2744 exposure roots rebuilt **from uncal** into an isolated
`CAMPFIRE_ROOT` clone (`abtest/a2744_fitorder`) with a private `fields.toml`,
so production products were never touched. Steps `detector1 → persistence →
wisp → image2 → edge` only; `bkg` was then run once per arm on a copy, via
`bkg_step(..., components_out=...)`.

- 32 canonical frames: 24 SW (F200W) + 8 LW (F444W).
- Selection: ranked by the artifact's actual precondition — a bright,
  extended, largely unmasked source — from a full scan of 540 F444W LW and
  1591 F200W SW production frames. One root
  (`jw06434438001_04201_00001`) contributes both channels.
- Arms (32 × 3 = 96 cells, all completed):
  | arm | detrend | `bkg2d.reject` |
  |---|---|---|
  | `control` | square `box_size = 256`, `filter_size = 3` | false |
  | `aniso` | `box_size = 96` (y) × `box_size_x = 32`, `filter_size = [1,5]` | true |
  | `aniso_norej` | same detrend | false |

A2744 production already sets `reject = false` globally, so **`aniso_norej`
is the one-variable change** against control; `aniso` moves two things.

**LW scaling** (handoff §5.1) applied: LW arms use `box_size = 192` so the
×0.5 channel scaling leaves ~96 rows. `box_size_x` is *not* doubled — it
tracks the halo's angular column profile. Confirmed in provenance:
`detrend=box96x32` (SW) / `box96x16` (LW), vs `box256` / `box128` for control.

## 2. The artifact exists on real frames, in a minority of exposures

Confirmed on the correction ledgers, **not** on the residual: the residual is
flat *because* the misattributed model was subtracted, so residual-based
searches find nothing. This cost a false start — two scans of production
frames (an amp-seam statistic and an SRCMASK-blob criterion) found nothing and
were both discarded as measurement artifacts.

The diagnostic that works is the amp-differential amp-row correction,
`d_a(r) = h_a(r) − median_a h(r)`, row-smoothed: genuine 1/f is common to all
four amps and cancels; a halo leak inflates only the host amp.

End-to-end on `jw02561003001_06101_00006_nrcb4`: a bright star's **unmasked**
PSF wings raise amp0's amp-differential unmasked background by +3.7/+2.1/+4.5σ
at rows ~230/~590/~1780, and `h` follows at exactly those rows and that amp
(+2.3/+1.3/+1.7σ), broadcast across amp0's full 512-column width.

5 of 32 frames showed a strong signature (peak |d_a| > 1σ), **all SW**. It is
not present in every exposure. On real A2744 the dominant driver is bright
**stars'** PSF wings and diffraction spikes rather than galaxy halos.

## 3. `fit_order = "first"` — rejected

Same 32 frames, arms `last` / `first`+`reject=false` / `first`+`reject=true`.

On the `h` ledger the reorder did reduce the leak on all five affected frames
(peak |d_a| ×0.52–0.78), and the falsification arm behaved as predicted
(`first`+`reject=true` gave ×0.86–1.19, i.e. no benefit — `reject` cancels the
fix on sky, as the synthetic scene said). But **by eye on the corrected SCI the
artifacts were often worse**, which is the verdict that decided it. The knob
stays in the code at `"last"`.

Caution for anyone re-reading those numbers: on the `h` ledger alone `first`
looked like a regression on 20/32 frames, but that was an artifact of reading
one ledger in isolation — on the *total* correction it was better-or-equal in
25/32. Neither statistic overrode the visual verdict.

## 4. Anisotropic detrend — preferred

Judged from post-bkg SCI per arm at a **common** zscale across arms (a
per-panel stretch renormalises each arm to itself and hides the difference).
`aniso_norej` was cleaner than control on the affected frames; the predicted
failure mode — ~50–150-row banding absorbed by the y-coarse mesh and left
underfit by `h` — was checked on a blank field at deep stretch (`contrast
0.08`).

## 5. Wisp interaction (why the frames were rebuilt twice)

The first pass predated PR #456: the branch was cut at `bd3d80a`, before #456
merged to main at `39a0cd3`, so the frames carried the old NMF wisp fit region
(`MASK_hSNR`). This matters here because imperfect wisp residuals live at the
same ~50–150-row scales as the banding check in §4.

After merging main, the frames were rebuilt and the sweep re-run. Verified by
`CFP_WISP` and by pixel differencing (identical uncal, identical
detector1/image2/edge, so the difference isolates #456):

- 11/12 wisp detectors now fit on `region=t50`. The change is confined to the
  wisp footprint (frame-wide MAD ≈ 0), peaking at 0.005–0.026 MJy/sr — up to
  ~1.5× sky σ on nrcb3 — and is **positive**, i.e. the new path subtracts
  *less*: the old region was over-subtracting.
- 1/12 fell back: `jw02561001004_06101_00005_nrcb4` stamps
  `region=hsnr(fallback)` and its pixels are **bit-identical** to the
  pre-merge reduction (max |new−old| = 0.0000). #456 changed nothing there.

**Known issue, tracked separately (not addressed by this PR).** That fallback
is self-defeating: the wisp step builds its source mask from the *pre*-wisp
frame with `detect_sources(nsigma=3, dilate=8)`, so a sufficiently bright wisp
is detected as a source and masks its own template core. On that frame
`t50 & ~mask` collapses (58 px in reconstruction, 0 in the run, against a
`50 × ncomp` = 150 threshold), and the solve falls back to exactly the
`MASK_hSNR` region #456 exists to avoid. Control frames keep 1699–1706 of 1717
t50 px. Candidate fixes: exclude `wmask` from the wisp step's own source
detection; lower the threshold; or step through looser template cuts (`t30`,
`t20`) before falling back to hSNR.

## 6. Reproducing

Scripts and figures (CANDIDE):
`$CAMPFIRE_ROOT/scripts/claude/bkg-fitorder-ab/` — `aniso_ab.py` (arms),
`render_after.py` (post-bkg SCI, common stretch), `render_ledgers.py`
(component ledgers), `render_wisp_ab.py`, `probe_t50_prewisp.py`,
`figs/w2_*.png`, session board `scripts/claude/sessions/bkg-fitorder-ab.md`.

Guard against the shared-editable-install hazard: jobs pin the worktree via
`PYTHONPATH` and run `guard.py`, which asserts the imported
`campfire_pipeline`/`campfire_layout` come from the pinned tree and that
`fit_order` exists in the loaded defaults. Verified non-vacuous (rc=1 on a
deliberately wrong pin).

**No-op check** worth repeating whenever this branch moves: `control` at
`e6faed8` was bit-identical to the pre-aniso `last` arm at `fbf4dae` on 32/32
frames, confirming the new knobs are genuinely dormant at their defaults.
