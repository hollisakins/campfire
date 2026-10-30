"""QA report for an empirical bar-shadow derivation (``barshadow_calibration.derive``).

Pages: the measured segments against the CRDS reference and the new table in three
wavelength bands; per-observation consistency; the two tables as images; the held-out
observation's empty shutters divided by each table (flat at 1 is the goal); the pooled
sky and the shutter scales.
"""
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

from campfire_pipeline.nirspec.barshadow_calibration import (
    SEGMENTS, SEGMENT_DOC, Y_CENTRES, cell_stats, evaluate_table, load_table)


def _bands(grid, n=3):
    lam = grid.wavelengths_um
    edges = np.linspace(0, grid.n_w, n + 1).astype(int)
    return [(edges[i], edges[i + 1], f'{lam[edges[i]]:.2f}-{lam[min(edges[i + 1], grid.n_w) - 1]:.2f} um') for i in range(n)]


def _band_profile(mean, var, seg_i, c0, c1):
    """Inverse-variance mean over columns c0..c1 of a (seg, ybin, col) cell array."""
    m = mean[seg_i, :, c0:c1]; v = var[seg_i, :, c0:c1]
    with np.errstate(invalid='ignore', divide='ignore'):
        w = 1.0 / v
        w[~np.isfinite(w)] = 0
        prof = np.nansum(np.nan_to_num(m) * w, axis=1) / np.sum(w, axis=1)
        err = 1.0 / np.sqrt(np.sum(w, axis=1))
    prof[np.sum(w, axis=1) == 0] = np.nan
    return prof, err


def _table_curve(table, seg_i, lam_um):
    """A table's profile for one segment at one wavelength on the Y_CENTRES rows."""
    return evaluate_table(table, np.full(len(Y_CENTRES), seg_i), Y_CENTRES, np.full(len(Y_CENTRES), lam_um))


def _segment_rows(seg_name):
    return {'edge_lower': (-1.0, 0.0), 'edge_upper': (0.0, 1.0), 'bar_lower': (-1.0, -0.5), 'bar_upper': (-0.5, 0.0)}[seg_name]


