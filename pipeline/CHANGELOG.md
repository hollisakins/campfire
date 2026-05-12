# CAMPFIRE Pipeline Changelog

All notable changes to `campfire-pipeline` are recorded here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), categorized
by scientific impact:

- **Calibration** — changes that alter pixel/flux values for the same input
  (CRDS context bumps, `jwst` upgrades, calibration defaults, reference data).
  Triggers a **MINOR** version bump.
- **Algorithm** — changes that alter output structure or behavior, not
  necessarily values. Triggers **MINOR** (additive) or **MAJOR** (breaking)
  depending on backwards compatibility.
- **Infrastructure** — no scientific impact (CLI ergonomics, plots, perf,
  internal refactors, tests). Triggers a **PATCH** bump.

Versions are git tags of the form `pipeline-vX.Y.Z` (resolved by
`setuptools-scm`). Install a specific release:

```
pip install "git+https://github.com/hollisakins/campfire.git@pipeline-vX.Y.Z#subdirectory=pipeline"
```

Release procedure: edit the `## Unreleased` section below, then run
`scripts/release-pipeline.sh X.Y.Z` (or the `/pipeline-release` slash command).

## Unreleased

### Calibration
- NIRCam stage 1 no longer runs `snowblind.SnowblindStep` after `Detector1Pipeline`.
  The jump step is already configured with `expand_large_events=True`,
  `sat_required_snowball=False`, `expand_factor=2.2`, `sat_expand=2`, and
  `mask_snowball_core_next_int=True` (4000 s), which detects and dilates large
  cosmic-ray clusters at the groupdq level — covering the same cases as
  `SnowblindStep` (and more aggressively, since groupdq flags exclude affected
  groups from the ramp fit and propagate flagging across integrations). The
  `remove_snowballs` orchestrator step and the `[nircam.stage1.remove_snowball]`
  config block are removed. `snowblind` remains a dependency for
  `PersistenceFlagStep` in the persistence step.

### Algorithm
- NIRCam ``diag_striping`` step — substantial rewrite of the iteration,
  amplitude estimator, and angle metric:
  - **Column blending bug fix**: ``_column_weights`` now ramps each
    strip's weight across the *full* overlap region (previously only
    over ``overlap // 2`` on each side). Adjacent strips' weights are
    now complementary, giving a constant unit weight sum across the
    overlap (a true partition of unity). The old behavior produced a
    kink at the strip boundary when adjacent per-bin amplitudes
    differed — visible as residual edges at strip seams when amplitude
    varied sharply (e.g. near a bright off-axis source).
  - **Global per-bin median fallback** in
    ``diagonal_stripe_model_blended``: when every strip covering a
    pixel has too few unmasked values in that pixel's bin (below
    ``min_pixels``), the model falls back to the global per-bin median
    (computed across all strips combined) instead of silently emitting
    zero. The previous behavior left stripes untouched at exactly the
    rows where SRCMASK had eaten the brightest pixels — a
    self-reinforcing trap once iteration started rebuilding the mask
    on the (still-bright) residual.
  - **Strip-blended applied every iteration**, not just iter 2+. The
    earlier global-only iter 1 was a guard against the SRCMASK-eats-
    stripe-peaks trap, but the global per-bin median fallback inside
    ``diagonal_stripe_model_blended`` already covers that case
    (pixels whose every covering strip lacks ``min_pixels`` in a bin
    get the global estimate). A global-only first pass meanwhile
    under-corrects when scattered-light amplitude varies across
    strips — exactly the regime the strip-blended model was added to
    handle. Under ``n_iterations >= 2``, iter 2+ rebuilds SRCMASK on
    the running residual (default when ``n_iterations > 1``) so
    stripe peaks initially flagged as sources are released as the
    amplitude bleeds into the running model. θ stays fixed at iter
    1's optimum: re-scoring on a cleaned residual gives a flat score
    landscape, so argmin walks rather than locks. Per-iteration
    diagonal and H+V contributions accumulate into single cumulative
    models.
  - **Angle metric**: scoring switched from ``MAD²(residual)`` of a
    global per-bin median to ``-Var(M(θ))`` on the strip-blended model
    image. Same argmin by total-variance decomposition
    (``Var(D) = Var(M) + Var(D−M)`` with the residual-cross-term
    independent of θ for fixed mask), but the score is the captured
    signal itself — sharper minimum and decoupled from the θ-
    independent un-modeled-source-residual floor. The score model now
    matches the applied model (strip-blended with the configured
    ``column_width``/``overlap``/``max_strip_delta_ratio``), so the
    angle search rewards exactly the model the pipeline will subtract.
  - **Robust per-bin clip**: ``_per_bin_clipped_median`` now uses an
    inlined ``mad_std = 1.4826 * MAD`` threshold instead of bespoke
    ``np.std``-based clipping. Non-robust ``np.std`` is inflated by
    the very SRCMASK leakers the iteration is meant to reject — for
    small per-bin N, a few stripe-peak leakers float the clip
    threshold above themselves, defeating the rejection. Inlined
    rather than calling ``astropy.stats.sigma_clipped_stats`` per bin:
    that helper has ~50–100 µs of per-call machinery overhead and we
    call it ~500 K times per exposure (n_bins × n_strips × n_angles).
  - **``maxiters`` threaded** from ``[nircam.diag_striping].maxiters``
    all the way through ``diagonal_stripe_model{,_blended}`` →
    ``_per_bin_clipped_median`` (previously hardcoded to 2 in the
    helper, so the config knob only affected the H+V residual fit).
  - **Scoring perf**: angle scoring (a) skips the global per-bin
    median fallback (``compute_fallback=False``) since NaN model
    pixels are filtered from the score anyway; (b) skips the
    cross-strip regularizer (``regularize=False``) since it
    compresses ``Var(M)`` slightly without shifting argmax; (c)
    reuses the output buffer as the ``np.divide`` target instead of
    allocating a (H, W) ``float64`` copy per call; (d) hoists the
    θ-independent masking pass (``np.where(mask | ~isfinite(data))``)
    out of the angle loop in ``_coarse_fine_search`` so it runs once
    instead of once per angle.
  - **NaN preservation**: pre-existing NaN pixels in the input SCI now
    propagate through to the corrected output unchanged (with the
    DO_NOT_USE bit still set). The previous behavior overwrote them
    with 0, silently changing pixel values relative to the post-sky
    upstream snapshot.
  - **Skip-condition gating** (new, default on via ``skip_abs_range``):
    after the angle search, exposures whose -Var(M(θ)) curve provides
    no meaningful stripe signal skip the subtraction entirely. Two-tier
    OR (empirically derived from the F356W UDS audit of 306 exposures
    in ``scripts/diag_striping_score_audit.py``):
      - ``abs_range < skip_abs_range`` (default 1e-7): the score curve
        is essentially flat at any θ — no real stripe geometry to fit.
      - ``abs_range < skip_abs_range_at_edge`` (default 2e-7) AND the
        optimum θ within ``skip_boundary_dist`` (default 0.3°) of the
        search-range boundary: the search hit a wall with no interior
        minimum. The flat-tier alone would let these through.
    Skipped exposures write ``CFP_DIAG = 'SKIPPED: <reason>; would-be
    theta=..., range=[...]'`` so the decision is auditable, and the
    diagnostic PDF still renders with a ``[SKIPPED]`` title annotation
    showing the flat or boundary-walked score curve. The canonical
    SCI is bit-identical to the post-sky input on skip (apart from
    the CFP_DIAG header and any new DO_NOT_USE flagging for NaN). Set
    ``skip_abs_range = 0`` (and the at-edge pair) to disable. Reapply
    on a different field requires re-auditing — defaults are tuned to
    UDS data character, not universal.
  - **Stripe-aware SRCMASK filter** (new, default on via
    ``unmask_stripe_aligned``): after θ is determined, connected
    components in the SRCMASK whose principal axis lies within
    ``stripe_angle_tol_deg`` of θ and whose aspect ratio exceeds
    ``stripe_aspect_min`` are unmasked before the per-bin median fit
    (and after every iter 2+ SRCMASK rebuild). The ``striping``
    masking pass uses a 25-px Gaussian smooth after a 40-px ring-
    median that occasionally connects a bright scattered-light
    stripe into a "source"; once masked, the diagonal bin running
    along that stripe loses every unmasked pixel, the per-bin
    median collapses to the (also-empty) global-median fallback,
    and the stripe survives the subtraction intact. The filter
    targets that failure mode without releasing genuine compact
    sources (round components fail the aspect test) or off-axis
    elongated galaxies (axis-orientation gating). Provenance:
    ``unmask_aligned=1(ar=...,tol=...)`` in ``CFP_DIAG``.
  - Provenance recorded as ``niter=N`` in ``CFP_DIAG``.
