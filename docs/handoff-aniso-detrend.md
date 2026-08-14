# Handoff: real-frame test of the anisotropic conditioning detrend

**Branch:** `claude/nircam-bkg-amp-row-oversubtraction-4t12t6`
**Date:** 2026-08-13
**Status:** synthetic-validated on the `amprow_halo` harness (standard +
giant-BCG stress scenes), judged by eye (HA) — "genuinely pretty good",
96×32 preferred, marginally better with `reject = false`. Needs real-frame
validation before any field flips.
**Audience:** a session on a machine with `$CAMPFIRE_ROOT`, the `campfire`
conda env, and reducible NIRCam data for a `subtract_2d` field showing the
amp-blocky halo-oversubtraction artifact.

## 1. Context — how we got here (one paragraph)

Bright galaxies spanning readout amps leak halo flux into the per-amp-row
1/f estimate, which broadcasts it across the amp: oversubtracted amp-blocks
with hard edges at cols 512/1024/1536. Levers tried and **rejected**:
`bkg2d.fit_order="first"` (real frames, by eye — often worse; see
`docs/handoff-bkg2d-fit-order.md`), global 1/f-mask growth
(`striping.extra_dilate` alone — starves anchors frame-wide, injects
row/column noise), selective growth at 80 px (no-op — halos are broader
than the push), selective at 300 px (trades blocks for fine noise lines).
The surviving candidate exploits geometry instead of anchor placement:
banding is fine in y and constant in x within an amp; halos are smooth in
both. A **y-coarse × x-fine conditioning detrend** is banding-blind by
construction (a 96-row box averages a ρ≈20 pattern to ~4%) yet follows halo
column profiles, and, being fit full-width and smooth in x, cannot
represent amp-*dependent* banding at any scale. On the harness it removed
the amp-row misattribution almost completely on both scenes, with no
visible banding absorption (the predicted failure mode), and survived the
BCG stress case. The rj0911 "fine detrend boxes are worse" A/B used
*square* fine boxes — fine in y too — so it never tested this.

## 2. The configuration under test

Per-field override (deep-merges over `[nircam.bkg]`):

```toml
[myfield_aniso.bkg]
    subtract_2d = true
    [myfield_aniso.bkg.detrend]
        box_size = 96          # y (row) box — SW native; use 192 on LW (§5.1)
        box_size_x = 32        # x box; > 0 activates the anisotropic mesh
        filter_size = [1, 5]   # [y, x]: keep full y mesh resolution
    [myfield_aniso.bkg.bkg2d]
        reject = false         # optional; marginally better on synthetic (HA)
```

Arms for a real-frame A/B (WCS-clone-arm pattern from
`pipeline/experiments/oneoverf_gp/README.md` — clone the field in
`fields.toml`, symlink `astrom_cats`, separate products trees):

1. **control** — `subtract_2d = true` only (current production behavior).
2. **aniso** — the block above with the default `reject = true`.
3. **aniso_norej** — the block above as written (`reject = false`).

The per-exposure skip is `CFP_BKG`-presence-based: already-processed trees
need `--overwrite` on the bkg step or `cfpipe nircam reset --from image2`.
`CFP_BKG` records `detrend=box96x32` (and `bkg2d_reject`), so arms are
auditable in headers.

```bash
conda run -n campfire cfpipe nircam run --field myfield_aniso --all --processes 4
```

## 3. What to look at (the metric is the eye — PNGs, not statistics)

1. **The artifact itself**: cal-frame and mosaic cutouts around bright
   multi-amp galaxies, same stretch across arms. Success = the amp-blocky
   oversubtraction and its hard vertical/horizontal edges are gone or
   clearly reduced; halos look round instead of clipped.
