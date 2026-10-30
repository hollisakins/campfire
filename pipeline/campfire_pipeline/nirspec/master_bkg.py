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
* ``MBKG2D`` / ``MBKG2DN`` / ``MBKG2DDQ`` — the sky painted back onto the detector in the
  rate frame (DN/s), every covering slitlet's sky summed where footprints overlap (on the
  gratings 38 % of the detector lies under more than one trace, §9), the number of slits
  painted per pixel, and flags: 1 nothing covers the pixel, 2 a covering trace was not
  painted (its source was not processed, or a failed-open shutter), 4 a trace's
  beyond-cutoff tail.

The inputs are what stage 2a already produced: the per-source canonicals (their empty
shutters) and the background-only slitlets of the ``bkgslits/`` file. Pixels are read
through ``barshadow_calibration.pixel_samples`` (uniform calibration, the source and
every catalogued neighbour padded away, DQ, one footprint only on the tail-aware
coverage counter); only the shutter core |f| ≤ ``core_frac`` enters the 1-D sky, so no
bar-shadow model touches the measurement (§9). Selection is the symmetric
scale-and-reject iteration of §4.2 step 4 against the per-bin median seed. The sky is
measured per exposure × detector — the grain the data demand (ember_uds_p2's second
exposure sits 14 % above the others at 3 µm) — and compared with the pooled sky of its
exposure group (every nod, both detectors): the scale and offset against the pool are
recorded (``MBKGSCL``, ``MBKGOFF``), never applied, and the pool stands in when an
exposure has too few clean shutters (``MBKGSRC``). Per-quadrant scales against the
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
from campfire_pipeline.nirspec.coverage import coverage_from_wcs, tail_px_for, default_cache_dir, cache_path

ALGORITHM_VERSION = '1.0'
_PREFIX = 'master_bkg_'
DEFAULTS = dict(
    master_bkg_source_pad_px=3.0,     # rows beyond the source shutter's edges dropped (§9: 3 px)
    master_bkg_core_frac=0.2,         # |f| of the shutter core that measures the 1-D sky
    master_bkg_bin_pixels=2.0,        # detector pixels per wavelength bin
    master_bkg_min_pixels=20,         # pixels a shutter needs
    master_bkg_min_bins=10,           # bins a shutter needs
    master_bkg_clip_sigma=3.0,        # per-bin clipping when shutters are combined
    master_bkg_line_sigma=4.0,        # a run of bins above this many sigma is a line
    master_bkg_line_min_bins=3,
    master_bkg_excess_min=0.05,       # reject a shutter brighter than the sky by this (and 3 sigma)
    master_bkg_low_min=0.30,          # reject a shutter fainter than the sky by this (and 3 sigma)
    master_bkg_n_iter=3,
    master_bkg_min_shutters=5,        # below this the exposure takes the group's pooled sky
    master_bkg_error_floor=0.03,      # fraction of the sky added in quadrature downstream
    master_bkg_quadrant_scales=True,
    master_bkg_model_check=True,      # compare with jwst_backgrounds when installed
    master_bkg_write_2d=True,
    master_bkg_plot=True,
)
REASONS = ('clean', 'too_few_bins', 'low_throughput', 'continuum_excess', 'line_excess')
DQ_UNCOVERED = 1          # no slitlet footprint on the pixel
DQ_UNPAINTED = 2          # a footprint covers the pixel but no slit was painted for it
DQ_TAIL = 4               # a trace's beyond-cutoff tail lands here


def options(stage_config):
    """The ``master_bkg_*`` knobs of ``[nirspec.stage2]`` without their prefix."""
    return {k[len(_PREFIX):]: stage_config.get(k, v) for k, v in DEFAULTS.items()}


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


def product_current(path, rate_path, canonicals, stuck_mtime, bs_name=None, bs_stamp=None):
    """True when ``path`` was derived from these inputs by this algorithm (and, when
    ``bs_name`` / ``bs_stamp`` are given, with that bar-shadow table, of that
    derivation date, in the 2-D expansion)."""
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
            and (bs_stamp is None or str(hdr.get('MBKGBSDT')) == bs_stamp))


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