def qa_report(res, out_pdf):
    grid = res['grid']; tables = res['tables']; total = res['total']
    crds = load_table(res['template'])
    new = dict(data1x1=tables['data1x1'], data1x3=tables['data1x3'], grid=grid)
    mean, var = cell_stats(total, n_min=20)
    prov = res['provenance']; opt = prov['options']
    title = f"empirical bar shadow, {opt['grating']} ({opt['filter']}), {len(prov['observations'])} observations, {prov['n_pixels']:,} pixels, {prov['n_shutters']} clean shutters"
    bands = _bands(grid)
    png_prefix = os.path.splitext(out_pdf)[0]
    page = [0]

    class _Pages:
        def __init__(self, pdf):
            self.pdf = pdf

        def savefig(self, fig):
            page[0] += 1
            self.pdf.savefig(fig)
            fig.savefig(f'{png_prefix}_p{page[0]}.png', dpi=110)

    with PdfPages(out_pdf) as _pdf:
        pdf = _Pages(_pdf)
        # --- page 1: measured segments vs reference vs new table, per band ---
        fig, axes = plt.subplots(len(bands), len(SEGMENTS), figsize=(5 * len(SEGMENTS), 3.6 * len(bands)), sharey=True)
        for bi, (c0, c1, blab) in enumerate(bands):
            lam_mid = grid.wavelengths_um[(c0 + c1) // 2]
            for si, name in enumerate(SEGMENTS):
                ax = axes[bi, si]
                prof, err = _band_profile(mean, var, si, c0, c1)
                lo, hi = _segment_rows(name); sel = (Y_CENTRES >= lo) & (Y_CENTRES <= hi)
                ax.errorbar(Y_CENTRES[sel], prof[sel], yerr=err[sel], fmt='.', ms=3, color='k', label='measured (SB / sky)')
                ax.plot(Y_CENTRES[sel], _table_curve(crds, si, lam_mid)[sel], 'r-', lw=1.2, label=f'CRDS {os.path.basename(res["template"])}')
                ax.plot(Y_CENTRES[sel], _table_curve(new, si, lam_mid)[sel], 'g-', lw=1.2, label='empirical table')
                ax.set_ylim(0.0, 1.15); ax.axhline(1, color="gray", lw=0.5)
                for b in (-0.5, 0.5): ax.axvline(b, color='gray', lw=0.5, ls=':')
                if bi == 0: ax.set_title(f'{name}\n{SEGMENT_DOC[name].split("(")[0].strip()}', fontsize=9)
                if si == 0: ax.set_ylabel(f'{blab}\nshadow (1 = open centre)')
                if bi == len(bands) - 1: ax.set_xlabel('row in the reference frame [shutter pitch]')
                if bi == 0 and si == 0: ax.legend(fontsize=7)
        fig.suptitle(title, fontsize=11); fig.tight_layout(); pdf.savefig(fig); plt.close(fig)

        # --- page 2: per-observation consistency (middle band) ---
        c0, c1, blab = bands[len(bands) // 2]
        fig, axes = plt.subplots(1, len(SEGMENTS), figsize=(5 * len(SEGMENTS), 4), sharey=True)
        spreads = []
        for si, name in enumerate(SEGMENTS):
            ax = axes[si]; lo, hi = _segment_rows(name); sel = (Y_CENTRES >= lo) & (Y_CENTRES <= hi)
            curves = []
            for obs, acc in sorted(res['per_obs'].items()):
                m_o, v_o = cell_stats(acc, n_min=10)
                prof, err = _band_profile(m_o, v_o, si, c0, c1)
                ax.plot(Y_CENTRES[sel], prof[sel], lw=1, label=obs); curves.append(prof[sel])
            curves = np.array(curves)
            if len(curves) > 1:
                dev = np.nanmedian(np.nanstd(curves, axis=0)[np.abs(Y_CENTRES[sel]) < 0.45])
                spreads.append(f'{name}: {dev:.3f}')
            ax.set_ylim(0.0, 1.15); ax.set_title(name, fontsize=10); ax.axhline(1, color='gray', lw=0.5)
            for b in (-0.5, 0.5): ax.axvline(b, color='gray', lw=0.5, ls=':')
            ax.set_xlabel('row [pitch]')
            if si == 0: ax.set_ylabel(f'shadow, {blab}'); ax.legend(fontsize=7)
        fig.suptitle(f'per-observation profiles, {blab}; median between-observation std inside the shutter: {"; ".join(spreads)}', fontsize=10)
        fig.tight_layout(); pdf.savefig(fig); plt.close(fig)

        # --- page 3: the tables as images ---
        fig, axes = plt.subplots(2, 3, figsize=(16, 8))
        ext = [grid.wavelengths_um[0], grid.wavelengths_um[-1], grid.rows[0], grid.rows[-1]]
        for r, key in enumerate(('data1x1', 'data1x3')):
            a_c = crds[key]; a_n = new[key]
            for ci, (arr, lab, vmin, vmax, cmap) in enumerate(((a_c, 'CRDS', 0.4, 1.0, 'viridis'), (a_n, 'empirical', 0.4, 1.0, 'viridis'),
                                                           (a_n - a_c, 'empirical - CRDS', -0.15, 0.15, 'RdBu_r'))):
                ax = axes[r, ci]; im = ax.imshow(arr, origin='lower', aspect='auto', extent=ext, vmin=vmin, vmax=vmax, cmap=cmap)
                ax.set_title(f'{key.upper()} {lab}', fontsize=10); fig.colorbar(im, ax=ax, shrink=0.8)
                ax.set_xlabel('wavelength [um]'); ax.set_ylabel('row [pitch]')
        fig.suptitle('the reference images'); fig.tight_layout(); pdf.savefig(fig); plt.close(fig)

        # --- page 4: held-out test ---
        if res.get('held_out'):
            h = res['held_out']
            fig, axes = plt.subplots(1, len(SEGMENTS), figsize=(5 * len(SEGMENTS), 4), sharey=True)
            rms_txt = []
            for si, name in enumerate(SEGMENTS):
                ax = axes[si]; lo, hi = _segment_rows(name); sel = (Y_CENTRES >= lo) & (Y_CENTRES <= hi) & (np.abs(Y_CENTRES) < 0.6) & (np.abs(Y_CENTRES + 1) > 0.0)
                out = {}
                for lab, acc, col in (('CRDS', h['crds'], 'r'), ('empirical', h['new'], 'g')):
                    m, _ = cell_stats(acc[si], n_min=20)
                    ax.plot(Y_CENTRES[sel], m[sel], color=col, lw=1.3, label=f'divided by the {lab} table')
                    inside = sel & (np.abs(Y_CENTRES - np.rint(Y_CENTRES)) < 0.5)
                    out[lab] = np.sqrt(np.nanmean((m[inside] - 1) ** 2))
                rms_txt.append(f'{name}: CRDS {out["CRDS"]*100:.1f} %, empirical {out["empirical"]*100:.1f} %')
                ax.axhline(1, color='k', lw=0.6); ax.set_ylim(0.8, 1.2); ax.set_title(name, fontsize=10); ax.set_xlabel('row [pitch]')
                for b in (-0.5, 0.5): ax.axvline(b, color='gray', lw=0.5, ls=':')
                if si == 0: ax.set_ylabel('empty-shutter SB / (sky x table)'); ax.legend(fontsize=7)
            fig.suptitle(f'held-out observation {h["obs"]}: rms from 1 inside the shutters — {"; ".join(rms_txt)}', fontsize=9)
            fig.tight_layout(); pdf.savefig(fig); plt.close(fig)

        # --- page 5: shutter centre levels vs wavelength, by nod and by side ---
        sh = res.get('shutters', {}); sky = res['sky']; lam = grid.wavelengths_um
        if sh:
            groups = {}
            for key, (spec, n) in sh.items():
                obs, root, nod, det, sid, k = key
                if not res['scales'].get(key, (np.nan, False))[1]:
                    continue
                own = res.get('skies', {}).get((obs, root, nod), sky)
                groups.setdefault(('nod', nod, int(np.sign(k)), abs(int(k))), []).append(spec / own)
                groups.setdefault(('det', det, int(np.sign(k)), 0), []).append(spec / own)
            fig, axes = plt.subplots(1, 2, figsize=(15, 5), sharey=True)
            for ax, kind in zip(axes, ('nod', 'det')):
                for key in sorted(k_ for k_ in groups if k_[0] == kind):
                    arr = np.array(groups[key]); med = np.nanmedian(arr, axis=0)
                    lab = (f'nod {key[1]}, k = {"+" if key[2] > 0 else "-"}{key[3]} (n={len(arr)})' if kind == 'nod'
                           else f'{key[1]}, k {"> 0" if key[2] > 0 else "< 0"} (n={len(arr)})')
                    ax.plot(lam, med, lw=1.2, label=lab)
                ax.axhline(1, color='k', lw=0.6); ax.set_ylim(0.9, 1.1); ax.set_xlabel('wavelength [um]'); ax.legend(fontsize=7)
                ax.set_title(f'empty-shutter centre level / pooled sky, by {"nod and shutter offset" if kind == "nod" else "detector and side"}', fontsize=10)
            axes[0].set_ylabel('median over clean shutters of (centre spectrum / pooled sky)')
            fig.suptitle('is the lower-vs-upper asymmetry tied to the nod, the side, or the detector?'); fig.tight_layout(); pdf.savefig(fig); plt.close(fig)

        # --- page 6: sky and scales ---
        fig, axes = plt.subplots(1, 2, figsize=(13, 4))
        axes[0].plot(grid.wavelengths_um, res['sky'], 'k-'); axes[0].set_xlabel('wavelength [um]'); axes[0].set_ylabel('pooled sky from shutter centres [MJy/sr]')
        ax2 = axes[0].twinx(); ax2.plot(grid.wavelengths_um, res['n_per_col'], 'b:', lw=0.8); ax2.set_ylabel('clean shutters per column', color='b')
        a = np.array([v[0] for v in res['scales'].values()]); cl = np.array([v[1] for v in res['scales'].values()])
        axes[1].hist(a[np.isfinite(a) & cl], bins=np.linspace(0.6, 1.6, 51), color='tab:green', label='clean')
        axes[1].hist(a[np.isfinite(a) & ~cl], bins=np.linspace(0.6, 1.6, 51), color='tab:red', alpha=0.6, label='rejected')
        axes[1].set_xlabel('shutter scale against the pooled sky (centre pixels)'); axes[1].legend()
        fig.suptitle('pooled sky and shutter selection'); fig.tight_layout(); pdf.savefig(fig); plt.close(fig)
    return out_pdf
