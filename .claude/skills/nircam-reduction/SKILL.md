---
name: nircam-reduction
description: Reduce a NIRCam imaging field with cfpipe, from raw data to published mosaics. Use when setting up a new field (fields.toml, tiles, reference catalog), running or re-running process / align / combine, inspecting and masking exposures, debugging a NIRCam reduction, or publishing a field to CAMPFIRE.
argument-hint: "[field name]"
---

# NIRCam reduction cookbook

Field: **$ARGUMENTS**

A NIRCam reduction is three steps. Each is one command; the decisions are in
setting up the field, not in running it.

1. **Process** every exposure through the per-exposure steps (detector1, image2,
   the CAMPFIRE background, wisp and striping corrections, and finally alignment to
   the reference catalog):
   ```bash
   cfpipe nircam process --field F --filters f444w -p 12
   ```
   An admin can then upload the exposures for inspection:
   `campfire deploy --field F --no-mosaics`.
2. **Inspect** the exposures, as much as you want, to catch artifacts (wisps,
   claws, scattered light) and either mask them or exclude a corrupted exposure.
   Optional, but worth doing at least for the long-wavelength filters, so that
   detection doesn't pick up artifacts. On the portal for deployed fields, in DS9
   otherwise. See `inspection.md`.
3. **Combine** into mosaics (outlier detection, drizzle, mosaic background
   subtraction):
   ```bash
   campfire config pull --masks --field F           # admins: bring the portal's masks/exclusions down
   cfpipe nircam combine --field F --filters f444w -p 4
   ```

`cfpipe nircam run --field F --all` does process then combine in one call; `run`
also takes `--process`, `--align` and `--combine` separately.

## Decisions for a new field

Make these before the first `process`. Details in the files listed.

- **The field entry** in `$CAMPFIRE_ROOT/config/fields.toml`: which programs and
  observations, which filters, the tangent point. `setup.md`.
- **The reference catalog for alignment.** The usual recipe has two hops. First,
  align one long-wavelength filter (F444W) to an external catalog (Legacy Surveys
  DR10 works well; Gaia where stars move). Then build a catalog from that mosaic
  and align every filter to it. For a field that already has a deep catalog (for
  example GOODS-N with UNICORN), align straight to that. `alignment.md`.
- **The output tile grid.** `combine` needs at least one tile defined in
  `fields.toml`. `size_tile.py` in this skill sizes a tile to cover the field's
  exposures. `setup.md`.

## Before running anything

- Read `pipeline/AGENTS.md`, and check `$CAMPFIRE_ROOT` points where you intend.
  Experiments go in a scratch root, never the production data tree.
- `cfpipe nircam status --field F` shows which steps each exposure has completed;
  `cfpipe nircam check --field F` shows which mosaic tiles are stale.
- Steps record completion as `CFP_*` header keys and are skipped on re-runs. To
  redo a step after changing its parameters, pass `--overwrite`, or use
  `cfpipe nircam reset --field F --from <step>`. Steps that change the science
  pixels (jackknife, wisp, image2, bkg) can't be reset in place; use
  `reset --uncal` and re-process.
- The process phase uses several GB per worker; size `-p` to the machine.

## Files in this skill

| File | Read it when |
|---|---|
| `setup.md` | Adding a field: download, `fields.toml`, tiles, non-default settings |
| `alignment.md` | Choosing or building the reference catalog; align failures |
| `inspection.md` | Masking and excluding exposures, on the portal or in DS9 |
| `publish.md` | Putting mosaics, exposure maps and the map on CAMPFIRE |
| `size_tile.py` | Computing a tile's `crpix` / `naxis` from the raw footprints |

`pipeline/campfire_pipeline/data/config_default.toml` documents every step
parameter at length; read the relevant block before changing one.
