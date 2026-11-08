"""NIRSpec MSA master background: the sky of one exposure from its empty shutters
(docs/design-nirspec-master-background.md §4, phase P1b).

One product per rate file, ``<root>_<nod>_<nrsN>_mbkg.fits`` beside it:

* ``MBKG1D`` — the sky surface brightness B(λ) of this exposure × detector in MJy/sr
  (uniform calibration, at the open-shutter centre, no bar shadow), its statistical
  error, the number of shutters per bin, the pooled sky of the exposure group and the
  ``jwst_backgrounds`` prediction when that package is installed;
* ``SHUTTERS`` — one row per empty shutter that was offered: where it is (slitlet,
  quadrant, shutter offset from the slitlet's source, the slit it came from), its scale
  against the sky, χ², the longest line-like run, ``clean`` and the ``reason``;
* ``MBKG1DR`` — the same sky in count-rate units (DN/s at the open-shutter centre, no
  flat or photometric calibration), measured from the clean shutters' core pixels over
  their whole footprints, so it reaches past the calibrated range to the disperser's
  wavelength cutoffs (``coverage.WAVELENGTH_CUTOFFS``);
* ``MBKG2D`` / ``MBKG2DN`` / ``MBKG2DDQ`` — the sky painted back onto the detector in the
  rate frame (DN/s), every covering footprint's sky summed where footprints overlap (on
  the gratings 38 % of the detector lies under more than one trace, §9), the light of one
  spot on the MSA counted once (``merge_painted``): inside the calibrated range through
  each slit's own calibration, past it (the columns before a cutout's first calibrated
  column and beyond its last, out to the cutoffs) as ``MBKG1DR`` times the bar shadow, and
  any slitlet without a slit and the failed-open shutters the same way over their whole
  footprint (each failed-open shutter at the fraction of the sky it is measured to pass;
  none for one open only for the internal lamp); the number of MSA spots painted per
  pixel; and flags: 1 nothing covers the
  pixel, 2 a covering footprint was not painted, 8 a failed-open shutter's sky is painted
  there (light above it, e.g. an object behind the shutter, is contamination).

The inputs are what stage 2a already produced: the per-source canonicals (their empty
shutters) and the background-only slitlets of the ``bkgslits/`` file. Pixels are read
through ``barshadow_calibration.pixel_samples`` (uniform calibration, the source and
every catalogued neighbour padded away, DQ, one footprint only on the tail-aware
coverage counter); only the shutter core |f| ≤ ``core_frac`` enters the 1-D sky, each pixel
divided by the bar-shadow table there (``core_to_centre``: the table is 1 over |f| < 0.15 and
0.97-0.99 at the core's edge, and the painting multiplies by it, so the sky is held at the
open-shutter centre). Shutters are combined with equal weights (``combine``: a pixel's
variance follows its own measured value, so inverse-variance weights pull the sky low).
Selection is the symmetric
scale-and-reject iteration of §4.2 step 4 against the per-bin median seed. The sky is
measured per exposure × detector and tested against the pooled sky of the other exposures
of its group (every nod, both detectors; ``pool_check``, band by band, with the
shutter-to-shutter scatter as the error): an exposure consistent with them is painted with
the group's pool, which has several times its shutters and so a fraction of its noise (a
G395M exposure × detector has one to five shutters per bin at the ends of its range); one
that differs (ember_uds_p2's second nod sits 18 % above the others at the 3 µm sky
minimum) keeps its own sky. The decision, the worst band and the thresholds are in the
header (``MBKGSRC``: pool / exposure / group when too few clean shutters; ``MBKGPDEC`` ...),
the exposure's own sky is kept in ``MBKG1D`` either way (``exposure_surf_bright``), and the
scale and offset against the pool are recorded (``MBKGSCL``, ``MBKGOFF``). Per-quadrant scales against the
global sky are recorded as ``MBKGQ1``..``MBKGQ4`` (the ±2.5 % quadrant term of §9) and
applied by ``sky_for_slit``.

``sky_for_slit`` is the consumer's entry point: the sky on one slit's cutout, in that
slit's own frame and units. The slit's own slitlet is evaluated analytically (the bar
shadow from the empirical table or CRDS, the pathloss ratio for a point-source
canonical, the pixel area for MJy per pixel) and is what the painter uses; what a
consumer subtracts is the painted total on the cutout, every covering slitlet's sky,
brought into the slit's frame through the exact per-pixel calibration factor
``SCI / rate`` (every step after ``extract_2d`` is multiplicative per pixel, §4.4),
with its variance including the ``MBKGEFLR`` error floor.
"""
import os
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.nirspec import barshadow_calibration as B
from campfire_pipeline.nirspec import crds_refs as CR
from campfire_pipeline.nirspec.coverage import (coverage_from_wcs, cutoffs_for, default_cache_dir, cache_path,
                                                 geometry_cache_path, slit_geometry_extended)

ALGORITHM_VERSION = '1.8'    # 1.1: painted past the calibrated range; 1.2: count-rate sky from step 1 (2026-10-31);
#                               1.3: equal shutter weights, sky divided to the open-shutter centre (2026-10-31)
#                               1.4: the group pool painted unless the exposure differs (pool_check) (2026-10-31)
#                               1.5: step-1 tables read from the rate file, no MBKG1DR copy, 2-D extensions
#                                    compressed losslessly, shared shutters taken from the lowest source id (2026-11-01)
#                               1.6: failed-open shutters (and slitlets without a slit) painted, each failed-open
#                                    shutter at its measured transmission; overlapping light summed unless it
#                                    comes through the same MSA spot (merge_painted) (2026-11-02)
#                               1.7: failed-open shutters get a commanded slitlet's margin and the real shutter
#                                    pitch (coverage.failed_open_slitlets) (2026-11-02)
#                               1.8: shutters holding a secondary source left out of the empty shutters, as in
#                                    step 1; a group's products are made together (2026-11-08)
# The derivation's settings. Fixed in the code since the 2026-11-06 configuration audit
# (Hollis: "30 master bkg thresholds is a *ton*"); the only one left in the config is
# USER_OPTIONS. Every product records the ones that shape it in its header.
SETTINGS = dict(
    source_pad_px=3.0,        # rows beyond the source shutter's edges dropped (§9: 3 px)
    core_frac=0.2,            # |f| of the shutter core that measures the 1-D sky
    bin_pixels=2.0,           # detector pixels per wavelength bin
    min_pixels=20,            # pixels a shutter needs
    min_bins=10,              # bins a shutter needs
    clip_sigma=3.0,           # per-bin clipping when shutters are combined
    line_sigma=4.0,           # a run of bins above this many sigma is a line ...
    line_min_bins=3,          # ... this many bins long
    excess_min=0.05,          # reject a shutter brighter than the sky by this (and 3 sigma)
    low_min=0.30,             # reject a shutter fainter than the sky by this (and 3 sigma)
    n_iter=3,                 # scale-and-reject iterations
    min_shutters=5,           # below this the exposure takes the group's pooled sky
    # each exposure x detector is painted with its group's pooled sky unless its own clean
    # shutters differ from the pool of the other exposures (pool_check) in one of pool_bands
    # wavelength bands by more than pool_tol AND pool_nsigma (shutter-to-shutter scatter)
    pool_tol=0.02,
    pool_nsigma=3.0,
    pool_bands=8,
    rate_excess_min=0.15,     # step 1 (count rate, no flat: ~7 % grey per slitlet): excess threshold
    rate_low_min=0.30,        # step 1: low-throughput threshold
    stuck_a_cut=0.3,          # step 1 stuck call: median sky amplitude below this ...
    stuck_min_n=2,            # ... from at least this many measurements (nods x detectors) ...
    stuck_guard=0.5,          # ... and the two detectors' medians within this
    error_floor=0.03,         # fraction of the sky added in quadrature downstream (MBKGEFLR)
    same_spot_px=1.0,         # 2-D: two slitlets' light on a pixel within this many columns of dispersion is one MSA spot
    failed_open_min_pixels=200,   # 2-D: core pixels outside source shutters a failed-open transmission needs (else 1)
    failed_open_nod_tol=0.1,      # ... nods within this of their median give one value per shutter and detector
)
# [nirspec.stage2] keys still read from the config, with their defaults
_PREFIX = 'master_bkg_'
USER_OPTIONS = dict(
    master_bkg_plot=True,     # the QA PDF of every exposure x detector (no product depends on it)
)
REASONS = ('clean', 'too_few_bins', 'low_throughput', 'continuum_excess', 'line_excess')
DQ_UNCOVERED = 1          # no slitlet footprint on the pixel
DQ_UNPAINTED = 2          # a footprint covers the pixel but no slit was painted for it
DQ_TAIL = 4               # retired (products before 2026-10-30 flagged a trace's unpainted tail; tails are painted now)
DQ_FAILED_OPEN = 8        # a failed-open shutter's sky is painted here (light above it is contamination)


def options(stage_config):
    """The derivation's settings: ``SETTINGS`` plus the user-facing ``master_bkg_*`` keys of
    ``[nirspec.stage2]`` (``USER_OPTIONS``) without their prefix."""
    o = dict(SETTINGS)
    o.update({k[len(_PREFIX):]: stage_config.get(k, v) for k, v in USER_OPTIONS.items()})
    return o


@dataclass
class ShutterSpectrum:
    """One empty shutter's spectrum on the exposure's wavelength grid."""
    rate_base: str
    source_id: int          # the canonical's source; -slitlet_id for a background slitlet
    slitlet_id: int
    quadrant: int
    k: int                  # shutter offset from the slitlet's 'x' shutter
    n_pix: int
    sb: np.ndarray
    err: np.ndarray
    n: np.ndarray
    scale: float = np.nan
    scale_err: float = np.nan
    chi2_red: float = np.nan
    line_run: int = 0
    clean: bool = False
    reason: str = 'pending'

    @property
    def slit(self):
        return str(self.source_id) if self.source_id > 0 else f'BKG{-self.source_id}'


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def rate_base_of(rate_path):
    return os.path.basename(rate_path)[:-len('_rate.fits')]


def product_path(obs, rate_base):
    return os.path.join(obs.workspace_dir, f'{rate_base}_mbkg.fits')


def qa_path(obs, rate_base):
    return os.path.join(obs.workspace_dir, f'{rate_base}_mbkg_qa.pdf')


def discover_inputs(obs):
    """``rate_base -> [Canonical]`` for every rate file of the observation: its stage-2a
    canonicals (one slit each) and the background-only slitlets of its ``bkgslits/``
    file (``source_id = -slitlet_id``, one entry per slit)."""
    products_dir = os.path.dirname(os.path.dirname(obs.workspace_dir))
    by_rate = defaultdict(list)
    for c in B.discover(obs.name, None, products_dir):
        by_rate[c.rate_base].append(c)
    for rate in obs.rate_files:
        base = rate_base_of(rate)
        by_rate.setdefault(base, [])
        if not obs.bkgslits_dir:
            continue
        bpath = os.path.join(obs.bkgslits_dir, f'{base}_bkgslits.fits')
        if not os.path.exists(bpath):
            continue
        with fits.open(bpath, memmap=False) as h:
            ph = h[0].header
            n = int(ph.get('CFNBKGSL', 0) or 0)
            root, nod, det = base.rsplit('_', 2)
            for i in range(1, n + 1):
                hs = h['SCI', i].header
                name = str(hs.get('SLTNAME', '')).strip()
                slitlet = int(name) if name.isdigit() else -i
                by_rate[base].append(B.Canonical(
                    path=bpath, obs=obs.name, workspace=obs.workspace_dir, root=root, nod=nod, detector=det,
                    source_id=-slitlet, grating=str(ph['GRATING']).upper(), filter=str(ph['FILTER']).upper(),
                    msametid=int(ph.get('MSAMETID', -1)), dither=int(ph.get('PATT_NUM', 0) or 0), extver=i))
    return dict(by_rate)


def inputs_stamp(canonicals):
    """Newest modification time of the inputs, the skip key beside the rate file's."""
    paths = sorted({c.path for c in canonicals})
    return max((os.path.getmtime(p) for p in paths), default=0.0)


