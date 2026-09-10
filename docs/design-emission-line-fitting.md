# Design: emission-line fluxes in the CAMPFIRE spectroscopic catalog

**Status:** implemented (first version) — this document is the rationale and the contract.
**Driver:** the catalog carries per-spectrum redshifts but no line measurements. We want
line fluxes, equivalent widths and kinematics for every spectrum — measured **only at
an inspected redshift**, so the catalog never carries fluxes fit at a wrong `z`.

## 1. The constraint that shapes everything

Line fitting needs a redshift. The pipeline's own redshift (`zfit`, `redshift_auto`) is
wrong often enough that fitting lines at it would fill the catalog with junk: mis-assigned
[OIII]/Hα, confidently measured "lines" on continuum bumps, blends attributed to the wrong
species. The redshift that is right is the one a human signed off on the portal —
`objects.redshift` with `objects.redshift_quality ≥ 3` (probable) or 4 (secure).

That inspected redshift lives in the cloud, at the *object* level (one sky position, all
gratings), weeks after the reduction ran; the reduction runs on the reducer's machine with
no cloud access. So line fitting cannot be one more stage of `cfpipe nirspec run --all`.
It is a **post-inspection step** with its own loop:

```
reduce → deploy → inspect on the portal → campfire pull → cfpipe nirspec linefit → campfire deploy lines
```

Three alternatives were considered and rejected:

| Option | Why not |
|---|---|
| Fit lines at `redshift_auto` during `zfit`, re-fit later | Fills the catalog with fits at unvetted redshifts; the "later" never has a trigger; every downstream consumer must remember to filter. |
| Fit in the web/DB tier on inspection sign-off | Needs the R-curve, the LSF calibration and scipy in a serverless function, re-implements pipeline numerics in TypeScript, and cannot version alongside the pipeline. |
| Fit in the Python client against the API (spectrum JSON + redshift) | Duplicates the pipeline's spectral machinery (R-curves, `f_LSF`, vacuum line list) outside the package that owns and versions it; the client is a consumer, not a producer. |

The chosen design keeps the pipeline the only producer of scientific numbers, keeps the
portal the only authority on inspection, and moves the redshift between them the way every
other reviewer decision already moves: a `campfire pull` materializer writing a file the
pipeline reads.

## 2. Architecture

```
portal (objects.redshift, redshift_quality, version)
        │  campfire pull --obs X   (any user with program access)
        ▼
reference/nirspec/<obs>/redshifts.toml        ← layout kind nirspec_redshifts (user-state)
        │  cfpipe nirspec linefit --obs X      (min_quality gate, incremental)
        ▼
products/nirspec/<obs>/<base>_lines.fits      ← layout kind nirspec_lines (cloud product)
        │  campfire deploy --obs X  |  campfire deploy lines --obs X
        ▼
spectrum_line_fits (one row per spectrum, lines jsonb)  +  _lines.fits on OSN
        │  /api/v1/sync/lines  →  campfire sync
        ▼
meta/campfire.db + meta/lines.csv (wide)  →  Campfire.query_lines()
```

### 2.1 Redshift pull (`campfire.deploy.nirspec_redshifts`)

`campfire pull --obs X` (and `campfire deploy nirspec pull-redshifts --obs X`) reads the
observation's `targets`, joins each to its `objects` row and writes one table per target:

```toml
[targets.ember_uds_p4_1234]
object_id = "CAMPFIRE-J021739.21-051204.7"
redshift = 5.123400
quality = 4
version = 7
inspected_at = "2026-08-30T21:14:03+00:00"
```

`redshift` is the portal's generated column (`COALESCE(redshift_inspected, redshift_auto)`,
NULL at quality 1 = impossible). The writer and the reader both live in
`campfire_pipeline.nirspec.redshift_reference`, so the two sides cannot drift. The file is
regenerated in full on every pull (the DB is authoritative — edit on the web), is never
registered in `storage_objects`, and is pulled for *any* logged-in user with program access:
it needs no more privilege than reading the catalog, unlike the admin-only mask/flag pulls.
`version` and `inspected_at` are the staleness ledger (§5).

