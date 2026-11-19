"""Measured arm profile, version 2: brightness along the arm scales with
wavelength, the shape across the arm does not.

From the measured cross-sections (measure_arms_pipe.py, pipeline-processed
frames):

  AXIS(u)      brightness on the arm axis x wavelength^2 / star flux [um^2/sr]
               against distance / wavelength u [arcsec/um]. Every filter and
               star follows one curve (collapse_test.py).
  SHAPE(r, p)  cross-section divided by its axis value, against distance r and
               offset p in ARCSEC. Against p / wavelength the filters spread
               apart; in arcsec they agree (wing_scaling.py).

Both are combined the same way: a noise-weighted estimate decides which
star-filter cross-sections are sensitive enough to see a cell; the value is the
plain median of those, kept only where it is at least 3x its uncertainty (the
larger of the formal one and the voters' scatter / sqrt(number)).

For the six segment arms (HEX) and the two strut arms (STRUT). Writes
spike_profile_v2.fits and a figure.
"""
import os, glob, numpy as np
from astropy.io import fits
from astropy.table import Table
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from campfire_pipeline.nircam import spike_mask as sm
import gaia_sed as gs
D = os.path.dirname(os.path.abspath(__file__))
NU = 56
U_EDGES = np.logspace(np.log10(0.6), np.log10(300.0), NU + 1)
UC = np.sqrt(U_EDGES[1:] * U_EDGES[:-1])
NR = 32
R_EDGES = np.logspace(np.log10(2.5), np.log10(300.0), NR + 1)
RC = np.sqrt(R_EDGES[1:] * R_EDGES[:-1])
DP, NP = 0.1, 100
PC = (np.arange(NP) + 0.5) * DP


def regrid(x_src, rows, x_dst, good):
    """Rows measured at x_src (log-spaced) interpolated onto x_dst, only
    between consecutive good rows."""
    out = np.full((len(x_dst),) + rows.shape[1:], np.nan)
    gi = np.nonzero(good)[0]
    lx = np.log(x_src)
    for i, x in enumerate(np.log(x_dst)):
        k = np.searchsorted(lx[gi], x)
        if k == 0 or k == len(gi) or gi[k] - gi[k - 1] > 1: continue
        a, b = gi[k - 1], gi[k]
        w = (x - lx[a]) / (lx[b] - lx[a])
        out[i] = (1 - w) * rows[a] + w * rows[b]
    return out


def vote(E, NZ):
    """E (entries, ...) values, NZ (entries, ...) their noise. Returns the
    median of the entries able to see the weighted estimate, its uncertainty,
    and the number of voters."""
    fin = np.isfinite(E) & np.isfinite(NZ) & (NZ > 0)
    W = np.where(fin, 1.0 / np.where(fin, NZ, 1.0) ** 2, 0.0)
    order = np.argsort(np.where(fin, E, np.inf), axis=0)
    Es, Ws = np.take_along_axis(E, order, 0), np.take_along_axis(W, order, 0)
    cum = np.cumsum(Ws, axis=0); tot = cum[-1]
    pick = np.argmax(cum >= 0.5 * tot[None], axis=0)
    Tw = np.take_along_axis(Es, pick[None], 0)[0]
    Tw = np.where(tot > 0, Tw, np.nan)
    votes = fin & (3.0 * NZ < np.where(np.isfinite(Tw), np.maximum(Tw, 0.0), 0.0)[None])
    nv = votes.sum(0)
    Ev = np.where(votes, E, np.nan)
    with np.errstate(all='ignore'):
        T = np.nanmedian(Ev, axis=0)
        sc = 1.4826 * np.nanmedian(np.abs(Ev - T[None]), axis=0)
        rng = (np.nanmax(Ev, axis=0) - np.nanmin(Ev, axis=0)) / 2
    sc = np.where(nv >= 3, sc, rng)
    err = np.fmax(np.where(tot > 0, 1 / np.sqrt(np.maximum(tot, 1e-300)), np.inf),
                  np.where(nv >= 2, sc / np.sqrt(np.maximum(nv, 1)), np.inf))
    T = np.where(nv >= 2, T, 0.0)
    T = np.where(tot > 0, T, np.nan)            # nan = nobody measured it
    return T, err, nv


