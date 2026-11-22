# Publishing a field to CAMPFIRE

Needs a CAMPFIRE admin login (`campfire login`) and the deploy extra
(`pip install -e "./python[deploy]"`). Every command below takes `--dry-run`.

1. **Exposure maps** (per filter, from the canonical exposures):
   ```bash
   cfpipe nircam expmap --field F
   ```
2. **Deploy the products**: mosaics, exposures and exposure maps, recorded as one
   field deployment. It also upserts the field's `fields.toml` entry to the
   database.
   ```bash
   campfire deploy --field F --draft                       # admin-only until published
   campfire deploy --field F --products mosaics --tile T   # or narrow what goes up
   campfire deploy publish --field F                       # make a draft public
   ```
   Without `--draft` the deployment is published immediately. Mosaics from a
   non-release pipeline version (a `.dev` build) deploy behind a confirmation
   prompt; see "Versioning and releases" in `pipeline/AGENTS.md`.
3. **The map**: build the FitsGL tile pyramid from the mosaics and deploy it. The
   map dataset's visibility follows the mosaics' deployment, so building against
   a draft shows nothing publicly until the draft is published.
   ```bash
   campfire fitsgl build --field F           # the fiducial_tiles composite
   campfire fitsgl deploy --field F
   campfire fitsgl build --field F --tile T  # a standalone off-grid tile
   ```
   After a mosaic's pixels change, rebuild with `--overwrite`: band reuse keys on
   presence, not content.

Re-deploying unchanged files is cheap: uploads dedup by content, and only changed
mosaics and exposures transfer.
