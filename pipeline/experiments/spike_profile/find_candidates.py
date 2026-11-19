"""Candidate public NIRCam imaging with a bright star in or near the frame.

1. MAST: all public level-3 NIRCam imaging (one row per observation x filter).
2. Keep high galactic latitude.
3. CDS XMatch of the pointing centres against Tycho-2 within 150".
Writes mast_nircam_l3.ecsv, tyc_matches.ecsv.
"""
import os, numpy as np, warnings
warnings.simplefilter('ignore')
from astropy.table import Table, unique
from astropy.coordinates import SkyCoord
import astropy.units as u
D = os.path.dirname(os.path.abspath(__file__))
f1 = D + '/mast_nircam_l3.ecsv'
if not os.path.exists(f1):
    from astroquery.mast import Observations
    t = Observations.query_criteria(obs_collection='JWST', instrument_name='NIRCAM/IMAGE',
                                    dataRights='PUBLIC', calib_level=3, dataproduct_type='image')
    t = t['proposal_id', 'obs_id', 'target_name', 'filters', 't_exptime', 's_ra', 's_dec',
          'intentType', 'proposal_pi', 'obs_title']
    t.write(f1, overwrite=True)
t = Table.read(f1)
print(len(t), 'public level-3 NIRCam image rows')
c = SkyCoord(t['s_ra'], t['s_dec'], unit='deg')
t['b'] = c.galactic.b.deg
t = t[np.abs(t['b']) > 25]
print(len(t), 'at |b| > 25')
t['pra'] = np.round(t['s_ra'], 2); t['pdec'] = np.round(t['s_dec'], 2)
pts = unique(t['pra', 'pdec'])
print(len(pts), 'distinct pointings (0.01 deg)')
f2 = D + '/gaia_matches.ecsv'
if not os.path.exists(f2):
    from astroquery.gaia import Gaia
    from astropy.table import vstack
    Gaia.ROW_LIMIT = -1
    out = []
    N = 40
    for i in range(0, len(pts), N):
        sub = pts[i:i + N]
        cones = ' OR '.join(f"1=CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{r},{d},0.042))"
                            for r, d in zip(sub['pra'], sub['pdec']))
        q = ("SELECT TOP 2000 source_id, ra, dec, phot_g_mean_mag AS g, bp_rp FROM gaiadr3.gaia_source "
             f"WHERE phot_g_mean_mag < 12.5 AND ({cones})")
        for attempt in range(3):
            try:
                r = Gaia.launch_job(q).get_results(); break
            except Exception as e:
                print('retry', i, str(e)[:80]); r = None
        if r is not None and len(r):
            out.append(r)
        print(i, len(r) if r is not None else -1, flush=True)
    m = unique(vstack(out), 'source_id')
    m.write(f2, overwrite=True)
m = Table.read(f2)
print(len(m), 'Gaia stars with G < 12.5 within 150" of a pointing')