- NIRCam ``wcs_shift`` step (new, opt-in): applies a per-rule bulk
  astrometric shift to the GWCS via ``jwst.tweakreg.utils.adjust_wcs``
  before ``jhat``, for visits whose pipeline astrometry lands outside
  JHAT's source-matching radius. Rules live as an array of tables under
  ``[[<field>.wcs_shift]]`` in ``fields.toml`` (``files`` rootname globs,
  optional ``filters``, ``delta_ra``/``delta_dec``/``delta_roll``/``scale``).
  The original GWCS is stashed in a ``WCS_BAK`` FITS extension on first
  apply and restored before re-applying on ``--overwrite``, so the step
  is declarative — config specifies the desired shift, on-disk state is
  brought into agreement. Provenance recorded in ``CFP_SHFT`` between
  ``CFP_VAR`` and ``CFP_JHAT``. No-op for fields without rules.
- NIRCam ``diag_striping`` step (new, opt-in): subtracts scattered-light
  diagonal stripe artifacts caused by off-axis bright stars. Runs after
  ``sky`` (so the data is flat-corrected and pedestal-subtracted, which
  the cross-strip ``max_strip_delta_ratio`` regularization needs to be
  meaningful — a fractional constraint against a non-zero pedestal is
  effectively unconstrained), before ``variance``. Reads the source mask
  from the ``SRCMASK`` extension that ``striping`` writes and that
  ``image2``/``edge``/``sky`` carry through. Coarse + fine grid search over θ scored by the
  residual MAD² of a global per-bin median; applies a strip-blended
  per-bin median at the optimal θ to capture spatial amplitude variation;
  re-fits horizontal + vertical 1/f residuals via a new
  ``fit_residual_striping`` helper extracted from ``striping`` (pure
  refactor — no change to ``striping`` behaviour). Provenance recorded
  in ``CFP_DIAG``. Disabled by default; enable per field with
  ``[field.diag_striping].enabled = true`` and tune
  ``theta_min``/``theta_max`` to the field's scattered-light geometry.
- NIRCam ``diag_striping``: default ``column_width`` raised from 256 to
  512 so each strip is one NIRCam amplifier (4 strips per SCA), with
  ``column_overlap`` defaulted to 0 (no inter-strip blending — strips
  align cleanly with amp boundaries). New ``max_strip_delta_ratio`` knob
  (default 0.3) regularizes the per-bin amplitude across adjacent amps
  via iterative pair projection — caps ``|M[k+1,b] - M[k,b]| ≤ ratio ·
  max(|M[k,b]|, |M[k+1,b]|)`` per diagonal bin so the spatial amplitude
  variation across amps stays smooth without letting any single amp's
  per-bin median run wild from a single bright source. Bin indices are
  now computed once on the full image rather than per-strip so bin ``b``
  refers to the same diagonal in every strip — required for the
  cross-strip constraint to be meaningful. Set
  ``max_strip_delta_ratio = 0`` to disable.
- NIRCam ``bad_pixel`` step: now disabled by default, only stacks the
  DO_NOT_USE bit (not all DQ bits), and defaults to a stricter
  ``threshold = 0.8``. The previous behaviour — flagging any pixel
  with *any* nonzero DQ bit in ≥20% of exposures as permanently bad
  — was adapted from a many-exposure COSMOS-Web reduction and
  over-rejected in the small-N regime: transient flags like JUMP_DET
  (cosmic rays, ~4–5%/exposure), SATURATED, and PERSISTENCE were
  promoted to permanent DO_NOT_USE, producing per-cal NaN fractions
  of ~20% in fields with only a handful of exposures per filter.
  Behaviour now: (1) the orchestrator skips the step unless
  ``[nircam.bad_pixel].enabled = true`` (intended to be opted in
  for COSMOS-style fields); (2) only the DO_NOT_USE bit (bit 0) is
  considered when stacking, so transients can no longer accumulate;
  (3) the threshold is normalised by the count of contributing
  exposures (was ``np.max(arr)``), making the threshold a true
  exposure fraction. Existing static defects are already covered by
  CRDS DQ in cal files, so disabling this step does not regress
  bad-pixel rejection — it only removes the over-counting.
- NIRCam tile WCS: ``Field.get_tile_wcs`` now converts the ``crpix``
  declared in ``fields.toml`` (FITS 1-indexed, the natural convention —
  ``(NAXIS+1)/2`` lands at array centre) to 0-indexed before returning.
  Both ``stcal.alignment.util.wcs_from_sregions`` (campfire-native
  drizzle) and ``jwst.resample.resample_step`` (jwst-path drizzle)
  document their ``crpix`` argument as 0-indexed; the previous
  pass-through introduced a constant +1-pixel astrometric offset on
  every mosaic. ``ResampleImage.update_fits_wcsinfo`` adds the +1 back
  when serialising to FITS-WCS, so the published ``CRPIX`` matches the
  user's intent and existing reference mosaics for the same tile. All
  mosaics produced before this fix carry a one-pixel sky offset
  relative to their declared ``crval`` and need to be re-drizzled.