def product_current(path, rate_path, canonicals, stuck_mtime, bs_name=None, bs_stamp=None, step1_path=None):
    """True when ``path`` was derived from these inputs by this algorithm (and, when
    ``bs_name`` / ``bs_stamp`` are given, with that bar-shadow table, of that
    derivation date, in the 2-D expansion; when ``step1_path`` is given, from that
    step-1 product as it is now)."""
    if not os.path.exists(path):
        return False
    try:
        hdr = fits.getheader(path)
    except Exception:
        return False
    rate_hdr = fits.getheader(rate_path)
    return (str(hdr.get('CFMBKGV')) == ALGORITHM_VERSION
            and str(hdr.get('MBKGBKDT')) == str(rate_hdr.get('CFBKGDT'))
            and abs(float(hdr.get('MBKGSTK', -1)) - float(stuck_mtime or 0)) < 1e-3
            and abs(float(hdr.get('MBKGINMT', -1)) - inputs_stamp(canonicals)) < 1e-3
            and int(hdr.get('MBKGNIN', -1)) == len(canonicals)
            and (bs_name is None or str(hdr.get('MBKGBSRF')) == bs_name)
            and (bs_stamp is None or str(hdr.get('MBKGBSDT')) == bs_stamp)
            and (step1_path is None or str(hdr.get('MBKGRDT')) == _step1_date(step1_path)))


def _step1_date(path):
    from campfire_pipeline.nirspec.master_bkg_rate import step1_header
    hdr = step1_header(path)
    return str(hdr.get('MBKGDATE')) if hdr is not None else 'None'


# ---------------------------------------------------------------------------
# The wavelength grid and the per-shutter spectra
# ---------------------------------------------------------------------------

def edges_from_rows(central_lams, bin_pixels):
    """Bin edges with ``bin_pixels`` detector pixels per bin, following the local
    dispersion measured on the central rows of a few slits."""
    lam, dl = [], []
    for row in central_lams:
        row = np.sort(row[np.isfinite(row)])
        if len(row) < 10:
            continue
        lam.append(row[1:]); dl.append(np.diff(row))
    lam = np.concatenate(lam); dl = np.concatenate(dl)
    good = dl > 0
    lam, dl = lam[good], dl[good]
    order = np.argsort(lam); lam, dl = lam[order], dl[order]
    qs = np.quantile(lam, np.linspace(0, 1, 61))
    centres, disp = [], []
    for lo, hi in zip(qs[:-1], qs[1:]):
        m = (lam >= lo) & (lam < hi)
        if m.sum() > 5:
            centres.append(0.5 * (lo + hi)); disp.append(np.median(dl[m]))
    centres, disp = np.array(centres), np.array(disp)
    edges = [lam.min()]
    while edges[-1] < lam.max():
        step = bin_pixels * np.interp(edges[-1], centres, disp)
        edges.append(edges[-1] + max(step, 1e-6))
    return np.array(edges)


