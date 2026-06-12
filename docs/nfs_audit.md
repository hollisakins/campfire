# NFS Performance Audit — `campfire_pipeline`

**Date:** 2026-06-11
**Scope:** `pipeline/campfire_pipeline/` (~23k lines), audited against the CANDIDE deployment model: code *and* data on NFS, where every metadata operation (stat, open, readdir, unlink) is a ~1ms+ network round trip and only large sequential I/O is cheap.
**Method:** 11 parallel Opus auditors (2 data-flow tracers, 7 module auditors, 2 cross-cutting specialists for concurrency and runtime I/O), each followed by an independent skeptical verifier that re-checked every cited `file:line` and loop-multiplier claim against the source. 74 raw findings → 73 survived verification (one claim — "NIRCam workers re-glob shared input dirs via the pickled `field` object" — was **refuted**: the orchestrator resolves file lists in the parent and passes per-task paths). I then re-validated the top findings against the code directly and consolidated overlapping findings across modules. Line numbers below were verified against the working tree at the time of audit. Frequency estimates are labeled **(guess)** when not derived from counted call sites.

**No code was modified in this pass.**

---

## 1. Data-flow summary

### NIRSpec arm

`cfpipe nirspec run --obs X --all -p N` iterates observations serially. Per observation, all products land in **one flat workspace directory** `$CAMPFIRE_ROOT/products/<obs>/` — file *naming*, not directory nesting, separates products. Multiprocessing (via `common/parallel.py::dispatch`, forkserver on Linux) fans out per stage:

- **stage1**: per-exposure (`_uncal.fits` → `_rate.fits`, in-place background subtraction + `_bkg.pdf`).
- **stage2a**: per-rate-file workers, each containing the **per-source inner loop** (~100–300 sources): write per-source MSA metafile + asn JSON, rewrite the shared rate-file header, run `Spec2Pipeline` → `<prod>_cal.fits`, delete the temp files. Then per-cal dispatches for `fix_units`, resample (`_s2d.fits`), and per-source `_nods.pdf` plots.
- **stage2b**: per-bkg-group nodded subtraction → `_cal_bkgsub.fits` / `_s2d_bkgsub.fits`.
- **stage3**: per-(source × grating) `Spec3Pipeline` → `_s2d`/`_x1d` → optimal extraction → packaged `_spec.fits` + `_prof.pdf` + `_spec.pdf`.
- **zfit**: *serial* loop over spec files (numba-threaded internally, no Pool) → `_zfit.fits` (+ `_zfit.pdf`).
- **summary**: globs + re-opens every spec/zfit/cal product → 3 ECSVs. Under `--all`, `_run_summary` runs **twice** (`nirspec/cli.py:519,533`), doubling all of its I/O.

**Volume per mid-size obs** (~200 sources, 2 gratings, ~24 exposures): ~5,500 residual files in one flat directory, ~4–5k *transient* create+unlink pairs (per-source msa/json, spec3 asn, crf cleanup), and an estimated **20–30k+ FITS header-open round trips** from the repeated `discover_files`/summary sweeps. Cal files are written as **per-source copies**, so the `*_cal.fits` population is sources × nods × detectors (1,000–5,000 files), which every later glob and header sweep pays for.

### NIRCam arm

`cfpipe nircam run --field X --all -p N` iterates filters; within a filter, ~12 in-place per-exposure steps (detector1, persistence, wisp, striping, image2, edge, sky, variance, preview, jhat, diag_striping, apply_masks) each dispatch over exposures, mutating **one canonical FITS per exposure** in `products/nircam/<field>/<filter>/` (also flat) via `atomic_save` (.tmp write → optional reopen-for-update → rename). Combine phase: per-visit outlier detection, per-tile drizzle/resample, background, RGB tiles.

**Volume per typical field run** (6 filters × 120 exposures, 8 visits, 4 tiles — **guess** on counts): ~8,600 canonical atomic-write path-touches (720 exposures × 12 steps × tmp+rename), ~1,440 `_jump.fits` create+delete ops, **~3,000–5,000 diagnostic PDFs/PNGs** (≈7 per exposure with default `plot=true`), ~340 mosaic/manifest files, plus several thousand header-only opens (status scans, prefetch, geometry).

### Where round trips concentrate

1. **Header re-opens of the same files** — `discover_files` × 3 stages, summary/shutters/pointings, geometry per-tile WCS scans, prefetch double-reads. The same FITS primary header is routinely opened 2–5× by different code paths within one run.
2. **The per-source inner loop in stage2a** — TOML re-parse, shared-header rewrite, and temp-file create/delete, all × sources × rate-files.
3. **Flat directories with thousands of entries** — every `glob` is a full readdir of a directory bloated by per-source copies and diagnostic files, and globs are re-run per step / per stage / per tile rather than cached.
4. **CRDS context resolution inside parallel workers** against an NFS cache of hundreds of small mapping files.

