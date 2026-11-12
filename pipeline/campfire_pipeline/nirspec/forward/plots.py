"""The data, the model and the residual of a forward-model fit, per exposure on its own pixels.

The layout follows stage 3's spectrum diagnostic (``plots.plot_spectrum_qa``): data and model as
S/N on one scale, the residual as (data - model) / noise, then the fitted spectra. Here each row is
one exposure as fitted (a nod difference, or a frame minus the master background), on the detector
pixels of its slitlet, not resampled.
"""
import numpy as np


def _images(M, values):
    """{exposure index: image on its group's common frame} of a per-pixel vector (NaN off the fit)."""
    out, n = {}, 0
    for k, m in M.pix_masks:
        img = np.full(m.shape, np.nan)
        img[m] = values[n:n + int(m.sum())]
        n += int(m.sum())
        out[k] = img
    return out


def panels(fit, result=None, path=None, title=None, labels=None, max_exposures=12):
    """The figure of a fit: ``fit`` a ``forward.Fit`` (or a ``ForwardModel`` with ``result`` a
    ``model.Solution``), ``result`` its ``FitResult`` (default: the fit at its current parameters).
    Rows: per exposure the data, the model and the residual over the noise (pixels left out of the fit
    grey); below, each component's spectrum with its error. Saved to ``path`` when given; returns
    the figure."""
    import matplotlib.pyplot as plt
    from matplotlib import gridspec
    from campfire_pipeline.nirspec.forward.fit import Fit

    if isinstance(fit, Fit):
        M = fit.model
        if result is None:
            result = fit.run() if not fit.free else None
        sol = result.solution if result is not None else fit.solve(covariance=True)
        spectra = sol.spectra
        errs = np.where(sol.good, sol.errors(), np.nan)
        labels = labels or fit.names
    else:
        M, sol = fit, result
        spectra, errs = sol.spectra, np.where(sol.good, sol.errors(), np.nan) if sol.cov is not None else None
        labels = labels or [f'c{i}' for i in range(spectra.shape[0])]
    sig = np.sqrt(M.pix['v'])
    used = np.where(M.use, 1.0, np.nan)
    data = _images(M, M.pix['d'] / sig * used)
    model = _images(M, sol.model / sig * used)
    resid = _images(M, (M.pix['d'] - sol.model) / sig * used)
    ks = [k for k, _ in M.pix_masks][:max_exposures]
    finite = np.concatenate([v[np.isfinite(v)] for v in data.values()]) if data else np.zeros(1)
    std = 1.4826 * np.median(np.abs(finite - np.median(finite))) if finite.size else 1.0
    std = std if np.isfinite(std) and std > 0 else 1.0

    fig = plt.figure(figsize=(15, 1.6 * len(ks) + 3.2), constrained_layout=True)
    gs = gridspec.GridSpec(len(ks) + 1, 3, figure=fig, height_ratios=[1] * len(ks) + [2.2])
    cmap = plt.colormaps['viridis'].copy()
    cmap.set_bad('0.8')
    rcmap = plt.colormaps['RdBu_r'].copy()
    rcmap.set_bad('0.8')
    for r, k in enumerate(ks):
        e = M.exps[k]
        rows = np.flatnonzero(np.any(np.isfinite(data[k]), axis=1))
        cols = np.flatnonzero(np.any(np.isfinite(data[k]), axis=0))
        if rows.size == 0:
            continue
        sl = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
        with np.errstate(all='ignore'):
            wave = np.nanmedian(np.where(np.isfinite(data[k]), e.wav, np.nan)[sl], axis=0)
        good = np.isfinite(wave)
        for c, (img, lab) in enumerate(((data[k] / std, 'data'), (model[k] / std, 'model'),
                                        (resid[k], 'residual'))):
            ax = fig.add_subplot(gs[r, c])
            ys = np.arange(sl[0].start, sl[0].stop)
            if lab == 'residual':
                ax.pcolormesh(wave[good], ys, img[sl][:, good], vmin=-5, vmax=5, cmap=rcmap, rasterized=True,
                              shading='nearest')
            else:
                ax.pcolormesh(wave[good], ys, img[sl][:, good], vmin=-3, vmax=8, cmap=cmap, rasterized=True,
                              shading='nearest')
            ax.tick_params(direction='in', which='both', labelsize=7, labelbottom=(r == len(ks) - 1))
            if c == 0:
                kind = 'frame' if not e.partners else 'nod difference'
                ax.set_ylabel(f'{e.name.split("_")[1]}_{e.name.split("_")[2]}\n{kind}\nrow', fontsize=7)
            if r == 0:
                ax.set_title(lab, fontsize=9)
    ax = fig.add_subplot(gs[len(ks), :])
    lam = M.lam_grid
    for i, lab in enumerate(labels):
        f = np.where(sol.good[i], spectra[i], np.nan)
        line, = ax.step(lam, f, where='mid', lw=1, label=lab)
        if errs is not None:
            ax.fill_between(lam, f - errs[i], f + errs[i], step='mid', alpha=0.2, color=line.get_color(),
                            edgecolor='none')
    ax.axhline(0, color='k', lw=0.5, alpha=0.4)
    ax.set_xlabel(r'Observed wavelength [$\mu$m]')
    ax.set_ylabel('total light per component [MJy]')
    ax.legend(fontsize=8, frameon=False, loc='upper left')
    if result is not None and getattr(result, 'params', None):
        par = ', '.join(f'{k} {v:.3g}' for k, v in result.params.items())
        title = (title + '   ' if title else '') + par + f'   chi2/dof {result.chi2_red:.2f}'
    if title:
        fig.suptitle(title, fontsize=10)
    if path:
        fig.savefig(path)
        plt.close(fig)
    return fig
