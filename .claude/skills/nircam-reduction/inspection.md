# Inspecting exposures

The point of inspection is to keep artifacts out of the mosaics: wisps and claws,
scattered-light arcs, and the occasional fully corrupted exposure. It's optional.
A sensible minimum is the long-wavelength filters, since detection catalogs are
usually built on them. Masks never change the canonical exposure's pixels. They're
recorded beside it and applied on combine's disposable working copies, so editing
or removing a mask is always reversible.

Two outcomes per exposure:
- **Mask** a region: the pixels inside are left out of outlier detection and the
  mosaic.
- **Exclude** the exposure: it's left out of combine entirely.

## On the portal (CAMPFIRE admins)

1. **Upload the processed exposures** (no mosaics yet; they'd be superseded once
   masks exist):
   ```bash
   campfire deploy --field F --no-mosaics [--filters f444w]
   ```
2. **Review** at `/admin/nircam`. The exposure page renders the exposure, lets you
   draw mask polygons, and mark the exposure approved or excluded. Approval is
   bookkeeping; only masks and exclusions change the mosaic.
3. **Pull the decisions down** and combine:
   ```bash
   campfire config pull --masks --field F
   cfpipe nircam combine --field F --filters f444w -p 4
   ```
   The pull writes masks to `reference/nircam/F/masks/<filter>/<rootname>.reg` and
   the triage status to `reference/nircam/F/exposures.json` (`excluded`, which
   combine drops, and `approved`; anything unlisted is pending). Combine re-applies
   the mask of any exposure whose `.reg` changed. (`campfire pull im --field F`
   also pulls them, but downloads products too.)

Masks and status sync both ways, per exposure, like the NIRSpec stuck-shutter
lists (`campfire config diff --masks --field F` shows where they differ):
- a portal edit to a mask or status you haven't touched locally is taken by the
  pull; one you also changed locally is a conflict, kept local and reported
  (`--theirs` takes the portal's);
- a mask drawn or edited in DS9, or an exposure added to `exposures.json`, goes up
  with `campfire config push --masks --field F` (and with `campfire deploy --field F`);
- removals travel only on a yes. To drop a mask or un-exclude an exposure
  everywhere, delete it locally, then `config push --masks` (it asks; `--prune`
  answers yes). A pull before that push puts it back. A removal made in the portal
  is asked about on the next `config pull --masks`.
- The portal stores image-coordinate polygons only: a `.reg` holding a circle or
  sky-coordinate region is reported and left out of the sync (combine still
  applies it).

## In DS9 (without CAMPFIRE admin access)

Draw regions on the canonical exposure
(`products/nircam/F/<filter>/<rootname>.fits`) and save them as
`reference/nircam/F/masks/<filter>/<rootname>.reg`. The step rasterizes each
region with the exposure's WCS, so image or sky coordinates both work. To drop an
exposure entirely, add its rootname to the field's `skip` list in `fields.toml`.
Then run combine as above.

## What to look at

Each exposure's directory holds per-step diagnostics next to the FITS:
`<rootname>_bkg.pdf` (background and striping model), `_wisp.pdf` where a wisp
template was fit, `_diag_striping.pdf` where that correction ran, and
`_outlier.pdf` after combine. Each mosaic has a `_quicklook.png` and a
`_bkgsub.png` (the mosaic background model), and `cfpipe nircam expmap` writes
per-filter exposure maps. Look at the images, not only the summary numbers:
residual structure that the statistics average away is what the masks are for.
