"""
spike_mask: record a diffraction-spike mask on each canonical exposure.

Combine-phase step, run per filter after ``apply_mask`` and before the working
copies are made. It is an ensemble step: whether a spike pixel is masked depends
on what the exposures at the other roll angles cover (see
``campfire_pipeline.nircam.spike_mask``), so the whole filter is recomputed
whenever any exposure lacks the ``CFP_SPKE`` stamp.

The result is a ``CFSPIKE`` uint8 extension (1 = masked) on the canonical file.
**Beta.** Developed and checked on one field (MACS0647, two roll angles); the
arm width, threshold and flux prediction are not yet tested on many fields.

SCI and DQ are not touched. ``Field.materialize_work`` fuses CFSPIKE into the
working copy's DQ as DO_NOT_USE when the step is enabled with
``mode = "mask"``; with ``mode = "report"`` the extension is written but not
used, so the footprints can be inspected first.

Disabled by default (``[nircam.spike_mask].enabled``).
"""

import json
import os

import numpy as np
from astropy.io import fits

from campfire_pipeline.common.io import log, atomic_save
from campfire_pipeline.common import cfp
from campfire_pipeline.nircam import spike_mask as sm

_DO_NOT_USE = 1


def _decimal_year(date_obs):
    from astropy.time import Time
    return float(Time(date_obs).decimalyear)


def _read_exposure(path):
    """Everything the step needs from one canonical exposure."""
    from astropy.wcs import WCS
    import warnings
    with fits.open(path, memmap=False) as hdul:
        phdr, shdr = hdul[0].header, hdul['SCI'].header
        sci = np.asarray(hdul['SCI'].data, dtype=np.float32)
        dq = np.asarray(hdul['DQ'].data)
        valid = np.isfinite(sci) & ((dq & _DO_NOT_USE) == 0)
        if 'CFMASK' in hdul:
            valid &= ~np.asarray(hdul['CFMASK'].data).astype(bool)
        quiet = valid.copy()
        if 'SRCMASK' in hdul:
            quiet &= ~np.asarray(hdul['SRCMASK'].data).astype(bool)
        v = sci[quiet] if quiet.sum() > 1000 else sci[valid]
        med = np.median(v)
        sigma = 1.4826 * float(np.median(np.abs(v - med)))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wcs = WCS(shdr, relax=True)
        return dict(
            wcs=wcs, shape=sci.shape, valid=valid, sigma=sigma,
            roll_ref=float(shdr['ROLL_REF']), ra_ref=float(shdr['RA_REF']),
            dec_ref=float(shdr['DEC_REF']), pa_v3=float(shdr['PA_V3']),
            year=_decimal_year(phdr['DATE-OBS']),
            channel='SW' if phdr['DETECTOR'].upper()[-1].isdigit() else 'LW',
        )


def _load_stars(field, cfg, ra0, dec0, radius_deg):
    """Star list for the field: a user catalog, the cached query, or Gaia."""
    from astropy.table import Table
    path = cfg.get('catalog') or os.path.join(field.reference_dir,
                                              'spike_stars.ecsv')
    if os.path.exists(path):
        t = Table.read(path)
        covers = (cfg.get('catalog') or (
            abs(t.meta.get('ra0', 1e9) - ra0) * np.cos(np.radians(dec0)) < 1e-3
            and abs(t.meta.get('dec0', 1e9) - dec0) < 1e-3
            and t.meta.get('radius_deg', 0) >= radius_deg - 1e-6
            and t.meta.get('g_max', 0) >= cfg['gaia_g_max']))
        if covers:
            log(f"spike_mask: {len(t)} stars from {path}")
            out = {k: np.asarray(t[k], dtype=float)
                   for k in ('ra', 'dec', 'g', 'bp_rp', 'pmra', 'pmdec')}
            return out
    log(f"spike_mask: querying Gaia DR3 ({radius_deg * 60:.1f}' radius, "
        f"G < {cfg['gaia_g_max']})")
    stars = sm.query_gaia(ra0, dec0, radius_deg, g_max=cfg['gaia_g_max'])
    if stars.pop('truncated'):
        log("spike_mask: WARNING Gaia query hit its row limit; the faintest "
            "stars are missing (lower gaia_g_max or supply `catalog`)")
    t = Table({k: stars[k] for k in ('ra', 'dec', 'g', 'bp_rp', 'pmra', 'pmdec')})
    t.meta.update(ra0=ra0, dec0=dec0, radius_deg=radius_deg,
                  g_max=cfg['gaia_g_max'], source='Gaia DR3, epoch 2016.0')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    t.write(path, overwrite=True)
    return stars