def wavelength_grid(canonicals, bin_pixels, n_sample=6):
    """Bin edges for one disperser from the central rows of the first ``n_sample``
    slits per detector (deterministic for a given set of inputs)."""
    from jwst.datamodels import MultiSlitModel
    rows = []
    for det in ('nrs1', 'nrs2'):
        for c in sorted((c for c in canonicals if c.detector == det), key=lambda c: (c.path, c.extver))[:n_sample]:
            with MultiSlitModel(c.path) as m:
                g = B.slit_geometry(m.slits[c.extver - 1])
            rows.append(g['lam'][g['lam'].shape[0] // 2])
    return edges_from_rows(rows, bin_pixels)


def bin_spectrum(lam, sb, var, edges):
    """Median surface brightness per wavelength bin, its error (the larger of the
    propagated and the empirical estimate) and the pixel count."""
    nb = len(edges) - 1
    idx = np.digitize(lam, edges) - 1
    ok = (idx >= 0) & (idx < nb)
    med = np.full(nb, np.nan); err = np.full(nb, np.nan); cnt = np.zeros(nb, int)
    if ok.sum() == 0:
        return med, err, cnt
    idx, vals, v = idx[ok], sb[ok], var[ok]
    order = np.argsort(idx, kind='stable')
    idx, vals, v = idx[order], vals[order], v[order]
    bounds = np.flatnonzero(np.diff(idx)) + 1
    for ci, cv, cvar in zip(np.split(idx, bounds), np.split(vals, bounds), np.split(v, bounds)):
        b = ci[0]; n = len(cv)
        cnt[b] = n
        med[b] = np.median(cv)
        emp = 1.2533 * np.median(np.abs(cv - med[b])) * 1.4826 / np.sqrt(n) if n > 2 else np.inf
        prop = 1.2533 * np.sqrt(np.mean(cvar) / n)
        err[b] = prop if not np.isfinite(emp) else max(emp, prop)
    return med, err, cnt


def core_to_centre(table, seg, yseg, lam):
    """The bar shadow at core pixels, by which their surface brightness is divided to give
    the sky at the open-shutter centre, the level the table is normalised to (1 over
    |f| < ``CENTRE_FRAC``). The core reaches |f| = ``core_frac`` where the shadow is
    already 0.97-0.99, and the painting multiplies by the table, so a sky measured on the
    undivided core would carry that shadow twice (§9, 2026-10-31). NaN where the pixel
    measures no segment."""
    bs = np.full(np.shape(lam), np.nan)
    inside = np.asarray(seg) >= 0
    if inside.any():
        bs[inside] = B.evaluate_table(table, np.asarray(seg)[inside], np.asarray(yseg)[inside], np.asarray(lam)[inside])
    bs[~(bs > 0)] = np.nan
    return bs


def _worker_samples(args):
    c, counter_path, edges, opt, bs_path = args
    counter = np.load(counter_path) if counter_path else None
    try:
        p = B.pixel_samples(c, counter, opt['source_pad_px'])
    except Exception as e:
        return c.key, [], f'{type(e).__name__}: {e}'
    core = p['is_open'] & (np.abs(p['f']) <= opt['core_frac'])
    bs = np.full(p['lam'].shape, np.nan)
    if core.any():
        bs[core] = core_to_centre(B.load_table(bs_path), p['seg'][core], p['yseg'][core], p['lam'][core])
    core &= np.isfinite(bs)
    out = []
    for kk in np.unique(p['k'][core]):
        sel = core & (p['k'] == kk)
        if sel.sum() < opt['min_pixels']:
            continue
        sb, err, n = bin_spectrum(p['lam'][sel], p['sb'][sel] / bs[sel], p['var'][sel] / bs[sel] ** 2, edges)
        if np.isfinite(sb).sum() < opt['min_bins']:
            continue
        out.append(ShutterSpectrum(rate_base=c.rate_base, source_id=c.source_id, slitlet_id=p['slitlet_id'],
                                   quadrant=p['quadrant'], k=int(kk), n_pix=int(sel.sum()), sb=sb, err=err, n=n))
    return c.key, out, None


def shutter_spectra(canonicals, counters, edges_of, opt, processes, bs_paths):
    """Pass 1 over every slit: the empty shutters' spectra at the open-shutter centre
    (``bs_paths``: the bar-shadow table per ``(grating, filter)``), grouped by rate file."""
    from multiprocessing import Pool
    args = [(c, counters.get(c.rate_path), edges_of[(c.grating, c.filter)], opt, bs_paths[(c.grating, c.filter)])
            for c in canonicals]
    found, failures = [], []

    def take(key, out, err):
        if err:
            failures.append((key, err)); return
        found.extend(out)

    if processes > 1:
        with Pool(processes) as pool:
            for key, out, err in pool.imap_unordered(_worker_samples, args, chunksize=4):
                take(key, out, err)
    else:
        for a in args:
            take(*_worker_samples(a))
    # two sources of one slitlet offer the same shutters, from cutouts that can differ by a
    # pixel: keep the lowest source id's, so the choice does not depend on which worker
    # finished first
    by_rate, seen = defaultdict(list), set()
    for s in sorted(found, key=lambda s: (s.rate_base, s.slitlet_id, s.k, s.source_id)):
        tag = (s.rate_base, s.slitlet_id, s.k)
        if tag not in seen:
            seen.add(tag)
            by_rate[s.rate_base].append(s)
    return by_rate, failures


# ---------------------------------------------------------------------------
# Selection (design §4.2 step 4)
# ---------------------------------------------------------------------------

def combine(spectra, clip_sigma):
    """Sigma-clipped mean over shutters per bin, every shutter weighted equally ->
    (sb, err, n_shutters).

    Equal weights on purpose: a shutter's error in a bin comes from its one or two
    pixels' variances, which jwst derives from each pixel's own measured slope, so a
    pixel that fluctuated low reports a small variance and inverse-variance weights
    favour the shutters that came out low (G395M sky 2 % low on held-out shutters, PRISM
    0.2 %; §9, 2026-10-31). Each shutter takes the bin's median error, for the clip and
    the weight alike; the error is that of the mean, median error / sqrt(n)."""
    if not spectra:
        return None
    S = np.array([s.sb for s in spectra], float); E = np.array([s.err for s in spectra], float)
    ok = np.isfinite(S) & np.isfinite(E) & (E > 0)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        e_bin = np.nanmedian(np.where(ok, E, np.nan), axis=0)
    E = np.where(ok, e_bin[None, :], np.nan)
    with np.errstate(invalid='ignore', divide='ignore'):
        W = 1.0 / E ** 2
    W[~np.isfinite(S) | ~np.isfinite(W)] = 0
    S = np.where(W > 0, S, np.nan)
    for _ in range(5):
        with np.errstate(invalid='ignore', divide='ignore'), warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            med = np.nanmedian(S, axis=0)
            mad = 1.4826 * np.nanmedian(np.abs(S - med), axis=0)
            dev = np.abs(S - med) > clip_sigma * np.maximum(mad, E)
        if not np.any(dev & (W > 0)):
            break
        W[dev] = 0; S[dev] = np.nan
    with np.errstate(invalid='ignore', divide='ignore'):
        wsum = np.nansum(W, axis=0)
        mean = np.nansum(W * S, axis=0) / wsum
        err = 1.0 / np.sqrt(wsum)
    n = np.sum(W > 0, axis=0)
    mean[n == 0] = np.nan; err[n == 0] = np.nan
    return mean, err, n


def fit_scale(sb, err, ref):
    """Weighted least-squares scale a (sb ~ a * ref[0]) -> (a, sigma_a, chi2_red, z, n_bins)."""
    m = np.isfinite(sb) & np.isfinite(ref[0]) & np.isfinite(err) & np.isfinite(ref[1]) & (ref[0] != 0)
    if m.sum() < 3:
        return np.nan, np.nan, np.nan, None, int(m.sum())
    r, e2 = ref[0][m], err[m] ** 2 + ref[1][m] ** 2
    w = 1.0 / e2
    a = np.sum(w * sb[m] * r) / np.sum(w * r * r)
    sa = 1.0 / np.sqrt(np.sum(w * r * r))
    z = np.full_like(sb, np.nan)
    z[m] = (sb[m] - a * r) / np.sqrt(e2)
    chi2 = np.nansum(z[m] ** 2) / max(m.sum() - 1, 1)
    return a, sa, chi2, z, int(m.sum())


def longest_run(z, thresh):
    """Longest run of consecutive bins with z > thresh (an emission line's shape)."""
    best = cur = 0
    for v in z:
        if np.isfinite(v) and v > thresh:
            cur += 1; best = max(best, cur)
        else:
            cur = 0
    return best


def median_seed(spectra):
    S = np.array([s.sb for s in spectra], float)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        sb = np.nanmedian(S, axis=0)
        n = np.sum(np.isfinite(S), axis=0)
        err = 1.2533 * 1.4826 * np.nanmedian(np.abs(S - sb), axis=0) / np.sqrt(np.maximum(n, 1))
    return sb, err, n


def select_clean(spectra, seed, opt):
    """Iterate the scale fits against the reference, marking every shutter; returns the
    final combined sky ``(sb, err, n_shutters)`` or None when nothing is clean."""
    ref = seed
    for _ in range(opt['n_iter']):
        for s in spectra:
            a, sa, chi2, z, nb = fit_scale(s.sb, s.err, ref)
            s.scale, s.scale_err, s.chi2_red = a, sa, chi2
            s.line_run = longest_run(z, opt['line_sigma']) if z is not None else 0
            if nb < opt['min_bins']:
                s.clean, s.reason = False, 'too_few_bins'
            elif s.line_run >= opt['line_min_bins']:
                s.clean, s.reason = False, 'line_excess'          # a run of excess bins: a line, whatever the scale
            elif a < 1 - opt['low_min'] and (1 - a) > 3 * sa:
                s.clean, s.reason = False, 'low_throughput'
            elif a > 1 + opt['excess_min'] and (a - 1) > 3 * sa:
                s.clean, s.reason = False, 'continuum_excess'
            else:
                s.clean, s.reason = True, 'clean'
        new = combine([s for s in spectra if s.clean], opt['clip_sigma'])
        if new is None:
            return None
        ref = new
    return ref


def quadrant_scales(spectra, sky, opt):
    """Scale of each quadrant's clean shutters against the global sky, ``{q: (a, n)}``;
    NaN with fewer than three shutters."""
    out = {}
    for q in (1, 2, 3, 4):
        qs = [s for s in spectra if s.clean and s.quadrant == q]
        if len(qs) < 3:
            out[q] = (np.nan, len(qs)); continue
        comb = combine(qs, opt['clip_sigma'])
        a, *_ = fit_scale(comb[0], comb[1], sky)
        out[q] = (float(a), len(qs))
    return out


def scale_offset(sky, pool):
    """``sky = a * pool + b`` by weighted least squares -> (a, a_err, b, b_err, n)."""
    m = np.isfinite(sky[0]) & np.isfinite(pool[0]) & np.isfinite(sky[1]) & np.isfinite(pool[1])
    if m.sum() < 10:
        return np.nan, np.nan, np.nan, np.nan, int(m.sum())
    w = 1.0 / (sky[1][m] ** 2 + pool[1][m] ** 2)
    A = np.vstack([pool[0][m], np.ones(m.sum())]).T * np.sqrt(w)[:, None]
    coef, *_ = np.linalg.lstsq(A, sky[0][m] * np.sqrt(w), rcond=None)
    cov = np.linalg.inv(A.T @ A)
    return float(coef[0]), float(np.sqrt(cov[0, 0])), float(coef[1]), float(np.sqrt(cov[1, 1])), int(m.sum())


def pool_check(clean, pool, centres, opt):
    """Is this exposure's sky consistent with the pooled sky of the other exposures?

    ``clean`` are the exposure's clean shutters, ``pool`` the ``combine`` of every other
    exposure of the group (leave-one-out, so the exposure is not part of its own
    reference). The finite bins are cut into ``pool_bands`` contiguous bands of equal
    size; in each band every shutter gives the ratio of its summed values to the pool's
    over the bins it covers (at least three), and the band's deviation is the median ratio
    minus one with the error of that median (from the shutters' MAD, so the real
    shutter-to-shutter spread is in it, floored at their propagated errors; a band needs
    ``min_shutters`` shutters). A band differs when it is off by more than
    ``pool_tol`` and by more than ``pool_nsigma`` sigma: the first keeps a 0.3 % prism
    difference that is formally 3 sigma from counting, the second keeps a noisy grating band
    from counting. Bands are what catch a local difference (ember_uds_p2 nod 2: +18 % at the
    3 um sky minimum and nowhere else, §9) that one overall scale would dilute.

    Returns ``dict(differs, dev, nsig, lam_lo, lam_hi, n_bands, verdict)``: the band with the
    largest |deviation| among those that differ, else among all tested; ``differs`` is None
    when no band could be tested (fewer than ``min_shutters`` shutters in every band)."""
    out = dict(differs=None, dev=np.nan, nsig=np.nan, lam_lo=np.nan, lam_hi=np.nan, n_bands=0, verdict='untested')
    if pool is None or not clean:
        return out
    P = np.asarray(pool[0], float)
    S = np.array([s.sb for s in clean], float)
    E = np.array([s.err for s in clean], float)
    fin = np.flatnonzero(np.isfinite(P) & (P > 0) & np.isfinite(S).any(axis=0))
    if fin.size < 3:
        return out
    tested = []
    for chunk in np.array_split(fin, max(1, int(opt['pool_bands']))):
        r, er = [], []
        for row, erow in zip(S[:, chunk], E[:, chunk]):
            ok = np.isfinite(row) & np.isfinite(erow)
            if ok.sum() >= 3:
                tot = np.sum(P[chunk][ok])
                r.append(np.sum(row[ok]) / tot); er.append(np.sqrt(np.sum(erow[ok] ** 2)) / tot)
        if len(r) < int(opt['min_shutters']):
            continue
        r, er = np.asarray(r), np.asarray(er)
        med = float(np.median(r))
        dev = med - 1
        # the median of the shutters (one shutter with a wild band ratio must not decide)
        # with its error from their scatter, never below what their own errors imply (a
        # handful of shutters can scatter little by chance)
        spread = max(1.4826 * float(np.median(np.abs(r - med))), float(np.sqrt(np.mean(er ** 2))))
        err = 1.2533 * spread / np.sqrt(r.size)
        nsig = abs(dev) / err if err > 0 else np.inf
        tested.append((abs(dev) > opt['pool_tol'] and nsig > opt['pool_nsigma'], dev, nsig,
                       float(centres[chunk[0]]), float(centres[chunk[-1]])))
    if not tested:
        return out
    flagged = [t for t in tested if t[0]]
    worst = max(flagged or tested, key=lambda t: abs(t[1]))
    differs = bool(flagged)
    out.update(differs=differs, dev=worst[1], nsig=worst[2], lam_lo=worst[3], lam_hi=worst[4], n_bands=len(tested),
               verdict=(f'differs {worst[3]:.2f}-{worst[4]:.2f} um {100 * worst[1]:+.1f} % ({worst[2]:.1f} sig)' if differs
                        else 'consistent'))
    return out


def pool_members(clean_of, centres, opt):
    """Which exposures form the pool: ``(members, checks)`` over ``clean_of`` (rate base ->
    its clean shutters, one grid). Every member is tested against the pool of the other
    members (``pool_check``); while any differs, the one with the largest deviation leaves
    and the rest are tested again, so one discrepant exposure cannot make the others look
    discrepant through their reference (ember_uds_p2: nod 2 +18 % at 3 um put nods 1 and 3
    at -6 % against a pool that held it). ``checks[base]`` is each member's last test and,
    for an exposure that left, its test against the final pool."""
    members = {b for b, sp in clean_of.items() if sp}
    checks = {b: pool_check([], None, centres, opt) for b in clean_of}

    def against(b, pool_of):
        others = [s for o in pool_of if o != b for s in clean_of[o]]
        return pool_check(clean_of[b], combine(others, opt['clip_sigma']) if others else None, centres, opt)

    while len(members) > 1:
        for b in members:
            checks[b] = against(b, members)
        bad = [b for b in members if checks[b]['differs'] is True]
        if not bad:
            break
        members.discard(max(bad, key=lambda b: abs(checks[b]['dev'])))
    for b in clean_of:
        if b not in members and clean_of[b]:
            checks[b] = against(b, members)
    if len(members) == 1:
        (b,) = members
        checks[b] = dict(checks[b], verdict='alone in the pool')
    return members, checks


def model_sky(lam_um, sb, ra, dec, date_obs):
    """``jwst_backgrounds`` total background for the pointing and date, and the one
    global scale that fits it to ``sb`` at R ~ 20 (§3.3: a shape, not a level)."""
    from jwst_backgrounds import jbt
    bg = jbt.background(ra, dec, wavelength=float(np.nanmedian(lam_um)))
    doy = datetime.fromisoformat(str(date_obs)[:19]).timetuple().tm_yday - 1
    cal = np.asarray(bg.bkg_data['calendar'])
    i = int(np.argmin(np.abs(cal - doy)))
    model = np.interp(lam_um, bg.bkg_data['wave_array'], bg.bkg_data['total_bg'][i], left=np.nan, right=np.nan)
    coarse = np.geomspace(np.nanmin(lam_um), np.nanmax(lam_um), 25)
    idx = np.digitize(lam_um, coarse) - 1
    d, m = [], []
    for b in range(len(coarse) - 1):
        sel = (idx == b) & np.isfinite(sb) & np.isfinite(model)
        if sel.sum() > 3:
            d.append(np.median(sb[sel])); m.append(np.median(model[sel]))
    d, m = np.array(d), np.array(m)
    scale = float(np.sum(d * m) / np.sum(m * m)) if len(m) else np.nan
    return model, scale


# ---------------------------------------------------------------------------
# The product
# ---------------------------------------------------------------------------

def _barshadow_reference(grating, example_path):
    """The bar-shadow table the 2-D expansion uses: the empirical one when it exists
    for the grating, else the CRDS reference. Returns (basename, path)."""
    from campfire_pipeline.config import get_empirical_barshadow_path
    p = get_empirical_barshadow_path(grating)
    if p is None:
        p = CR.barshadow_path(example_path)
    return os.path.basename(p), p


def barshadow_stamp(path):
    """What identifies a table's content for the skip key: its derivation date
    (``CFBSDATE``, an empirical table) or ``USEAFTER`` (CRDS), else the file's mtime."""
    try:
        hdr = fits.getheader(path)
        return str(hdr.get('CFBSDATE') or hdr.get('USEAFTER') or os.path.getmtime(path))
    except Exception:
        return str(os.path.getmtime(path)) if os.path.exists(path) else ''


def resolve_barshadow_table(bs_ref, slit_path):
    """The table ``sky_for_slit`` evaluates, from the product's ``MBKGBSRF`` card."""
    if str(bs_ref).startswith('empirical_'):
        from campfire_pipeline import config as cfg
        path = os.path.join(os.path.dirname(cfg.__file__), 'data', bs_ref)
    else:
        path = CR.barshadow_path(slit_path)
    return B.load_table(path)


def _rate_sky_of(hdul):
    """The ``MBKG1DR`` table of an open file; None when absent or empty (step 1 found no
    background for that exposure)."""
    if 'MBKG1DR' not in hdul or not hdul['MBKG1DR'].header.get('NAXIS2', 0):
        return None
    return Table(hdul['MBKG1DR'].data)


def load_product(path, with_2d=False):
    """The content of a ``_mbkg.fits`` as the dict ``sky_for_slit`` takes. The 2-D arrays
    (``mbkg2d``, ``dq2d``, ``n2d``) and the exposure's rate frame (``rate``) are read with
    ``with_2d`` or on first use by ``sky_for_slit``, and kept in the dict."""
    with fits.open(path, memmap=False) as h:
        hdr = h[0].header
        t = Table(h['MBKG1D'].data)
        out = dict(path=path, header=hdr, wavelength=np.array(t['wavelength'], float),
                   surf_bright=np.array(t['surf_bright'], float), surf_bright_err=np.array(t['surf_bright_err'], float),
                   n_shutters=np.array(t['n_shutters'], int),
                   qscales={q: header_float(hdr, f'MBKGQ{q}') for q in (1, 2, 3, 4)},
                   floor=header_float(hdr, 'MBKGEFLR', 0.0), bs_ref=str(hdr.get('MBKGBSRF', 'CRDS')),
                   shutters=Table(h['SHUTTERS'].data) if 'SHUTTERS' in h else None,
                   rate_sky=_rate_sky_of(h), mbkg2d=None, dq2d=None, n2d=None, rate=None)
    if out['rate_sky'] is None:                # since 1.5 it lives in the rate file only (step 1)
        rate_path = os.path.join(os.path.dirname(os.path.abspath(path)), str(hdr.get('MBKGRATE', '')))
        if os.path.isfile(rate_path):
            with fits.open(rate_path, memmap=False) as h:
                out['rate_sky'] = _rate_sky_of(h)
    if with_2d:
        _load_2d(out)
    return out


def _load_2d(prod):
    """Read the 2-D arrays and the rate frame into ``prod``; False when the product has
    no ``MBKG2D``."""
    if prod.get('mbkg2d') is not None:
        return True
    with fits.open(prod['path'], memmap=False) as h:
        if 'MBKG2D' not in h:
            return False
        prod['mbkg2d'] = np.array(h['MBKG2D'].data, float)
        prod['dq2d'] = np.array(h['MBKG2DDQ'].data) if 'MBKG2DDQ' in h else np.zeros(prod['mbkg2d'].shape, np.uint8)
        prod['n2d'] = np.array(h['MBKG2DN'].data) if 'MBKG2DN' in h else None
    rate_path = os.path.join(os.path.dirname(os.path.abspath(prod['path'])), str(prod['header'].get('MBKGRATE', '')))
    if os.path.exists(rate_path):
        prod['rate'] = np.array(fits.getdata(rate_path), float)
    return prod['rate'] is not None


def frame_factor(bunit, pix_sr, pthloss, pu, pp, bar):
    """Multiplicative factor taking a sky surface brightness seen through the shutters
    (MJy/sr, bar shadow included) into one slit's own ``SCI`` frame: the pixel area for
    MJy per pixel, the pathloss ratio for a point-source calibration, and the division
    jwst applied when the file carries its own bar-shadow correction."""
    f = np.ones_like(np.asarray(bar if bar is not None else (pu if pu is not None else pix_sr), float)) \
        if bar is not None or pu is not None else 1.0
    f = f * (1.0 if 'sr' in str(bunit).lower() else float(pix_sr))
    with np.errstate(invalid='ignore', divide='ignore'):
        to_uniform = CR.point_to_uniform(str(pthloss).upper(), pp, pu)
        if to_uniform is not None:
            f = f / to_uniform
    if bar is not None:
        with np.errstate(invalid='ignore', divide='ignore'):
            f = f / bar
    return f


def read_slit_frame(path, extver=1):
    """What the expansion needs from one slit's file: the pre-subtraction ``sci``, its
    ``bunit`` (``MJy`` per pixel for every canonical after ``fix_units``, ``MJy/sr`` in
    the ``bkgslits`` file), the pixel area, the pathloss kind and arrays, the file's own
    bar-shadow array when jwst applied one, and the cutout's detector corner."""
    v = int(extver)
    with fits.open(path, memmap=False) as h:
        hs = h['SCI', v].header; ph = h[0].header
        pre = B._hdu(h, 'PRE_BKGSUB_SCI', v) is not None
        sci = np.array(h['PRE_BKGSUB_SCI' if pre else 'SCI', v].data, float)
        pthloss, pp, pu = CR.pathloss_arrays(h, v)
        bar = B._hdu(h, 'BARSHADOW', v) if str(ph.get('S_BARSHA', '')).upper() == 'COMPLETE' else None
        bar = np.array(bar.data, float) if bar is not None else None
        return dict(sci=sci, bunit=str(hs.get('BUNIT', 'MJy')), pix_sr=float(hs['PIXAR_SR']),
                    pthloss=pthloss, pu=pu, pp=pp, bar=bar,
                    x0=int(hs['SLTSTRT1']) - 1, y0=int(hs['SLTSTRT2']) - 1)


def sky_on_geometry(prod, frame, g, table):
    """The product's sky on a slit's pixels (pure arithmetic; ``sky_for_slit`` reads
    the inputs). ``prod`` is ``load_product`` output, ``frame`` ``read_slit_frame``
    output, ``g`` ``slit_geometry`` output, ``table`` the bar-shadow table."""
    lam, seg, yseg, j = g['lam'], g['seg'], g['yseg'], g['j']
    w = prod['wavelength']; fin = np.isfinite(prod['surf_bright'])
    sb = np.interp(lam, w[fin], prod['surf_bright'][fin], left=np.nan, right=np.nan)
    err = np.interp(lam, w[fin], prod['surf_bright_err'][fin], left=np.nan, right=np.nan)
    q = prod['qscales'].get(g['quadrant'], np.nan)
    if np.isfinite(q) and q > 0:
        sb = sb * q; err = err * q
    bs = np.full(lam.shape, np.nan)
    inside = seg >= 0
    if inside.any():
        bs[inside] = B.evaluate_table(table, seg[inside], yseg[inside], lam[inside])
    closed = ~inside & (j >= 0) & (j < len(g['state']))          # closed shutters inside the slitlet
    bs[closed] = float(np.nanmin(table['data1x1']))
    sb_through = sb * bs
    f = frame_factor(frame['bunit'], frame['pix_sr'], frame['pthloss'], frame['pu'], frame['pp'], frame['bar'])
    sky = sb_through * f
    var = ((err * bs) ** 2 + (prod['floor'] * sb_through) ** 2) * (np.asarray(f) ** 2)
    return dict(sky=sky, var=var, sb=sb_through, bs=bs, lam=lam, sci=frame['sci'], x0=frame['x0'], y0=frame['y0'],
                units=frame['bunit'], quadrant=g['quadrant'], shape=frame['sci'].shape, open=g['is_open'])


def sky_for_slit(product, path, extver=1, table=None, mode='total', mask_bits=0):
    """The master background on one slit's cutout, in that slit's own frame and units.

    ``product`` is a ``_mbkg.fits`` path or ``load_product`` output (pass the same dict
    for every slit of one exposure: the 2-D arrays and the rate frame are read once
    into it); ``path`` a canonical (or the ``bkgslits`` file with ``extver``). Returns
    ``sky`` and ``var`` in the slit's ``SCI`` units (what a consumer subtracts: MJy per
    pixel for a canonical, point or extended, exactly as ``fix_units`` left it), ``sb``
    (MJy/sr through the slit's own shutters), ``bs`` (the bar shadow evaluated), ``lam``,
    the slit's pre-subtraction ``sci`` and its detector corner ``x0`` / ``y0``.

    ``mode='total'`` (default) is the painted ``MBKG2D`` on the cutout through the
    pixelwise factor: every slitlet whose trace crosses the pixel, which on the gratings
    is several (``dq`` and ``n_cover`` cutouts say where a covering trace is missing);
    ``own_sky`` carries the slit's own slitlet alone, ``sky_rate`` the painted total in
    count-rate units (DN/s, the ``MBKG2D`` cutout itself) and ``rate`` the rate cutout.
    ``sky`` has the slit's units only where the slit is calibrated (``SCI`` finite); past
    the calibrated range -- the columns before the first calibrated column and beyond
    the last, where the sky is painted in count-rate units with no flat or photometric
    calibration -- ``sky`` is NaN and ``sky_rate`` is what there is. ``mode='own'``
    returns only the slit's own slitlet (the painter's input; also the fallback when the
    product has no ``MBKG2D`` or the rate file is not beside it). Nothing is masked by
    default: ``mask_bits`` (``DQ_UNPAINTED``: a covering trace that was not painted) is
    opt-in, and pixels nothing covers are NaN. There is no per-slitlet or smooth rescaling
    of the master background (decided 2026-11-02, design doc).
    """
    from jwst.datamodels import MultiSlitModel
    prod = load_product(product) if isinstance(product, str) else product
    frame = read_slit_frame(path, extver)
    m = MultiSlitModel(path)
    g = B.slit_geometry(m.slits[int(extver) - 1])
    m.close()
    if table is None:
        table = resolve_barshadow_table(prod['bs_ref'], path)
    own = sky_on_geometry(prod, frame, g, table)
    if mode == 'own':
        return own
    if not _load_2d(prod):
        # on the gratings the own slitlet is a fraction of what the cutout carries (a third
        # to a half where traces cross), so a silent fallback reads as a sky far too low
        warnings.warn(f'sky_for_slit: {os.path.basename(prod["path"])} has no MBKG2D or no rate file '
                      f'{prod["header"].get("MBKGRATE")} beside it; returning the slit\'s own slitlet only '
                      f'(mode="own"), not the painted total', RuntimeWarning, stacklevel=2)
        return own
    return sky_total_on_cutout(prod, own, frame, mask_bits=mask_bits)


def pixelwise_factor(sci, rate_box):
    """``sci / rate`` on a cutout: the exact factor from the rate frame to the slit's
    calibrated frame, since every step after ``extract_2d`` multiplies per pixel. A
    flagged or zero pixel takes its column's median (the calibration is smooth)."""
    with np.errstate(invalid='ignore', divide='ignore'):
        F = sci / rate_box
    F[~np.isfinite(F) | (rate_box == 0) | (sci == 0)] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        colmed = np.nanmedian(F, axis=0)
    return np.where(np.isfinite(F), F, colmed[None, :])


def sky_total_on_cutout(prod, own, frame, mask_bits=0):
    """What a consumer subtracts: the painted total (every covering slitlet's sky,
    ``MBKG2D``) on the slit's cutout, in the slit's frame through the pixelwise factor.
    ``own`` is ``sky_on_geometry`` output for the slit. Pixels carrying any of
    ``mask_bits`` (opt-in; ``DQ_UNPAINTED`` where a covering trace was not painted, the
    total being incomplete there) and pixels nothing covers are NaN. Returns the dict
    with ``sky``, ``var``, ``dq``
    (the ``MBKG2DDQ`` cutout), ``n_cover``, ``own_sky`` (the slit's own slitlet),
    ``sky_rate`` (the painted total in DN/s, finite past the calibrated range too),
    ``rate`` (the rate cutout) and ``calibrated`` (where the slit's ``SCI`` is finite,
    the only place ``sky`` has the slit's units)."""
    ny, nx = own['shape']
    box = (slice(frame['y0'], frame['y0'] + ny), slice(frame['x0'], frame['x0'] + nx))
    mb = prod['mbkg2d'][box]; dq = prod['dq2d'][box]; rate_box = prod['rate'][box]
    if mb.shape != own['shape']:
        raise ValueError('cutout does not fit the detector frame')
    F = pixelwise_factor(frame['sci'], rate_box)
    total = mb * F
    own_sky = own['sky']
    masked =(dq & (DQ_UNCOVERED | int(mask_bits))) > 0
    total[masked] = np.nan
    sky_rate = np.where(masked, np.nan, mb.astype(float))
    with np.errstate(invalid='ignore', divide='ignore'):
        ratio = np.where(np.isfinite(own_sky) & (own_sky > 0), total / own_sky, np.nan)
    var = np.where(np.isfinite(ratio), own['var'] * ratio ** 2, (prod['floor'] * total) ** 2)
    var[~np.isfinite(total)] = np.nan
    return dict(own, sky=total, var=var, dq=dq, own_sky=own_sky, sky_rate=sky_rate, rate=rate_box.astype(float),
                calibrated=np.isfinite(frame['sci']), n_cover=prod['n2d'][box] if prod.get('n2d') is not None else None)


def rate_sky_edges(centres, lam_lo, lam_hi):
    """Bin edges of the rate-unit sky: the ``MBKG1D`` grid (midpoints of its centres),
    continued at each end with that end's bin width until it covers ``[lam_lo, lam_hi]``."""
    c = np.asarray(centres, float)
    edges = np.concatenate([[c[0] - 0.5 * (c[1] - c[0])], 0.5 * (c[1:] + c[:-1]), [c[-1] + 0.5 * (c[-1] - c[-2])]])
    lo_step = max(edges[1] - edges[0], 1e-6); hi_step = max(edges[-1] - edges[-2], 1e-6)
    n_lo = int(np.ceil(max(edges[0] - lam_lo, 0.0) / lo_step)); n_hi = int(np.ceil(max(lam_hi - edges[-1], 0.0) / hi_step))
    return np.concatenate([edges[0] - lo_step * np.arange(n_lo, 0, -1), edges, edges[-1] + hi_step * np.arange(1, n_hi + 1)])


def _geometries(c):
    """A canonical's cutout geometry and its extended geometry, plus the slit's quadrant."""
    from jwst.datamodels import MultiSlitModel
    m = MultiSlitModel(c.path)
    try:
        s = m.slits[int(c.extver) - 1]
        g = B.slit_geometry(s)
        gx = slit_geometry_extended(s)
    finally:
        m.close()
    return g, gx


def failed_open_transmission(rate, base, val, core, n_min=200):
    """The fraction of an empty shutter's sky a failed-open shutter passes, from its pixels:
    ``rate`` the count rate, ``base`` everything else painted there, ``val`` the shutter's
    full-sky paint, ``core`` the pixels that measure it (open-shutter core, good, outside
    source shutters). Returns (a, n, scale): the median of (rate - base) / val over the
    usable core pixels, their number, and the scale painted: a clipped to [0, 1], or 1
    when fewer than ``n_min`` pixels measure it."""
    use = core & np.isfinite(val) & (val > 0) & np.isfinite(rate) & np.isfinite(base)
    n = int(use.sum())
    if n < n_min:
        return np.nan, n, 1.0
    a = float(np.median((rate[use] - base[use]) / val[use]))
    return a, n, float(np.clip(a, 0.0, 1.0))


def combine_failed_open(per_rate, tol=0.1):
    """One transmission per failed-open shutter per detector over the nods of a configuration.

    ``per_rate``: {rate base: the painter's failed-open entries (``name``, ``quadrant``,
    ``a``, ``scale``)}. A shutter's measurements over the nods (same root, same
    detector: the same wavelengths) are combined when at least two of them, and more than
    half, lie within ``tol`` of their median: the scale is then the median of those, clipped
    to [0, 1], for every nod (an object behind the shutter in one nod is the outlier, and a
    nod with too few pixels to measure takes it too). Otherwise each nod keeps its own.
    The two detectors are not combined (5/q1 on p1 G395M: 0.0 on nrs1, 0.25 on nrs2).

    Returns ({rate base: {(name, quadrant): scale}}, report rows)."""
    groups = {}
    for base, fos in per_rate.items():
        root, det = base.rsplit('_', 2)[0], base.rsplit('_', 1)[-1]
        for f in fos:
            groups.setdefault((root, det, f['name'], f['quadrant']), []).append((base, f))
    scales, report = {b: {} for b in per_rate}, []
    for (root, det, name, quad), items in sorted(groups.items()):
        a = np.array([f['a'] for _, f in items], float)
        meas = a[np.isfinite(a)]
        combined, agree_n = np.nan, 0
        if meas.size >= 2:
            agree = np.abs(meas - np.median(meas)) <= tol
            agree_n = int(agree.sum())
            if agree_n >= 2 and agree_n > meas.size / 2:
                combined = float(np.clip(np.median(meas[agree]), 0.0, 1.0))
        for base, f in items:
            scales[base][(name, quad)] = combined if np.isfinite(combined) else f['scale']
        report.append(dict(root=root, detector=det, name=name, quadrant=quad,
                           a={base: f['a'] for base, f in items}, n_measured=int(meas.size),
                           n_agree=agree_n, combined=combined))
    return scales, report


def rescale_failed_open(mb, dq, cover, fos, scales):
    """Repaint each failed-open shutter of one rate file from the scale it was painted at
    (``applied``) to ``scales[(name, quadrant)]`` in place. The total is linear in the
    shutter's paint (``merge_painted``'s weights ``w``), so the change is
    (new - old) x its full-sky paint x w on its pixels; ``MBKG2DN`` (where it is alone at its
    MSA spot) and DQ bit 8 follow. Returns whether anything changed."""
    changed = False
    mflat, cflat, dflat = mb.reshape(-1), cover.reshape(-1), dq.reshape(-1)
    for f in fos:
        old = f['applied']
        new = scales.get((f['name'], f['quadrant']), old)
        if new == old:
            continue
        mflat[f['pix']] += ((new - old) * f['val'] * f['w']).astype(mb.dtype)
        if old <= 0 < new:
            cflat[f['pix'][f['alone']]] += 1
        elif new <= 0 < old:
            cflat[f['pix'][f['alone']]] -= 1
        f['applied'] = new
        changed = True
    if changed:
        dflat &= np.asarray(~DQ_FAILED_OPEN).astype(dq.dtype)
        for f in fos:
            if f['applied'] > 0:
                dflat[f['pix']] |= DQ_FAILED_OPEN
    return changed


def _rate_dq(rate_path):
    with fits.open(rate_path, memmap=False) as h:
        return np.array(h['DQ'].data) if 'DQ' in h else np.zeros(h['SCI'].data.shape, np.uint32)


def _source_shutter_pixels(rate_path, shape):
    """Flat mask of the pixels in any slitlet's source shutter (the trace map step 1 stored
    in the rate file); all False when the rate file holds none."""
    from campfire_pipeline.nirspec import trace_overlap as TO
    tm = TO.trace_map_from_rate(rate_path, set())
    if tm is None:
        return np.zeros(shape[0] * shape[1], bool)
    kind = np.asarray(tm.slits['kind']).astype(str)
    slit = (tm.index >= 0) & (kind[np.clip(tm.index, 0, None)] == 'slitlet')
    return (slit & ((tm.flag & TO.IN_SOURCE) > 0)).any(0).ravel()


def _failed_open_closed(rate_path):
    """Names (``create_slitlets`` numbering) of the msaoper failed-open shutters whose science
    ``state`` is not open: jwst flags a shutter open in any of state / TA state / Internal
    state, and one open for the internal lamp only (e.g. vignetted) passes no sky."""
    import json
    from jwst.msaflagopen import MSAFlagOpenStep
    from jwst.msaflagopen import msaflag_open as MF
    try:
        ref = MSAFlagOpenStep().get_reference_file(rate_path, 'msaoper')
        with open(ref) as fh:
            shutters = json.load(fh)['msaoper']
    except Exception:                                        # noqa: BLE001
        return set()
    flagged = [s for s in shutters if any(s[k] == 'open' for k in MF.FLAGGABLE_STATES)]
    return {str(i + 1) for i, s in enumerate(flagged) if s.get('state') != 'open'}


def _rate_frame(rate_path, counter_path):
    """The rate frame, its variance and the pixels the rate-unit sky may use (finite,
    DQ-clean, one footprint on the coverage counter)."""
    from jwst.datamodels import dqflags
    with fits.open(rate_path, memmap=False) as h:
        rate = np.array(h['SCI'].data, float)
        rerr = np.array(h['ERR'].data, float) if 'ERR' in h else np.full(rate.shape, np.nan)
        rdq = np.array(h['DQ'].data) if 'DQ' in h else np.zeros(rate.shape, np.uint32)
    counter = np.load(counter_path) if counter_path and os.path.exists(counter_path) else None
    bad = dqflags.pixel['DO_NOT_USE'] | dqflags.pixel['MSA_FAILED_OPEN']
    rate_ok = np.isfinite(rate) & ((rdq & bad) == 0)
    if counter is not None:
        rate_ok &= counter == 1
    rvar = np.where(np.isfinite(rerr) & (rerr > 0), rerr ** 2, np.nan)
    return rate, rvar, rate_ok, counter


def rate_sky_for(base, specs_of, clip_sigma, min_shutters=3, min_overlap=10, pool_first=False):
    """The rate-unit sky of one rate file ``base`` (``<root>_<nod>_<det>``) on the
    disperser's extended grid, with its fallbacks bin by bin: the exposure's own clean
    shutters (source 1); the exposure group's pool on the same detector (2); the other
    detector's pool scaled by the median ratio over the bins both have (3: the same sky
    through a different detector response); the nearest filled bin held outward (4).
    ``specs_of`` maps every rate base of the group to its spectra. With ``pool_first`` (the
    exposure is consistent with the pool, ``pool_check``) the pool comes first and the
    exposure's own shutters fill only the bins the pool lacks. Returns
    ``(sb, err, n_shutters, source)`` or None when nothing at all was measured."""
    root, nod, det = base.rsplit('_', 2)
    other = 'nrs2' if det == 'nrs1' else 'nrs1'
    own = combine(specs_of.get(base, []), clip_sigma)
    same = combine([s for b, sp in specs_of.items() if b.endswith(det) for s in sp], clip_sigma)
    oth = combine([s for b, sp in specs_of.items() if b.endswith(other) for s in sp], clip_sigma)
    ref = own if own is not None else same
    if ref is None and oth is None:
        return None
    nb = len((ref if ref is not None else oth)[0])
    sb = np.full(nb, np.nan); err = np.full(nb, np.nan); n = np.zeros(nb, int); src = np.zeros(nb, np.int16)
    for level, cand in (((2, same), (1, own)) if pool_first else ((1, own), (2, same))):
        if cand is None:
            continue
        take = ~np.isfinite(sb) & np.isfinite(cand[0]) & (cand[2] >= min_shutters)
        sb[take] = cand[0][take]; err[take] = cand[1][take]; n[take] = cand[2][take]; src[take] = level
    if oth is not None:
        both = np.isfinite(sb) & np.isfinite(oth[0]) & (oth[2] >= min_shutters) & (oth[0] > 0)
        if both.sum() >= min_overlap:
            scale = float(np.median(sb[both] / oth[0][both]))
            take = ~np.isfinite(sb) & np.isfinite(oth[0]) & (oth[2] >= min_shutters)
            sb[take] = scale * oth[0][take]; err[take] = scale * oth[1][take]; n[take] = oth[2][take]; src[take] = 3
    fin = np.isfinite(sb)
    if fin.any() and not fin.all():
        idx = np.arange(nb)
        sb[~fin] = np.interp(idx[~fin], idx[fin], sb[fin]); err[~fin] = np.interp(idx[~fin], idx[fin], err[fin]); src[~fin] = 4
    return sb, err, n, src


def _flat_dispersion(pix, lam, ncols):
    """|dλ/dx| (µm per column) at each of one slitlet's painted pixels (flat detector
    index ``pix``), from the next column of the same row, else the previous; the block's
    median where the row has neither."""
    o = np.argsort(pix); ps = pix[o]; ls = lam[o]
    d = np.full(ps.size, np.nan)
    for step in (1, -1):
        i = np.clip(np.searchsorted(ps, ps + step), 0, max(ps.size - 1, 0))
        ok = ~np.isfinite(d) & (ps[i] == ps + step) & ((ps + step) // ncols == ps // ncols)
        d[ok] = np.abs(ls[i[ok]] - ls[ok])
    fin = np.isfinite(d) & (d > 0)
    d[~fin] = np.median(d[fin]) if fin.any() else np.nan
    out = np.empty_like(d); out[o] = d
    return out


def merge_painted(shape, layers, lam_tol_px=1.0, weights_of=None):
    """The painted total from every slitlet's layer (flat ``pix``, ``lam``, ``val``,
    ``is_open``, one entry per slitlet per pixel), counting the light of one spot on the
    MSA once.

    A detector pixel and a wavelength fix the spot on the MSA the light came through
    (the spectrograph maps MSA position × wavelength onto the detector), so two slitlets
    whose layers reach a pixel at the same wavelength (within ``lam_tol_px`` columns of
    dispersion; the next MSA column is ~2.7 columns away) are looking at the same spot:
    a slitlet's margin row over the open shutter of the next slitlet in its MSA column, or
    two slitlets' margins over the bar between them. That spot's light is the open
    shutter's sky when one of them is open there, else the margins' mean. Different spots
    on one pixel (traces crossing on the gratings, a margin under another slitlet's open
    shutter) are summed. Returns ``(total, n_spots, n_layers)`` on ``shape``.

    The spots depend on where the layers fall, not on their values, so the total is linear
    in each layer: with ``weights_of`` (layer indices) a fourth element maps each of those
    layers to ``(w, alone)`` per entry, ``w`` the total's change per unit of the entry's
    value (1 / open entries at its spot if it is open; 0 for a margin where a shutter is
    open; 1 / margins where none is) and ``alone`` whether it is the only entry at its spot."""
    npx = int(np.prod(shape))
    if not layers:
        z = np.zeros(npx)
        out = (z.reshape(shape).astype(np.float32), z.reshape(shape).astype(np.int16), z.reshape(shape).astype(np.int16))
        return out + ({},) if weights_of is not None else out
    pix = np.concatenate([lay[0] for lay in layers]); lam = np.concatenate([lay[1] for lay in layers])
    val = np.concatenate([lay[2] for lay in layers]); op = np.concatenate([lay[3] for lay in layers])
    disp = np.concatenate([_flat_dispersion(lay[0], lay[1], shape[1]) for lay in layers])
    o = np.lexsort((lam, pix))
    pix, lam, val, op, disp = pix[o], lam[o], val[o], op[o], disp[o]
    new = np.ones(pix.size, bool)
    with np.errstate(invalid='ignore'):
        tol = lam_tol_px * np.fmax(disp[1:], disp[:-1])
        new[1:] = ~((pix[1:] == pix[:-1]) & (np.abs(lam[1:] - lam[:-1]) < tol))
    gid = np.cumsum(new) - 1
    n_open = np.bincount(gid, op.astype(float)); s_open = np.bincount(gid, np.where(op, val, 0.0))
    n_marg = np.bincount(gid, (~op).astype(float)); s_marg = np.bincount(gid, np.where(op, 0.0, val))
    with np.errstate(invalid='ignore', divide='ignore'):
        spot = np.where(n_open > 0, s_open / np.maximum(n_open, 1), s_marg / np.maximum(n_marg, 1))
    gpix = pix[new]
    total = np.bincount(gpix, spot, minlength=npx)
    n_spots = np.bincount(gpix, minlength=npx); n_layers = np.bincount(pix, minlength=npx)
    out = (total.reshape(shape).astype(np.float32), n_spots.reshape(shape).astype(np.int16),
           n_layers.reshape(shape).astype(np.int16))
    if weights_of is None:
        return out
    has_open = n_open[gid] > 0
    with np.errstate(invalid='ignore', divide='ignore'):
        w_sorted = np.where(op, 1.0 / np.maximum(n_open[gid], 1),
                            np.where(has_open, 0.0, 1.0 / np.maximum(n_marg[gid], 1)))
    alone_sorted = (n_open + n_marg)[gid] == 1
    w = np.empty_like(w_sorted); alone = np.empty_like(alone_sorted)
    w[o] = w_sorted; alone[o] = alone_sorted
    edges = np.cumsum([0] + [lay[0].size for lay in layers])
    return out + ({i: (w[edges[i]:edges[i + 1]], alone[edges[i]:edges[i + 1]]) for i in weights_of},)


def _rate_sky_through(lam, seg, yseg, j, n_state, q, centres, rate_sky, table, floor_bs):
    """The count-rate sky through a slitlet at its pixels: the rate-unit sky at the
    open-shutter centre times the quadrant scale and the bar shadow at the pixel's
    slit-frame row (the closed-shutter floor on closed rows inside the slitlet)."""
    fin = np.isfinite(rate_sky[0])
    sb = np.interp(lam, centres[fin], rate_sky[0][fin], left=np.nan, right=np.nan)
    if np.isfinite(q) and q > 0:
        sb = sb * q
    bs = np.full(lam.shape, np.nan)
    inside = seg >= 0
    if inside.any():
        bs[inside] = B.evaluate_table(table, seg[inside], yseg[inside], lam[inside])
    bs[~inside & (j >= 0) & (j < n_state)] = floor_bs
    return sb * bs


def _worker_paint(args):
    """Pass 2b for one rate file: the sky of every slitlet footprint on the detector.

    Inside the calibrated range (where the cutout's ``SCI`` is finite) each input slit's
    sky is evaluated in its own frame and brought back to the rate frame through the
    pixelwise factor ``rate / SCI`` (exact wherever the calibration is a product of
    per-pixel factors, §4.4). Past it, over the rest of the slitlet's footprint (the
    extended geometry cut to the disperser's wavelength cutoffs: the columns before the
    cutout's first calibrated column and beyond its last, and the columns jwst's bounding
    box leaves outside the calibrated range), the sky is painted in count-rate units with
    no flat or photometric calibration: the exposure's rate-unit sky at the open-shutter
    centre ``rate_sky`` and its quadrant scales ``rate_q`` (step 1, ``master_bkg_rate``)
    times the bar shadow at the pixel's slit-frame row. Every other footprint of the
    coverage counter (the rate file's ``RateGeometry`` at ``geometry_path``) is painted
    the same way over its whole extent: any slitlet with neither a canonical nor a
    background-slit entry, and the failed-open shutters open in science exposures, each
    at the fraction of an empty shutter's sky it is measured to pass
    (``failed_open_transmission``; light above it, an object behind the shutter, is
    contamination).
    Each slitlet is painted once (two sources of one slitlet get cutouts a pixel apart);
    the layers are combined by ``merge_painted``.
    """
    (rate_path, product, canonicals, bs_path, counter_path, centres, rate_sky, opt, rate_q,
     geometry_path) = args
    table = B.load_table(bs_path)
    prod = load_product(product)
    rate, _, _, counter = _rate_frame(rate_path, counter_path)
    ny_d, nx_d = rate.shape
    core_frac = float(opt.get('core_frac', SETTINGS['core_frac']))
    layers = []                         # one (pix, lam, val, is_open) per slitlet painting
    n_painted, n_ext, n_failed_open, n_geometry, failures = 0, 0, 0, 0, []
    painted = set()
    check = []                          # (lam, in-range painted value) on core pixels, for the continuity check
    rs_fin = rate_sky is not None and centres is not None and np.isfinite(rate_sky[0]).any()
    floor_bs = float(np.nanmin(table['data1x1']))
    failed_open = np.zeros(rate.size, bool)

    def paint(rows, cols, lam, val, is_open):
        """One slitlet's layer: ``lam`` / ``val`` / ``is_open`` on a block (``rows`` x
        ``cols``) or on flat pixels (``rows`` and ``cols`` per pixel)."""
        ok = np.isfinite(val) & np.isfinite(lam)
        rr, cc = np.meshgrid(rows, cols, indexing='ij') if np.ndim(lam) == 2 else (rows, cols)
        layers.append(((rr[ok] * nx_d + cc[ok]).astype(np.int64), lam[ok].astype(float),
                       val[ok].astype(float), np.asarray(is_open)[ok]))
        return ok

    for c in canonicals:
        try:
            frame = read_slit_frame(c.path, c.extver)
            g, gx = _geometries(c)
        except Exception as e:
            failures.append((c.key, f'{type(e).__name__}: {e}')); continue
        key = (gx['name'], gx['quadrant'])
        if key in painted:
            continue                      # a second source of the same slitlet: the same sky
        painted.add(key)
        r = sky_on_geometry(prod, frame, g, table)
        ny, nx = r['shape']
        y0, x0 = r['y0'], r['x0']
        box = (slice(y0, y0 + ny), slice(x0, x0 + nx))
        rbox = rate[box]
        if rbox.shape != r['shape']:
            continue
        with np.errstate(invalid='ignore', divide='ignore'):
            val = r['sky'] / pixelwise_factor(r['sci'], rbox)
        ok = paint(np.arange(y0, y0 + ny), np.arange(x0, x0 + nx), g['lam'], val, r['open'])
        n_painted += 1
        ccore = ok & r['open'] & (np.abs(g['f']) <= core_frac) & (r['bs'] > 0)
        if ccore.any():
            check.append((g['lam'][ccore], val[ccore] / r['bs'][ccore]))     # both at the open-shutter centre
        if not rs_fin:
            continue
        # the extension: the footprint this slit has not painted, in count-rate units
        foot = gx['footprint']
        done = np.zeros(foot.shape, bool)
        r_a = y0 - gx['row_lo']
        if r_a < 0 or r_a + ny > foot.shape[0]:
            continue
        done[r_a:r_a + ny, x0:x0 + nx] = ok
        todo = foot & ~done
        if not todo.any():
            continue
        q = rate_q.get(int(gx.get('quadrant', 0)), np.nan)          # step 1's, in count rate
        valx = _rate_sky_through(gx['lam'], gx['seg'], gx['yseg'], gx['j'], len(gx['state']), q,
                                 centres, rate_sky, table, floor_bs)
        valx = np.where(todo, valx, np.nan)
        rows = np.arange(gx['row_lo'], gx['row_lo'] + foot.shape[0])
        n_ext += int(paint(rows, np.arange(nx_d), gx['lam'], valx, gx['is_open']).sum())

    # every other footprint the coverage counter knows, in count-rate units over its
    # whole extent: the failed-open shutters, and any slitlet stage 2a left no slit for
    fo_layers, fo_amps = [], []
    if rs_fin and geometry_path and os.path.exists(geometry_path):
        from campfire_pipeline.nirspec.coverage import RateGeometry
        geo = RateGeometry.load(geometry_path)
        in_source = _source_shutter_pixels(rate_path, rate.shape)
        closed = _failed_open_closed(rate_path)
        for b in geo.blocks():
            if b['kind'] == 'failed' and str(b['name']) in closed:
                continue                  # failed open for the internal lamp only: no sky reaches it
            if b['kind'] != 'failed' and (b['name'], b['quadrant']) in painted:
                continue
            val = _rate_sky_through(b['lam'], b['seg'], b['yseg'], b['j'], len(b['state']),
                                    rate_q.get(int(b['quadrant']), np.nan), centres, rate_sky, table, floor_bs)
            pix = b['pix']
            if b['kind'] == 'failed':
                fo_layers.append((b, val))
                continue
            paint(pix // nx_d, pix % nx_d, b['lam'], val, b['is_open'])
            n_geometry += 1
            painted.add((b['name'], b['quadrant']))
    if fo_layers:
        # a failed-open shutter passes anything from none to all of an empty shutter's sky
        # (measured 2026-11-02, repeatable between nods), so it is painted at its own
        # transmission: the median, over its open-shutter core outside source shutters, of
        # (rate - everything else painted there) / its full-sky paint, clipped to [0, 1].
        # Light above 1 (an object behind the shutter) is contamination.
        rate_good = np.isfinite(rate).ravel() & ((_rate_dq(rate_path) & 1) == 0).ravel()
        base = merge_painted(rate.shape, layers, float(opt.get('same_spot_px', SETTINGS['same_spot_px'])))[0].ravel()
        n_min = int(opt.get('failed_open_min_pixels', SETTINGS['failed_open_min_pixels']))
        for b, val in fo_layers:
            pix = b['pix']
            core = b['is_open'] & (np.abs(b['f']) <= 0.3) & rate_good[pix] & ~in_source[pix]
            a, n, scale = failed_open_transmission(rate.ravel()[pix], base[pix], val, core, n_min)
            # painted even at 0, so the MSA spots on every pixel do not depend on the scale
            ok = paint(pix // nx_d, pix % nx_d, b['lam'], val * scale, b['is_open'])
            fo_amps.append(dict(name=str(b['name']), quadrant=int(b['quadrant']), a=a, n=n, scale=scale,
                                layer=len(layers) - 1, pix=pix[ok].astype(np.int64),
                                val=val[ok].astype(np.float32)))
            if scale > 0:
                n_failed_open += 1
                failed_open[pix[ok]] = True

    ratio = np.nan
    if check and rs_fin:
        fin = np.isfinite(rate_sky[0])
        lam_c = np.concatenate([p[0] for p in check]); v_c = np.concatenate([p[1] for p in check])
        model = np.interp(lam_c, centres[fin], rate_sky[0][fin], left=np.nan, right=np.nan)
        with np.errstate(invalid='ignore', divide='ignore'):
            rr = v_c / model
        rr = rr[np.isfinite(rr) & (model > 0)]
        ratio = float(np.median(rr)) if rr.size else np.nan

    # the total is linear in each failed-open shutter's paint (merge_painted's weights), so
    # run_master_bkg can rescale a shutter to its value over all nods afterwards, exactly
    mb, cover, n_layers, weights = merge_painted(rate.shape, layers, float(opt.get('same_spot_px', SETTINGS['same_spot_px'])),
                                                 weights_of=[f['layer'] for f in fo_amps])
    for f in fo_amps:
        w, alone = weights[f['layer']]
        f['w'] = w.astype(np.float32)
        f['alone'] = alone
    dq = np.zeros(rate.shape, np.uint8)
    dq[cover == 0] |= DQ_UNCOVERED
    if counter is not None:
        dq[(counter > n_layers) & (counter > 0)] |= DQ_UNPAINTED
    dq[failed_open.reshape(rate.shape)] |= DQ_FAILED_OPEN
    for f in fo_amps:
        f['applied'] = f['scale']
        if f['scale'] <= 0:
            cover.ravel()[f['pix'][f['alone']]] -= 1    # painted at 0 alone at its spot: no light from there
    counts = dict(slits=n_painted, extension_px=n_ext, failed_open=n_failed_open, geometry=n_geometry,
                  failed_open_amps=fo_amps)
    return rate_path, mb, dq, np.clip(cover, 0, 255).astype(np.uint8), counts, failures, ratio


def _finite_cards(cards):
    """FITS cannot hold NaN: a card whose value is not finite is left out (readers
    take a missing card as NaN, ``header_float``)."""
    out = {}
    for k, v in cards.items():
        val = v[0] if isinstance(v, tuple) else v
        if isinstance(val, (float, np.floating)) and not np.isfinite(val):
            continue
        if isinstance(val, np.generic):
            val = val.item()
            v = (val, v[1]) if isinstance(v, tuple) else val
        out[k] = v
    return out


def header_float(hdr, key, default=np.nan):
    v = hdr.get(key)
    try:
        return float(v) if v is not None and v != '' else default
    except (TypeError, ValueError):
        return default


def write_product(path, hdr_cards, grid_centres, sky, pool, model, shutters, mb2d=None, dq2d=None, history=(), n2d=None,
                  own=None):
    phdr = fits.Header()
    for k, v in _finite_cards(hdr_cards).items():
        phdr[k] = v
    for line in history:
        phdr['HISTORY'] = line
    t = Table()
    t['wavelength'] = np.asarray(grid_centres, np.float64); t['wavelength'].unit = 'um'
    t['surf_bright'] = np.asarray(sky[0], np.float32); t['surf_bright'].unit = 'MJy/sr'
    t['surf_bright_err'] = np.asarray(sky[1], np.float32); t['surf_bright_err'].unit = 'MJy/sr'
    t['n_shutters'] = np.asarray(sky[2], np.int32)
    t['pool_surf_bright'] = np.asarray(pool[0] if pool is not None else np.full(len(grid_centres), np.nan), np.float32)
    t['pool_surf_bright_err'] = np.asarray(pool[1] if pool is not None else np.full(len(grid_centres), np.nan), np.float32)
    t['model_surf_bright'] = np.asarray(model if model is not None else np.full(len(grid_centres), np.nan), np.float32)
    # this exposure's own sky, whichever MBKGSRC chose to paint
    nanv = np.full(len(grid_centres), np.nan)
    t['exposure_surf_bright'] = np.asarray(own[0] if own is not None else nanv, np.float32)
    t['exposure_surf_bright_err'] = np.asarray(own[1] if own is not None else nanv, np.float32)
    t['exposure_n_shutters'] = np.asarray(own[2] if own is not None else np.zeros(len(grid_centres)), np.int32)
    sh = Table()
    sh['slit'] = [s.slit for s in shutters] or np.zeros(0, dtype='U16')
    sh['source_id'] = np.array([s.source_id for s in shutters], np.int64)
    sh['slitlet_id'] = np.array([s.slitlet_id for s in shutters], np.int32)
    sh['quadrant'] = np.array([s.quadrant for s in shutters], np.int16)
    sh['k'] = np.array([s.k for s in shutters], np.int16)
    sh['n_pix'] = np.array([s.n_pix for s in shutters], np.int32)
    sh['scale'] = np.array([s.scale for s in shutters], np.float32)
    sh['scale_err'] = np.array([s.scale_err for s in shutters], np.float32)
    sh['chi2_red'] = np.array([s.chi2_red for s in shutters], np.float32)
    sh['line_run'] = np.array([s.line_run for s in shutters], np.int16)
    sh['clean'] = np.array([s.clean for s in shutters], bool)
    sh['reason'] = [s.reason for s in shutters] or np.zeros(0, dtype='U20')
    hdus = [fits.PrimaryHDU(header=phdr), fits.BinTableHDU(t, name='MBKG1D'), fits.BinTableHDU(sh, name='SHUTTERS')]
    if mb2d is not None:
        hdus.append(fits.ImageHDU(np.asarray(mb2d, np.float32), name='MBKG2D'))
        hdus[-1].header['BUNIT'] = 'DN/s'
        if n2d is not None:
            hdus.append(fits.ImageHDU(np.asarray(n2d, np.uint8), name='MBKG2DN'))
        hdus.append(fits.ImageHDU(np.asarray(dq2d, np.uint8), name='MBKG2DDQ'))
    atomic_save(fits.HDUList(hdus), path)
    return path


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------

def plot_product(path, out_pdf, obs_name=''):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    prod = load_product(path)
    hdr = prod['header']; sh = prod['shutters']
    lam = prod['wavelength']
    with fits.open(path, memmap=False) as h:
        t = Table(h['MBKG1D'].data)
        mb2d = np.array(h['MBKG2D'].data) if 'MBKG2D' in h else None
    base = os.path.basename(path)[:-len('_mbkg.fits')]
    with PdfPages(out_pdf) as pdf:
        fig, axes = plt.subplots(3, 1, figsize=(10, 11))
        ax = axes[0]
        ax.plot(lam, t['pool_surf_bright'], color='tab:orange', lw=1.0, label=f"exposure group pool (scale {header_float(hdr, 'MBKGSCL'):.3f}, offset/sky {header_float(hdr, 'MBKGOFFR'):+.3f})")
        own = t['exposure_surf_bright'] if 'exposure_surf_bright' in t.colnames else t['surf_bright']
        ax.plot(lam, own, color='k', lw=1.2, label=f"this exposure x detector: {hdr.get('MBKGNSH')} clean of {hdr.get('MBKGNTOT')} shutters")
        ax.plot(lam, t['surf_bright'], color='tab:green', lw=0.8,
                label=f"painted ({hdr.get('MBKGSRC')}: {hdr.get('MBKGPDEC', '')})")
        if np.isfinite(t['model_surf_bright']).any():
            ax.plot(lam, t['model_surf_bright'], color='tab:blue', ls='--', lw=1, label=f"jwst_backgrounds x {header_float(hdr, 'MBKGMDL'):.2f}")
        ax.set_ylabel('sky surface brightness [MJy/sr]'); ax.set_title(f'{obs_name} {base}: master background')
        hi = np.nanpercentile(t['surf_bright'], 99) if np.isfinite(t['surf_bright']).any() else 1
        ax.set_ylim(0, 2 * hi); ax.legend(fontsize=8, loc='upper left')
        if prod.get('rate_sky') is not None:
            tr = prod['rate_sky']
            ax2 = ax.twinx()
            ax2.plot(tr['wavelength'], tr['rate'], color='tab:red', lw=0.8,
                     label=f"rate-unit sky, {hdr.get('MBKGCBLU')}-{hdr.get('MBKGCRED')} um (in-range painted / this: {header_float(hdr, 'MBKGRRAT'):.3f})")
            ax2.set_ylabel('DN/s at the open-shutter centre', color='tab:red')
            rhi = np.nanpercentile(tr['rate'], 99) if np.isfinite(tr['rate']).any() else 1
            ax2.set_ylim(0, 2 * rhi); ax2.legend(fontsize=8, loc='upper right')
        ax = axes[1]
        with np.errstate(invalid='ignore', divide='ignore'):
            ax.plot(lam, own / t['pool_surf_bright'], color='k', lw=0.8, label='this exposure / pool')
        if np.isfinite(header_float(hdr, 'MBKGPLO')):
            ax.axvspan(header_float(hdr, 'MBKGPLO'), header_float(hdr, 'MBKGPHI'), color='tab:red', alpha=0.1,
                       label=f"worst band {100 * header_float(hdr, 'MBKGPDEV'):+.1f} % ({header_float(hdr, 'MBKGPSIG'):.1f} sigma)")
        for q in (1, 2, 3, 4):
            a = header_float(hdr, f'MBKGQ{q}')
            if np.isfinite(a):
                ax.axhline(a, color=f'C{q}', lw=0.6, ls=':', label=f'quadrant {q} scale {a:.3f}')
        ax.axhline(1, color='gray', lw=0.5); ax.set_ylim(0.8, 1.2); ax.set_ylabel('ratio'); ax.set_xlabel('wavelength [um]')
        ax.legend(fontsize=7, ncol=2)
        ax = axes[2]
        if sh is not None and len(sh):
            a = np.array(sh['scale'], float); cl = np.array(sh['clean'], bool); ok = np.isfinite(a)
            ax.hist(a[ok & cl], bins=40, range=(0, 2), color='tab:green', label='clean')
            ax.hist(a[ok & ~cl], bins=40, range=(0, 2), color='tab:red', alpha=0.6, label='rejected')
            reasons = {r: int(np.sum(sh['reason'] == r)) for r in REASONS if np.sum(sh['reason'] == r)}
            ax.legend(fontsize=8, title=', '.join(f'{k} {v}' for k, v in reasons.items()), title_fontsize=7)
        ax.set_xlabel('per-shutter scale against the sky')
        fig.tight_layout(); pdf.savefig(fig); plt.close(fig)
        if mb2d is not None:
            fig, ax = plt.subplots(figsize=(8, 8))
            fin = mb2d[np.isfinite(mb2d) & (mb2d > 0)]
            vmax = np.percentile(fin, 99) if fin.size else 1
            ax.imshow(mb2d, origin='lower', cmap='gray', vmin=0, vmax=vmax, interpolation='nearest')
            ax.set_title(f'{base} MBKG2D (rate frame, DN/s, 0-{vmax:.3g}); painted slits: {hdr.get("MBKGN2D")}', fontsize=9)
            fig.tight_layout(); pdf.savefig(fig); plt.close(fig)
    return out_pdf


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _failed_open_over_nods(obs, fo_of, tol):
    """After every rate file is painted: one transmission per failed-open shutter and
    detector over the nods (``combine_failed_open``), written into the products already
    on disk (``rescale_failed_open``)."""
    scales, report = combine_failed_open(fo_of, tol)
    for r in report:
        vals = ' '.join(f'{v:.2f}' if np.isfinite(v) else '-' for _, v in sorted(r['a'].items()))
        log(f'master background: failed-open {r["name"]}/q{r["quadrant"]} {r["root"]} {r["detector"]}: '
            f'nods {vals} -> ' + (f'{r["combined"]:.2f}' if np.isfinite(r['combined'])
                                  else f'each its own ({r["n_agree"]}/{r["n_measured"]} agree)'))
    for base, fos in fo_of.items():
        if not fos:
            continue
        out = product_path(obs, base)
        how = ('measured, nods', 'failed-open transmission: how, over what')
        if all(scales[base].get((f['name'], f['quadrant']), f['applied']) == f['applied'] for f in fos):
            # every shutter keeps the scale it was painted at: the arrays stand and only the
            # cards change (both already written by the painter, so the header keeps its size)
            with fits.open(out, mode='update', memmap=False) as h:
                h[0].header['MBKGFOSC'] = how
            continue
        with fits.open(out, memmap=False) as h:
            hdul = fits.HDUList([x.copy() for x in h])
        mb = np.array(hdul['MBKG2D'].data, np.float32)
        cover = np.array(hdul['MBKG2DN'].data, np.uint8)
        dq = np.array(hdul['MBKG2DDQ'].data, np.uint8)
        rescale_failed_open(mb, dq, cover, fos, scales[base])
        n_fo = (int(sum(f['applied'] > 0 for f in fos)), 'failed-open shutters painted')
        bunit = hdul['MBKG2D'].header.get('BUNIT', 'DN/s')
        for name in ('MBKG2D', 'MBKG2DN', 'MBKG2DDQ'):
            del hdul[name]
        hdul.append(fits.CompImageHDU(mb, name='MBKG2D', compression_type='GZIP_2', quantize_level=0))
        hdul[-1].header['BUNIT'] = bunit
        hdul.append(fits.CompImageHDU(cover, name='MBKG2DN', compression_type='RICE_1'))
        hdul.append(fits.CompImageHDU(dq, name='MBKG2DDQ', compression_type='RICE_1'))
        hdul[0].header['MBKGNFO'], hdul[0].header['MBKGFOSC'] = n_fo, how
        atomic_save(hdul, out)


def run_master_bkg(obs, stage_config, n_processes=1, overwrite=False, plot=True):
    """Derive the ``_mbkg.fits`` product of every rate file of ``obs`` from its stage-2a
    canonicals and background slitlets. Skips a product whose inputs have not changed
    unless ``overwrite``, and a rate file that is not an MSA exposure (a standalone
    fixed slit has no empty shutters; step 1 skips it too). Returns the paths written."""
    from campfire_pipeline.nirspec.master_bkg_rate import has_master_background
    opt = options(stage_config)
    plot = plot and opt['plot']
    try:
        from campfire_pipeline import __version__ as version
    except Exception:
        version = 'unknown'
    inputs = discover_inputs(obs)
    stuck_mtime = obs.stuck_closed_shutters_mtime or 0.0
    todo, bs_name_of, eligible = {}, {}, {}
    skipped_non_msa = 0
    for rate in obs.rate_files:
        base = rate_base_of(rate)
        if not has_master_background(fits.getheader(rate)):
            skipped_non_msa += 1
            continue
        cans = inputs.get(base, [])
        if not cans:
            log(f'master background: {base}: no stage-2a inputs, skipped')
            continue
        out = product_path(obs, base)
        eligible[base] = (rate, cans)
        # a new bar-shadow table for the grating makes the product stale too
        if cans[0].grating not in bs_name_of:
            name, bs_path = _barshadow_reference(cans[0].grating, cans[0].path)
            bs_name_of[cans[0].grating] = (name, barshadow_stamp(bs_path))
        from campfire_pipeline.nirspec.master_bkg_rate import product_path as step1_path
        if not overwrite and product_current(out, rate, cans, stuck_mtime, *bs_name_of[cans[0].grating],
                                             step1_path=step1_path(obs, base)):
            log(f'master background: {base}: current, skipped')
            continue
        todo[base] = (rate, cans)
    if skipped_non_msa:
        log(f'master background: {obs.name}: {skipped_non_msa} rate file(s) not MSA exposures '
            f'(fixed slit), skipped')
    if not todo:
        return []
    # every rate file of a group contributes to its pool and to the failed-open
    # transmissions combined over its nods, so one stale product makes the whole group's
    # products stale
    groups = {base.rsplit('_', 2)[0] for base in todo}
    todo = {base: v for base, v in eligible.items() if base.rsplit('_', 2)[0] in groups}
    log(f'master background for {obs.name}: {len(todo)} rate file(s), '
        f'{sum(len(v[1]) for v in todo.values())} input slits, options {opt}')
    sample = {base: inputs[base] for base in inputs if base.rsplit('_', 2)[0] in groups and inputs[base]}
    canonicals = [c for cans in sample.values() for c in cans]

    # the footprints reach as far as the light does: G140M / G235M geometry is always built
    # on the extended wavelengthrange, whatever the reduction calibrates
    # (coverage.resolve_wavelengthrange)
    cache_dir = default_cache_dir()
    counters, geometries, cutoffs_of = {}, {}, {}
    for base in sorted(sample):
        rate = os.path.join(obs.workspace_dir, f'{base}_rate.fits')
        if not os.path.exists(rate):
            continue
        c0 = sample[base][0]
        cutoffs = cutoffs_for(c0.grating, c0.filter)
        cutoffs_of[rate] = cutoffs
        coverage_from_wcs(rate, cache_dir=cache_dir, cutoffs=cutoffs)
        counters[rate] = cache_path(rate, cache_dir, cutoffs)
        geometries[rate] = geometry_cache_path(rate, cache_dir, cutoffs)
    log(f'master background: {len(counters)} coverage counters')

    dispersers = sorted({(c.grating, c.filter) for c in canonicals})
    edges_of, bs_of = {}, {}
    for key in dispersers:
        cs = [c for c in canonicals if (c.grating, c.filter) == key]
        edges_of[key] = wavelength_grid(cs, opt['bin_pixels'])
        bs_of[key] = _barshadow_reference(key[0], cs[0].path)
        log(f'master background: {key[0]}/{key[1]}: {len(edges_of[key]) - 1} bins '
            f'{edges_of[key][0]:.3f}-{edges_of[key][-1]:.3f} um, bar shadow {bs_of[key][0]}')

    by_rate, failures = shutter_spectra(canonicals, counters, edges_of, opt, n_processes,
                                       {k: v[1] for k, v in bs_of.items()})
    if failures:
        log(f'master background: {len(failures)} input slit(s) gave no shutters, e.g. {failures[0]}')

    # per rate file: selection and sky; per exposure group: the pool
    skies, shutters_of = {}, {}
    for base, spectra in by_rate.items():
        if len(spectra) < 3:
            continue
        sky = select_clean(spectra, median_seed(spectra), opt)
        skies[base] = sky
        shutters_of[base] = spectra
    # the group's pool: the clean shutters of the exposures consistent with each other
    # (pool_members); the others keep their own sky
    pools, checks, in_pool = {}, {}, set()
    for grp in groups:
        clean_of = {base: [s for s in sp if s.clean] for base, sp in shutters_of.items() if base.rsplit('_', 2)[0] == grp}
        members = set()
        if clean_of:
            key0 = next(c for c in canonicals if c.rate_base in clean_of)
            e = edges_of[(key0.grating, key0.filter)]
            members, chk = pool_members(clean_of, 0.5 * (e[1:] + e[:-1]), opt)
            checks.update(chk); in_pool |= members
        pooled = [s for b in members for s in clean_of[b]]
        pools[grp] = combine(pooled, opt['clip_sigma']) if pooled else None

    # 2-D expansion, parallel over rate files, after the 1-D products are written
    written, paint_args = [], []
    for base, (rate, cans) in sorted(todo.items()):
        key = (cans[0].grating, cans[0].filter)
        edges = edges_of[key]; centres = 0.5 * (edges[1:] + edges[:-1])
        spectra = shutters_of.get(base, by_rate.get(base, []))
        own = skies.get(base)             # this exposure's own sky, always recorded
        sky = own
        grp = base.rsplit('_', 2)[0]
        pool = pools.get(grp)
        n_clean = sum(1 for s in spectra if s.clean)
        src = 'exposure'
        check = checks.get(base, pool_check([], None, centres, opt))
        if (sky is None or n_clean < opt['min_shutters']) and pool is not None:
            sky, src = pool, 'group'
        elif sky is not None and pool is not None and base in in_pool:
            sky, src = pool, 'pool'          # consistent with the other members (pool_members)
        if sky is None:
            log(f'master background: {base}: no clean shutters and no pool; no product')
            continue
        a, ae, b, be, nfit = scale_offset(own, pool) if (pool is not None and own is not None) else (np.nan,) * 4 + (0,)
        qsc = quadrant_scales(spectra, sky, opt)
        model, mscale = None, np.nan
        try:                              # recorded only (MBKGMDL); skipped without jwst_backgrounds
            ph = fits.getheader(rate)
            model, mscale = model_sky(centres, sky[0], float(ph['TARG_RA']), float(ph['TARG_DEC']), ph['DATE-OBS'])
            model = model * mscale
        except Exception as e:
            log(f'master background: {base}: jwst_backgrounds comparison skipped ({type(e).__name__}: {e})')
        rate_hdr = fits.getheader(rate)
        med_sky = float(np.nanmedian(sky[0])) if np.isfinite(sky[0]).any() else np.nan
        cards = {
            'CFMBKGV': (ALGORITHM_VERSION, 'campfire master-background algorithm version'),
            'MBKGVERS': (version, 'campfire-pipeline version'),
            'MBKGDATE': (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'), 'derivation date (UTC)'),
            'MBKGRATE': (os.path.basename(rate), 'rate file this product belongs to'),
            'MBKGGRAT': (key[0], 'disperser'), 'MBKGFILT': (key[1], 'filter'),
            'MBKGNIN': (len(cans), 'input slits (canonicals + background slitlets)'),
            'MBKGINMT': (inputs_stamp(cans), 'newest input mtime'),
            'MBKGBKDT': (str(rate_hdr.get('CFBKGDT')), 'rate CFBKGDT at derivation'),
            'MBKGSTK': (float(stuck_mtime), 'stuck-shutter TOML mtime'),
            'MBKGNTOT': (len(spectra), 'empty shutters offered'),
            'MBKGNSH': (n_clean, 'clean shutters in this exposure x detector'),
            'MBKGNGRP': (sum(1 for sp in shutters_of.values() for s in sp if s.clean and s.rate_base.rsplit('_', 2)[0] == grp),
                         'clean shutters in the group pool'),
            'MBKGSRC': (src, 'MBKG1D: pool (consistent), exposure (differs), group (few)'),
            'MBKGPDEC': (check['verdict'], 'vs the pool of the other members, worst band'),
            'MBKGPMEM': (' '.join(sorted(f"{int(b.rsplit('_', 2)[1])}/{b.rsplit('_', 2)[2][-1]}" for b in in_pool
                                         if b.rsplit('_', 2)[0] == grp)) or 'none', 'pool members (nod/detector)'),
            'MBKGPDEV': (check['dev'], 'that band: mean shutter / pool - 1'),
            'MBKGPSIG': (check['nsig'], 'that band: significance [sigma]'),
            'MBKGPLO': (check['lam_lo'], 'that band: from [um]'),
            'MBKGPHI': (check['lam_hi'], 'that band: to [um]'),
            'MBKGPNB': (check['n_bands'], 'bands tested'),
            'MBKGPTOL': (float(opt['pool_tol']), 'a band differs above this fraction'),
            'MBKGPNSG': (float(opt['pool_nsigma']), '... and above this many sigma'),
            'MBKGSCL': (a, 'this sky / group pool, recorded only'),
            'MBKGSCLE': (ae, 'its error'),
            'MBKGOFF': (b, 'offset vs group pool [MJy/sr], recorded'),
            'MBKGOFFE': (be, 'its error'),
            'MBKGOFFR': (b / med_sky if np.isfinite(b) and med_sky else np.nan, 'offset / median sky'),
            'MBKGMDL': (mscale, 'data / jwst_backgrounds, one scale'),
            'MBKGEFLR': (float(opt['error_floor']), 'error floor (fraction of sky)'),
            'MBKGBSRF': (bs_of[key][0], 'bar-shadow table of the 2-D expansion'),
            'MBKGBSDT': (barshadow_stamp(bs_of[key][1]), 'that table: derivation date / USEAFTER'),
            'MBKGBINP': (float(opt['bin_pixels']), 'detector pixels per bin'),
            'MBKGCORE': (float(opt['core_frac']), '|f| of the shutter core measured'),
            'MBKGSPAD': (float(opt['source_pad_px']), 'source pad [px]'),
        }
        for q in (1, 2, 3, 4):
            cards[f'MBKGQ{q}'] = (qsc[q][0], f'quadrant {q} scale vs sky ({qsc[q][1]} shutters)')
        hist = ['Derived by campfire_pipeline.nirspec.master_bkg from the empty shutters of the',
                'stage-2a canonicals and the background slitlets of this exposure;',
                'see docs/design-nirspec-master-background.md section 4.']
        out = product_path(obs, base)
        write_product(out, cards, centres, sky, pool, model, spectra, history=hist, own=own)
        written.append(out)
        log(f'master background: {base}: {n_clean}/{len(spectra)} shutters clean ({src}); '
            f'scale vs pool {a:.4f}, offset/sky {cards["MBKGOFFR"][0]:+.4f}; data/model {mscale:.3f}')
        # always painted: stage 2b (neighbor test, background spectra) and stage 3's
        # master-background spectrum read MBKG2D through sky_for_slit
        paint_args.append(((rate, out, cans, bs_of[key][1], counters.get(rate), opt), key))

    if paint_args:
        from multiprocessing import Pool
        # the count-rate sky and its quadrant scales come from step 1 (master_bkg_rate,
        # before stage 2a, from the rate files alone); without its product a rate file is
        # painted in the calibrated range only
        from campfire_pipeline.nirspec import master_bkg_rate as R
        rate_skies, rate_q, centres_of, step1_date = {}, {}, {}, {}
        for (rate, out, cans, bs_path, counter, o), key in paint_args:
            base = rate_base_of(rate)
            p1 = R.product_path(obs, base)
            h1 = R.step1_header(p1)
            sky_t, q = R.read_rate_sky(p1) if h1 is not None else (None, {})
            if sky_t is None:
                log(f'master background: {base}: no step-1 background in the rate file, painted in the calibrated range only')
                continue
            centres_of[base] = np.asarray(sky_t['wavelength'], float)
            rate_skies[base] = tuple(np.asarray(sky_t[c]) for c in ('rate', 'rate_err', 'n_shutters', 'source'))
            rate_q[base] = q
            step1_date[base] = str(h1.get('MBKGDATE'))
        paint_args = [(rate, out, cans, bs_path, counter, centres_of.get(rate_base_of(rate)),
                       rate_skies.get(rate_base_of(rate)), o, rate_q.get(rate_base_of(rate), {}), geometries.get(rate))
                      for (rate, out, cans, bs_path, counter, o), key in paint_args]

        fo_of = {}

        def _finish(res):
            rate, mb, dq, cover, counts, fails, ratio = res
            n_painted, n_ext = counts['slits'], counts['extension_px']
            base = rate_base_of(rate)
            out = product_path(obs, base)
            fo_of[base] = counts.get('failed_open_amps', [])
            rate_sky = rate_skies.get(base)
            centres_ext = centres_of.get(base)
            with fits.open(out, memmap=False) as h:
                hdul = fits.HDUList([x.copy() for x in h])
            blue, red = cutoffs_of.get(rate, (None, None))
            hdul[0].header['MBKGN2D'] = (n_painted, 'slits painted into MBKG2D')
            hdul[0].header['MBKGNEXT'] = (int(n_ext), 'pixels painted past the calibrated range')
            hdul[0].header['MBKGNFO'] = (int(counts['failed_open']), 'failed-open shutters painted')
            hdul[0].header['MBKGFOSC'] = ('measured, exposure',
                                          'failed-open transmission: how, over what')
            hdul[0].header['MBKGNGEO'] = (int(counts['geometry']), 'slitlets painted with no slit (rate units)')
            hdul[0].header['MBKGCBLU'] = (float(blue) if blue is not None else 'edge', 'footprint blue cutoff [um]')
            hdul[0].header['MBKGCRED'] = (float(red) if red is not None else 'edge', 'footprint red cutoff [um]')
            if np.isfinite(ratio):
                hdul[0].header['MBKGRRAT'] = (float(ratio), 'in-range painted / rate-unit sky, core, median')
            hdul[0].header['MBKGDQ1'] = ('1 uncovered, 2 trace unpainted, 8 failed-open', 'MBKG2DDQ bits')
            hdul[0].header['MBKGRDT'] = (step1_date.get(base, 'None'), 'step-1 tables (in the rate file) derivation date')
            if rate_sky is not None:                    # the table itself stays in the rate file (load_product reads it there)
                srcs = np.bincount(np.asarray(rate_sky[3], int), minlength=5)
                hdul[0].header['MBKGRSRC'] = (f'own {srcs[1]} pool {srcs[2]} other {srcs[3]} held {srcs[4]}', 'MBKG1DR bins by source')
            # lossless compression: 25 MB -> ~3 MB, the arrays read back bit-identical
            hdul.append(fits.CompImageHDU(mb, name='MBKG2D', compression_type='GZIP_2', quantize_level=0))
            hdul[-1].header['BUNIT'] = 'DN/s'
            hdul.append(fits.CompImageHDU(cover, name='MBKG2DN', compression_type='RICE_1'))
            hdul.append(fits.CompImageHDU(dq, name='MBKG2DDQ', compression_type='RICE_1'))
            atomic_save(hdul, out)
            log(f'master background: {rate_base_of(rate)}: {n_painted} slits painted, {n_ext} pixels past the calibrated range, '
                f'{counts["failed_open"]} failed-open shutters, {counts["geometry"]} slitlets without a slit'
                + (f', in-range / rate-unit sky {ratio:.3f}' if np.isfinite(ratio) else ''))
            if fails:
                log(f'master background: {rate_base_of(rate)}: {len(fails)} slit(s) not painted, e.g. {fails[0]}')
        if n_processes > 1:
            with Pool(min(n_processes, len(paint_args))) as pool:
                for res in pool.imap_unordered(_worker_paint, paint_args):
                    _finish(res)
        else:
            for a in paint_args:
                _finish(_worker_paint(a))
        if any(fo_of.values()):
            _failed_open_over_nods(obs, fo_of, float(opt['failed_open_nod_tol']))

    if plot:
        for out in written:
            base = os.path.basename(out)[:-len('_mbkg.fits')]
            try:
                plot_product(out, qa_path(obs, base), obs.name)
            except Exception as e:
                log(f'master background: QA plot of {base} failed', e)
    return written