Bulk pixel I/O (rate/cal/mosaic reads and writes) is sequential and large — that part of the architecture is NFS-fine. The problem is almost entirely metadata amplification around it.

---

## 2. Findings — High impact

### H1. Uncached TOML `@property` re-parsed inside the stage2a per-source loop — **[local fix]**

**Location:** `pipeline/campfire_pipeline/nirspec/observation.py:276-372` (properties), `pipeline/campfire_pipeline/nirspec/stage2.py:580-582` (hot access)

`Observation.stuck_closed_shutters` and `Observation.bkg_overrides` are plain `@property` methods with **no caching**: each access does `os.path.exists` → `toml.load` (open+read+close) → rebuild an astropy Table / nested dict from scratch. `stuck_closed_shutters_mtime` / `bkg_override_mtime` additionally call `os.path.getmtime` each time. `run_stage2a_single_rate` accesses `obs.stuck_closed_shutters` **at line 580, inside the `for source_id` loop** (and the mtime at 582); `run_stage2b` reads `obs.bkg_overrides` per bkg-group.

**Why it's costly:** every access ≈ 3+ NFS round trips (stat + open/read/close + getmtime) for a file whose contents never change during a run. At ~100–300 sources × 10–100 rate files this is **up to ~30,000 redundant re-reads of the same small TOML per observation**, re-done independently in every worker. This is likely the single largest avoidable metadata multiplier in the NIRSpec arm.

**Fix:** hoist `stuck_tab = obs.stuck_closed_shutters` and `stuck_mtime = obs.stuck_closed_shutters_mtime` above the per-source loop in `run_stage2a_single_rate` (one-line change, removes the ×sources factor immediately), and convert the properties to lazily-cached attributes:

```python
@property
def stuck_closed_shutters(self):
    if self._stuck_cache is None:
        ...  # existing exists/toml.load/Table build
        self._stuck_cache = tab
    return self._stuck_cache
```

The file is only legitimately re-read between stage2a passes (the stuck-shutter re-run path merges new entries), so an explicit `invalidate()` after the merge keeps that working.

### H2. Summary phase opens every spec FITS ~4× and SHA-256-streams every byte — twice per `--all` run — **[local fix]**, hash consolidation **[architectural]**

**Location:** `pipeline/campfire_pipeline/metadata/summary.py:339-377`, `reader.py:179-185,257`, `shutters.py:49,68-90,152`, `slits.py:17,30`, `pointings.py:46-72`; doubled by `nirspec/cli.py:519,533`

Per spec file, the summary stack performs: `read_fits_metadata` (`fits.open`, reader.py:195) **plus an unconditional `_compute_file_hash`** that streams the entire file through SHA-256 (reader.py:257); each zfit file is opened **twice** (`read_zfit_data` reader.py:117, `read_zfit_chi2` reader.py:161); `generate_shutters_table` then **re-globs the same `*_spec.fits`** (shutters.py:49) and re-opens each file 2× more (`_get_grating` for the primary header, `compute_slit_centers`→`get_exposure_table` for HDU 7), plus `get_source_pos` once per source. `generate_pointings_table` globs **every per-source cal copy** (thousands of entries) and then opens each unique exposure twice via two separate `fits.getheader(ext=0/1)` calls. All of it runs **twice** under `cfpipe nirspec run --all`.

**Why it's costly:** ~150–900 spec files × ~4 opens + 2× zfit opens + the cal-copy glob ≈ **8,000–16,000 fits opens per observation**, plus a full-file re-read of every spectrum (multi-GB of NFS traffic — sequential, so bandwidth-OK, but pure overhead) just to recompute a provenance hash that never changes.

**Fix (local):**
- Open each spec file **once** per generator: pull GRATING from `hdul[0].header`, HDU 7 as a Table, and the source position from one `with fits.open(spec_file, memmap=False)` block; add a `exp_table=`/`pre_read` kwarg to `compute_slit_centers`.
- Merge `read_zfit_data`/`read_zfit_chi2` into a single open.
- List the obs dir **once** per `_run_summary` with `os.scandir` and partition by suffix in memory, instead of 4 independent globs (`*_spec.fits` ×2, `*_zfit.fits`, `*_cal.fits`).
- Read both pointings headers from one open: `with fits.open(cal, memmap=False) as h: h0, h1 = h[0].header, h[1].header`.
- Drop the redundant `zfit_path.exists()` guards (reader.py:113,157) — the paths come from a glob; the existing try/except already handles absence.

**Fix (architectural):** compute the SHA-256 **once, when stage3 writes the spec file**, and stash it in the header (e.g. `CMPFRHSH`); `read_fits_metadata` then does `primary.get('CMPFRHSH') or _compute_file_hash(...)`. Alternatively adopt the size+mtime_ns fast path that `nircam/manifest.py:59-73` already implements. Needs your call because the hash is part of the deploy provenance contract.