def _worker_samples(args):
    c, counter_path, edges, opt = args
    counter = np.load(counter_path) if counter_path else None
    try:
        p = B.pixel_samples(c, counter, opt['source_pad_px'])
    except Exception as e:
        return c.key, [], f'{type(e).__name__}: {e}'
    core = p['is_open'] & (np.abs(p['f']) <= opt['core_frac'])
    out = []
    for kk in np.unique(p['k'][core]):
        sel = core & (p['k'] == kk)
        if sel.sum() < opt['min_pixels']:
            continue
        sb, err, n = bin_spectrum(p['lam'][sel], p['sb'][sel], p['var'][sel], edges)
        if np.isfinite(sb).sum() < opt['min_bins']:
            continue
        out.append(ShutterSpectrum(rate_base=c.rate_base, source_id=c.source_id, slitlet_id=p['slitlet_id'],
                                   quadrant=p['quadrant'], k=int(kk), n_pix=int(sel.sum()), sb=sb, err=err, n=n))
    return c.key, out, None


def shutter_spectra(canonicals, counters, edges_of, opt, processes):
    """Pass 1 over every slit: the empty shutters' spectra, grouped by rate file."""
    from multiprocessing import Pool
    args = [(c, counters.get(c.rate_path), edges_of[(c.grating, c.filter)], opt) for c in canonicals]
    by_rate, failures = defaultdict(list), []
    seen = set()          # (rate_base, slitlet, shutter): two sources of one slitlet offer the same shutters

    def take(key, out, err):
        if err:
            failures.append((key, err)); return
        for s in out:
            tag = (s.rate_base, s.slitlet_id, s.k)
            if tag in seen:
                continue
            seen.add(tag)
            by_rate[s.rate_base].append(s)

    if processes > 1:
        with Pool(processes) as pool:
            for key, out, err in pool.imap_unordered(_worker_samples, args, chunksize=4):
                take(key, out, err)
    else:
        for a in args:
            take(*_worker_samples(a))
    return by_rate, failures


# ---------------------------------------------------------------------------
# Selection (design §4.2 step 4)
# ---------------------------------------------------------------------------

def combine(spectra, clip_sigma):
    """Sigma-clipped inverse-variance mean over shutters per bin -> (sb, err, n_shutters)."""
    if not spectra:
        return None
    S = np.array([s.sb for s in spectra], float); E = np.array([s.err for s in spectra], float)
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
        p = B.crds_barshadow_path(example_path)
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
        path = B.crds_barshadow_path(slit_path)
    return B.load_table(path)


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
                   mbkg2d=None, dq2d=None, n2d=None, rate=None)
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
    if str(pthloss).upper() == 'POINT' and pu is not None and pp is not None:
        with np.errstate(invalid='ignore', divide='ignore'):
            f = f * (pu / pp)
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
        pu = B._hdu(h, 'PATHLOSS_UN', v); pu = np.array(pu.data, float) if pu is not None else None
        pp = B._hdu(h, 'PATHLOSS_PS', v); pp = np.array(pp.data, float) if pp is not None else None
        bar = B._hdu(h, 'BARSHADOW', v) if str(ph.get('S_BARSHA', '')).upper() == 'COMPLETE' else None
        bar = np.array(bar.data, float) if bar is not None else None
        return dict(sci=sci, bunit=str(hs.get('BUNIT', 'MJy')), pix_sr=float(hs['PIXAR_SR']),
                    pthloss=str(hs.get('PTHLOSS', 'POINT')), pu=pu, pp=pp, bar=bar,
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


def sky_for_slit(product, path, extver=1, table=None, mode='total', mask_bits=DQ_UNPAINTED | DQ_TAIL,
                 slitlet_scale_on=False):
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
    ``own_sky`` carries the slit's own slitlet alone. ``mode='own'`` returns only the
    slit's own slitlet (the painter's input; also the fallback when the product has no
    ``MBKG2D`` or the rate file is not beside it). NaN where the product has no
    wavelength coverage, the pixel lies beyond the slitlet, nothing covers it, or the
    pixel carries one of ``mask_bits`` (default: a covering trace not painted, or a
    tail). ``slitlet_scale_on`` rescales the slit's own contribution by the median scale
    of its own clean empty shutters (``slitlet_scale``), the per-slitlet term of §9;
    off by default, a design choice rather than a correction.
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
    if mode == 'own' or not _load_2d(prod):
        return own
    scale = 1.0
    if slitlet_scale_on:
        name = os.path.basename(path)[:-5].split('_')[-1] if int(extver) == 1 else f'BKG{g["slitlet_id"]}'
        scale = slitlet_scale(prod, name)
    return sky_total_on_cutout(prod, own, frame, mask_bits=mask_bits, own_scale=scale)


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


