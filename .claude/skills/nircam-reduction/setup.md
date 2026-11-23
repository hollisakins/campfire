# Setting up a field

## Raw data

```bash
cfpipe download --program 1837 --instrument nircam --obs-id 1 --obs-id 2 --filters f444w
```

Files land in `$CAMPFIRE_ROOT/raw/nircam/<PID>/<filter>/*_uncal.fits`. Proprietary
data needs a MAST token (`--token` or `$MAST_API_TOKEN`). `--dry-run` lists what
would be fetched.

With a CAMPFIRE account, `campfire config pull --fields` writes the database's
field definitions into your `fields.toml`. Use the production fields as worked
examples.

## The `fields.toml` entry

```toml
[myfield]
    filters = ['f115w', 'f150w', 'f277w', 'f444w']
    files = [
        'jw01837{001,002,008}',   # PID + observation numbers; brace lists expand
        'jw06434',                # every entry starts with jw + 5-digit PID
    ]
    skip = ['jw01837002006_02201_00002']   # dropped from every step
    tangent_point = [34.3689275, -5.200961666667]
    fiducial_tiles = ['main']     # tiles that form the web map composite

    [myfield.main]                # a tile: see "Tiles" below
        rotation = 0
        [myfield.main.30mas]
            crpix = [20899.5, 15999.5]
            naxis = [41800, 32000]

    [myfield.align]               # a per-field step override
        enabled = true
        refcat  = "myfield_f444w_plus_lsdr10_refcat.ecsv"
```

- **`files`** are prefixes of uncal rootnames (`jwPPPPPOOOVVV_...`); the pipeline
  appends `*_uncal.fits`, so `'jw06434'` selects the whole program. The PID is how
  the raw directory is found, so a pattern without one is rejected. Select by
  observation number to keep out parallels or other pointings of the same program.
- **`filters`** is the default list; `--filters` narrows a run.
- **Per-field step overrides**: a sub-table named after a step is merged over
  `config.toml` for this field only (package default → `config.toml` →
  `[<field>.<step>]`). Step names are reserved, so never name a tile after a step
  (`detector1`, `wisp`, `bkg`, `align`, `outlier`, `resample`, …), and the keys
  `filters files skip tangent_point rgb epochs fiducial_tiles` are reserved too.
- **`[[<field>.wcs_shift]]`** (array of tables: `delta_ra`, `delta_dec` in degrees,
  optional `delta_roll`, `scale`) shifts exposures whose pointing is too far off
  for align to recover. It runs before align.
- **`[<field>.epochs.<name>]`** (`files = [...]` and/or
  `date_range = ["YYYY-MM-DD", "YYYY-MM-DD"]`) defines a subset for a separate
  mosaic: `combine --epoch <name>`, which appends the epoch to the mosaic name.
  Only combine uses epochs.
- **`[<field>.diag_striping]`** corrects diagonal scattered-light stripes from an
  off-axis star. Opt-in, and the angle window must be tuned per field. Keep the
  window narrow so frames without the feature are still skipped, and judge it by
  the residual images, not the rms.
- **`[<field>.rgb]`** is read only by `cfpipe nircam rgb`.

## Tiles

A tile is the output WCS of a mosaic: a named sub-table with a TAN projection at
the field's tangent point (or its own `tangent_point`), a `rotation` (position
angle of the output +y axis, east of north) and, at one pixel scale, `crpix`
(FITS 1-indexed) and `naxis` (`[nx, ny]`). Other pixel scales are derived.
Exposures are assigned to tiles by footprint overlap.

**Sizing a single tile**: run the helper, which reads every selected uncal's
`S_REGION` footprint and prints the block to paste:

```bash
python .claude/skills/nircam-reduction/size_tile.py --field myfield --tile main --margin 6
python .claude/skills/nircam-reduction/size_tile.py --field myfield --tile main --rotation 20
```

Size over all the field's filters (the default): different filters often come
from different visits and cover different areas. On MACS0647 the helper with a 6″
margin reproduces the production tile to within a few pixels.

**Gridding a large field**: give every tile the same tangent point and rotation
and step `crpix` by whole pixels, so tiles are pixel-aligned with each other and
overlap by about an arcminute. COSMOS uses 20 tiles of 19200 × 24910 px at
rotation 20°, with x steps of 17300 px and y steps of 23081 px. `crpix` then lies
outside the array, because the tangent point is the field centre. Tiles in
`fiducial_tiles` must share the tangent point and rotation (the code enforces
this). An off-grid tile, such as COSMOS `primer`, works as a standalone product.

**Memory**: resampling holds the whole tile in memory, and the drizzle context
cube costs `tile area × 4 B × ceil(N_inputs / 32)`. Keep tiles to about 0.5 Gpix
(COSMOS tiles are 0.48 Gpix) unless the machine is very large. If a tile doesn't
fit, `[nircam.resample].write_context = false` (campfire resample backend only)
drops the context cube, which nothing downstream reads.

## Settings production uses

These differ from the package defaults in the production `config.toml`:

```toml
[nircam.outlier]
    implementation = "campfire"    # CAMPFIRE's own per-visit drizzle / median / blot
    grow_large_regions = true      # grows detector-fixed artifact masks (scattered-light arcs)

[nircam.resample]
    implementation = "campfire"    # CAMPFIRE-native drizzle
```

Per-field overrides live in each field's `fields.toml` entry, for example a looser
`[<field>.align].max_residual_arcsec = 0.2` on cluster fields and
`grow_large_regions = false` on MACS0647. The CRDS context is
`[environment].CRDS_CONTEXT` in `config_default.toml`; changing it is a
Calibration change (see `pipeline/AGENTS.md`).
