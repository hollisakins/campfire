"""
jackknife: re-zero jump-segmented ramp fits against frame-common curvature.

Per-exposure step. Runs immediately after ``detector1`` (it needs the
``<rootname>_jump.fits`` sidecar, which the ``persistence`` step deletes).

The mean calibrated ramp is not exactly linear in time: a frame-wide,
exposure-dependent common-mode curvature survives superbias/refpix/linearity/
dark (amplitude varies by ~an order of magnitude between consecutive
exposures of the same detector). Ramp fitting is estimator-independent only
on a linear ramp, so pixels whose jump/saturation flags give them a different
segment pattern read a different zero-point from the same curved baseline
than their full-ramp neighbours. Scattered CR hits pick up incoherent ±
offsets; a snowball's expansion ellipse hands one pattern to thousands of
contiguous pixels and the offset becomes a coherent disk (up to ~25% of sky
on pathological exposures — the SNR-preview "circles").

The fix is a delete-d jackknife in the original (bias-correction, not
resampling-variance) sense: measure what deleting each observed group
subset does to the fitted slope, by deleting exactly those groups from
*clean* pixels' ramps and refitting with the production fitter, then
subtract the measured offset from the pixels whose deletion was forced by
flags. Concretely:

1. Extract each pixel's excluded-group pattern from the sidecar GROUPDQ
   (``JUMP_DET`` bits only — SATURATED/DO_NOT_USE have different segment
   semantics in stcal, and pixels carrying them are left to downstream
   masking).
2. Assign candidate patterns to clean pixels on a fixed diagonal lattice
   (coprime strides, so no column/row comb can alias 1/f structure into the
   measurement), stamp the corresponding ``JUMP_DET`` bits into a synthetic
   GROUPDQ, and rerun the production ``RampFitStep`` once on the real ramp.
3. Because the calibration rate is bit-identical to the canonical rate on
   unflagged pixels, the per-pixel difference ``rate_cal - rate`` *is* the
   pattern-induced bias: the paired estimator needs no reference population
   and is immune to sky gradients. delta(zone, pattern) is its biweight
   location per zone (amp-column x detector-half for full frame).
4. Subtract delta from the flagged pixels' SCI. ERR/DQ are untouched (this
   is a bias, not noise), and a split-half null plus the exact-zero identity
   check on unflagged pixels are recorded as per-exposure diagnostics.

stcal segment semantics (documented here because it is easy to get
backwards): a ``JUMP_DET``-flagged group *starts* the new segment — the
fit discards the group difference *into* the flagged group, not the group
itself. The calibration is agnostic to this (it imposes and measures the
same convention), but any analytic modelling must use it.

Known limits (measured on jw01345064001_07201: see the investigation on the
branch): the correction removes the estimator term only. A separate
jump-*selection* bias (~-0.2 DN/group on threshold-marginal detections) and
an amplitude-proportional real charge deficit on high-dose event cores
survive it; both are spatially unclustered or core-confined and do not
reproduce the circle artifact. delta is calibrated on sky-dominated pixels
and drifts for very bright pixels (<~0.5% photometric ceiling; flagged
source cores are excluded from correction via the SAT/DNU rule anyway).

Provenance: stamps ``CFP_JACK`` with a compact summary and writes the full
delta table + diagnostics to ``<rootname>_jackknife.json`` (non-FITS so the
deploy/layout scanners ignore it). NINTS>1, NGROUPS>16, and missing-sidecar
exposures are sentinel-stamped ``skipped (...)`` and left untouched.
"""

import json
import os

import numpy as np

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp

# GROUPDQ bits (jwst.datamodels.dqflags.group)
_DNU, _SAT, _JUMP = 1, 2, 4

_MAX_NGROUPS = 16          # pattern bitmask capacity guard (uint32 is ample,
                           # but >16 groups was never validated)
_LATTICE_ROW, _LATTICE_COL = 1009, 31   # coprime with any power-of-two axis


def _patterns_from_groupdq(gdq_int):
    """Per-pixel excluded-group bitmask (JUMP bits only) + SAT/DNU carrier map.

    ``gdq_int``: (ngroups, ny, nx) uint8 GROUPDQ of one integration.
    Returns ``(pat, carries_satdnu)`` where ``pat[y, x]`` has bit g set when
    group g is JUMP-flagged, and ``carries_satdnu`` marks pixels with any
    SAT/DNU group — excluded from correction (different segment semantics,
    and they sit on saturated source/event cores that downstream masking
    owns).
    """
    ngroups = gdq_int.shape[0]
    pat = np.zeros(gdq_int.shape[1:], dtype=np.uint32)
    for g in range(ngroups):
        pat |= ((gdq_int[g] & _JUMP) != 0).astype(np.uint32) << g
    carries = ((gdq_int & (_SAT | _DNU)) != 0).any(axis=0)
    return pat, carries