def slitlet_scale(prod, slit_name):
    """The median scale of a slit's own clean empty shutters against the exposure's sky
    (the per-slitlet term of §9), from the ``SHUTTERS`` table; 1 when it has none."""
    t = prod.get('shutters')
    if t is None or len(t) == 0:
        return 1.0
    sel = (np.char.strip(t['slit'].astype(str)) == str(slit_name)) & np.asarray(t['clean'], bool)
    v = np.asarray(t['scale'], float)[sel]
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size else 1.0


def sky_total_on_cutout(prod, own, frame, mask_bits=DQ_UNPAINTED | DQ_TAIL, own_scale=1.0):
    """What a consumer subtracts: the painted total (every covering slitlet's sky,
    ``MBKG2D``) on the slit's cutout, in the slit's frame through the pixelwise factor.
    ``own`` is ``sky_on_geometry`` output for the slit. Pixels carrying any of
    ``mask_bits`` (default: a covering trace not painted, or a tail -- the total is
    incomplete there by construction) and pixels nothing covers are NaN. ``own_scale``
    rescales the slit's own contribution (``total + own * (s - 1)``), the per-slitlet
    term from its own empty shutters. Returns the dict with ``sky``, ``var``, ``dq``
    (the ``MBKG2DDQ`` cutout), ``n_cover`` and ``own_sky`` (the slit's own slitlet)."""
    ny, nx = own['shape']
    box = (slice(frame['y0'], frame['y0'] + ny), slice(frame['x0'], frame['x0'] + nx))
    mb = prod['mbkg2d'][box]; dq = prod['dq2d'][box]; rate_box = prod['rate'][box]
    if mb.shape != own['shape']:
        raise ValueError('cutout does not fit the detector frame')
    F = pixelwise_factor(frame['sci'], rate_box)
    total = mb * F
    own_sky = own['sky']
    if own_scale != 1.0:
        add = np.where(np.isfinite(own_sky), own_sky * (own_scale - 1.0), 0.0)
        total = total + add
    total[(dq & (DQ_UNCOVERED | int(mask_bits))) > 0] = np.nan
    with np.errstate(invalid='ignore', divide='ignore'):
        ratio = np.where(np.isfinite(own_sky) & (own_sky > 0), total / own_sky, np.nan)
    var = np.where(np.isfinite(ratio), own['var'] * ratio ** 2, (prod['floor'] * total) ** 2)
    var[~np.isfinite(total)] = np.nan
    return dict(own, sky=total, var=var, dq=dq, own_sky=own_sky, n_cover=prod['n2d'][box] if prod.get('n2d') is not None else None)