def build_spike_masks(filtname, exposures, field, cfg):
    """Compute the spike mask of every exposure of one filter.

    Returns ``{path: dict(mask, n_raw, n_masked, sigma, cluster, stars)}``.
    """
    from scipy.ndimage import binary_dilation, binary_opening

    pivot = sm.PIVOT_UM[filtname.upper()]
    meta = {p: _read_exposure(p) for p in exposures}
    channels = {m['channel'] for m in meta.values()}
    models = {ch: sm.load_model(ch, pivot) for ch in channels}

    # field centre / extent from the exposure reference points
    ras = np.array([m['ra_ref'] for m in meta.values()])
    decs = np.array([m['dec_ref'] for m in meta.values()])
    ra0, dec0 = float(np.mean(ras)), float(np.mean(decs))
    ex, ey = sm.gnomonic(ras, decs, ra0, dec0)
    half = float(np.max(np.hypot(ex, ey))) + 150.0       # + one LW detector
    max_radius = float(cfg['max_radius_arcsec'])
    stars = _load_stars(field, cfg, ra0, dec0, (half + max_radius) / 3600.0)

    mag = sm.band_ab_mag(stars['g'], stars['bp_rp'], pivot)
    keep = mag < float(cfg['mag_limit'])
    stars = {k: v[keep] for k, v in stars.items()}
    mag = mag[keep]
    log(f"spike_mask: {filtname}: {keep.sum()} stars brighter than "
        f"{cfg['mag_limit']} AB (predicted in band)")

    grid = sm.SkyGrid(ra0, dec0, half, float(cfg['gate_cell_arcsec']))
    paths = list(exposures)
    clusters = sm.cluster_position_angles(
        [meta[p]['pa_v3'] for p in paths], float(cfg['pa_cluster_deg']))
    n_cl = int(clusters.max()) + 1
    log(f"spike_mask: {filtname}: {len(paths)} exposures at {n_cl} roll angle(s)")
    # per roll angle and sky cell: the lowest predicted spike brightness among
    # the usable pixels there (inf where that angle has no usable pixel)
    best = np.full((n_cl, grid.n * grid.n), np.inf, dtype=np.float32)

    raw_px, raw_cell, raw_rel, used_stars = {}, {}, {}, {}
    for p, cl in zip(paths, clusters):
        m = meta[p]
        model = models[m['channel']]
        ny, nx = m['shape']
        yy, xx = np.mgrid[0:ny, 0:nx]
        ra, dec = m['wcs'].all_pix2world(xx.ravel(), yy.ravel(), 0)
        s_ra, s_dec = sm.stars_at_epoch(stars, m['year'])
        amp = sm.star_amplitude(mag, model.pixarea0)
        level = float(cfg['threshold_sigma']) * m['sigma']
        sb, used = sm.predict_spike_sb(
            ra, dec, s_ra, s_dec, amp, model, pivot / model.wave,
            m['roll_ref'], m['ra_ref'], m['dec_ref'], level, max_radius,
            roll_tol=float(cfg['roll_tolerance_deg']),
            width_scale=float(cfg['width_scale_lw'] if m['channel'] == 'LW'
                              else cfg['width_scale']))
        raw = (sb >= level).reshape(ny, nx)
        if cfg['grow'] > 0 and raw.any():
            raw = binary_dilation(raw, iterations=int(cfg['grow']))
        raw = raw.ravel()
        # in units of the threshold, so exposures of different depth compare;
        # pixels added by the dilation count as "at threshold"
        rel = sb / np.float32(level)
        rel[raw & (rel < 1)] = 1.0
        cell = grid.index(ra, dec)
        ok = m['valid'].ravel() & (cell >= 0)
        np.minimum.at(best[cl], cell[ok], rel[ok])
        raw_px[p] = np.nonzero(raw)[0]
        raw_cell[p] = cell[raw_px[p]]
        raw_rel[p] = rel[raw_px[p]]
        used_stars[p] = [(float(s_ra[i]), float(s_dec[i]), float(mag[i]))
                         for i in used]

    # A spike pixel is masked where another roll angle sees the same sky with
    # less predicted spike light: spike-free if possible, otherwise the fainter
    # of two crossing spikes is kept and the brighter one masked.
    require = bool(cfg['require_other_angle'])
    open_iter = int(cfg['min_width_px']) // 2
    out = {}
    for p, cl in zip(paths, clusters):
        m = meta[p]
        cell = raw_cell[p]
        if require:
            other = np.full(cell.shape, np.inf, dtype=np.float32)
            for c2 in range(n_cl):
                if c2 != cl:
                    other = np.minimum(other, best[c2, np.maximum(cell, 0)])
            keep = (cell >= 0) & ((other < 1.0) | (other < raw_rel[p]))
        else:
            keep = np.ones(cell.shape, dtype=bool)
        mask = np.zeros(m['shape'][0] * m['shape'][1], dtype=bool)
        mask[raw_px[p][keep]] = True
        mask = mask.reshape(m['shape'])
        if open_iter > 0 and mask.any():
            # drop fragments narrower than min_width_px (ragged edges where the
            # two angles' patterns interleave); this only ever un-masks
            mask = binary_opening(mask, iterations=open_iter)
        masked = np.nonzero(mask.ravel())[0]
        mask = mask.astype(np.uint8)
        out[p] = dict(mask=mask, n_raw=int(raw_px[p].size),
                      n_masked=int(masked.size), sigma=m['sigma'],
                      cluster=int(cl), pa_v3=m['pa_v3'], stars=used_stars[p])
    return out