def _coprime_stride(start, n):
    """Smallest stride >= ``start`` that is coprime to ``n`` (n >= 1)."""
    s = int(start)
    while np.gcd(s, n) != 1:
        s += 1
    return s


def _class_map(shape, n_classes):
    """Deterministic diagonal-lattice class assignment (0..n_classes-1).

    Both strides are forced coprime to ``n_classes`` at runtime — not just
    to the detector axes. A stride sharing a factor with the class count
    collapses the lattice into a row or column comb (e.g. a column stride
    of 31 with 31 classes makes every row a single class), which lets
    banded 1/f structure alias straight into the deltas — the same failure
    mode as ``index % n`` layouts (2048 % 64 == 0), just rotated.
    """
    ny, nx = shape
    if n_classes <= 1:
        return np.zeros(shape, dtype=np.int32)
    sr = _coprime_stride(_LATTICE_ROW, n_classes)
    sc = _coprime_stride(_LATTICE_COL, n_classes)
    r = np.arange(ny, dtype=np.int64)[:, None] * sr
    c = np.arange(nx, dtype=np.int64)[None, :] * sc
    return ((r + c) % n_classes).astype(np.int32)


def _zone_map(shape, n_zones):
    """Zone index map: 4 amp columns x (n_zones // 4) row bands, or all-zero."""
    ny, nx = shape
    if n_zones <= 1:
        return np.zeros(shape, dtype=np.int32), 1
    bands = max(1, n_zones // 4)
    xz = np.clip(np.arange(nx) * 4 // nx, 0, 3)
    yz = np.clip(np.arange(ny) * bands // ny, 0, bands - 1)
    return (yz[:, None] * 4 + xz[None, :]).astype(np.int32), 4 * bands


def _biweight(values):
    from astropy.stats import biweight_location
    return float(biweight_location(values))


def _measure_deltas(diff, classmap, zonemap, n_zones, clean, cands,
                    min_cell):
    """Per-(zone, pattern) paired deltas with frame-global fallback.

    Returns ``(delta, sem, nulls)``: ``delta[(z, p)]`` in the rate's units,
    with ``z = -1`` holding the frame-global value per pattern; ``nulls`` is
    the list of split-half half-differences pooled for the null diagnostic.
    """
    delta, sem, nulls = {}, {}, []
    finite = np.isfinite(diff)
    for k, p in enumerate(cands):
        if p == 0:
            continue
        m = clean & finite & (classmap == k)
        v = diff[m]
        if v.size < min_cell:
            continue
        d = _biweight(v)
        if not np.isfinite(d):
            continue
        delta[(-1, int(p))] = d
        sem[(-1, int(p))] = float(np.std(v) / np.sqrt(v.size))
        # Split-half null: alternate elements in raster order. Any *spatial*
        # parity rule degenerates here — the class lattice is linear in
        # (row, col), so entire classes can share one parity — while the
        # element-order split is balanced for every class by construction.
        a, b = v[0::2], v[1::2]
        if a.size > min_cell // 4 and b.size > min_cell // 4:
            nulls.append(0.5 * (_biweight(a) - _biweight(b)))
        if n_zones > 1:
            for z in range(n_zones):
                vz = diff[m & (zonemap == z)]
                if vz.size >= min_cell:
                    dz = _biweight(vz)
                    if np.isfinite(dz):
                        delta[(z, int(p))] = dz
                        sem[(z, int(p))] = float(np.std(vz)
                                                 / np.sqrt(vz.size))
    return delta, sem, nulls


def _stamp_skip(exposure_file, reason):
    """Sentinel-stamp CFP_JACK without touching pixel data."""
    from jwst.datamodels import ImageModel
    with ImageModel(exposure_file, memmap=False) as model:
        atomic_save(model, exposure_file,
                    header_updates=cfp.format(CFP_JACK=f'skipped ({reason})'))


def jackknife_step(exposure_file, field, step_config, overwrite=False,
                   status=None):
    """Apply the jackknife zero-point correction to a single exposure."""
    rootname = os.path.basename(exposure_file).removesuffix('.fits')

    if cfp.should_skip(exposure_file, 'CFP_JACK', rootname,
                       'jackknife', status, overwrite):
        return

    # Double-subtraction guard: an overwrite re-run cannot recompute the
    # correction (the sidecar may be gone and SCI already carries the first
    # pass). Refuse rather than corrupt; a real redo goes through
    # `reset --uncal` / a detector1 re-reduction.
    if overwrite:
        has = (status.has(exposure_file, 'CFP_JACK') if status is not None
               else cfp.has_step(exposure_file, 'CFP_JACK'))
        if has and not str(
                cfp.step_value(exposure_file, 'CFP_JACK')).startswith(
                    'skipped'):
            raise RuntimeError(
                f"{rootname}: CFP_JACK already applied; the correction is "
                f"not re-runnable in place. Rebuild from uncal "
                f"(`cfpipe nircam reset --uncal`) to redo it.")

    jump_path = os.path.join(os.path.dirname(exposure_file),
                             f'{rootname}_jump.fits')
    if not os.path.exists(jump_path):
        log(f"jackknife: {rootname} has no _jump.fits sidecar (legacy "
            f"reduction?); stamping skipped — a detector1 re-reduction is "
            f"required to correct it")
        _stamp_skip(exposure_file, 'no _jump.fits')
        return

    log(f"Running jackknife on {rootname}")

    from jwst.datamodels import ImageModel, RampModel, dqflags
    from jwst.ramp_fitting.ramp_fit_step import RampFitStep

    max_patterns = int(step_config.get('max_patterns', 256))
    min_pix_per_pattern = int(step_config.get('min_pixels_per_pattern', 2000))
    min_pix_per_cell = int(step_config.get('min_pixels_per_cell', 500))
    zones_cfg = int(step_config.get('zones', 8))
    bright_clip = float(step_config.get('bright_clip_sigma', 5.0))

    try:
        with ImageModel(exposure_file, memmap=False) as model:
            rate = np.asarray(model.data, dtype=np.float64)
            dq2d = np.asarray(model.dq)
            nints = int(model.meta.exposure.nints or 1)
            subarray = str(model.meta.subarray.name or 'FULL')
            noutputs = int(model.meta.exposure.noutputs or 4)
            gain_factor = model.meta.exposure.gain_factor

            if nints > 1:
                log(f"jackknife: {rootname} has NINTS={nints}; per-"
                    f"integration patterns are not supported — skipping")
                atomic_save(model, exposure_file, header_updates=cfp.format(
                    CFP_JACK=f'skipped (nints={nints})'))
                return

            with RampModel(jump_path, memmap=False) as jump:
                gdq = np.asarray(jump.groupdq[0], dtype=np.uint8)
                ngroups = gdq.shape[0]
                if ngroups > _MAX_NGROUPS or ngroups < 4:
                    log(f"jackknife: {rootname} NGROUPS={ngroups} outside "
                        f"validated range [4, {_MAX_NGROUPS}]; skipping")
                    atomic_save(model, exposure_file,
                                header_updates=cfp.format(
                                    CFP_JACK=f'skipped (ngroups={ngroups})'))
                    return

                pat, carries_satdnu = _patterns_from_groupdq(gdq)
                correctable = (pat != 0) & ~carries_satdnu

                # Candidate patterns, most-populous first, capped by config
                # and by available clean-pixel statistics.
                vals, cnts = np.unique(pat[correctable], return_counts=True)
                order = np.argsort(cnts)[::-1]

                # Clean calibration pixels: unflagged ramps, clean 2-D DQ,
                # finite rate, sigma-clipped against bright sources (there
                # is no SRCMASK yet at this stage), 4-px border trim.
                dnu2d = dqflags.pixel['DO_NOT_USE']
                sat2d = dqflags.pixel['SATURATED']
                clean = ((pat == 0) & ~carries_satdnu
                         & ((dq2d & (dnu2d | sat2d)) == 0)
                         & np.isfinite(rate))
                clean[:4, :] = clean[-4:, :] = 0
                clean[:, :4] = clean[:, -4:] = 0
                med = np.median(rate[clean])
                mad = np.median(np.abs(rate[clean] - med)) * 1.4826
                clean &= rate < med + bright_clip * mad

                n_cand = min(len(vals), max_patterns,
                             max(1, int(clean.sum()) // min_pix_per_pattern))
                cands = np.concatenate(
                    [[0], vals[order][:n_cand]]).astype(np.uint32)

                classmap = _class_map(rate.shape, len(cands))
                gdq_cal = np.zeros((1,) + gdq.shape, dtype=np.uint8)
                for k, p in enumerate(cands):
                    if p == 0:
                        continue
                    m = classmap == k
                    for g in range(ngroups):
                        if (p >> g) & 1:
                            gdq_cal[0, g][m] |= _JUMP

                jump.groupdq = gdq_cal
                # Match production ramp_fit exactly (detector1.py config;
                # suppress_one_group is the shared default).
                rate_cal_model, _ = RampFitStep.call(
                    jump, algorithm='OLS_C', maximum_cores='none')
                rate_cal = np.asarray(rate_cal_model.data, dtype=np.float64)
                del rate_cal_model

            # gain_scale ran on the canonical but not on the calibration
            # fit; reconcile before differencing. (No-op for full frame,
            # where the factor is absent/1.) The multiply must happen at
            # the canonical array's storage precision — production scales
            # the float32 SCI in place, so scaling our float64 promotion
            # instead would differ by float32 rounding and trip the exact
            # identity check below.
            gain_factor = float(gain_factor) if gain_factor else 1.0
            if gain_factor != 1.0:
                rate_cal = (rate_cal.astype(model.data.dtype)
                            * model.data.dtype.type(gain_factor)
                            ).astype(np.float64)

            # Paired identity check: on clean class-0 pixels the two fits
            # ran identical flags on identical data, so any nonzero
            # difference means the canonical SCI is not the detector1 rate
            # (config drift, mutated file) and the calibration is invalid.
            m0 = clean & (classmap == 0)
            ident = float(np.max(np.abs(rate_cal[m0] - rate[m0]))) \
                if m0.any() else np.inf
            if not ident == 0.0:
                log(f"jackknife: {rootname} paired identity check failed "
                    f"(max |rate_cal - rate| = {ident:.3e} on unflagged "
                    f"pixels); canonical is not the detector1 rate — "
                    f"skipping")
                atomic_save(model, exposure_file,
                            header_updates=cfp.format(
                                CFP_JACK='skipped (identity check failed)'))
                return

            n_zones_req = zones_cfg if (subarray == 'FULL'
                                        and noutputs == 4) else 1
            zonemap, n_zones = _zone_map(rate.shape, n_zones_req)

            diff = rate_cal - rate
            delta, sem, nulls = _measure_deltas(
                diff, classmap, zonemap, n_zones, clean, cands,
                min_pix_per_cell)

            # Apply: zonal delta where the cell had enough statistics,
            # frame-global fallback otherwise.
            corr = np.zeros_like(rate)
            n_corr = 0
            for p in cands:
                p = int(p)
                if (-1, p) not in delta:
                    continue
                sel = correctable & (pat == p)
                if not sel.any():
                    continue
                dvals = np.full(n_zones, delta[(-1, p)])
                for z in range(n_zones):
                    if (z, p) in delta:
                        dvals[z] = delta[(z, p)]
                corr[sel] = dvals[zonemap[sel]]
                n_corr += int(sel.sum())

            model.data = (rate - corr).astype(model.data.dtype)

            null_rms = float(np.sqrt(np.mean(np.square(nulls)))) \
                if nulls else 0.0
            n_flagged = int((pat != 0).sum())
            coverage = n_corr / n_flagged if n_flagged else 1.0
            mean_corr = float(np.mean(corr[correctable])) \
                if correctable.any() else 0.0

            stamp = (f'npat={len(delta)}, cand={len(cands) - 1}, '
                     f'cov={100 * coverage:.1f}%, zones={n_zones}, '
                     f'mean={mean_corr:.3e}, null={null_rms:.2e}, '
                     f'gain={gain_factor:g}')

            sidecar = {
                'version': 1,
                'ngroups': ngroups,
                'nints': nints,
                'gain_factor': gain_factor,
                'zones': n_zones,
                'n_flagged': n_flagged,
                'n_corrected': n_corr,
                'coverage': coverage,
                'null_rms': null_rms,
                'clean_pixels': int(clean.sum()),
                'patterns': [
                    {
                        'bits': p,
                        'groups': [g + 1 for g in range(ngroups)
                                   if (p >> g) & 1],
                        'n_flagged': int(((pat == p) & correctable).sum()),
                        'delta': {str(z): delta[(z, p)]
                                  for z in [-1] + list(range(n_zones))
                                  if (z, p) in delta},
                        'sem': {str(z): sem[(z, p)]
                                for z in [-1] + list(range(n_zones))
                                if (z, p) in sem},
                    }
                    for p in sorted({pp for _, pp in delta})
                ],
            }
            side_path = os.path.join(os.path.dirname(exposure_file),
                                     f'{rootname}_jackknife.json')
            with open(side_path + '.tmp', 'w') as f:
                json.dump(sidecar, f, indent=1)
            os.replace(side_path + '.tmp', side_path)

            atomic_save(model, exposure_file,
                        header_updates=cfp.format(CFP_JACK=stamp))
            log(f"jackknife done ({stamp}): {rootname}")

    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001 — one bad exposure must not kill the phase
        log(f"jackknife: {rootname} failed ({type(e).__name__}: {e}); "
            f"stamping skipped, SCI left untouched")
        try:
            _stamp_skip(exposure_file, f'error: {type(e).__name__}')
        except Exception:
            pass