Also worth a look: whether the second `_run_summary` call under `--all` can pass/reuse the first call's partial results instead of redoing everything.

### H3. `discover_files` opens every cal product twice — and is re-run per stage — **[local fix]**

**Location:** `pipeline/campfire_pipeline/nirspec/observation.py:433-445`; callers `stage2.py:295`, `stage2.py:392/459`, `stage3.py:64`, plus detect-stuck and summary paths

The discovery loop calls `fits.getheader(f, ext=0)` **and** `fits.getheader(f, ext=1)` for every matching product — two full open/read/close cycles per file (`getheader` does not hold the handle between calls). The cal-file set is per-source copies: 1,000–5,000 files. `discover_files` runs fresh in stage2a (post-Spec2), twice in stage2b, and again in stage3, all serially in the parent.

**Why it's costly:** 2 opens × N_products × ~4–5 invocations ≈ **10,000–50,000 serial header opens per observation**, none of it parallelized, all of it re-deriving metadata that doesn't change once a product exists.

**Fix:** (1) one open per file: `with fits.open(f, memmap=False) as h: hdr0, hdr1 = h[0].header, h[1].header` — halves the count for a few lines' change. (2) Add `SRCFLUX` to the columns extracted here, killing the separate double-`getheader` sweeps at `stage2.py:400-404` and `stage3.py:75-79` (each currently opens every file twice more because the `'SRCFLUX' in fits.getheader(...)` membership test and the value read are two separate calls). (3) Bigger win, still self-contained: persist the discovered table to a single workspace-level sidecar (e.g. `_<obs>_products.ecsv`, invalidated by directory mtime or explicitly by the stages that add files) so later stages read one file instead of re-sweeping thousands.

### H4. NIRCam reference data re-read from NFS for every exposure (wisp templates 5×/exposure, flats via CRDS per exposure) — **[local fix]**

**Location:** `pipeline/campfire_pipeline/nircam/steps/wisp.py:157,197`, `_flat.py:39-46,59`, `bad_pixel.py:176`

`wisp_step` re-reads each of the 4 candidate template FITS (~16MB each) via `fits.getdata` inside the fit loop **for every exposure**, then re-reads the chosen template a **fifth** time for the subtraction. `resolve_flat` + `apply_flat_with_retry` open a fresh `FlatModel` per exposure from both `striping` and `wisp`. `bad_pixel` reads its mask per exposure. Nothing is memoized per worker or preloaded before the fork.

**Why it's costly:** a fixed reference working set is multiplied by exposures × workers: ~400 template reads + up to ~2×N_exposures flat opens per run **(guess on exposure counts)** — each a full NFS open plus memmap'd data pull of a 16MB array that was identical last time.

**Fix:** module-level `lru_cache` keyed on path, copying before exposure-specific mutation:

```python
@functools.lru_cache(maxsize=8)
def _load_template(path):
    a = fits.getdata(path)
    a[np.isnan(a)] = 0
    return a
# per exposure:
w = _load_template(p).copy(); w[sci_before == 0] = 0
```

Same pattern for `FlatModel` data keyed on resolved flat path. One read per (worker, file) instead of per (exposure, file). See also H5 for eliminating the per-exposure CRDS *resolution*.

### H5. CRDS cache on NFS with read-write mode: every worker re-resolves context against hundreds of small mapping files — **[local fix]**

**Location:** `pipeline/campfire_pipeline/config.py:158-164` (CRDS_PATH default), `nircam/steps/_flat.py:45` (per-exposure `crds.getreferences` from wisp.py:122 and striping.py:270)

`CRDS_PATH` defaults to `$CAMPFIRE_ROOT/cache/crds` — on NFS — and `CRDS_READONLY_CACHE` is never set anywhere in the package (verified by grep). The serial prefetch (`prefetch.py`, `_prefetch_crds_references`) correctly warms the reference *bytes*, but each `crds.getreferences()` call made **inside a worker** still walks/stats the `mappings/jwst/` tree (hundreds of `.pmap/.imap/.rmap` files) and may refresh `server_config` / touch cache-write state. `resolve_flat` does this once per exposure in two different steps.

**Why it's costly:** several hundred `getreferences` calls per run × tens-to-hundreds of small mapping-file stats each ≈ **order 10k+ NFS metadata round trips (guess)**, bunched at dispatch start when all workers cold-resolve simultaneously.

**Fix (both cheap):**
1. In `setup_environment`, when the context is pinned (it always is — `config_default.toml:12`): `os.environ.setdefault('CRDS_READONLY_CACHE', '1')`, optionally behind a `[environment]` config flag. With a pinned context and prefetch-warmed cache this is safe and removes server-refresh/lock/write-attempt traffic from every call.
2. Memoize flat resolution by `(detector, filter, pupil)` — either `lru_cache` in `_flat.py` or, better, resolve once per unique config in the parent during prefetch and pass the resolved path into workers via `step_config`, so workers never call `getreferences` at all.

