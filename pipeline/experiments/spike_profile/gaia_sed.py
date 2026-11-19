"""Gaia stars with the near-infrared photometry the Gaia archive already
cross-matches (2MASS J H Ks, AllWISE W1 W2), and in-band AB magnitudes from them.

predict_ab(stars, pivot_um, mode):
  'nir'    interpolate (in log wavelength) through the measured AB magnitudes
           G, RP, J, H, Ks, W1, W2 that exist for the star
  'locus'  the same, with J..W2 replaced by the colour relation G-X(BP-RP)
           fitted to stars that have them (for stars with Gaia only)
"""
import os, numpy as np
from astropy.table import Table

# pivot wavelengths [um] and Vega -> AB offsets
BANDS = {'g': (0.622, 0.114), 'rp': (0.777, 0.356), 'j': (1.235, 0.91), 'h': (1.662, 1.39),
         'ks': (2.159, 1.85), 'w1': (3.353, 2.699), 'w2': (4.603, 3.339)}
ORDER = ['g', 'rp', 'j', 'h', 'ks', 'w1', 'w2']


def query(ra, dec, radius_deg, g_max=19.5, cache=None):
    if cache and os.path.exists(cache):
        return Table.read(cache)
    from astroquery.gaia import Gaia
    from astropy.table import join, vstack
    q = f"""
    SELECT TOP 5000 g.source_id, g.ra, g.dec, g.phot_g_mean_mag AS g, g.bp_rp,
           g.phot_rp_mean_mag AS rp, g.pmra, g.pmdec
    FROM gaiadr3.gaia_source AS g
    WHERE g.phot_g_mean_mag < {g_max}
      AND 1 = CONTAINS(POINT('ICRS', g.ra, g.dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg}))
    ORDER BY g.phot_g_mean_mag
    """
    t = Gaia.launch_job(q).get_results()
    t.rename_columns(t.colnames, [c.lower() for c in t.colnames])
    ids = [int(x) for x in t['source_id']]
    tm, aw = [], []
    for i in range(0, len(ids), 200):
        lst = ','.join(str(x) for x in ids[i:i + 200])
        r = Gaia.launch_job(f"""
        SELECT xt.source_id, tm.j_m AS j, tm.h_m AS h, tm.ks_m AS ks
        FROM gaiadr3.tmass_psc_xsc_best_neighbour AS xt
        JOIN gaiadr3.tmass_psc_xsc_join AS xj ON xt.clean_tmass_psc_xsc_oid = xj.clean_tmass_psc_xsc_oid
        JOIN gaiadr1.tmass_original_valid AS tm ON xj.original_psc_source_id = tm.designation
        WHERE xt.source_id IN ({lst})""").get_results()
        if len(r): tm.append(r)
        r = Gaia.launch_job(f"""
        SELECT xa.source_id, aw.w1mpro AS w1, aw.w2mpro AS w2
        FROM gaiadr3.allwise_best_neighbour AS xa
        JOIN gaiadr1.allwise_original_valid AS aw ON xa.original_ext_source_id = aw.designation
        WHERE xa.source_id IN ({lst})""").get_results()
        if len(r): aw.append(r)
    for extra, cols in ((tm, ('j', 'h', 'ks')), (aw, ('w1', 'w2'))):
        if extra:
            e = vstack(extra)
            e.rename_columns(e.colnames, [c.lower() for c in e.colnames])
            t = join(t, e, keys='source_id', join_type='left')
        else:
            for c in cols: t[c] = np.nan
    for c in t.colnames:
        if c != 'source_id':
            t[c] = np.ma.filled(np.ma.asarray(t[c], dtype=float), np.nan)
    t.sort('g')
    if cache:
        t.write(cache, overwrite=True)
    return t


def fit_locus(t, deg=3):
    """G - X (Vega) against BP-RP for X in J, H, Ks, W1, W2."""
    out = {}
    c = np.asarray(t['bp_rp'], float)
    for b in ('j', 'h', 'ks', 'w1', 'w2'):
        y = np.asarray(t['g'], float) - np.asarray(t[b], float)
        ok = np.isfinite(c) & np.isfinite(y) & (c > 0.2) & (c < 3.5) & (np.asarray(t['g']) < 17.5)
        p = np.polyfit(c[ok], y[ok], deg)
        for _ in range(3):
            res = y - np.polyval(p, c)
            s = 1.4826 * np.median(np.abs(res[ok] - np.median(res[ok])))
            ok2 = ok & (np.abs(res) < 3 * s)
            p = np.polyfit(c[ok2], y[ok2], deg)
        out[b] = (p, float(s), int(ok2.sum()))
    return out


def predict_ab(t, pivot_um, mode='nir', locus=None):
    n = len(t)
    c = np.clip(np.where(np.isfinite(t['bp_rp']), t['bp_rp'], 1.5), 0.2, 3.5)
    lam = np.array([BANDS[b][0] for b in ORDER])
    mags = np.full((n, len(ORDER)), np.nan)
    for k, b in enumerate(ORDER):
        v = np.asarray(t[b], float).copy()
        if b not in ('g', 'rp') and (mode == 'locus' or locus is not None):
            model = np.asarray(t['g'], float) - np.polyval(locus[b][0], c)
            v = model if mode == 'locus' else np.where(np.isfinite(v), v, model)
        mags[:, k] = v + BANDS[b][1]
    out = np.full(n, np.nan)
    x = np.log(pivot_um)
    for i in range(n):
        ok = np.isfinite(mags[i])
        if ok.sum() < 2:
            continue
        xl, m = np.log(lam[ok]), mags[i, ok]
        if x <= xl[-1]:
            out[i] = np.interp(x, xl, m)
        else:                       # Rayleigh-Jeans beyond the reddest band
            out[i] = m[-1] + 5.0 * np.log10(pivot_um / lam[ok][-1])
    return out


if __name__ == '__main__':
    t = query(150.0399, 2.3968, 0.09)
    print(len(t), 'stars;', {b: int(np.isfinite(t[b]).sum()) for b in ('j', 'ks', 'w1', 'w2')})
    print(t[:5])
