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
   campfire deploy nircam pull --field F
   cfpipe nircam combine --field F --filters f444w -p 4 --overwrite
   ```
   The pull writes masks to `reference/nircam/F/masks/<filter>/<rootname>.reg` and
   exclusions to `reference/nircam/F/exposures.json`, and prints how the local
   masks differ from the portal's. (`campfire pull im --field F` also refreshes
   them, but downloads products too.) **Don't delete masks on the report's
   "STALE … remove it" advice** for a field with more than 5,000 exposures: the
   report reads a capped query there and flags live masks (#478).

**Use `--overwrite` on a combine after masks change.** The first combine step
records that an exposure's masks were applied and skips it on later runs, so an
edited mask isn't picked up otherwise. Combine's other work is redone from
disposable copies, so `--overwrite` costs time but nothing else.

The pull only adds, it never overwrites or removes:
- a `.reg` that already exists locally is kept even if the mask was edited in the
  portal since (the pull reports the difference). To take the portal's version,
  delete the local `.reg` and pull again;
- to un-exclude an exposure, also delete its rootname from `exposures.json`;
- a local `.reg` with no portal mask is left alone.

(Issue #709 tracks moving masks onto `campfire config pull/push`, which would make
edits and removals travel both ways.)

## In DS9 (without CAMPFIRE admin access)

Draw regions on the canonical exposure
(`products/nircam/F/<filter>/<rootname>.fits`) and save them as
`reference/nircam/F/masks/<filter>/<rootname>.reg`. The step rasterizes each
region with the exposure's WCS, so image or sky coordinates both work. To drop an
exposure entirely, add its rootname to the field's `skip` list in `fields.toml`.
Then run combine with `--overwrite` as above.

## What to look at

Each exposure's directory holds per-step diagnostics next to the FITS:
`<rootname>_bkg.pdf` (background and striping model), `_wisp.pdf` where a wisp
template was fit, `_diag_striping.pdf` where that correction ran, and
`_outlier.pdf` after combine. Each mosaic has a `_quicklook.png` and a
`_bkgsub.png` (the mosaic background model), and `cfpipe nircam expmap` writes
per-filter exposure maps. Look at the images, not only the summary numbers:
residual structure that the statistics average away is what the masks are for.