Pointing `CRDS_PATH` at node-local scratch seeded per node is a further deployment-level option, but readonly mode + memoization captures most of the win without changing the layout.

### H6. ~7 diagnostic files per exposure (NIRCam) and ~5 PDFs per source (NIRSpec) written into the same flat directories everything else globs — **[architectural]** (with a one-line config mitigation)

**Location (NIRCam):** `steps/striping.py:358-366`, `sky.py:91-101`, `wisp.py:184-194,219-227`, `diag_striping.py:849-869`, `preview.py:41-94` (2 PNGs via .tmp+rename), `outlier.py:319-335` — all defaulting `plot=True`, all into `field.filter_dir(filt)`.
**Location (NIRSpec):** `nirspec/plots.py:190-192,418,459,652-653,770-771,1038-1040` — `_zfit.pdf`, `_nods.pdf`, `_prof.pdf`, `_spec.pdf`, stuck-shutter diagnostics, into the workspace dir.

**Why it's costly:** a 200-exposure × 4-filter NIRCam run emits **~5,600 small diagnostic files** (each ≥2 metadata ops; preview pays create+rename ×2); a NIRSpec obs adds 500–1,500 per-source PDFs. Beyond the create/close round trips themselves, these inflate the very directories that `Field.get_exposure_files`, `Observation.glob`, `discover_files`, RGB's `_find_mosaic`, and the summary generators repeatedly list — so every later glob pays O(entries) for files the pipeline never reads back. This is the *compounding* finding: it makes every glob-shaped finding worse.

**Fix:**
- **Today, zero code:** `plot = false` in cluster config for production runs.
- **Architectural (recommended):** a `diagnostics/` subdirectory per filter / per obs (`field.diag_dir(filt)`, and a workspace `plots/` dir for NIRSpec), so diagnostics stop polluting the canonical-glob namespace even when enabled. Self-contained path change in each step, but it changes where you look for QA products, so it's your call. Consolidating per-step diagnostics into one multi-page PDF per (filter, step) via `PdfPages` is a further option where you'd accept serializing plot output.
- Same routing applies to jhat's diagnostic copies (`jhat.py:84-97` listdir + `copy2` per exposure into the canonical dir) and the `savephottable` per-exposure ECSV.

The parallel-write pattern is *not* an obstacle here: workers can write per-exposure diagnostics into a subdirectory just as safely as into the parent.

### H7. `select_overlapping_files` re-opens every candidate exposure's WCS per tile (N_exposures × N_tiles) — **[local fix]**

**Location:** `pipeline/campfire_pipeline/nircam/geometry.py:37-46`; per-tile callers `manifest.py:334-351` (`get_stale_tiles`) and `steps/resample.py:223`

The function `fits.open`s every exposure to read `hdul[1].header` WCS and build a footprint polygon — and both callers invoke it **inside their tile loop** over the same candidate list. An exposure's footprint never changes between tiles.

**Why it's costly:** ~100–300 exposures × ~4–30 tiles = **1,000–9,000 redundant fits opens per resample run or `cfpipe nircam check`** (guess on tile counts). `get_stale_tiles` additionally resolves `with_step='CFP_OUT'` without a `StepStatus`, adding one more `fits.open` per candidate (`manifest.py:327-331` → `field.py:456-461` fallback), so each file is opened twice per check before any work happens.

**Fix:** compute footprints once per filter — `polys = {f: footprint(f) for f in candidate_files}` (or reuse the S_REGION strings `orchestrate._read_sregions` already collects) — and make the per-tile loop pure shapely intersection. Thread a `StepStatus.scan` into `get_stale_tiles` like `_run_resample` already does (`orchestrate.py:478`).

### H8. Drizzle and outlier open every input 2–3× per tile/visit — **[local fix]**

**Location:** `pipeline/campfire_pipeline/nircam/drizzle.py:110-128` (header pass) + `:387` (`ImageModel` pass, via 530-557); `outlier_detect.py:44-70` (visit-WCS pass) + drizzle_tile_singles (`drizzle.py:387`) + `:191-218` (flag-loop third open)

`drizzle_tile` does a header-only scan of all N CRF inputs to size the output WCS (`ImageModel` for input[0], `getheader` for the rest), then re-opens **every** input via `ImageModel` (which also parses the embedded ASDF/gwcs — many small reads) in the drizzle loop. `outlier_detect_for_visit` does the same dance **plus a third open** of each visit file to mutate DQ.