hdus = [fits.PrimaryHDU()]
fig, ax = plt.subplots(2, 3, figsize=(22, 11))
for row, cls in enumerate(('hex', 'strut')):
    EA, NA, ES, NS = [], [], [], []
    for f in sorted(glob.glob(D + '/pipe/arms_*_F*.npz')):
        star, filt = os.path.basename(f)[5:-4].rsplit('_', 1)
        z = np.load(f)
        if cls + '_data' not in z or filt not in sm.PIVOT_UM or star == 'p5398_c': continue
        lam = sm.PIVOT_UM[filt]
        flux = 3631e-6 * 10 ** (-0.4 * gs.predict_ab(Table.read(f'{D}/gaia_{star}.ecsv')[:1], lam, 'nir')[0])
        dat, rc, pc = z[cls + '_data'], z['rc'], z['pc']
        noise = float(z['sigma']) / np.sqrt(30)
        p = pc[pc > 0]
        with np.errstate(all='ignore'):
            fold = np.nanmean([dat[:, pc > 0], dat[:, pc < 0][:, ::-1]], axis=0)     # (nr, np)
            a0 = np.nanmean(fold[:, p < 0.25], axis=1)
        good = np.isfinite(a0)
        if good.sum() < 2: continue
        # axis, wavelength-scaled
        EA.append(regrid(rc / lam, (a0 * lam ** 2 / flux)[:, None], UC, good)[:, 0])
        NA.append(np.full(NU, noise / np.sqrt(2) * lam ** 2 / flux))
        # shape, in arcsec, where the axis is well above the noise
        strong = good & (a0 > 30 * noise)
        if strong.sum() >= 2:
            shp = np.full((len(rc), NP), np.nan)
            nzr = np.full((len(rc), 1), np.nan)
            for j in np.nonzero(strong)[0]:
                ok = np.isfinite(fold[j])
                if ok.sum() < 5: continue
                shp[j] = np.interp(PC, p[ok], fold[j][ok] / a0[j], left=1.0, right=np.nan)
                nzr[j] = noise / a0[j]
            g2 = strong & np.isfinite(nzr[:, 0])
            ES.append(regrid(rc, shp, RC, g2))
            NS.append(np.broadcast_to(regrid(rc, nzr, RC, g2), (NR, NP)).copy())
    # ---- axis
    A, eA, nvA = vote(np.array(EA), np.array(NA))
    meas = np.isfinite(A)
    det = meas & (A > 3 * eA)
    gap = 0
    for i in range(NU):          # the arm is the run of detections outward
        if meas[i] and not det[i]:
            gap += 1
        elif det[i]:
            if gap >= 3:
                det[i:] = False; break
            gap = 0
    iu = np.nonzero(det)[0]
    fit = UC[iu] > (15 if cls == 'hex' else 4)
    slope, icpt = np.polyfit(np.log(UC[iu][fit]), np.log(A[iu][fit]), 1)
    law = np.exp(icpt) * UC ** slope
    last, first = iu.max(), iu.min()
    absent = meas & ~det & (np.arange(NU) > last) & (A + 3 * eA < law / 3.0)
    end = int(np.nonzero(absent)[0].min()) if absent.any() else None
    inner = np.polyfit(np.log(UC[iu][:6]), np.log(A[iu][:6]), 1)[0]
    axis = np.where(det, A, 0.0)
    for i in range(NU):
        if det[i]: continue
        if i < first:
            axis[i] = A[first] * (UC[i] / UC[first]) ** min(inner, 0.0)
        elif end is not None and i >= end:
            axis[i] = 0.0
        else:
            j = iu[np.argmin(np.abs(np.log(UC[iu]) - np.log(UC[i])))]
            axis[i] = A[j] * (UC[i] / UC[j]) ** slope
    axis = np.maximum.accumulate(axis[::-1])[::-1]
    print(f'{cls} axis: {len(EA)} star-filter pairs; detected u = {UC[first]:.1f}-{UC[last]:.0f} arcsec/um; slope {slope:.2f}; '
          + (f'ends at u = {UC[end]:.0f}' if end is not None else 'continued by the power law'))
    # ---- shape
    S, eS, nvS = vote(np.array(ES), np.array(NS))
    S = np.where(np.isfinite(S) & (S > 3 * eS), S, np.where(np.isfinite(S), 0.0, np.nan))
    rmeas = np.isfinite(S[:, 5]) & (np.nansum(S[:, 3:] > 0, axis=1) > 0)
    for i in np.nonzero(rmeas)[0]:
        r_ = np.where(np.isfinite(S[i]), S[i], 0.0)
        r_[:3] = 1.0
        S[i] = np.minimum.accumulate(np.minimum(r_, 1.0))
    ir = np.nonzero(rmeas)[0]
    for i in range(NR):
        if not rmeas[i]:
            S[i] = S[ir[np.argmin(np.abs(np.log(RC[ir]) - np.log(RC[i])))]]
    S = np.maximum.accumulate(S, axis=0)          # an arm does not narrow outward
    def hw(level):
        return [PC[np.argmax(s < level)] if (s < level).any() else PC[-1] for s in S]
    h10, h1 = hw(0.1), hw(0.01)
    print(f'{cls} shape: {len(ES)} pairs; measured r = {RC[ir].min():.0f}-{RC[ir].max():.0f} arcsec; half-width at 10% / 1% of the axis: '
          + ', '.join(f'{RC[i]:.0f}": {h10[i]:.1f}/{h1[i]:.1f}' for i in range(2, NR, 5)))
    h = fits.ImageHDU(axis.astype(np.float32), name=cls.upper() + '_AXIS')
    h.header['U0'] = (U_EDGES[0], 'first distance bin edge [arcsec/um]')
    h.header['U1'] = (U_EDGES[-1], 'last distance bin edge, log spaced')
    h.header['SLOPE'] = (slope if end is None else -8.0, 'power law of the axis beyond U1')
    h.header['BUNIT'] = 'um2/sr'
    hdus.append(h)
    h = fits.ImageHDU(S.astype(np.float32), name=cls.upper() + '_SHAPE')
    h.header['R0'] = (R_EDGES[0], 'first distance bin edge [arcsec]')
    h.header['R1'] = (R_EDGES[-1], 'last distance bin edge, log spaced')
    h.header['DP'] = (DP, 'offset bin [arcsec], from 0')
    hdus.append(h)
    for e in EA: ax[row, 0].plot(UC, e, '-', color='grey', lw=0.5)
    ax[row, 0].errorbar(UC[meas], np.maximum(A[meas], 1e2), 3 * np.where(np.isfinite(eA[meas]), eA[meas], 0), fmt='k.', ms=4, lw=0.8)
    ax[row, 0].plot(UC, np.maximum(axis, 1e2), 'r', lw=2, label='axis profile used')
    ax[row, 0].set_xscale('log'); ax[row, 0].set_yscale('log'); ax[row, 0].set_ylim(1e2, 3e9); ax[row, 0].legend()
    ax[row, 0].set_xlabel('distance / wavelength [arcsec/um]'); ax[row, 0].set_ylabel('axis brightness x wavelength^2 / flux [um^2/sr]')
    ax[row, 0].set_title(f'{cls}: on the arm axis')
    ax[row, 1].imshow(np.log10(np.maximum(S, 1e-4)), origin='lower', aspect='auto', extent=[0, NP * DP, 0, NR], vmin=-3.5, vmax=0)
    ax[row, 1].set_title('shape across the arm (log10 of fraction of the axis)'); ax[row, 1].set_xlabel('offset [arcsec]')
    ax[row, 1].set_ylabel('distance bin (log, 2.5-300 arcsec)')
    for i in range(2, NR, 5):
        ax[row, 2].plot(PC, S[i], label=f'{RC[i]:.0f}"')
    ax[row, 2].set_yscale('log'); ax[row, 2].set_ylim(3e-4, 1.5); ax[row, 2].set_xlim(0, 8); ax[row, 2].legend(fontsize=8)
    ax[row, 2].set_xlabel('offset [arcsec]'); ax[row, 2].set_title('cross-sections at several distances')
fits.HDUList(hdus).writeto(D + '/spike_profile_v2.fits', overwrite=True)
plt.tight_layout(); plt.savefig(D + '/spike_profile_v2.png', dpi=55); print(D + '/spike_profile_v2.png')
