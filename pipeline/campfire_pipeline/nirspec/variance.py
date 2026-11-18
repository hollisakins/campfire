"""
The Poisson variance of a NIRSpec rate file, made independent of each pixel's own noise.

jwst's ramp fit sets a pixel's Poisson variance from that pixel's own counts, so a pixel that
came out low reports a small variance and gets a large weight: every fit weighted by
1 / variance is pulled low by about one electron per pixel per exposure (the background's
level by -1 % on PRISM and -3.9 % on G395M, a faint source on a frame with its background
removed by tens of percent). The plan and its validation are in
``scratch/claude/variance_fix/plan.md`` (2026-11-10, sections 3a and 5) and the design doc's
"Pixel variances" section.

The rule (``poisson_variance``), frozen 2026-11-10 (user):

1. Neighbors: the usable lit pixels (``lit``, no DO_NOT_USE, finite) within ``HALF`` = 5
   columns of the same detector row (the dispersion direction of every NIRSpec mode
   tested), the pixel itself left out.
2. Excess: (SCI - median SCI of the neighbors) / sqrt(median variance of the neighbors + VR).
3. The pixel's Poisson variance is the mean of jwst's variances of its neighbors, leaving
   out neighbors whose own excess is above ``EXCESS_SIGMA`` = 5. (The median of the
   neighbors reads 4-6 % low at these counts; this mean matches jwst's level within 0.5 %.)
4. Guard: a pixel keeps jwst's own variance when its excess is above ``EXCESS_SIGMA`` and
   the pixel directly above or below it (across the slit) has an excess above
   ``SUPPORT_SIGMA`` = 2: a line or a source, which always has light across the slit. A
   single high pixel (cosmic-ray residue, a hot pixel) has none and gets its neighbors'
   variance, so it no longer protects itself from outlier rejection with its own.

A lit pixel with no neighbor keeps its own value; unlit pixels (the stage-1 background
mask ``CFBKGMASK``) keep jwst's. jwst's own array is kept in the rate file as
``VAR_POISSON_JWST`` and every rewrite starts from it (``apply_to_rate``), so applying the
rule again gives the same arrays. ERR is rewritten as sqrt(VAR_POISSON + VAR_RNOISE).

The read noise (``read_noise_scale``, from ``nbr-mean-v2``): jwst's VAR_RNOISE is scaled by
the factor s that gives the background pixels (``CFBKGMASK``, after stage 1's background
subtraction) unit scatter in SCI / sqrt(VAR_POISSON + s VAR_RNOISE), the sigma-clipped standard
deviation. The factor is ``CFBKGRMS`` (VAR_RNOISE = jwst's x ``CFBKGRMS``, which
``masks.restore_pre_bkgsub`` divides back out). Stage 1 used to fit it against VAR_RNOISE
alone, so the scaled read noise already held the background pixels' Poisson noise and
sqrt(VP + VR) counted it twice: EMBER obs 24's ERR was 1.29x the scatter of the background
pixels and the extracted spectra's errors 1.26x their scatter.

Cards on the rate file: ``CFVARPS`` (method and version), ``CFVARHW``, ``CFVARXS``,
``CFVARSS`` (the constants) and ``CFVARNG`` (fraction of lit pixels guarded). Staleness
downstream follows ``CFVARPS``: stage 2a remakes a canonical whose card differs from its
rate's (``stage2``), master background step 1 keys on it (``MBKGVARP``), and stage 3
copies it into the ``_spec.fits``.
"""

import os
import warnings

import numpy as np

HALF = 5                 # neighbors within +-5 columns of the same row
EXCESS_SIGMA = 5.0       # a pixel more than this above its neighbors: left out as a neighbor, guarded if supported
SUPPORT_SIGMA = 2.0      # ... supported when the pixel above or below stands this far above its own neighbors
VERSION = 'nbr-mean-v2'  # v2: the read-noise rescale fitted with the Poisson variance (module doc)
RNOISE_SCALE_BOUNDS = (0.01, 1000.0)
MIN_BACKGROUND = 100     # background pixels needed to fit the read-noise scale
DO_NOT_USE = 1
EXT = 'VAR_POISSON_JWST'
CARDS = ('CFVARPS', 'CFVARHW', 'CFVARXS', 'CFVARSS', 'CFVARNG')
BLOCK = 128              # rows per block (memory: block x columns x 2 HALF values)


