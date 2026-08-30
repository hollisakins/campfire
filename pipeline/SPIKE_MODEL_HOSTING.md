# Spike model hosting — public bucket setup

The NIRCam spike footprint models (WebbPSF extended PSF + scattered-light,
see `docs/design-nircam-spike-masking.md` §3.4) are distributed exactly like
the wisp templates: anonymous-HTTPS GET from a public bucket, validated
against a checksummed manifest shipped inside the package, cached under
`$CAMPFIRE_ROOT/cache/spike_models/`.

**The entire bucket/domain/versioning flow in `WISP_TEMPLATE_HOSTING.md`
carries over as-is.** This doc records only the deltas.

## Deltas from the wisp flow

- **Path prefix:** upload under `spike_models/<version>/` (versioned,
  immutable, never overwrite — same rule as `wisps/<version>/`). The same
  public bucket/domain as the wisps is fine; the version segment keeps the
  namespaces independent.
- **Filenames** (flat, byte-exact, derived from the manifest):

  ```
  SPIKE_MODEL_<SW|LW>_<λ.λλ>UM_<mask|photometric>.fits
  ```

  e.g. `SPIKE_MODEL_SW_0.90UM_mask.fits`, `SPIKE_MODEL_LW_4.40UM_photometric.fits`.

- **Two grades per anchor wavelength.** `mask` (block-**max**-downsampled
  float32, ×4 both channels = a 4-native-px cell — footprint-fidelity
  only, never under-masks; what Phases 0–2 fetch, ~174 MB total) and
  `photometric` (full-resolution float32, the Phase-3 candidate, ~2.7 GB
  total). Both grades must exist for every published anchor; the manifest
  builder enforces this.
- **Builder script:** `scripts/build_spike_models.py` replaces
  `build_wisp_manifest.py` and also performs the repack:

  ```bash
  # 1. repack the raw WebbPSF set (float64 originals) into the published set
  python scripts/build_spike_models.py repack <raw_dir> --out <pub_dir>

  # 2. upload <pub_dir> to the bucket under spike_models/<version>/
  rclone copy <pub_dir> r2:<bucket>/spike_models/<version>/ \
    --header-upload "Cache-Control: public, max-age=31536000, immutable" --progress

  # 3. regenerate + commit the manifest
  python scripts/build_spike_models.py manifest <pub_dir> \
    --base-url https://<host>/spike_models/<version> --version <version>
  ```

  The committed manifest is
  `campfire_pipeline/data/spike_model_manifest.toml`.
- **Raw inputs are not published.** The float64
  `PSF+scatlight_<λ>micron.fits` originals stay in private archive; each
  published file records its source name + sha256 (`CFSPKSRC`/`CFSPKSHA`)
  so the repack is reproducible. The 5.0 μm file in the raw set is a MIRI
  model and is skipped by the repack.
- **Mask-grade downsample factor** is validated by
  `experiments/spike_model_grade/downsample_drift.py` (isophote drift must
  stay under the mask `grow` tolerance); the accepted factor is the script's
  default `--mask-factor`.