### 2.2 Fitter (`campfire_pipeline.nirspec.linefit`, stage runner `linefit_stage`)

`cfpipe nirspec linefit --obs X` fits every `*_spec.fits` whose target has
`quality ≥ [nirspec.line_fitting].min_quality` (default **3**, probable). Everything else
is skipped and counted — never fit at a guess. `--allow-auto` falls back to the spectrum's
own `zfit` `ZBEST` for QA runs; such products are stamped `ZSRC='auto'` and
`campfire deploy` refuses them unless told `--allow-auto-z`.

The product is incremental: a `_lines.fits` is rewritten only when its inputs changed —
the redshift, the quality, the spectrum bytes (`SPECHASH`) or the algorithm version
(`LFITVER`) — so re-running after a re-pull refits exactly the spectra whose inspected
redshift moved. `--overwrite` forces everything.

**Model.** Rest-frame vacuum wavelengths from one declarative catalog
(`nirspec/linelist.py`, 43 lines, UV to Paα). Every line that lands on the valid range gets
a support window; overlapping supports merge into *complexes* fit independently over a
window extended by `cont_pixels` of continuum on each side, with pixels under any other
complex's lines masked. Inside a window:

    F_λ(λ) = Σ_k F_k φ_k(λ; dv, σ_v) + Σ_m c_m x^m

`φ_k` is a unit-area Gaussian **integrated over the pixel**, so `F_k` is the line flux in
erg s⁻¹ cm⁻²; its width is `σ² = σ_LSF(λ)² + (σ_v λ/c)²` with `σ_LSF` from the grating's
CRDS R-curve scaled by the **same `f_LSF_<grating>`** the redshift fitter uses; the
continuum is a low-order polynomial in the scaled window coordinate. Doublets whose ratio
atomic physics fixes ([OIII] 4959/5007, [NII] 6548/6583, [OI] 6300/6363) are tied (flag
`tied`) when both fall in one complex. Lines closer than `blend_sigma × σ` (default 2σ)
are unresolvable and merged: the heavier line reports the blended flux (`blend`, e.g. the
prism's Hα+[NII]), modelled as one Gaussian at the weight-averaged wavelength of the pair,
and the companion is `blended` with no flux of its own — the catalog says "blend" instead
of splitting a degenerate pair by luck.

**Doublet totals.** The blend decision depends on the LSF at that wavelength, so `CIII1907`
would mean "the 1907 component" in G140M and "the whole doublet" in the prism, and a
catalog selection on it would mix the two. Close doublets whose ratio is free
(`linelist.DOUBLETS`: NV, CIV, OIII], CIII], MgII, [OII], [SII]) are therefore *also*
reported as a total under the doublet's own name (`CIII1908`, `OII3727`, `SII6725`, ...;
`component = 'doublet'`): the sum of the two components with their full covariance where
the grating resolves them (flag `resolved`), the single blended measurement where it does
not. Near the resolution limit the component fluxes are strongly anti-correlated with large
individual errors while their sum stays well constrained — the covariance term is what
makes the total's S/N honest — so the total is the quantity that means the same thing in
every grating and the one catalog selections ("all CIII] detections at S/N > 3") should use.
Components keep their own rows (or `blended`) for the ratio where it is measured; the
`resolved` flag on the total says whether it is. A member folded into a line *outside* the
doublet (NV under Lyα in the prism) leaves the total `blended` with `blend_into` naming the
carrier. The total is the *narrow* total: an accepted broad component on a member (CIV,
MgII) stays in `<member>_broad` as for any line, and the total carries the `broad` flag so
the reader knows it exists. Ratio-tied doublets need no total.

