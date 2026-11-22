# Alignment

Use the `align` step. It is opt-in per field (`[<field>.align].enabled = true`;
the package default is off), and when it is on, the process phase runs it in
place of the older `jhat`. Every production field uses `align`; a `[<field>.jhat]`
block left in an entry is unused once align is on.

## What align does

For each filter, each exposure group gets a pooled coarse fit (shift + rotation
across both modules), matched to the reference catalog by an offset-histogram
consensus after removing differential velocity aberration. Each detector then
gets its own fine fit, kept only when its offset is significant against the fit
noise. A detector whose final residual exceeds `max_residual_arcsec` is marked
`NOT_ALIGNED`: its WCS is left alone and **combine leaves it out** unless you pass
`--include-unaligned`. `cfpipe nircam status` shows the `CFP_ALGN` column.

Healthy solutions leave residuals of about 0.02–0.03″; misregistrations show up at
0.2″ and above.

```toml
[myfield.align]
    enabled = true
    refcat  = "<file>.ecsv"          # in reference/nircam/<field>/astrom_cats/
    max_residual_arcsec = 0.2        # default 0.1; cluster fields use 0.2
    # objmag_lim    = [19.0, 28.0]   # AB window on image detections (off by default)
    # delta_mag_lim = [-3.0, 4.0]    # image − refcat window; needs trustworthy refcat mags
```

There is one catalog per field (no per-filter mapping). Align records the
catalog's content hash on each solution, so replacing or editing the catalog makes
the next run re-solve. After retuning `[<field>.align]`, re-run just the align step
with `cfpipe nircam align --field F`.

## The two-hop recipe

External catalogs are sparse at NIRCam depth, so align one filter to the external
catalog, then use that mosaic as the reference for everything else.

1. **Absolute frame.** Query an external catalog over the field, set it as the
   refcat, then process and combine **one long-wavelength filter** (F444W, or
   F356W / F277W), which has the most matches per detector:
   ```bash
   cfpipe nircam refcat query --field F --backend ls_dr10      # or gaia, hsc_ssp
   cfpipe nircam run --field F --process --filters f444w -p 8
   cfpipe nircam run --field F --combine --filters f444w
   ```
2. **Anchor.** Extract sources from that mosaic, merge them with the external
   catalog (so sources outside the NIRCam footprint stay covered), set the result
   as the field's refcat, then re-align every filter and combine:
   ```bash
   cfpipe nircam refcat extract --field F --filter f444w --tile T --mag-range 18,26
   cfpipe nircam refcat merge --field F F_f444w_extract_refcat.ecsv F_ls_dr10_i_refcat.ecsv \
       --out F_f444w_plus_lsdr10_refcat.ecsv
   # set [F.align].refcat = "F_f444w_plus_lsdr10_refcat.ecsv", then:
   cfpipe nircam run --field F --process -p 8      # new filters: full process, incl. align
   cfpipe nircam run --field F --align -p 8        # filters already processed: re-align only
   cfpipe nircam run --field F --combine -p 4
   ```
   The anchor filter ends up re-aligned against a catalog built from its own
   mosaic. That's circular but harmless.

All `refcat` commands work in the field's `astrom_cats/`: outputs land there
(including a relative `--out`), and relative input names are looked up there
first. In `merge`, the first catalog wins duplicates (3″ match radius).
`refcat compare --field F A.ecsv B.ecsv` plots the ΔRA / ΔDec offsets between two
catalogs, which is the quickest check for a frame offset.

## Choosing the external catalog

- **Legacy Surveys DR10** is a good default at high galactic latitude. It has holes
  at low latitude (PG004 has no DR10 sources at all).
- **Gaia** for fields with stellar proper motion (low latitude, toward the bulge),
  because a catalog without proper motions can't follow the drift. Cut to
  G ≈ 17–21: brighter stars have masked cores and give ~60 mas residuals, and
  fainter ones are dominated by mismatches. Hop 2 doesn't need proper motions when
  every filter comes from the same visit.
- **HSC SSP** (`hsc_ssp`) needs an account (`HSC_SSP_USER` / `HSC_SSP_PASSWORD`, or
  `~/.netrc`).
- **An existing deep catalog** of the field can be the refcat directly, skipping hop 1.

## Catalog format (`campfire-refcat-v1`)

An ECSV with columns `RA`, `DEC` (degrees, ICRS), `mag` (AB, any band) and
`mag_err`. Any table with those four columns works, so an external catalog can be
converted by hand. Optional proper-motion columns (`ref_epoch` as a Julian year,
`pmra` including cos δ, `pmdec`, in mas/yr) make align move each position to the
exposure's mid-time; rows with NaN motions are treated as stationary, so a mixed
star and galaxy catalog works.

## When alignment goes wrong

- **Exposures marked NOT_ALIGNED**: check how many catalog sources fall on those
  detectors. An external catalog too sparse for the detector is a common cause,
  and hop 2 (a catalog built from your own mosaic) addresses it. Loosen
  `max_residual_arcsec` only when residuals sit uniformly just above the
  threshold, not when a few detectors are far off.
- **A whole visit far off**: a sparse catalog against a dense image can lock onto a
  wrong offset. The `gross_min_keep_frac` check guards against this; don't disable
  it. If a pointing is too far off to recover, add a `[[<field>.wcs_shift]]` rule.
- **After changing the refcat**, align re-solves on its own. Mosaic manifests
  record each input's WCS hash, so `cfpipe nircam check --field F` lists the tiles
  the moved exposures made stale and `combine` rebuilds them.