def _worker_paint(args):
    """Pass 2 for one rate file: every input slit's sky in its own frame, then back to
    the rate frame through the pixelwise factor ``rate / SCI`` (exact wherever the
    calibration is a product of per-pixel factors, §4.4), summed on the detector."""
    rate_path, product, canonicals, bs_path, counter_path, counter_notail_path = args
    table = B.load_table(bs_path)
    prod = load_product(product)
    with fits.open(rate_path, memmap=False) as h:
        rate = np.array(h['SCI'].data, float)
    # Two layers. A pixel inside an open shutter of several slitlets (traces crossing on
    # the gratings) receives every one's sky, so those are summed. A slit's margin and
    # closed-shutter rows (the half pitch beyond its outer shutters, which jwst's cutout
    # includes) carry only the closed-shutter leak -- unless they are the open shutter
    # of the slitlet next to it in the same MSA column, in which case the light there is
    # that neighbour's sky and the margin model must not be added to it. So margin
    # pixels count only where no open shutter was painted.
    mb_open = np.zeros(rate.shape, np.float32)
    mb_margin = np.zeros(rate.shape, np.float32)
    cover = np.zeros(rate.shape, np.int16)
    cover_margin = np.zeros(rate.shape, np.int16)
    n_painted, failures = 0, []
    painted_boxes = set()
    for c in canonicals:
        try:
            r = sky_for_slit(prod, c.path, c.extver, table, mode='own')
        except Exception as e:
            failures.append((c.key, f'{type(e).__name__}: {e}')); continue
        ny, nx = r['shape']
        key = (r['y0'], r['x0'], ny, nx)
        if key in painted_boxes:
            continue                      # a second source of the same slitlet: same cutout, same sky
        painted_boxes.add(key)
        box = (slice(r['y0'], r['y0'] + ny), slice(r['x0'], r['x0'] + nx))
        rbox = rate[box]
        if rbox.shape != r['shape']:
            continue
        with np.errstate(invalid='ignore', divide='ignore'):
            val = r['sky'] / pixelwise_factor(r['sci'], rbox)
        ok = np.isfinite(val)
        is_open = ok & r['open']
        mb_open[box][is_open] += val[is_open].astype(np.float32)
        cover[box][is_open] += 1
        marg = ok & ~r['open']
        mb_margin[box][marg] += val[marg].astype(np.float32)
        cover_margin[box][marg] += 1
        n_painted += 1
    with np.errstate(invalid='ignore', divide='ignore'):
        margin_only = (cover == 0) & (cover_margin > 0)
        mb = np.where(cover > 0, mb_open, np.where(margin_only, mb_margin / np.maximum(cover_margin, 1), 0)).astype(np.float32)
    cover = np.where(cover > 0, cover, np.where(margin_only, 1, 0)).astype(np.int16)
    dq = np.zeros(rate.shape, np.uint8)
    dq[cover == 0] |= DQ_UNCOVERED
    if counter_notail_path and os.path.exists(counter_notail_path):
        n_foot = np.load(counter_notail_path)
        dq[(n_foot > cover) & (n_foot > 0)] |= DQ_UNPAINTED
        if counter_path and os.path.exists(counter_path):
            tail = np.load(counter_path) - n_foot
            dq[tail > 0] |= DQ_TAIL
    return rate_path, mb, dq, np.clip(cover, 0, 255).astype(np.uint8), n_painted, failures


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


def write_product(path, hdr_cards, grid_centres, sky, pool, model, shutters, mb2d=None, dq2d=None, history=(), n2d=None):
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
        ax.plot(lam, t['surf_bright'], color='k', lw=1.2, label=f"this exposure x detector: {hdr.get('MBKGNSH')} clean of {hdr.get('MBKGNTOT')} shutters ({hdr.get('MBKGSRC')})")
        if np.isfinite(t['model_surf_bright']).any():
            ax.plot(lam, t['model_surf_bright'], color='tab:blue', ls='--', lw=1, label=f"jwst_backgrounds x {header_float(hdr, 'MBKGMDL'):.2f}")
        ax.set_ylabel('sky surface brightness [MJy/sr]'); ax.set_title(f'{obs_name} {base}: master background')
        hi = np.nanpercentile(t['surf_bright'], 99) if np.isfinite(t['surf_bright']).any() else 1
        ax.set_ylim(0, 2 * hi); ax.legend(fontsize=8)
        ax = axes[1]
        with np.errstate(invalid='ignore', divide='ignore'):
            ax.plot(lam, t['surf_bright'] / t['pool_surf_bright'], color='k', lw=0.8, label='this / pool')
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

