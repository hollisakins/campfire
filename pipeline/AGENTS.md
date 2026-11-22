# pipeline/ — `campfire-pipeline` (`cfpipe`)

JWST reduction for NIRSpec MSA spectroscopy (`campfire_pipeline/nirspec/`) and NIRCam imaging (`campfire_pipeline/nircam/`), with shared code in `common/`. The pipeline code runs locally and doesn't talk to the cloud: it reads and writes `$CAMPFIRE_ROOT`. Its products are published separately by the `campfire` CLI (`python/`). Usage, the data-directory layout and the package structure are in `README.md`.

```bash
cfpipe nirspec run --obs <obs_name> --all --processes 4
cfpipe nircam run --field <field_name> --all --processes 4
cfpipe download --program 6585 --instrument nirspec
cfpipe config > my_config.toml          # export the defaults
pytest pipeline/tests -q
```

## Configuration

- Resolution order: package defaults (`campfire_pipeline/data/config_default.toml`), then the user config (`--config` or `$CAMPFIRE_ROOT/config/config.toml`), then per-observation / per-field overrides (in `config/observations.toml` or `config/fields.toml`).
- Config is parametric only: it controls *how* stages run, not *whether* they run. `--overwrite` and `--processes` are CLI-only.
- Observations are defined in `$CAMPFIRE_ROOT/config/observations.toml` and fields in `fields.toml`. These are synced with the cloud by `campfire config push/pull` (see `python/AGENTS.md`).
- `[environment].CRDS_CONTEXT` in `config_default.toml` is the canonical CRDS context for the release. Changing it is always a **Calibration** change (MINOR).

## Rules

**Tests use the current model.** When a reference table or model is superseded, update the test to the current one and restate what it checks. Don't pin a failing test to the old table.

**The emission-line fitter runs only at inspected redshifts.** `cfpipe nirspec linefit` is not part of `run --all`:
1. `campfire pull spec --obs X` writes `reference/nirspec/<obs>/redshifts.toml`.
2. `linefit` fits spectra at `[nirspec.line_fitting].min_quality` or better into `<base>_lines.fits`, refitting only when the redshift, quality or spectrum bytes change.
3. `campfire deploy lines --obs X` publishes them.

Fits at the automatic redshift are opt-in end to end (`--allow-auto`). Line names come from `nirspec/linelist.py`. After editing it, run `python scripts/sync_linelist.py` so the web mirror matches. Why the fitter is a separate post-inspection step: the `linefit_stage.py` docstring.

## Versioning and releases

The version comes from git tags `pipeline-vX.Y.Z` via `setuptools-scm`. Never edit it by hand. Outputs record it in `CMPFRVER`, and `campfire deploy` asks for confirmation before shipping data from a non-release version.

| Bump | When | Changelog category |
|---|---|---|
| MAJOR | Breaking output format change (FITS schema, summary columns, file naming) | Algorithm |
| MINOR | Any change to pixel or flux values for the same input: CRDS bump, `jwst` upgrade, new calibration default, algorithm change with scientific impact | Calibration / Algorithm |
| PATCH | No change to scientific output: CLI, plots, performance, refactors, tests | Infrastructure |

- A PR touching `pipeline/**` carries one categorized entry under `## Unreleased` in `pipeline/CHANGELOG.md`, covering that PR's pipeline changes (pure refactors included). Write it when the PR is about to open, not after every task; several commits can share one entry. If unsure of the category, pick the closest one and say so in the entry.
- PRs don't bump versions. Tags are cut after merge with `/pipeline-release` or `scripts/release-pipeline.sh X.Y.Z`, which require `main`, a clean up-to-date tree, a non-empty Unreleased section and an unused tag.