def _windows(a, valid, half, reduce, block=BLOCK):
    """``reduce`` (nanmedian / nanmean) over each pixel's valid neighbors within +-``half``
    along the rows, the pixel itself left out; NaN where there is none."""
    x = np.where(valid, a, np.nan).astype(np.float64)
    out = np.full(x.shape, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)          # all-NaN windows
        for r0 in range(0, x.shape[0], block):
            pad = np.pad(x[r0:r0 + block], ((0, 0), (half, half)), constant_values=np.nan)
            st = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1, axis=1)
            st = np.delete(st, half, axis=-1)
            out[r0:r0 + block] = reduce(st, axis=-1)
    return out


def neighbour_median(a, valid, half=HALF):
    """Per pixel, the median of the ``valid`` pixels within +-``half`` columns of the same row,
    the pixel itself left out (NaN where there are none)."""
    return _windows(a, valid, half, np.nanmedian)


def neighbour_mean(a, valid, half=HALF):
    """``neighbour_median`` with the mean."""
    return _windows(a, valid, half, np.nanmean)


def excess(sci, vp, vr, valid, half=HALF):
    """How far each pixel stands above its neighbors (module doc, 2), in sigma; NaN where it
    has no neighbor."""
    smed = neighbour_median(sci, valid, half)
    vmed = neighbour_median(vp, valid, half)
    with np.errstate(invalid='ignore', divide='ignore'):
        return (sci - smed) / np.sqrt(vmed + vr)


def poisson_variance(vp_jwst, sci, vr, dq, lit, half=HALF, excess_sigma=EXCESS_SIGMA, support_sigma=SUPPORT_SIGMA):
    """The rule of the module doc on one frame. Returns (variance, guarded mask)."""
    vp_jwst = np.asarray(vp_jwst, float)
    sci = np.asarray(sci, float)
    vr = np.asarray(vr, float)
    lit = np.asarray(lit, bool)
    good = ((np.asarray(dq) & DO_NOT_USE) == 0) & np.isfinite(vp_jwst) & np.isfinite(sci) & np.isfinite(vr)
    valid = good & lit
    exc = excess(sci, vp_jwst, vr, valid, half)
    high = np.nan_to_num(exc, nan=-np.inf) > excess_sigma
    mean = neighbour_mean(vp_jwst, valid & ~high, half)
    e = np.nan_to_num(exc, nan=-np.inf)
    across = np.full(e.shape, -np.inf)
    across[1:] = e[:-1]                                  # the row below
    across[:-1] = np.maximum(across[:-1], e[1:])         # the row above
    guarded = valid & high & (across > support_sigma)
    out = np.where(lit & np.isfinite(mean) & ~guarded, mean, vp_jwst)
    return out, guarded


def read_noise_scale(sci, vp, vr, background, bounds=RNOISE_SCALE_BOUNDS):
    """The factor s on the read-noise variance that gives the ``background`` pixels unit scatter
    in sci / sqrt(vp + s vr) (sigma-clipped standard deviation; module doc), by bisection in
    log s within ``bounds`` (a bound when the scatter cannot reach 1 inside them; 1, jwst's read
    noise, with fewer than ``MIN_BACKGROUND`` background pixels)."""
    from astropy.stats import sigma_clipped_stats
    sci, vp, vr = (np.asarray(a, float) for a in (sci, vp, vr))
    ok = np.asarray(background, bool) & np.isfinite(sci) & np.isfinite(vp) & np.isfinite(vr) & (vr > 0)
    if ok.sum() < MIN_BACKGROUND:
        return 1.0
    s_, p_, r_ = sci[ok], np.clip(vp[ok], 0, None), vr[ok]

    def scatter(s):
        return float(sigma_clipped_stats(s_ / np.sqrt(p_ + s * r_))[2])

    lo, hi = np.log(bounds[0]), np.log(bounds[1])
    if scatter(np.exp(lo)) <= 1:
        return float(bounds[0])
    if scatter(np.exp(hi)) >= 1:
        return float(bounds[1])
    for _ in range(40):                                   # scatter falls as s grows
        mid = 0.5 * (lo + hi)
        if scatter(np.exp(mid)) > 1:
            lo = mid
        else:
            hi = mid
    return float(np.exp(0.5 * (lo + hi)))