**Why it's costly:** at COSMOS-Web scale (~200 inputs/tile, 2–30 tiles, 8–15 filters — **guess**) the redundant first-pass scan alone is 10³–10⁴ extra opens per field run, each an open+stat+header+ASDF-parse over NFS. The flag-loop open is a necessary write, but the WCS pre-pass duplicates data the main pass already holds.

**Fix:** make the pre-pass uniformly cheap (plain `fits.getheader` for *all* inputs including input[0] — drop the `ImageModel` open there) so the expensive ASDF-parsing open happens exactly once per input, in the drizzle loop. Pass `memmap=False` on the `ImageModel`/`fits.open` calls since all six arrays (data/err/dq/var_*) are fully materialized anyway (see M2).

---

## 3. Findings — Medium impact

### M1. `atomic_save` reopens the just-written file in update mode on essentially every step write — **[local fix]**

**Location:** `pipeline/campfire_pipeline/common/io.py:45-65`; called with `header_updates=cfp.format(...)` by ~12 NIRCam steps per exposure

After staging the write to `.tmp`, any `header_updates`/`extra_hdus` triggers a second `fits.open(tmp, mode='update')` — re-open, re-parse, header mutate (and with `extra_hdus`, HDU replace/append, which forces astropy to rewrite from the change point), flush, close — then `os.replace`. Since every step stamps a `CFP_*` key, the branch is always taken: **~8,640 reopen cycles per typical NIRCam field run** (720 exposures × 12 steps). Nuance: the `.tmp` was written microseconds earlier so its pages are warm in the client cache — the cost is the open/close/commit round trips and the rewrite-on-append, not a cold re-read; this is why it's Medium, not High.

**Fix:** apply `header_updates` *before* the staging write when only headers change — set keys on `hdul[0].header` / the datamodel's fits-pointer header, then save once. Keep the reopen **only** for the `extra_hdus` path (jwst datamodels can't carry non-schema extensions through `.save`). The atomicity contract (`.tmp` + `os.replace`) is correct and must be preserved.

### M2. astropy/stdatamodels default `memmap=True` over NFS — full-array and header-only reads both pay for it — **[local fix]**

memmap turns one sequential read (NFS's strong suit) into demand-paged small reads (its weak suit). Two distinct cases, both worth `memmap=False`:

- **Full-array reads** that immediately materialize everything: `drizzle.py:387` (6 full arrays per input), `nirspec/stage3.py:314-315` (opt-ext s2d/x1d, all extensions copied into `_spec.fits`), `manifest.py:42` `compute_file_hash` (**explicitly passes `memmap=True`**, then `.tobytes()` the whole SCI+DQ — the worst combination; ~33MB per file as page faults), per-exposure step opens (`striping.py:260`, `sky.py:46`, `edge.py:36`, `variance.py:68`, `bad_pixel.py:102,176`).
- **Header-only opens** where memmap setup is pure waste: `common/cfp.py:103,128,147`, `nircam/status.py` scan/add_paths, `metadata/reader.py:117,161,195`, `shutters.py:152`, `slits.py:17,30`.

**Fix:** add `memmap=False` at these call sites (or `fits.getheader` for pure header probes). For jwst `ImageModel`, stdatamodels forwards open kwargs in current versions — `ImageModel(path, memmap=False)`; verify on the installed version. Mechanical, low-risk, and the hash case alone converts thousands of page-fault RPCs into one streamed read per file.

### M3. stage2a per-source side effects: shared rate-header rewrite, temp-file churn, and NFS cwd — **[architectural]**

**Location:** `pipeline/campfire_pipeline/nirspec/stage2.py:605-617,685-688` (per-source msa FITS + asn JSON create→delete), `:608-610` (shared rate file `mode='update'`+flush per source), `:497,766,893` + `stage3.py:219`, `stage1.py:812` (`os.chdir(workspace_dir)` before jwst pipeline calls)

Three compounding behaviors in the hottest loop: (a) the shared rate file's primary header is rewritten (open-update + flush + close) once per source just to repoint `MSAMETFL`, ~N_sources × N_rate_files times per run; (b) two tiny files are created and deleted per source on NFS (~4 directory-metadata ops each); (c) workers chdir into the NFS workspace, so jwst's internal scratch and intermediates land on NFS.

**Why architectural:** the per-source metafile/asn is forced by the jwst `Spec2Pipeline` API, and the header toggle is how the pipeline currently communicates the metafile. The clean fix changes where transient I/O happens, not what's computed: route the per-source metafile + asn into **node-local scratch** (`$TMPDIR`), point the association at it, chdir workers into scratch, and move only the final `_cal/_s2d/_x1d` to the workspace. If `Spec2Pipeline` can take the metafile via the asn/step override without mutating the rate header (worth testing — the header is restored from `OGMETFL` anyway), the per-source rewrite disappears entirely. This eliminates ~N_sources × (1 header rewrite + 4 metadata ops) per rate file but changes the I/O contract for intermediates, so it needs your sign-off and a careful test of jwst's relative-path behavior.

### M4. Outlier/resample staleness machinery hashes full SCI+DQ when the stat fast path is available but unused — **[local fix]**

**Location:** `pipeline/campfire_pipeline/nircam/orchestrate.py:439-444` (`_visit_up_to_date` calls `compute_file_hash` directly), `manifest.py:23-50,59-73,76-87`

`manifest.file_unchanged` has the right design — size+mtime_ns stat fast path, hash only on mismatch — but `orchestrate._visit_up_to_date` bypasses it and hashes every visit file's full SCI+DQ (~32MB each, memmap'd) **on every combine run, including no-ops**: ~800 full hash-reads for a 200-exposure × 4-filter field where nothing changed (guess on counts). `input_entry` also unconditionally hashes on every manifest write.