2. **The predicted failure mode — banding absorption**: blank regions,
   deep stretch, arm vs control. If the conditioning is eating common-mode
   row structure (~50–150-row band), `h` underfits it and stripes at those
   scales SURVIVE into the corrected frame. This did not appear on the
   synthetic scenes; real scattered light lives at exactly these scales, so
   this is the check that matters most. (Wisps are removed before `bkg`,
   but imperfect wisp residuals also live here.)
3. **Component attribution** (optional): `bkg_step(..., components_out=)` —
   the amp-blocky component should vanish from `h` rather than migrate
   into it; `pipeline/experiments/bkg2d_synthetic/inspect_components.py`
   is the existing harness.
4. **Mosaic level**: drizzle an affected filter and compare the artifact
   region plus overlap noise (skymatch invariant: per-exposure masked
   background median stays ≈ 0; `meta.background.level` sensible).

Note the smooth halo-shaped residual around very bright galaxies is the
`b2d` fit's own error (identical in the harness's `ideal_1f` floor — wing
riding + mask-hole interpolation) and is NOT the target of this change;
don't count it against the aniso arms. `reject=false` recovers a modest
part of it; the rest is a separate `bkg2d`-side workstream.

## 4. Also riding on this branch (present in ALL arms including control)

- **GP `amplitude_data` restoration**: the amp-row GP kernel amplitude is
  measured on the pre-detrend residual again (per `gp_amprow_offsets`'s
  own rj0911 calibration contract; the unified step had regressed). This
  changes pixels on every field. Control-vs-pre-branch products isolates
  it if a blank-field spot-check is wanted.
- Dormant opt-in knobs, all default-off, no behavior change unless set:
  `bkg2d.fit_order` (keep `"last"`), `striping.extra_dilate` /
  `.extra_dilate_min_area`, `striping.gp.min_row_pixels`.

## 5. Open items to resolve during/after validation

1. **LW scaling**: ρ is a readout property in native ROWS; `box_size`
   channel-scales as an angular length (×0.5 at LW). For LW arms set
   `box_size = 192` so the scaled y-box stays ~96 rows. If the lever
   validates, consider making the detrend y-box scale-exempt (a code
   change) rather than documenting the ×2 rule.
2. **`reject` default for `subtract_2d` fields**: synthetic says
   `false` is marginally better with aniso conditioning, but `false` also
   drops the leaked-compact-source guard. Decide on real frames.
3. **Default flip**: if validated, decide whether `box_size_x = 32` (with
   `box_size = 96`, `filter_size = [1, 5]`) becomes the `[nircam.bkg.detrend]`
   default (Algorithm/MINOR, changelog entry already covers the knob) or
   stays per-field. Note the current defaults were tuned for blank fields
   where the artifact doesn't bite; a per-field flip on `subtract_2d`
   fields is the low-risk first step.
4. Flux conservation: the aniso detrend is fit-only, so it cannot move
   flux directly, but it changes what `h` fits, so per-source photometry
   arm-vs-control on real frames is a cheap sanity check (the
   `bkg2d_synthetic` sweep with a `detrend` arm is the truth-based
   version if wanted).

## 6. Tooling

- Synthetic harness: `pipeline/experiments/amprow_halo/` (README there;
  `run_harness.py --giant` for the BCG stress scene; arms are a dict at
  the top of the script). The `compare_hrow_err.png` ledger-error view —
  fitted amp-row profile minus injected truth banding — is the artifact
  isolator; on real data there is no truth, so the eye criteria in §3
  replace it.
- All work is on the branch; tests green (`tests/test_nircam_bkg.py`,
  `test_nircam_gp_striping.py`). The PR, when opened, already has its
  `## Unreleased` changelog entry (Algorithm) covering the whole series.

## 7. Definition of done

- Three-arm real-frame A/B on at least one affected field/filter (SW), one
  LW spot-check with the ×2 y-box; eye verdict recorded with cutout PNGs.
- §5.1–5.3 decisions made; production field config (or default) updated
  accordingly; follow-up issues opened for whatever is deferred (e.g. the
  `bkg2d` smooth-residual workstream).