def apply_to_rate(rate_file):
    """Rewrite a background-subtracted rate file's VAR_RNOISE, VAR_POISSON and ERR (module doc),
    in place, from jwst's arrays (``VAR_POISSON_JWST``, stored on the first call; VAR_RNOISE /
    ``CFBKGRMS``), the current DQ and the stage-1 background mask (``CFBKGMASK``: lit = not in
    it). Stamps the cards and the new ``CFBKGRMS``. Raises ValueError for a rate file without
    ``CFBKGMASK`` (stage 1's background subtraction has not run). Returns the fraction of lit
    pixels guarded."""
    from astropy.io import fits
    with fits.open(rate_file, mode='update') as h:
        if 'CFBKGMASK' not in h:
            raise ValueError(f'{os.path.basename(rate_file)}: no CFBKGMASK (stage 1 background subtraction has not run)')
        if EXT not in h:
            h.append(fits.ImageHDU(np.asarray(h['VAR_POISSON'].data, np.float32).copy(), name=EXT))
        hdr = h[0].header
        vp_j = np.asarray(h[EXT].data, float)
        sci = np.asarray(h['SCI'].data, float)
        vr_j = np.asarray(h['VAR_RNOISE'].data, float) / float(hdr.get('CFBKGRMS', 1.0))
        dq = np.asarray(h['DQ'].data)
        lit = ~np.asarray(h['CFBKGMASK'].data, bool)
        good = (dq & DO_NOT_USE) == 0
        # background pixels are unlit, where the rule leaves jwst's Poisson variance: s first
        s = read_noise_scale(sci, vp_j, vr_j, ~lit & good)
        vr = vr_j * s
        h['VAR_RNOISE'].data[...] = vr.astype(h['VAR_RNOISE'].data.dtype)
        hdr['CFBKGRMS'] = (s, 'VAR_RNOISE / jwst read-noise variance')
        vp, guarded = poisson_variance(vp_j, sci, vr, dq, lit)
        h['VAR_POISSON'].data[...] = vp.astype(h['VAR_POISSON'].data.dtype)
        with np.errstate(invalid='ignore'):
            h['ERR'].data[...] = np.sqrt(vp + vr).astype(h['ERR'].data.dtype)
        n_lit = int(np.sum(lit & good))
        frac = float(guarded.sum()) / max(n_lit, 1)
        hdr['CFVARPS'] = (VERSION, 'Poisson variance: mean of row neighbors')
        hdr['CFVARHW'] = (HALF, 'its neighbors: +- columns of the same row')
        hdr['CFVARXS'] = (EXCESS_SIGMA, 'sigma above neighbors: left out / guarded')
        hdr['CFVARSS'] = (SUPPORT_SIGMA, 'guard: sigma of the pixel above or below')
        hdr['CFVARNG'] = (round(frac, 6), 'fraction of lit pixels keeping jwst variance')
    return frac


def stamp(path):
    """The ``CFVARPS`` card of a file's primary header, '' when it has none."""
    from astropy.io import fits
    try:
        return str(fits.getheader(path).get('CFVARPS', '') or '')
    except OSError:
        return ''


def is_current(rate_file):
    """Whether a rate file's variance was made by this ``VERSION``."""
    return stamp(rate_file) == VERSION


def ensure_variance(rate_files, log=None):
    """``apply_to_rate`` on every background-subtracted rate file whose variance is missing or
    from another ``VERSION`` (stage 2a's entry: existing rate files are brought up to date without
    rerunning stage 1). Returns the files rewritten."""
    from astropy.io import fits
    log = log or (lambda *a: None)
    done = []
    for f in rate_files:
        h = fits.getheader(f)
        if not bool(h.get('CFBKGSUB', False)) or is_current(f):
            continue
        with fits.open(f) as hd:
            if 'CFBKGMASK' not in hd:
                continue
        frac = apply_to_rate(f)
        log(f'{os.path.basename(f)}: Poisson variance from its row neighbors ({VERSION}), '
            f'{100 * frac:.3f} % of lit pixels keep their own')
        done.append(f)
    return done


def stamps_of(paths):
    """The distinct ``CFVARPS`` cards of a set of files, joined by ',' ('none' when no file has
    one): stage 3's provenance card on the ``_spec.fits``."""
    s = sorted({stamp(p) for p in paths} - {''})
    return ','.join(s) if s else 'none'