**Kinematics** are fit in two passes so faint lines cannot wander. Pass 1 fits each complex
with free, bounded `(dv, σ_v)` (`dv_max` 1000 km/s on gratings, 2500 on the prism whose
inspected redshifts are coarser). Complexes with a line at S/N ≥ `kinematics_snr_min` (5)
anchor the spectrum's global `(dv, σ_v)` (inverse-variance mean); every other complex is
refit in pass 2 with `(dv, σ_v)` fixed to it — a linear problem (`kin_global`). With no
anchor, `dv = 0`, `σ_v = sigma_v_default` (`kin_default`). The global `dv` gives a refined
`z_fit` per spectrum.

**Broad lines.** On permitted lines (Hα, Hβ, Hγ, MgII, CIV, HeII, Paα, Paβ) where the LSF
can resolve it (`σ_LSF < broad_sigma_min/2` — never the prism), a second Gaussian with
`σ ∈ [800, 5000]` km/s is tried and kept only if it improves χ² by `broad_delta_chi2` (25)
with a detected broad flux; it is reported as `<line>_broad` and the narrow line is flagged
`broad`. Default on; `fit_broad = false` disables it.

**Errors** come from the Jacobian of the final bounded least-squares solve (no χ² rescaling
by default; `scale_errors_by_chi2` opts in). Fluxes are unconstrained in sign, so
non-detections carry honest `flux ± err` from which readers form their own limits
(`n_detected` counts S/N ≥ `detect_snr` = 3). EW is `flux / continuum / (1+z)` in rest-frame
Å, `no_continuum` when the local continuum is below 1σ.