**Fix:** make `_visit_up_to_date` consult `file_unchanged` with the manifest's stored size/mtime_ns (the data is already recorded), so unchanged files cost one stat. Add `memmap=False` to `compute_file_hash` (see M2) for the cases that genuinely must hash.

### M5. Repeated directory listings: per-step, per-stage, per-tile globs of large flat dirs — **[local fix]**

The same listing is re-derived many times against directories whose contents are static within a phase:

| Call site | Pattern | Multiplier |
|---|---|---|
| `nircam/field.py:397-463` `get_exposure_files` | glob per pattern + per skip-pattern | ~12–16 calls per filter per phase (every runner + `_scan_status`) |
| `nirspec/observation.py:374-402` `Observation.glob` | glob per include + per exclude pattern, no memoization; `check_exp_type=True` adds `fits.getheader` per matched uncal | every `_setup`, `discover_raw_files`, `discover_files` |
| `nirspec/stage3.py:291` | two `glob.glob` of the whole workspace **per source task** to find that source's crf/cal intermediates | hundreds–thousands of tasks × workers |
| `nircam/rgb.py:51-73,179-186` `_find_mosaic` | glob of the filter dir per (tile, filter) | n_filters × n_tiles, inside pool workers |
| `metadata/*` | 4 independent globs per `_run_summary` (×2 under `--all`) | see H2 |
| `nirspec/redshift_fitting.py:481,670,688` | glob per grating + `os.path.exists` per spec file for skip checks | ~600 stats + 4 globs per zfit run |

**Fix pattern, applied per site:** list once, reuse. Memoize `get_exposure_files` per (filter, skip) with an explicit invalidate after detector1 adds files; cache `Observation.glob` per (directory, ext) on the instance; in stage3 cleanup, the intermediate names are deterministic from `product_name` — construct them and `try: os.remove(...) except FileNotFoundError: pass` with **zero** globs; resolve RGB mosaic paths once per filter in the parent and pass them to workers; replace per-file `os.path.exists` skip loops with one `os.scandir` into a basename set.

### M6. Plotting re-reads data the process just had in memory — **[local fix]**

**Location:** `nirspec/plots.py:329-387` (`plot_stage2a_results` re-opens each s2d 3–4× via `fits.getdata`: norm pass, shape pass, imshow pass — ~5,600 opens for a 200-source obs vs ~1,600 if read once); `plots.py:113-133` + `redshift_fitting.py:306-308` (`plot_zfit_results` re-opens the zfit FITS written milliseconds earlier *and* re-reads the spec FITS the fitter already loaded — up to ~1,800 redundant opens per zfit run); same pattern in `plot_stuck_shutter_diagnostics`.

**Fix:** read-once dicts keyed by path inside the plot function; pass in-memory arrays (`spec_data=`, `zfit_data=` kwargs) from the fitter to the plotter, falling back to file reads only when called standalone.

### M7. Redundant double/triple header reads in NIRCam orchestration — **[local fix]**

- `prefetch.py:84-92,102-110`: `prefetch_detector1_references` and `prefetch_image2_references` each `fits.getheader` every uncal, back-to-back (`:128-129`), and `_filter_imaging_uncals` (`orchestrate.py:161`) reads EXP_TYPE in a third pass → up to 3 header opens per uncal per detector1 run. **Fix:** one `{file: header}` pass feeding all three consumers.
- `orchestrate.py:114-122,459`: `_read_sregions` opens **all** filter exposures before outlier dispatch even when only one visit is pending. **Fix:** read sregions only for pending visits + overlap candidates, or fold S_REGION capture into the `StepStatus.scan` that already opens every header.
- `orchestrate.py:187-200`: detector1 skip-check does `os.path.exists` per uncal on top of the status cache that already encodes absence. **Fix:** trust `status.has`.

### M8. Look-before-you-leap `exists()`-then-open throughout — **[local fix]**, aggregate