def spike_mask_filter(filtname, exposures, field, cfg, overwrite=False,
                      status=None):
    """Compute and record CFSPIKE for every exposure of ``filtname``."""
    results = build_spike_masks(filtname, exposures, field, cfg)
    fuse = cfg.get('mode', 'mask') == 'mask'
    report = {}
    for path, res in results.items():
        root = os.path.basename(path).removesuffix('.fits')
        hdu = fits.ImageHDU(res['mask'], name='CFSPIKE')
        hdu.header['SPKFUSE'] = (fuse, 'mask is applied in the combine phase')
        hdu.header['SPKTHR'] = (float(cfg['threshold_sigma']),
                                'predicted SB threshold [exposure sigma]')
        hdu.header['SPKSIG'] = (res['sigma'], 'exposure pixel noise [MJy/sr]')
        hdu.header['SPKNRAW'] = (res['n_raw'], 'predicted spike pixels')
        hdu.header['SPKNMSK'] = (res['n_masked'], 'of those, masked')
        hdu.header['SPKNSTAR'] = (len(res['stars']), 'stars contributing')
        # short enough that the card keeps its comment (threshold is SPKTHR)
        value = (f"nstar={len(res['stars'])} raw={res['n_raw']} "
                 f"masked={res['n_masked']}")
        with fits.open(path, memmap=False) as hdul:
            atomic_save(hdul, path,
                        header_updates=cfp.format(CFP_SPKE=value),
                        extra_hdus=[hdu])
        report[root] = {k: res[k] for k in
                        ('n_raw', 'n_masked', 'sigma', 'cluster', 'pa_v3', 'stars')}
        log(f"spike_mask: {root}: {res['n_masked']}/{res['n_raw']} predicted "
            f"spike px masked ({len(res['stars'])} stars)")
    with open(os.path.join(field.filter_dir(filtname),
                           f'spike_mask_{filtname}.json'), 'w') as fh:
        json.dump(report, fh, indent=1)