def run_master_bkg(obs, stage_config, n_processes=1, overwrite=False, plot=True):
    """Derive the ``_mbkg.fits`` product of every rate file of ``obs`` from its stage-2a
    canonicals and background slitlets. Skips a product whose inputs have not changed
    unless ``overwrite``. Returns the paths written."""
    opt = options(stage_config)
    plot = plot and opt['plot']
    try:
        from campfire_pipeline import __version__ as version
    except Exception:
        version = 'unknown'
    inputs = discover_inputs(obs)
    stuck_mtime = obs.stuck_closed_shutters_mtime or 0.0
    todo, bs_name_of = {}, {}
    for rate in obs.rate_files:
        base = rate_base_of(rate)
        cans = inputs.get(base, [])
        if not cans:
            log(f'master background: {base}: no stage-2a inputs, skipped')
            continue
        out = product_path(obs, base)
        # a new bar-shadow table for the grating makes the product stale too
        if cans[0].grating not in bs_name_of:
            name, bs_path = _barshadow_reference(cans[0].grating, cans[0].path)
            bs_name_of[cans[0].grating] = (name, barshadow_stamp(bs_path))
        if not overwrite and product_current(out, rate, cans, stuck_mtime, *bs_name_of[cans[0].grating]):
            log(f'master background: {base}: current, skipped')
            continue
        todo[base] = (rate, cans)
    if not todo:
        return []
    log(f'master background for {obs.name}: {len(todo)} rate file(s), '
        f'{sum(len(v[1]) for v in todo.values())} input slits, options {opt}')

    # every rate file of the exposure groups involved contributes to the pools, so
    # sample the whole group even when only part of it is stale
    groups = {base.rsplit('_', 2)[0] for base in todo}
    sample = {base: inputs[base] for base in inputs if base.rsplit('_', 2)[0] in groups and inputs[base]}
    canonicals = [c for cans in sample.values() for c in cans]

    # with the extended-wavelength reduction on, the footprints must reach the extended
    # red end too, so the counter's assign_wcs takes the same wavelengthrange override
    extended_refs = {}
    if stage_config.get('extend_g140m_g235m'):
        from campfire_pipeline.nirspec.extended_wavelength import build_extended_reference_files
        extended_refs = build_extended_reference_files(obs.rate_files) or {}
    cache_dir = default_cache_dir()
    counters, counters_notail = {}, {}
    for base in sorted(sample):
        rate = os.path.join(obs.workspace_dir, f'{base}_rate.fits')
        if not os.path.exists(rate):
            continue
        c0 = sample[base][0]
        ext = extended_refs.get((c0.detector.upper(), c0.grating, c0.filter))
        wr = ext.get('wavelengthrange') if ext else None
        tail = tail_px_for(c0.grating)
        coverage_from_wcs(rate, cache_dir=cache_dir, tail_px=tail, wavelengthrange_override=wr)
        counters[rate] = cache_path(rate, cache_dir, tail, wr)
        if tail:
            coverage_from_wcs(rate, cache_dir=cache_dir, tail_px=0, wavelengthrange_override=wr)
        counters_notail[rate] = cache_path(rate, cache_dir, 0, wr)
    log(f'master background: {len(counters)} coverage counters')

    dispersers = sorted({(c.grating, c.filter) for c in canonicals})
    edges_of, bs_of = {}, {}
    for key in dispersers:
        cs = [c for c in canonicals if (c.grating, c.filter) == key]
        edges_of[key] = wavelength_grid(cs, opt['bin_pixels'])
        bs_of[key] = _barshadow_reference(key[0], cs[0].path)
        log(f'master background: {key[0]}/{key[1]}: {len(edges_of[key]) - 1} bins '
            f'{edges_of[key][0]:.3f}-{edges_of[key][-1]:.3f} um, bar shadow {bs_of[key][0]}')

    by_rate, failures = shutter_spectra(canonicals, counters, edges_of, opt, n_processes)
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
    pools = {}
    for grp in groups:
        members = [s for base, sp in shutters_of.items() if base.rsplit('_', 2)[0] == grp for s in sp if s.clean]
        pools[grp] = combine(members, opt['clip_sigma']) if members else None

    # 2-D expansion, parallel over rate files, after the 1-D products are written
    written, paint_args = [], []
    for base, (rate, cans) in sorted(todo.items()):
        key = (cans[0].grating, cans[0].filter)
        edges = edges_of[key]; centres = 0.5 * (edges[1:] + edges[:-1])
        spectra = shutters_of.get(base, by_rate.get(base, []))
        sky = skies.get(base)
        grp = base.rsplit('_', 2)[0]
        pool = pools.get(grp)
        n_clean = sum(1 for s in spectra if s.clean)
        src = 'exposure'
        if (sky is None or n_clean < opt['min_shutters']) and pool is not None:
            sky, src = pool, 'group'
        if sky is None:
            log(f'master background: {base}: no clean shutters and no pool; no product')
            continue
        a, ae, b, be, nfit = scale_offset(sky, pool) if (pool is not None and src == 'exposure') else (np.nan,) * 4 + (0,)
        qsc = quadrant_scales(spectra, sky, opt) if opt['quadrant_scales'] else {q: (np.nan, 0) for q in (1, 2, 3, 4)}
        model, mscale = None, np.nan
        if opt['model_check']:
            try:
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
            'MBKGSRC': (src, 'MBKG1D: this exposure, or the group pool'),
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
        write_product(out, cards, centres, sky, pool, model, spectra, history=hist)
        written.append(out)
        log(f'master background: {base}: {n_clean}/{len(spectra)} shutters clean ({src}); '
            f'scale vs pool {a:.4f}, offset/sky {cards["MBKGOFFR"][0]:+.4f}; data/model {mscale:.3f}')
        if opt['write_2d']:
            paint_args.append((rate, out, cans, bs_of[key][1], counters.get(rate), counters_notail.get(rate)))

    if paint_args:
        from multiprocessing import Pool
        def _finish(res):
            rate, mb, dq, cover, n_painted, fails = res
            out = product_path(obs, rate_base_of(rate))
            with fits.open(out, memmap=False) as h:
                hdul = fits.HDUList([x.copy() for x in h])
            hdul[0].header['MBKGN2D'] = (n_painted, 'slits painted into MBKG2D')
            hdul[0].header['MBKGDQ1'] = ('1 uncovered, 2 covering trace unpainted, 4 tail', 'MBKG2DDQ bits')
            hdul.append(fits.ImageHDU(mb, name='MBKG2D')); hdul[-1].header['BUNIT'] = 'DN/s'
            hdul.append(fits.ImageHDU(cover, name='MBKG2DN'))
            hdul.append(fits.ImageHDU(dq, name='MBKG2DDQ'))
            atomic_save(hdul, out)
            if fails:
                log(f'master background: {rate_base_of(rate)}: {len(fails)} slit(s) not painted, e.g. {fails[0]}')
        if n_processes > 1:
            with Pool(min(n_processes, len(paint_args))) as pool:
                for res in pool.imap_unordered(_worker_paint, paint_args):
                    _finish(res)
        else:
            for a in paint_args:
                _finish(_worker_paint(a))

    if plot:
        for out in written:
            base = os.path.basename(out)[:-len('_mbkg.fits')]
            try:
                plot_product(out, qa_path(obs, base), obs.name)
            except Exception as e:
                log(f'master background: QA plot of {base} failed', e)
    return written
