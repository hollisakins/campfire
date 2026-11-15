"""Build one measured arm profile from the calibration stars.

Each measured cross-section (measure_arms.py: median brightness against
distance r from the star and offset p across the arm) is divided by the star's
in-band flux (from 2MASS / WISE) and put on wavelength-scaled axes:

    u = r / wavelength,  v = |p| / wavelength   [arcsec / um]
    T = brightness x wavelength^2 / star flux   [um^2 / sr]

on which every filter and star follows one curve (collapse_test.py). T(u, v) is
the median over all stars and filters, for the six segment arms and the two
strut arms separately. It is then made monotone (falling away from the axis and
with distance) and continued beyond the last measured distance with the power
law fitted to the peak.

Writes spike_profile_measured.fits (HDUs HEX, STRUT; axes in the header) and a
figure.
"""
import os, glob, numpy as np
from astropy.io import fits
from astropy.table import Table
from scipy.stats import binned_statistic_2d
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from campfire_pipeline.nircam import spike_mask as sm
import gaia_sed as gs
D = os.path.dirname(os.path.abspath(__file__))
import sys
SRC = D + ('/' + sys.argv[1] if len(sys.argv) > 1 else '')   # 'pipe' = pipeline-processed
TAG = ('_' + sys.argv[1]) if len(sys.argv) > 1 else ''
NU, NV, DV = 48, 150, 0.03
U_EDGES = np.logspace(np.log10(1.5), np.log10(300.0), NU + 1)
V_EDGES = np.arange(NV + 1) * DV
UC, VC = np.sqrt(U_EDGES[1:] * U_EDGES[:-1]), 0.5 * (V_EDGES[1:] + V_EDGES[:-1])
hdus = [fits.PrimaryHDU()]
fig, ax = plt.subplots(2, 3, figsize=(20, 10))
for row, cls in enumerate(('hex', 'strut')):
    entries = []
    noises = []
    for f in sorted(glob.glob(SRC + '/arms_*_F*.npz')):
        star, filt = os.path.basename(f)[5:-4].rsplit('_', 1)
        z = np.load(f)
        if cls + '_data' not in z or filt not in sm.PIVOT_UM or star == 'p5398_c': continue
        cat = Table.read(f'{D}/gaia_{star}.ecsv')[:1]
        lam = sm.PIVOT_UM[filt]
        flux = 3631e-6 * 10 ** (-0.4 * gs.predict_ab(cat, lam, 'nir')[0])
        dat, rc, pc = z[cls + '_data'], z['rc'], z['pc']
        noise = z['sigma'] / np.sqrt(30)
        ap = np.abs(pc)
        # every distance with data on the axis counts, detected or not: dropping
        # the non-detections would bias the faint end of the profile high
        good = np.array([np.isfinite(d[ap < 0.3]).any() for d in dat])
        if good.sum() < 2: continue
        # each row: fold about the axis and interpolate onto the v grid
        rows_ = np.full((len(rc), NV), np.nan)
        for j in np.nonzero(good)[0]:
            fold = np.nanmean([dat[j][pc > 0], dat[j][pc < 0][::-1]], axis=0)
            pv = pc[pc > 0] / lam
            ok = np.isfinite(fold)
            if ok.sum() < 3: continue
            rows_[j] = np.interp(VC, pv[ok], fold[ok] * lam ** 2 / flux, left=np.nan, right=np.nan)
            first = pv[ok][0]
            rows_[j, VC < first] = fold[ok][0] * lam ** 2 / flux
        # between consecutive measured distances: interpolate in log distance
        t = np.full((NU, NV), np.nan)
        lu = np.log(rc / lam)
        gi = np.nonzero(good & np.isfinite(rows_[:, 0]))[0]
        for i, u in enumerate(np.log(UC)):
            k = np.searchsorted(lu[gi], u)
            if k == 0 or k == len(gi) or gi[k] - gi[k - 1] > 1: continue
            a_, b_ = gi[k - 1], gi[k]
            wgt = (u - lu[a_]) / (lu[b_] - lu[a_])
            t[i] = (1 - wgt) * rows_[a_] + wgt * rows_[b_]
        entries.append(t)
        noises.append(noise * lam ** 2 / flux)   # one cell's noise in profile units
    E = np.array(entries)
    n = np.isfinite(E).sum(0)
    # Combine with weights 1/noise^2: a bright star in a deep exposure measures
    # the faint parts of an arm that a fainter star cannot, and an unweighted
    # median lets the uninformative cross-sections outvote it. Weighted median
    # (robust to a galaxy on one arm); uncertainty from the summed weights.
    Wt = np.where(np.isfinite(E), 1.0 / np.array(noises)[:, None, None] ** 2, 0.0)
    order = np.argsort(np.where(np.isfinite(E), E, np.inf), axis=0)
    Es, Ws = np.take_along_axis(E, order, 0), np.take_along_axis(Wt, order, 0)
    cum = np.cumsum(Ws, axis=0)
    tot = cum[-1]
    pick = np.argmax(cum >= 0.5 * tot[None], axis=0)
    T = np.take_along_axis(Es, pick[None], 0)[0]
    T[(n < 1) | (tot <= 0)] = np.nan
    # That weighted value only decides which cross-sections can see the cell: a
    # cross-section votes if its own noise would show the weighted value at
    # 3 sigma. The profile is the plain median of the voters, so several stars
    # and filters agree before a faint feature is accepted (one field's
    # neighbouring spike or galaxy sits at a different scaled distance in each
    # filter and is voted out). No voters: zero.
    nz = np.array(noises)[:, None, None]
    votes = np.isfinite(E) & (3.0 * nz < np.where(np.isfinite(T), np.maximum(T, 0.0), 0.0)[None])
    nv = votes.sum(0)
    Tv = np.nanmedian(np.where(votes, E, np.nan), axis=0)
    measured = np.isfinite(T)
    T = np.where(nv > 0, Tv, 0.0)
    T[~measured] = np.nan
    err = np.where(tot > 0, 1.0 / np.sqrt(np.maximum(tot, 1e-300)), np.nan)
    # on the axis (innermost three cells)
    ax_val = np.nanmean(T[:, :3], axis=1)
    ax_err = np.nanmean(err[:, :3], axis=1) / np.sqrt(3)
    meas = np.isfinite(ax_val)
    det = meas & (ax_val > 3 * ax_err)
    # The arm is the run of detections outward from the star. A detection that
    # comes after three or more measured distances with nothing is not the arm.
    gap = 0
    for i in range(NU):
        if meas[i] and not det[i]:
            gap += 1
        elif det[i]:
            if gap >= 3:
                det[i:] = False
                break
            gap = 0
    # keep only what is measured: cells not at least 3x their uncertainty are zero
    weak = np.isfinite(T) & (T < 3.0 * err)
    T[weak] = 0.0
    raw = T.copy()
    iu = np.nonzero(det)[0]
    # power law through the detected axis values
    fit = UC[iu] > (15 if cls == 'hex' else 4)
    slope, icpt = np.polyfit(np.log(UC[iu][fit]), np.log(ax_val[iu][fit]), 1)
    law = np.exp(icpt) * UC ** slope
    # Where does the arm end? Beyond the last detection, a measured distance
    # whose 3-sigma upper limit is under a third of the power law says the arm
    # is not there: the profile is zero from the first such distance outward.
    # With no such distance, the power law continues the arm.
    last = iu.max()
    absent = meas & ~det & (np.arange(NU) > last) & (ax_val + 3 * ax_err < law / 3.0)
    end = int(np.nonzero(absent)[0].min()) if absent.any() else None
    for i in np.nonzero(det)[0]:
        T[i, :3] = ax_val[i]                  # a detected axis is never zeroed
        r_ = T[i]; ok = np.isfinite(r_)
        r_ = np.interp(VC, VC[ok], r_[ok], right=0.0)
        T[i] = np.minimum.accumulate(np.maximum(r_, 0.0))
    # An arm gets wider with distance. Half-width where the cross-section falls
    # to 10% of the axis value, against distance, as a power law; rows beyond
    # the detections keep widening at that rate.
    def hw10(row):
        below = np.nonzero(row < 0.1 * row[0])[0]
        return VC[below[0]] if below.size else np.nan
    hw = np.array([hw10(T[i]) if det[i] else np.nan for i in range(NU)])
    okw = np.isfinite(hw) & (UC > 8)
    beta = np.polyfit(np.log(UC[okw]), np.log(hw[okw]), 1)[0] if okw.sum() > 3 else 0.0
    beta = float(np.clip(beta, 0.0, 1.0))
    for i in range(NU):
        if det[i]:
            continue
        if end is not None and i >= end:
            T[i] = 0.0
        else:
            j = iu[np.argmin(np.abs(np.log(UC[iu]) - np.log(UC[i])))]
            stretch = (UC[i] / UC[j]) ** beta
            T[i] = np.interp(VC / stretch, VC, T[j], right=0.0) * (UC[i] / UC[j]) ** slope
    print(f'{cls}: half-width at 10% of the axis grows as distance^{beta:.2f}; '
          + ', '.join(f'u={UC[i]:.0f}: {hw[i]:.2f}' for i in np.nonzero(okw)[0][::4]))
    T = np.maximum.accumulate(T[::-1], axis=0)[::-1]
    print(f'{cls}: {len(E)} star-filter cross-sections; detected on the axis u = {UC[iu].min():.1f}-{UC[last]:.0f}; '
          f'slope {slope:.2f}; ' + (f'arm ends at u = {UC[end]:.0f} (upper limit there {(ax_val[end] + 3 * ax_err[end]) / law[end]:.2f} of the power law)'
                                   if end is not None else 'continued by the power law'))
    h = fits.ImageHDU(T.astype(np.float32), name=cls.upper())
    h.header['U0'] = (U_EDGES[0], 'first distance bin edge [arcsec/um]')
    h.header['U1'] = (U_EDGES[-1], 'last distance bin edge [arcsec/um], log spaced')
    h.header['DV'] = (DV, 'offset bin [arcsec/um], from 0')
    h.header['SLOPE'] = (slope, 'power law of the peak beyond U1')
    h.header['BETA'] = (beta, 'half-width grows as distance^BETA')
    h.header['BUNIT'] = 'um2/sr'
    h.header['NCURVE'] = len(E)
    hdus.append(h)
    ext = [0, V_EDGES[-1], 0, NU]
    ax[row, 0].imshow(np.log10(np.maximum(raw, 1e3)), origin='lower', aspect='auto', extent=ext, vmin=4, vmax=8.5)
    ax[row, 0].set_title(f'{cls}: median of the measurements (log10)')
    ax[row, 1].imshow(np.log10(np.maximum(T, 1e3)), origin='lower', aspect='auto', extent=ext, vmin=4, vmax=8.5)
    ax[row, 1].set_title('profile used (monotone, continued)')
    for a in ax[row, :2]: a.set_xlabel('offset across the arm [arcsec/um]'); a.set_ylabel('distance bin (log, 1.5-300 arcsec/um)')
    for e in E: ax[row, 2].plot(UC, e[:, 0], '.-', color='grey', lw=0.5, ms=2)
    ax[row, 2].errorbar(UC[meas], np.maximum(ax_val[meas], 1e2), 3 * ax_err[meas], fmt='k.', ms=4, lw=0.8, label='median, 3 sigma')
    ax[row, 2].set_ylim(1e2, 1e9)
    ax[row, 2].plot(UC, T[:, 0], 'r', lw=2, label='profile used, on the axis')
    ax[row, 2].set_xscale('log'); ax[row, 2].set_yscale('log'); ax[row, 2].legend()
    ax[row, 2].set_xlabel('distance [arcsec/um]'); ax[row, 2].set_ylabel('brightness x wavelength^2 / star flux [um^2/sr]')
fits.HDUList(hdus).writeto(D + f'/spike_profile_measured{TAG}.fits', overwrite=True)
plt.tight_layout(); plt.savefig(D + f'/spike_profile_measured{TAG}.png', dpi=55)
print(D + f'/spike_profile_measured{TAG}.png')