Individually Low, collectively thousands of redundant GETATTR round trips per run. Verified sites: `nirspec/stage1.py:55` (exists per uncal), `observation.py:241-249` (2–3 exists per uncal in symlink), `stage3.py:112-200` (~3–5 exists per source), `redshift_fitting.py:688`, `stuck_shutters.py:89-91`, `metadata/reader.py:70,84,113,157`, `nircam/steps/` (detector1.py:52, preview.py:46-49, image2.py:102, apply_masks.py:57, persistence.py:130, wisp.py:105), `manifest.py:201-204` (`load_manifest`), `refcat/extract.py:57,107-116`, `config.py:76-85,211-308` (config resolution chains), `common/query.py:388-401,752-781` (download path — once-per-download, lowest priority).

**Fix pattern:** where the next operation opens the same path, try/except `FileNotFoundError` around the open (halves round trips and removes the TOCTOU window); where the check gates a *skip*, batch one `os.scandir` listing into a set and test membership.

### M9. `cfpipe` startup runs 4 git subprocesses — including a full `git status` subtree walk — on every invocation — **[local fix]**

**Location:** `pipeline/campfire_pipeline/cli.py:29`

`@click.version_option(version=get_reduction_version(), ...)` evaluates eagerly at module import, for **every** command. `_git_version` runs `git describe`, `git rev-list`, `git rev-parse`, and `git status --porcelain -- pipeline` — the last stats every tracked file under `pipeline/` against the NFS-resident `.git` and working tree. Pure startup latency for every command (and every shell-out in a SLURM array); the codebase already treats NFS startup latency as a known pain (`_thread_caps.py` docstring).

**Fix:** replace with a lazy eager-option callback so the git work runs only for `cfpipe --version`. Reduction runs that stamp `CMPFRVER` already call `get_reduction_version(config)` later — they keep working.

---

## 4. Findings — Low impact

- **L1. Per-(source, grating) `_zfit.fits` files** (`redshift_fitting.py:277-304`) — ~200–900 small FITS per run. Acceptable if they're the science contract (they are: summary + inspection consume them). Consolidation into one per-obs container is possible but changes the deploy read path. **[architectural]** — flagged for awareness, not action.
- **L2. Per-visit/per-tile manifest JSONs** (`manifest.py:169-204`) — tens of small JSONs per filter; the look-before-leap in `load_manifest` is the fixable part. Consolidating to one per-filter manifest would serialize the parallel per-visit writes — probably not worth it. **[local fix]** for the open pattern only.
- **L3. `bkgsub.call()` re-opens the input mosaic** it already read, just for the WCS header / HDU template (`bkgsub.py:130,444`) — 1 redundant open per (filter, tile). Stash the header at first open. **[local fix]**
- **L4. `bad_pixel` writes a diagnostic `stack_dq_*.fits` alongside every mask** (`bad_pixel.py:116-142`) — 8 extra 16MB FITS per SW filter when enabled (off by default). Gate behind `save_stack_dq=False`. **[local fix]**
- **L5. jhat copies diagnostics into the canonical dir per exposure** (`jhat.py:84-97,269-282`) — listdir + 1–3 small copies per exposure; the scratch-on-`$TMPDIR` design itself is correct. Route to the diagnostics dir (H6); consider `savephottable=False` for production. **[local fix]**
- **L6. `load_r_curve` leaks its HDUList and memmaps a tiny table** (`common/spectral.py:53-54`) — ~1–4 calls/run; latent fd leak under forkserver. Context-manager + `memmap=False`. **[local fix]**
- **L7. Extended-wavelength `_gated_configs` reads every rate header twice** (build + verify both iterate it; `extended_wavelength.py:63,215,276`) — only when `extend_g140m_g235m` is on. Share one pass. The extended-ref cache on `$CAMPFIRE_ROOT/cache` is fine (write-once, guarded). **[local fix]**
- **L8. `masks.py` sentinel probes open the same rate file 5–8× per apply cycle** (`masks.py:134,255,364` and the per-file apply chain) — only for observations with manual masks. Read the 3 sentinel keywords in one open and pass them down. **[local fix]**
- **L9. `stuck_shutters` analysis opens each s2d twice** (`stuck_shutters.py:302,305` — SCI then VAR_RNOISE as separate `fits.getdata`) — one `fits.open` reading both. The dispatch pattern itself (paths in, data read in-worker, no shared writes) is the correct template. **[local fix]**
- **L10. Logging is `print()`-to-stdout with heavy per-source chatter** (`common/io.py:9-11`; 32 call sites in stage2.py alone). No log *files* are created by the pipeline — the NFS exposure depends entirely on whether the SLURM script redirects stdout to NFS. **(guess — job-config concern, not code.)** Recommend `#SBATCH --output` to node-local scratch with copy-back, or accept sequential append as cheap. **[architectural]** (deployment docs)
- **L11. Download path (`common/query.py:388-401,752-781`)** — exists+stat per file and a header open per uncal at download time; dominated by MAST transfer bandwidth, runs once. Collapse exists+stat to one try/except `os.stat` if touched anyway. **[local fix]**