- NIRCam mosaic resample now sets SCI=NaN at WHT=0 pixels in the final i2d
  before extension splitting (`steps/resample.py`), matching the ERR
  convention. The drizzle output already initialises SCI=0 at uncovered
  pixels, but `bkgsub` subtracts a smooth background everywhere, leaving
  small nonzero residuals there; the explicit NaN-fill makes the
  "no coverage ⇒ no signal" state unambiguous in the published
  `_sci.fits` and `_i2d.fits`, and matches ERR=NaN at the same pixels.
- NIRCam campfire-native drizzle (`drizzle.drizzle_tile` →
  `_write_i2d_fits`) now calls
  `jwst.resample.resample.ResampleImage.update_fits_wcsinfo(model)`
  before `model.save()`, populating `model.meta.wcsinfo` (CRPIX/CRVAL/
  CDELT/PC/CTYPE) directly from the gwcs's forward-transform parameters.
  `model.save` then serialises those into the SCI extension header in
  the standard PC+CDELT form a jwst i2d carries. Previously the campfire
  path wrote the gwcs only into the asdf-in-fits extension and left the
  SCI header without any legacy FITS-WCS keys, so DS9 (and astropy.wcs)
  saw no celestial WCS at all on campfire-path mosaics. Using the
  canonical jwst helper (rather than re-deriving keys ourselves)
  guarantees byte-equivalent encoding to a reference jwst pipeline
  i2d for the same geometry.
- NIRCam outlier detection's cross-visit overlap padding is now
  scoped to the same JWST program by default. The previous behavior
  (any spatially-overlapping exposure regardless of program) is
  available behind `[nircam.outlier].cross_program_overlap = true`.
  Motivation: in heavily-observed footprints (e.g. COSMOS-Web center
  in F200W where many programs dither over the same area), the
  cross-program padding caused each CRF to be drizzled once for its
  own visit plus once for every other program's visit it overlapped,
  driving an N²-ish redundant-drizzle cost. Intra-program scoping
  removes that scaling problem; CR statistics within the program
  (the only median pool that contributes to that program's
  exposures) are unchanged.

- NIRCam outlier detection has an opt-in campfire-native drizzle path
  (`[nircam.outlier].implementation = "campfire"`) in
  `nircam/outlier_detect.py:outlier_detect_for_visit`. Same per-visit
  grouping, intra-program scoping, manifest conventions, and
  `CFP_OUT` semantic as the jwst path; the drizzle/median/blot
  routine routes through campfire's bbox-sliced
  `drizzle.drizzle_tile_singles` + `stcal.MedianComputer` instead of
  `Image3Pipeline`'s stcal Resample. The per-visit intermediate WCS
  is built via `wcs_from_sregions` with `pscale=None`, `rotation=None`
  (input native scale, ref-input rotation — the same convention
  `jwst.outlier_detection` uses internally), so the drizzle/blot
  roundtrip preserves PSF cores rather than smearing them through a
  fixed-rotation tile grid. CR flagging still goes through the
  upstream `flag_resampled_model_crs` two-pass SNR scheme. Default
  stays `"jwst"` until COSMOS-scale validation confirms the speed and
  flagging-quality trade.

  Replaces the dead-end per-tile path that briefly lived under
  `outlier_step_per_tile` / `outlier_detect_for_tile` — the per-tile
  framing forced the median onto the science tile WCS, which both
  inflated per-input drizzle scaffolding (full-tile output buffers
  per input) and degraded PSF-core preservation in the blot
  roundtrip due to rotation/pscale mismatch with the inputs. The
  per-visit framing fixes both. The bbox-sliced `drizzle_tile_singles`
  primitive that came out of that work is retained and reused.

  Helpers extracted to `nircam/geometry.py:select_overlapping_files`
  (deduplicated from `steps/resample.py` and `manifest.py`) and
  `nircam/drizzle.py:_prepare_drizzle_input` /
  `_add_image_kwargs` (shared between `drizzle_tile` and
  `drizzle_tile_singles`).
- NIRCam stage-3 resample now has an opt-in campfire-native drizzle path
  (`[nircam.resample].implementation = "campfire"`) that replaces
  `jwst.pipeline.calwebb_image3.Image3Pipeline` with a direct
  `drizzle.resample.Drizzle` loop in `nircam/drizzle.py`. The structural
  win over `stcal.resample.resample.Resample` is the **variance trick** —
  a single persistent accumulator is filled by drizzling
  `var_total · wht` weighted by `wht`, with the final ERR computed as
  `sqrt(outvar / outwht)`. This replaces stcal's three transient
  per-component variance drizzles plus full-tile Python masked
  accumulator updates (the `wsum[mask] = ...` loops at COSMOS-Web tile
  size were the dominant per-tile cost).

  The output WCS is built via `stcal.alignment.util.wcs_from_sregions`
  using the campfire-supplied `(crpix, crval, shape, rotation,
  pixel_scale)` from `Field.get_tile_wcs`. The i2d FITS is written
  through `stdatamodels.jwst.datamodels.ImageModel` so the
  `SCI`/`ERR`/`WHT`/`CON` HDU layout matches what `bkgsub` and the
  extension splitter consume; per-component `VAR_*` extensions are
  intentionally not written (nothing in pipeline/, python/, or web/
  reads them from i2d files).

  Validation on rj0911 venus f277w (60mas, 8 inputs, 23 MP): SCI, WHT,
  and coverage are bit-exact (modulo float32 accumulation order). ERR
  is systematically ~5% larger than stcal's ERR at the median because
  the trick computes the canonical kernel-weighted estimator
  `V = (Σᵢ kᵢ wᵢ² varᵢ_total) / (Σᵢ kᵢ wᵢ)²` while stcal computes a
  per-component sum `Σ_xx wsum_xx / (wt² · pixel_scale_ratio²)` after
  drizzling each `sqrt(varᵢ)` separately. The bias is concentrated at
  low-coverage edges (1.13× at p25 WHT) and uniform at ~1.03× in
  well-covered regions; nearly zero correlation with var_poisson /
  var_rnoise (Spearman 0.008) so it's a geometry/kernel artifact, not
  a noise-model artifact. Wall-time speedup on the validation tile is
  4.4× (28.4 s vs 125.9 s); expected to grow at COSMOS-Web tile sizes
  where stcal's per-input full-tile bookkeeping dominates. Default
  stays `"jwst"` until COSMOS-Web spot-checks confirm the bias is
  acceptable for downstream catalog use.