Per line: `component` (narrow | broad | doublet), `flux, flux_err, snr, ew_rest,
ew_rest_err, cont, cont_err, dv, dv_err, sigma_v, sigma_v_err, sigma_lsf_kms, wave_obs,
complex, chi2, dof, npix, flags, blend_into, tied_to`. Flags (bitmask,
`campfire.flags.LineFlags`): 1 tied, 2 blended, 4 blend, 8 edge, 16 kin_global, 32
kin_default, 64 broad, 128 no_continuum, 256 fit_failed, 512 masked, 1024 sigma_unresolved,
2048 resolved (doublet totals only). `LFITVER` is 2 (1 had no doublet totals and centred
blends on the primary's rest wavelength).

**Product** `<base>_lines.fits`: PRIMARY (ZUSED/ZSRC/ZQUAL/OBJID/OBJVER, ZFIT, DVGLOB,
SIGGLOB, KINSRC, NLINES/NDETECT/NBROAD, LFITVER, SPECHASH, FLSF, CMPFRVER, CMPFRTIM),
`LINES` table, `MODEL` (wave, model, cont on the spectrum grid), `COMPLEXES`. Plus a QA PDF
per spectrum (one panel per complex) when `plot = true`. Registered in the layout as
`nirspec_lines` (products tree, canonical key, no legacy key) so `push`/`pull`/`verify`
handle it like any product.

Validation: `pipeline/tests/test_linefit.py` fits synthetic G395M/prism spectra with known
fluxes (recovery within the quoted errors, tie ratios, prism blends, broad-line
acceptance/rejection, kinematics fallback) and drives the stage runner end to end
(quality gate, incremental refit, `--allow-auto` provenance).

### 2.3 Publication (`campfire.deploy.lines`)

`campfire deploy --obs X` now also uploads the `_lines.fits` products (plus a `_lines.json`
sidecar per product, layout kind `nirspec_lines_json`, generated by
`campfire.deploy.generate.generate_lines_json`: the `MODEL` extension converted to fν on
the spectrum grid, null outside the fitted windows, the provenance scalars and a compact
per-line summary — the payload the portal's spectrum plot draws) and, after the
spectra upsert, writes one `spectrum_line_fits` row per fitted spectrum; `campfire deploy
lines --obs X` does the same for an already-deployed observation (re-publish after a re-fit
without touching the spectra). Rows are keyed on `spectra.id` and replaced wholesale on
every re-fit, so a line dropped from the catalog never lingers; a removed spectrum cascades.
The per-line measurements are a `lines` jsonb keyed by line name — the same shape as
`object_photometry.photometry` — so adding a line to the catalog is a pipeline change, not
a schema migration, and the sync client pivots it into wide columns.

Provenance columns on the row: `z_used, z_source, z_quality, object_id, object_version,
fit_version, cfpipe_version, f_lsf, spectrum_hash, fitted_at`.

### 2.4 Catalog exposure

**Portal filter and sort (`spectrum_lines`).** The jsonb is unnested by the
`sync_spectrum_lines` trigger into `spectrum_lines(spectrum_id, line, component, flux,
flux_err, snr, ew_rest, ew_rest_err, flags, blend_into)`, primary key `(spectrum_id, line)`,
with a partial index on `(line, snr DESC)`, so "all CIII] detections at S/N > 3" is one
index range scan. The table is derived (never written by deploy; the FK cascades a dropped
fit) and visible under the parent spectrum's RLS. Every RPC on the catalog filter contract
(`get_filtered_objects_paginated`, `get_filtered_spectra_paginated`,
`get_filtered_object_ids`, `get_adjacent_objects`, `get_csv_export_*`) takes `p_line`,
`p_line_snr_min`, `p_line_snr_max`, `p_line_include_stale` and the `line_snr` sort: the
matching object / spectrum set is materialized once per call by
`objects_matching_line_filter` / `spectra_matching_line_filter` (viewer-visible programs,
publication gate, and — unless `p_line_include_stale` — the same staleness rule as the
ledger, `line_fit_stale_redshift`) and probed with `= ANY(...)` like the grating and
observation sets; `object_line_snr` gives the object's best S/N for the sort key and the
"Line S/N" column. The web sends the `p_line*` parameters only while a line is set, so the
default lists keep working against a database that has not yet applied the migration. The
picker (`web/lib/linelist.ts`, generated from `linelist.py` by `scripts/sync_linelist.py`
with a CI drift check) offers the doublet totals and the stand-alone lines only: a
component means different things on different gratings, a total means one thing. The
object page's Emission Lines section (`/api/objects/lines`, fetched in view) lists every
member spectrum's fit, totals and stand-alone lines by default, components and broad
components behind a disclosure, with the staleness of each fit.

**Model overlay on the spectrum plot.** `SpectrumPlot` has a "Lines" toggle next to the
zfit "Model" one: it fetches the `_lines.json` sidecar (resolved with the other three
sidecars by `/api/spectrum/sidecars` / the object page render, served from the delivery
front, `/api/line-fit` as the streaming fallback) and draws the fitted model and the dotted
continuum in the current flux unit, with the redshift the lines were fit at in the legend.
The sidecar is fetched only when toggled on — most spectra have no fit until inspected —
and the toggle greys out once absence is definitive (`has_lines: false` from the
registry, or the route's 404). Staleness is not the sidecar's call: while the overlay is
on the plot asks `/api/objects/lines` for the spectrum's fit status (the
`spectrum_line_fits_status` view, the same answer the Emission Lines table shows) and
draws a fit flagged `stale_redshift` / `stale_spectrum` greyed, dashed and labelled
"stale", never as the current model. The provenance gate holds for the bytes as it does
for the row: `lines_upload_tasks` uploads neither the FITS nor the sidecar of a product
fit at the auto redshift unless the deploy passes `--allow-auto-z`.

* `GET /api/v1/sync/lines` — keyset bulk fetch (mirrors `/sync/photometry`; RPC
  `get_line_fits_for_sync`, service role with the caller's program scope, publish gate via
  the parent spectrum, `stale_redshift` / `stale_spectrum` computed against the live
  inspection state).
* `campfire sync` — fifth stream into `meta/campfire.db` (`spectrum_line_fits`) and
  `meta/lines.csv`: one row per spectrum, `f_<line>`, `e_<line>`, `ew_<line>`,
  `ewe_<line>`, `flag_<line>` columns ordered by rest wavelength (broad components as
  `<line>_broad`).
* `Campfire.query_lines(observations=…, gratings=…, min_quality=…, exclude_stale=…,
  wide=True)` → the same wide table as an astropy `Table`.

RLS: a fit is readable iff its parent spectrum is (an `EXISTS` on `spectra` under the
caller's own policy — program access, publish gate, share-link scope, all in one place);
writes are admin (deploy) only.

## 3. Why the pipeline, not the client, owns the fitter

* **Versioning.** Line fluxes are scientific output: they belong under the pipeline's
  changelog discipline (Algorithm → MINOR) and carry `CMPFRVER` like every other product,
  plus their own `LFITVER` so an algorithm change is visible in the catalog independently
  of a reduction change.
* **One LSF.** The R-curves and the empirical `f_LSF_<grating>` calibration already live in
  the pipeline and are what `zfit` uses; the line widths must be consistent with them.
* **One vacuum line list.** The pipeline's `linelist.py` is the catalog key space; the web
  overlay carries the same vacuum values in microns. (The `zfit` templates keep their own
  air→vac basis; unifying them is a separate change.)
* **Deploy machines already carry the pipeline.** `campfire[deploy]` depends on
  `campfire-pipeline`, so the deploy side reads `_lines.fits` with the pipeline's own
  reader instead of a second parser.

## 4. Operating procedure

```bash
# after inspection on the portal
campfire pull --obs ember_uds_p4                 # products + redshifts.toml (+ admin annotations)
cfpipe nirspec linefit --obs ember_uds_p4 -p 8   # fits quality >= 3; skips up-to-date products
campfire deploy lines --obs ember_uds_p4         # or a full `campfire deploy --obs` — publishes rows

# anyone
campfire sync                                    # meta/lines.csv
```

```python
cf = Campfire()
t = cf.query_lines(observations=["ember_uds_p4"], min_quality=4, exclude_stale=True)
t["spectrum_name", "z_used", "f_Halpha", "e_Halpha", "ew_OIII5007", "flag_Halpha"]
```

Per-observation overrides go in `observations.toml` under `[<obs>.line_fitting]` (same
keys as `[nirspec.line_fitting]`).

## 5. Staleness, provenance, and re-fits

A published fit records what it was fit at (`z_used`, `z_quality`, `object_version`,
`spectrum_hash`). The portal keeps moving: inspectors revise redshifts (`objects.version`
increments), spectra get re-reduced (`spectra.file_hash` changes). The view
`spectrum_line_fits_status` (and the same two booleans on every sync record) flags
`stale_redshift` / `stale_spectrum` by comparing the ledger with the live state, so a
catalog user can `exclude_stale=True` and an operator can see what to re-fit. The re-fit
itself is the same loop: `campfire pull` (new redshifts) → `cfpipe nirspec linefit`
(refits only what changed) → `campfire deploy lines`.

Products fit at the auto redshift (`z_source = 'auto'`) are the one deliberate escape
hatch — for evaluating the fitter before inspection, or for programs that will never be
inspected — and are opt-in at every step (`--allow-auto` to fit, `--allow-auto-z` to
publish, `z_source` in every row for readers to filter).

## 6. Not in this version (follow-ups)

* Per-line labels on the plot overlay: the `_lines.json` sidecar carries a `lines`
  summary (name, component, observed wavelength, S/N, flags) that the plot does not draw
  yet; the catalog line markers at the slider redshift cover the common case.
* Public API row endpoint (`/api/v1/lines`) with filters; today the catalog comes through
  the sync stream and the Python client.
* Unify the `zfit` template line list with `linelist.py`.
* Cross-grating consistency checks (the same line measured in the prism and a grating).