---

## 5. What's already NFS-friendly (don't re-fix)

Worth calling out so the good patterns get reused, not accidentally regressed:

- **`StepStatus.scan`** (`nircam/status.py:31-48`): one header open per exposure per phase, consulted in-memory thereafter — the model consolidation pattern. CFP provenance lives *in* the FITS headers, not sidecar files.
- **Serial CRDS prefetch before every parallel dispatch**, deduplicated by detector/filter config (`nircam/prefetch.py`, `nirspec/stage1.py:297,359`, `stage2.py:146`) — workers never race on cold downloads. (The residual gap is context *resolution*, H5.)
- **`dispatch` + forkserver preload** (`common/parallel.py:45-60`): heavy modules imported once and inherited copy-on-write; kwargs pickled, not re-read; **no SQLite anywhere, no file locking, no concurrent writes to any shared file** — each worker writes only its own canonical output via `atomic_save`. The refuted finding confirms the orchestrator resolves file lists in the parent.
- **Scratch on `$TMPDIR`**: jhat (`jhat.py:156`), outlier's chdir-into-scratch so stcal's MedianComputer `.bin` churn stays off NFS (explicitly documented in `outlier_detect.py`), `refcat/hsc.py:281`, `stage1.py:166`.
- **`expmap.py`**: on-disk header cache keyed by (path, mtime_ns, size), `memmap=False`, thread-pooled misses — the model pattern for metadata scans, worth replicating in `discover_files` (H3).
- **`manifest.file_unchanged`** size+mtime_ns fast path (the gap is the caller that bypasses it, M4).
- **`atomic_save`'s** `.tmp` + `os.replace` atomicity contract.
- **Consolidated ECSV outputs** (one summary/shutters/pointings file per obs); **zfit reference data** (template pickle, R-curve, IGM grid) loaded once per grating before the serial fit loop.

---

## 6. Triage: quick wins vs larger refactors

### Quick wins (local, low-risk, biggest first)

1. **H1** — hoist the two TOML properties out of the per-source loop + cache on the instance. Two small edits, removes up to ~30k NFS ops per NIRSpec obs.
2. **H5** — `CRDS_READONLY_CACHE=1` in `setup_environment` + memoize `resolve_flat`. ~10 lines.
3. **H3** — single-open `discover_files` + fold in SRCFLUX. Halves a 10–50k-open sweep.
4. **H4** — `lru_cache` wisp templates and flat data. ~15 lines.
5. **H7** — hoist footprint computation out of the tile loops; pass `status` into `get_stale_tiles`.
6. **M4** — route `_visit_up_to_date` through `file_unchanged`.
7. **M2** — `memmap=False` sweep (mechanical; prioritize `compute_file_hash`, drizzle inputs, opt-ext, header-only probes).
8. **H2 (local half)** — one open per spec file in summary/shutters, merged zfit reads, single scandir per `_run_summary`.
9. **M9** — lazy `--version`.
10. **H6 (config half)** — `plot=false` in the CANDIDE production config, today.
11. **M6/M7/M8** — plot read-once caches, prefetch single-pass headers, try/except-open conversions; good batched cleanup PRs.

### Larger refactors (need your decision — they change layout or I/O contracts)

1. **H6** — `diagnostics/` subdirectories (and/or consolidated multi-page PDFs). Changes where QA products live; biggest structural payoff because it shrinks every directory the pipeline repeatedly lists.
2. **H2 (hash half)** — compute spec-file hashes at write time (header keyword) or adopt stat-based provenance. Touches the deploy contract.
3. **M3** — stage2a transients on node-local scratch + non-mutating metafile handoff to `Spec2Pipeline`; jwst cwd to `$TMPDIR` with explicit copy-back of finals. Highest-risk/highest-reward NIRSpec change; needs validation that jwst resolves relative inputs correctly from scratch.
4. **H3 (sidecar half)** — persisted products-metadata table per workspace replacing repeated `discover_files` sweeps (mirror `expmap.py`'s cache design).
5. **Per-source cal copies** (flagged in H2/pointings): the layout multiplies the cal population by source count. Any consolidation (e.g. recording unique-exposure headers once at stage2) ripples into stage2b/stage3 grouping — design discussion, not a patch.

### Explicit non-findings

- No SQLite on NFS, no file locking, no shared-file concurrent writes anywhere in the package.
- The bulk pixel I/O architecture (large sequential FITS reads/writes, drizzle accumulation, SHA-256 streams) is bandwidth-bound and NFS-appropriate as-is.
- `redshift_fitting` is serial-with-numba-threads, not a Pool — it does not multiply NFS load by `n_processes` (the unused `Pool` import at `redshift_fitting.py:515` is dead code worth deleting to avoid future confusion).