- NIRCam combine phase no longer runs `skymatch`. The step has been removed
  from `COMBINE_STEPS` (and dropped from `STEP_NAMES`, `_SCI_MUTATING_STEPS`,
  `_STEP_LABELS`, and the `CFP_*` provenance keys), along with the
  `nircam/steps/skymatch.py` module and the `[nircam.skymatch]` config block.
  The step had been a silent no-op since it was wired through
  `Image3Pipeline` with every other substep skipped — `Image3Pipeline.process`
  only propagates `save_results` to `outlier_detection`/`resample`/
  `source_catalog`, so the modified models were never written to disk and
  the in-place SCI subtraction was discarded. Per-exposure background
  subtraction (the `sky` step, `CFP_SKY`) and the resample-time 2-D source-
  masked background (`SubtractBackground` inside `resample_step`) cover the
  remaining background work; existing reductions have effectively been
  running this two-pass setup all along, so this changelog entry records
  the removal of plumbing that wasn't doing anything rather than a change
  in pixel values. `outlier_step` and `resample_step` still pass
  `'skymatch': {'skip': True}` to their JWST `Image3Pipeline` calls — that
  keeps JWST's own skymatch substep disabled inside those calls and is
  unrelated to the orchestrator-level step we removed.

### Infrastructure
- NIRCam: new `cfpipe nircam expmap` command. Builds per-filter exposure
  maps by stacking each input's `S_REGION` polygon weighted by `XPOSURE`
  into an auto-sized TAN WCS (no tile dependency, no drizzling — exposure
  time is a scalar per-exposure property, so polygon-mask × XPOSURE is the
  correct accumulator). Outputs `expmap_{filter}_{stage}.fits` (BUNIT='s',
  WCS in header), a matching diagnostic PDF with RA/Dec gridlines, and a
  combined `footprints_{stage}.reg` ds9 file color-coded by filter.
  Supports `--stage uncal` (raw quick-look) and `--stage canonical`
  (post-jhat). Default pixel scale 0.5"/pix; per-filter parallelism via
  `-p`.
- NIRCam `expmap` polish:
  - **Shared WCS across filters**: the auto-WCS is now sized to enclose
    the union of S_REGION polygons across *every* filter in the
    invocation (rather than per-filter), so per-filter expmaps are
    pixel-registered and can be stacked or differenced directly.
  - **Header-scan progress**: per-filter `tqdm` bar while reading
    XPOSURE/S_REGION headers in phase 1 (previously silent).
  - **PDF colormap**: switched from `Greys` (lowest-exposure pixels
    indistinguishable from white background) to `magma` with zeros
    masked, and dropped `vmin` from the 5th percentile to the actual
    nonzero minimum. Low-exposure edges are now clearly visible against
    the off-footprint background.
- NIRCam `resample` step (campfire-native drizzle path): log a
  `[i/N] drizzled <basename>` line per input exposure, mirroring the
  per-exposure progress the JWST `Image3Pipeline` path already prints.
  Inputs that don't overlap the tile log `[i/N] <basename>: no tile
  overlap, skipping` in place of the prior batched summary.
- NIRCam `outlier` step: keep stcal's on-disk median scratch out of the
  user's home directory. `MedianComputer` creates its temp dir via
  `tempfile.TemporaryDirectory(dir=tempdir)`, where the stcal default
  `tempdir=""` resolves against the **current working directory**
  (not `$TMPDIR`). On networked-FS clusters like CANDIDE, CWD is the
  user's home, so every visit's median buffer (`tmpXXXX/N.bin` per
  section) accumulated against the home quota. Both outlier paths are
  fixed: the campfire path (`outlier_detect_for_visit`) now passes
  `tempdir=tempfile.gettempdir()` when no explicit tempdir is given,
  and the jwst path (`outlier_step`) `chdir`s into its `outlier-*`
  scratch (which already lives under `$TMPDIR` and is auto-cleaned)
  for the duration of `Image3Pipeline.call`, so the implicit CWD-rooted
  scratch lands inside the scratch dir.
- NIRCam orchestrator: skip the `_scan_status` pre-scan when
  `--overwrite` is set, returning an empty `StepStatus` instead. With
  `--overwrite`, every step runs regardless of prior state, so the
  pre-scan's only product (cached "this CFP key is already present"
  decisions) is unused for skip purposes. But `StepStatus.mark_all`
  only adds keys to the cache and never removes them, so the
  pre-scanned snapshot went stale mid-phase whenever a fresh-model
  step (`detector1`, `image2`) stripped prior CFP_* keys and
  non-schema extensions like `WCS_BAK` from disk on its rerun. The
  symptom was a `wcs_shift` `RuntimeError` during a `process
  --overwrite` rerun ("CFP_SHFT is set but WCS_BAK extension is
  missing"): the cache lied that `CFP_SHFT` was still present after
  `image2` had wiped both it and `WCS_BAK` from disk, so `wcs_shift`
  took the restore-and-reapply branch and found the backup gone.
  With an empty cache, `StepStatus.has` falls back to a live
  `cfp.has_step` read for any path not yet seen, so in-step checks
  match disk reality at the moment they fire.
- NIRCam `outlier` step: dispatch one visit per worker via
  `common.parallel.dispatch` so the combine phase honors `--processes N`
  past `apply_mask`/`bad_pixel`. Previously visits ran sequentially and
  `n_processes` was silently dropped past those two steps. Each visit
  writes only to its own canonical files (atomic_save); cross-visit
  overlap files are read-only inputs and outlier_detection only adds
  DQ bits, so parallel runs cannot crash. The only semantic difference
  vs. serial is that a worker may read an overlap file's DQ before the
  visit owning that file has stamped its new outlier bits — a small
  median bias in those overlap pixels. Intra-program overlap scoping
  (the default) keeps the affected footprint small. Use `--processes 1`
  for a strictly ordering-stable run.
- NIRCam `jhat` step: stage the JHAT-aligned exposure to a sibling `.tmp`
  on the products filesystem before the atomic rename, instead of
  `os.replace`-ing directly out of the `tempfile.TemporaryDirectory`
  scratch area. On networked-FS clusters where `TMPDIR` is node-local
  (e.g. CANDIDE: `/tmp` per compute node, products on `/n23data2/...`),
  the direct rename failed with `OSError: [Errno 18] Invalid cross-device
  link`. Copying into `<canonical>.tmp.fits` first puts the rename within
  the products device, preserving atomicity, and keeps JHAT's many
  intermediate writes on fast local scratch.
- Cap BLAS/OpenMP thread counts to 1 by default for all `cfpipe` runs
  (`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `OMP_NUM_THREADS`,
  `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `BLIS_NUM_THREADS`),
  set only when not already in the environment. Pipeline stages
  parallelize via fork-pool processes; without this, each worker spawns
  one BLAS thread per visible core and on high-core HPC nodes (e.g.
  candide, 64 cores) the collective thread count exhausts
  `RLIMIT_NPROC`. Symptom on candide was JHAT failing mid-run with
  cascading `OpenBLAS blas_thread_init: pthread_create failed for
  thread N of 64: Resource temporarily unavailable` warnings followed
  by spurious `KeyboardInterrupt` tracebacks inside
  `astropy.modeling.core` (workers losing a thread-spawn race during
  the `world_to_detector` transform on the full reference catalog in
  `jhat/simple_jwst_phot.py`). Implemented as a tiny
  `campfire_pipeline._thread_caps` shim imported as the first thing in
  every CLI entry point (before `matplotlib`/`numpy`), with the same
  defaults applied in `setup_environment` for the programmatic-import
  path. Override per-run via `[environment].OPENBLAS_NUM_THREADS = "N"`
  in your config or by exporting the variable before `cfpipe`.
  Numba-parallel hot paths (NIRSpec redshift fitting,
  `common.spectral` LSF resampling) are unaffected — they use
  `numba.set_num_threads(ncores)` against `NUMBA_NUM_THREADS`, which we
  do not pin.
- Switch `common.parallel.dispatch` from the platform-default multiprocessing
  start method (`fork` on Linux, `spawn` on macOS) to an explicit `forkserver`
  context. Forking 32 workers from a multi-GB parent — the state after
  `persistence` even with batch-by-detector cleanup, since glibc malloc
  doesn't return arena memory to the OS — was failing on candide with
  `OSError: [Errno 12] Cannot allocate memory` at `os.fork()` because Linux
  commit accounting requires `parent_RSS × n_workers` of committable memory
  upfront, regardless of copy-on-write. Forkserver launches a small helper
  early in the run; subsequent worker forks come from that ~tens-of-MB helper
  rather than the bloated main process. Heavy scientific imports (`numpy`,
  `scipy`, `astropy.io.fits/wcs/table`, `jwst`, `jwst.datamodels`,
  `stdatamodels`, `stcal`, `crds`, `snowblind`, plus campfire common
  modules) are listed in `set_forkserver_preload` so workers inherit them
  COW from the helper instead of re-importing per pool. `jhat` and
  `tweakreg` are intentionally not preloaded (they touch the CRDS singleton
  in a way that locks the context — see `feedback_lazy_jwst_imports`); they
  remain lazy imports inside the worker functions that need them. Behaviour
  is unchanged on macOS (already used `spawn`-equivalent semantics by
  default); the change is the cross-platform consistency and the Linux
  ENOMEM fix.
- NIRCam `persistence` step: hand snowblind one detector at a time instead of
  the whole filter. Snowblind's `process()` does `results = images.copy()`,
  deep-copying every model's SCI/ERR/DQ; with a full SW filter that doubled
  the working set into multi-GB territory and leaked into the next step.
  Per-detector batching caps peak at `exposures_per_detector × 2` (≈8× win
  for SW, 2× for LW). Also explicitly closes input models after each batch
  (snowblind copies them, so the originals are independent objects whose
  asdf-backed arrays don't always release on refcount alone) and
  `gc.collect()`s between detectors so the parent process is lean before
  wisp/striping dispatch.
- Fix CRDS serverless-mode lock-in on machines without `CRDS_SERVER_URL` set
  in the shell. The `jhat` step's `from jhat import align_wcs_batch` ran at
  module load and transitively imported `stpipe → crds` before
  `setup_environment()` populated `CRDS_SERVER_URL`, locking CRDS's module-
  level proxy into serverless mode for the rest of the process. Moved the
  import into `jhat_step()` so it fires after env setup. Symptom was
  `CrdsNetworkError: Failed downloading cache config from: JSON RPC service
  at 'https://crds-serverless-mode.stsci.edu'` even with a reachable server
  and `cfpipe info` showing the correct URL.
- `cfpipe download` accepts a positional filter: `--target` (object name or
  `"RA Dec"` decimal-degree string, repeatable) plus `--radius` /
  `--radius-units` (default 3 arcmin; server cap 30 arcmin). Forwarded to the
  MAST JWST search API's native `target`/`radius` fields, so spatial pruning
  happens server-side and returns only filesets within the cone.
- `cfpipe download`: parallelized the MAST `/list_products` step. Batches
  (still 25 filesets each — bigger batches push past the server's per-request
  budget and time out) are dispatched concurrently via a ThreadPoolExecutor
  with workers from `--processes` (default 4), and each request retries on
  429 / 5xx / transient network errors with exponential backoff that honours
  the `Retry-After` header. Cuts the product-listing phase ~`workers`×.
- New `cfpipe nircam rgb --field <name>` subcommand: combines per-filter
  per-tile mosaics produced by `cfpipe nircam combine` into trilogy-style
  RGB PNGs (one native-resolution PNG plus one downsampled preview per
  tile, written next to the per-filter products under
  `products/nircam/<field>/<tile>_<pixscale>_rgb[_preview].png`). Filter
  channel weights and stretch tunables (`noisesig`, `noiselum`,
  `satpercent`) come from a new optional `[<field>.rgb]` block in
  `fields.toml`; pixel scale defaults to `[nircam.resample].pixel_scale`
  and is overridable via `--pixel-scale`. The trilogy stretch core lives
  in `nircam/trilogy.py` (small, dependency-light copy of the algorithm
  in `python/campfire/deploy/tiles_engine.py` — to be consolidated when
  the deploy-side tile generator is rewired to read from NIRCam outputs
  directly). Pipeline-only; produces no FITS, does not build a tile
  pyramid.
- NIRCam striping: removed the unused `find_optimal_threshold` maskparam
  sweep (an 11-point per-exposure search that was dead code under the default
  asymmetry-based fallback) and the legacy mask-fraction code path
  (`CAMPFIRE_STRIPING_METHOD` env var, `maskparam` config key). The
  asymmetry-based per-row fallback introduced in `bb348f4` is now the only
  behavior. CFP_1F header is now stamped with the asymmetry/prefilter
  thresholds instead of the (always-overwritten) maskparam.
- NIRCam now serializes a CRDS reference-file pre-fetch pass before parallel
  `detector1` / `wisp` / `striping` / `image2` dispatch. Mirrors the existing
  NIRSpec pattern (`nirspec/stage1.py`, `nirspec/stage2.py`): one
  `crds.getreferences()` call per unique `(DETECTOR, READPATT, SUBARRAY)` for
  Detector1Pipeline reftypes and one per `(DETECTOR, FILTER, PUPIL)` for
  Image2Pipeline reftypes (covers the `flat` lookup used by wisp/striping
  too). Fixes "empty or corrupt FITS" / "no SIMPLE card found" failures
  caused by multiple workers racing to download the same reference file on
  cold-cache runs. No-op when `--processes 1`. Wired in
  `nircam/orchestrate.py::run_process` and (for CRDS-touching steps only)
  `run_step`; new module at `nircam/prefetch.py`.
- NIRCam `fields.toml` now supports bash-style brace expansion in `files`
  patterns (e.g. `'jw01727{001,002,003}*'` → three patterns), and a
  field-wide top-level `skip = [...]` exclude list that applies to every step
  resolving exposures via `Field.get_uncal_files` / `get_exposure_files`.
  Both lists go through the same `_expand_braces` pre-filter; per-step
  `files_to_skip` (e.g. under `[field.resample]`) stacks on top of the
  field-wide list. Skip patterns must start with `jwNNNNN` like `files`.
- `cfpipe download` now writes raw uncal files to a PID directory named with
  the unpadded integer program ID (e.g. `raw/1727/...` and
  `raw/nircam/1727/{filter}/...`), instead of the 5-digit zero-padded form
  (`raw/01727/...`). The NIRCam field-config PID extractor strips leading
  zeros from `jwNNNNN*` patterns to match. Existing downloads under
  `raw/0NNNN/` need to be renamed (or re-downloaded) to the unpadded form.
- NIRCam diagnostic plots extended across the per-exposure and mosaic
  steps. Previously only `striping` and `wisp` produced diagnostic PDFs.
  Adds: `<rootname>_sky.pdf` (histogram of the masked sky-pixel
  distribution with the fitted Gaussian and pedestal overlaid, plus
  before/after SCI stamps); `<rootname>_outlier.pdf` (SCI snapshot plus
  newly flagged OUTLIER pixels — works for both
  `[nircam.outlier].implementation = "jwst"` and `"campfire"` paths);
  `<mosaic>_thumb.png` (block-mean-downsampled ZScale render of the
  final i2d, default 4× downsample, axis-free PNG); and
  `<mosaic>_bkgsub.png` (three-panel PNG: pre-bkgsub, post-bkgsub,
  background model, with shared SCI ZScale on the first two and a
  symmetric diverging colormap on the model panel — diagnostic for
  over-subtraction of extended sources). Each is gated behind a
  `plot = true` flag in the corresponding config block
  (`[nircam.sky]`, `[nircam.outlier]`, `[nircam.resample]`); mosaic
  downsample factor is configurable via
  `[nircam.resample].plot_downsample`. `fit_sky_tot` gains a
  `return_diagnostics=True` mode that returns the full Gaussian
  `popt` alongside the fitted mean so the histogram overlay aligns
  without re-fitting.
- NIRCam products directory is now flat per (field, filter). The previous
  layout nested outputs under `products/nircam/<field>/exposures/<filter>/`
  (canonical FITS, plus `diagnostics/` and `manifests/` subdirs) and
  `products/nircam/<field>/mosaics/<filter>/` (i2d files, plus
  `extensions/` and `manifests/` subdirs); everything now lives directly
  in `products/nircam/<field>/<filter>/`. `Field.exposures_dir` and
  `Field.mosaic_dir` are removed; the new `Field.filter_dir(filter_name)`
  returns the single per-filter directory used by every step (detector1,
  wisp/striping/jhat diagnostics, outlier and mosaic manifests, mosaic
  i2d + split extensions). The `jw*` field globs naturally exclude
  `mosaic_*` outputs from `get_exposure_files`. No change to filenames
  or FITS contents; existing reductions need to be re-run (or relocated)
  to populate the new layout.
- NIRCam: new `cfpipe nircam refcat {query,extract,merge,compare}` utility
  for building and managing astrometric reference catalogs. ``query``
  pulls from Gaia DR3 (astroquery) or Legacy Surveys DR10 (NOIRLab TAP);
  ``extract`` runs SEP-on-SNR detection + Kron/circle photometry on a
  mosaic to bootstrap relative-alignment refcats from an absolutely-aligned
  filter; ``merge`` stacks catalogs with positional dedup (first wins);
  ``compare`` reports ΔRA/ΔDec residuals between two catalogs with a 2D
  histogram diagnostic. Output schema (`RA`, `DEC`, `mag`, `mag_err` ECSV)
  matches what `[<field>.jhat.refcat_dict]` already consumes. Adds `sep`,
  `astroquery`, and `pyvo` to the pipeline dependencies. (`nircam/refcat/`)
- NIRCam mosaic-level background subtraction (`nircam/bkgsub.py`) is now
  ~10–50× faster on COSMOS-Web-scale tiles. The dominant cost — the
  ring-median filter at `radius=80, width=4` — now runs on a block-reduced
  copy of the SCI array (configurable via `[nircam.stage3].ring_downsample`,
  default `4`); the result is bilinearly zoomed back to full resolution
  before subtraction. The ring-median is by construction a smooth
  large-scale estimator, so this is equivalent to within sampling noise.
  Per-tier dilation in `tier_mask` switches from `binary_dilation` with a
  large `circular_footprint` to `scipy.ndimage.distance_transform_edt`
  thresholded at the dilate radius (O(N) instead of O(N×footprint), and
  bit-identical for integer radii since `circular_footprint` is itself an
  integer Euclidean disk). Tier convolution moves from
  `astropy.convolution.convolve_fft` to `scipy.ndimage.gaussian_filter`
  (separable, C-optimized, no full-image FFT). The biweight scale/location
  used by every tier is hoisted out of the tier loop in `mask_sources`
  (single pass over the unmasked image instead of four), as is the
  filled-image array fed to the smoothing kernel. `np.choose` is replaced
  by `np.where` throughout. No CRDS / pipeline / output-format change;
  default behaviour for the per-exposure variance step is unchanged
  (`ring_downsample` defaults to `1` in the dataclass and is only enabled
  in `[nircam.stage3]`).
- NIRCam `resample_step` extracts its per-tile drizzle body into
  `_drizzle_tile_via_jwst(selected_files, output_path, *, crpix, crval, shape,
  rotation, pixel_scale, resample_cfg, reduction_version)`. The function
  builds the ASN, runs `Image3Pipeline` with every substep but `resample`
  skipped, and stamps `CMPFRTIM` / `CMPFRVER` on the i2d primary header.
  No behavior change — sets up a clean swap point for the upcoming
  campfire-native drizzle (issue #138).
- NIRCam tile `corners` are now optional in `fields.toml`. If omitted, the
  pipeline derives the tile sky polygon from the first `<scale>mas`
  subsection (`crpix` + `naxis`) plus the tile/field `tangent_point` and
  `rotation`, so a tile that already specifies its WCS doesn't need to
  duplicate the same information as a hand-typed corner list. Existing
  `corners` entries continue to override the WCS-derived polygon.
- NIRCam tiles only need to declare `crpix`/`naxis` at one pixel scale.
  `Field.get_tile_wcs(tile, pixel_scale=...)` now rescales `crpix` and
  `naxis` from any defined subsection to the requested pixel scale (the
  tile covers the same sky region at every scale); explicit `[<scale>mas]`
  blocks still take precedence when present. Resampling at `60mas` no
  longer requires duplicating a `30mas` definition (or vice-versa).
- `compute_file_hash` (NIRCam mosaic manifests) now opens FITS with
  `do_not_scale_image_data=True` so memmap stays available on extensions
  that carry `BZERO`/`BSCALE`/`BLANK` keywords. The previous behavior
  raised `ValueError: Cannot load a memory-mapped image` on the first
  manifest write for a visit whose CRF outputs were stored with integer
  scaling (jwst 1.20.x).
- Pin `pandas<3` to keep `jhat` 0.3.6 working. pandas 3.0 removed the
  `delim_whitespace` keyword that `jhat/pdastro.py` still passes to
  `pd.read_csv` / `pd.read_table` when loading reference catalogs, which
  caused the jhat WCS-alignment step to crash on every exposure under
  pandas 3.x. Lift this pin once a pandas-3-compatible `jhat` release is
  available on PyPI.
- NIRCam orchestrator pre-scans every canonical exposure's primary header
  once at the top of `run_process` / `run_combine` / `run_step` and caches
  the set of present `CFP_*` keys in a `StepStatus` object
  (`nircam/status.py`). Each per-exposure step now filters out
  already-stamped files *before* spinning up the multiprocessing pool, so
  no-op passes on a finished field skip the worker spin-up entirely
  (worker processes use `spawn` on macOS, so each one re-imports
  astropy/jwst — that overhead used to be paid once per step regardless
  of whether any work needed to happen). Skymatch and outlier likewise
  short-circuit whole visits whose every member is already up-to-date.
  No change to outputs; `cfp.has_step` remains the fallback path for
  ad-hoc/CLI callers (`status`, `reset`, standalone scripts) and as a
  defensive check inside each step.
- NIRCam `Detector1Pipeline` no longer writes `_rateints.fits`, `_output_pers.fits`,
  `_trapsfilled.fits`, or `_persistence.fits` intermediates. Pipeline-level
  `save_results` is now `False` and the returned rate model is saved explicitly;
  `persistence.save_persistence` and `persistence.save_results` are likewise
  `False`. `_jump.fits` is still written (the jump substep keeps
  `save_results=True`) because `PersistenceFlagStep` reads `groupdq` from it,
  and is removed by the persistence step's cleanup. No change to pixel values
  or `_rate.fits` contents.
- `cfpipe download --instrument nircam` now resolves the per-detector filter
  from MAST's `opticalElements` field (e.g. `"F090W;CLEAR, F410M;CLEAR"`)
  instead of using the fileset's top-level `filter`. Previously a request for
  a single filter pulled in all 10 detectors of every matching fileset and
  tagged them all with the searched filter — so SW detectors landed under
  the LW filter directory (and vice versa) with bogus filter metadata that
  then propagated into `manifest.ecsv` and downstream stages. Pupil-mounted
  narrowbands (`F150W2;F162M`-style) are also handled. Files whose actual
  filter isn't in `--filters` are dropped with a count printed.
- Foundation pieces for the upcoming NIRCam canonical-exposure restructure:
  `common.io.atomic_save` (tmp+rename with optional primary-header updates
  applied in the same atomic operation), the `common.cfp` provenance module
  (ordered `CFP_KEYS`, plus `format`/`has_step`/`get_steps`/`clear_from`),
  and additive `Field.exposures_dir` / `get_exposure_files` /
  `get_exposure_path` getters. Existing stage dirs and getters are
  unchanged; the current pipeline is unaffected.
- Per-step modules `nircam/steps/detector1.py` and `nircam/steps/persistence.py`
  rewritten against the canonical exposures layout (one file per exposure,
  CFP_DET1 / CFP_PERS stamped via `atomic_save`). Not yet wired into a CLI;
  legacy `stage1.py` orchestrator continues to drive the pipeline. The new
  persistence step also moves earlier in the sequence (right after
  detector1) so the 1/f striping source-mask construction sees persistence
  DQ flags — this becomes a real behavior change when the new orchestrator
  lands.
- Per-step modules `nircam/steps/wisp.py` and `nircam/steps/striping.py`
  also written against the canonical layout. Wisp drops the
  `_rate_without_wisps_sub.fits` backup (PDFs generated inline with the
  in-memory before/after arrays). Striping replaces the
  `_rate_1fmask.fits` sidecar with a `SRCMASK` extension on the canonical
  file, written atomically alongside the SCI mutation via the new
  `atomic_save(..., extra_hdus=...)` parameter. Diagnostic PDFs land in
  `exposures/<filter>/diagnostics/`. A small shared
  `nircam/steps/_plots.py` carries the `plot_two` helper so the new
  modules don't import from `stage1.py`.
- Per-step modules `nircam/steps/{image2,edge,sky,variance}.py` round out
  the calibrate-phase per-exposure rewrites. image2 runs
  `Image2Pipeline.call(input, save_results=False)` and atomic-saves the
  returned cal-stage model to the canonical path, re-attaching the
  `SRCMASK` extension that the JWST pipeline doesn't carry through. sky
  reads `SRCMASK` from the canonical file's extension instead of the
  former `_rate_1fmask.fits` sidecar in `stage1_dir`. variance uses a new
  `SubtractBackground.compute()` method that performs the source-rejection
  + background fit in memory only — the legacy `_cal_bkgsub.fits` scratch
  file is no longer written. `SubtractBackground.call()` is refactored as
  a thin wrapper around `compute()` plus the existing FITS write so the
  mosaic-level usage in stage3 is unaffected.
- Per-step module `nircam/steps/jhat.py` finishes the calibrate-phase
  rewrites. Runs `jhat.align_wcs_batch` against a private scratch dir
  (one `TemporaryDirectory` per worker), stamps `CFP_JHAT` with the
  refcat name on the scratch output, then atomic-replaces the canonical
  file. JHAT preserves all FITS extensions through its WCS update so the
  `SRCMASK` extension carries through unchanged. Diagnostic PDFs and
  photometry tables are copied from the scratch dir to
  `exposures/<filter>/diagnostics/` before the scratch dir is cleaned up.
- Mosaic-phase per-step modules
  `nircam/steps/{apply_masks,bad_pixel,skymatch,outlier,resample}.py`.
  apply_masks rebuilds a `CFMASK` extension from the user `.reg` files
  on every run (replaces any existing CFMASK; OR's into DQ — DQ is
  cumulative, so mask removal requires `--reset-from apply_masks`).
  bad_pixel splits into a `build_bad_pixel_masks` ensemble step (writes
  `fl_pixels_<filter>_<detector>.fits` reference products) and a
  per-exposure `bad_pixel_step` that ORs the per-detector mask into DQ.
  skymatch and outlier both run JWST `Image3Pipeline` in a private
  scratch dir per visit, stamp `CFP_SMAT` / `CFP_OUT` on the scratch
  outputs, and atomic-replace the canonicals (with belt-and-suspenders
  capture/restore of `SRCMASK`/`CFMASK`). Outlier manifests now live in
  `exposures/<filter>/manifests/`. resample switches input source to
  `field.get_exposure_files(filter, with_step='CFP_OUT')` so only
  outlier-detection-finished exposures are eligible to be drizzled;
  mosaic outputs and the manifest format are unchanged. `CFP_SMAT` is
  added to `common.cfp.CFP_KEYS` between `CFP_BPIX` and `CFP_OUT`.

### Infrastructure (continued)
- `cfpipe nircam status --field <name>` reads CFP_* keywords across all
  canonical exposures and prints a per-step completion table plus a
  per-step summary (done / skipped / total). Reads each FITS primary
  header once via `cfp.get_steps`.
- `cfpipe nircam reset --field <name>` for clearing pipeline state.
  `--from <step>` clears the named CFP key and every later one on each
  canonical exposure (header-only, atomic via tmp+rename); refused for
  SCI-mutating steps (wisp, striping, image2, sky, variance, skymatch)
  since re-running them on already-mutated data would compound the
  effect. `--uncal` deletes every canonical exposure file (and any
  ``_jump.fits`` sidecars) for the selected filters; reference products
  (`bad_pixel_dir`, `refcat`, mask `.reg` files) and diagnostic PDFs
  are kept. Both modes prompt for confirmation; pass `--yes` to skip.

### Infrastructure (cleanup)
- Removed the legacy NIRCam stage modules (`stage1.py`, `stage2.py`,
  `stage3.py`) and the `engine.py` `ReductionEngine` wrapper. Their
  numerical helpers (`fit_pedestal`, `fit_sky`, `fit_sky_tot`,
  `collapse_image`, `find_optimal_threshold`,
  `measure_fullimage_striping`) move to a new `nircam/skyfit.py` module
  which `steps/striping.py` and `steps/sky.py` now import from.
  `Field.stage{1,2,3}_dir`, `Field.stage_overrides`, and the
  `get_rate_files` / `get_cal_files` / `get_jhat_files` /
  `get_all_jhat_files` / `get_crf_files` / `get_files` getters are
  removed; `Field.exposures_dir` and `get_exposure_files` /
  `get_exposure_path` are the single source of truth for per-exposure
  paths. `manifest.get_stale_tiles` switches its CRF glob to
  `field.get_exposure_files(filter, with_step='CFP_OUT')`. The
  `get_nircam_stage_config` helper is dropped from `config.py` (only
  `get_nircam_step_config` remains).

### Algorithm
- NIRCam pipeline restructured into a two-phase canonical-exposure flow.
  `cfpipe nircam process` runs the per-exposure work (detector1 →
  persistence → wisp → striping → image2 → edge → sky → variance →
  jhat) into a single canonical FITS file per exposure at
  `products/nircam/<field>/exposures/<filter>/<rootname>.fits`. User
  intervention (region masks in `mask_dir/<filter>/<rootname>.reg`,
  exclusion contract) sits between the two phases. `cfpipe nircam
  combine` runs the ensemble work (apply_mask → bad_pixel → skymatch →
  outlier → resample), promoting per-visit Image3Pipeline outputs back
  to the canonical paths via atomic_save. Persistence moves earlier in
  the sequence (immediately after detector1 instead of last in the
  per-exposure flow), so the 1/f striping source-mask construction now
  sees persistence DQ flags — small calibration delta. Snowblind's
  `jumpify` expects a `_rate.fits` filename, so the persistence step
  temporarily munges `meta.filename` around the call. Single canonical
  file per exposure replaces the old rate/cal/jhat/crf chain and the
  `_rate_orig`/`_rate_without_wisps_sub`/`_rate_1fmask`/`_cal_bkgsub`
  scratch files. NIRCam config namespace flattens from
  `[nircam.stage{1,2,3}.<step>]` to `[nircam.<step>]`. Per-field
  overrides in `fields.toml` use the matching flat layout
  (`[<field>.<step>]`). Legacy `cfpipe nircam stage{1,2,3}` CLI
  commands are removed; the new CLI is `process` / `combine` /
  `<step>` / `run` / `check` (status and reset land next). The
  `Image2Pipeline` round-trip drops custom extensions, so image2 and
  the Image3Pipeline-driven steps capture/restore `SRCMASK` / `CFMASK`
  via `atomic_save(extra_hdus=...)`.

## v0.4.0 — 2026-05-04

### Algorithm
- NIRSpec now includes an optional masking step between stage1 and stage2, 
  to mask specific regions contaminated by artifacts (e.g., shorts) which
  then get set as DQ `DO_NOT_USE`. 
- NIRSpec optimal extraction now falls back to a 3-pixel boxcar when the
  in-aperture collapsed cross-dispersion profile is corrupted (fewer than
  3 finite positive pixels, or positive flux / total |flux| below 0.5),
  e.g. due to background over-subtraction. Previously such cases produced
  a near-delta-function profile from the single surviving positive pixel
  and a degenerate optimal extraction. The chosen extraction method is
  recorded in the new `CMPFROPT` primary-header keyword (`'optimal'` or
  `'boxcar-3px'`); the QA profile plot is relabeled accordingly when the
  fallback triggers.

### Infrastructure
- Switched version resolution to `setuptools-scm` with a `pipeline-v*` tag
  prefix, scoping releases to the pipeline subpackage rather than the monorepo
  HEAD. The reduction version embedded in FITS (`CMPFRVER`) is now PEP 440 —
  e.g. `0.4.0` for releases, `0.4.1.dev3+g7f4e2c1.d20260504` for dev builds —
  rather than a raw monorepo git short SHA.
- Added `scripts/release-pipeline.sh` and the `/pipeline-release` Claude Code
  slash command to drive the tag-and-push workflow.
- Scoped the `CMPFRVER` dirty-flag check to `pipeline/`, so edits in `web/`,
  `python/`, `supabase/`, etc. no longer flip `+dDATE` on the pipeline
  version string (#135). `git describe --dirty` checks the whole working
  tree; we now call `git describe` (no `--dirty`) and pair it with a
  pipeline-scoped `git status --porcelain -- pipeline`.

## v0.3.0 — legacy

Initial unified `cfpipe` package version, prior to this changelog format and
prior to setuptools-scm. See `git log -- pipeline/` for the change history up
to this point.
