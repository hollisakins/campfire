"""A forward-model fit on the product's combined 2-D spectrum: the data and the model drizzled onto the
grid of the target's ``s2d`` the way stage 3 drizzles its own, from the canonicals alone.

    grid = forward.product_grid(files)                  # the product's 2-D grid
    data = forward.data_2d(grid, frames=True)           # dict(data, err, wht): SCI_MBKG / ERR_MBKG
    model2d = forward.model_2d(fit, result, grid)       # the fitted model on the same grid

Stage 3 removes its crf (the exposures Spec3 resampled), so the grid is rebuilt from the canonicals
(``drizzle_map.DrizzleMap.from_canonicals``): same exposures, same WCS, the same output grid. The
nod differences' 2-D is drizzled from the canonicals with Spec3's outlier flags (DQ bit 4), the
master-background frames' with their own (bit 4 of ``PRE_BKGSUB_DQ``) and the hot pixels, as stage 3
drizzled them. The model is evaluated on every pixel of each exposure's slitlet, not only the
fitted ones, put back in the canonicals' units (the path-loss correction stage 2b applied) and
drizzled with the data's masks and weights, so data and model combine the same exposures at every
pixel of the grid.
"""
import os

import numpy as np

from campfire_pipeline.nirspec import canonical as CN
from campfire_pipeline.nirspec import drizzle_map as DM
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.forward.model import ForwardModel
from campfire_pipeline.nirspec.forward.products import science_names


def product_grid(files, s2d=None):
    """The product's 2-D grid and pixel maps (``drizzle_map.DrizzleMap``) from a target's canonicals
    (``files_for``; the science exposures among them are the ones stage 3 combined). ``s2d``: the
    product's ``_s2d.fits``, whose resample settings are taken; None: Spec3's settings for the MSA
    products (``drizzle_map.SPEC3_SETTINGS``). The map's ``paths`` gives each exposure's canonical."""
    files = [str(f) for f in files]
    sci = science_names(files)
    paths = [p for p in files if os.path.basename(p) in sci]
    if not paths:
        raise PF.NoUsablePixels('forward.product_grid: no background-subtracted exposure with the source')
    settings = DM.step_settings(s2d) if s2d is not None else dict(DM.SPEC3_SETTINGS)
    grid = DM.DrizzleMap.from_canonicals(paths, settings)
    by_name = {os.path.basename(p): p for p in paths}
    grid.paths = {x.name: by_name[x.name] for x in grid.exposures}
    return grid


def _nod_inputs(grid):
    """The canonicals' own arrays with Spec3's outlier flags (bit 4) left out, as in its crf."""
    out = {}
    for x in grid.exposures:
        a = {k: np.array(v) for k, v in x.arrays.items()}
        a['dq'] = np.where(a['dq'] & CN.OUTLIER_BIT, a['dq'] | DM.DO_NOT_USE, a['dq']).astype(a['dq'].dtype)
        out[x.name] = a
    return out


def _frame_inputs(grid):
    """The master-background frames as stage 3 drizzled them (``mbkg_extraction.flagged_inputs``):
    the stored master background (``CONTAM`` planes) subtracted, their own outlier flags and hot pixels."""
    from campfire_pipeline.nirspec import mbkg_extraction as MX
    paths = [grid.paths[x.name] for x in grid.exposures]
    stored = {os.path.basename(p): CN.master_background_planes(p) for p in paths}
    frames = MX.frames_for(paths, backgrounds={k: v for k, v in stored.items() if v is not None})
    own = {n: (np.asarray(f.arrays['dq']) & CN.OUTLIER_BIT) > 0 for n, f in frames.items()}
    return MX.flagged_inputs(grid, frames, own)


def _inputs(grid, frames):
    if not hasattr(grid, 'paths'):
        raise ValueError('forward: a grid from forward.product_grid is needed (it knows the canonicals)')
    return _frame_inputs(grid) if frames else _nod_inputs(grid)


def data_2d(grid, frames=True):
    """The data drizzled onto ``grid`` (``product_grid``): the master-background frames (``frames``
    True, the product's ``SCI_MBKG`` / ``ERR_MBKG`` before stage 3's scale term) or the nod differences
    (False, its ``SCI`` / ``ERR``). Returns dict(data, err, wht, var_*)."""
    return grid.drizzle(_inputs(grid, frames), variances=True)


class _WholeSlit(ForwardModel):
    """A ``ForwardModel`` whose pixels are every pixel of each fitted exposure's slitlet that the
    model can be evaluated on, not only the fitted ones: for drawing a fitted model, never for fitting."""

    def _build_pixels(self):
        rows = []
        for k, e in enumerate(self.exps):
            if not e.use_data:
                continue
            m = np.isfinite(e.y) & np.isfinite(e.wav) & np.isfinite(e.pl_point) & np.isfinite(e.pl_applied)
            m &= np.asarray(e.pl_applied) != 0
            for j in e.partners or ():
                p = self.exps[j]
                m &= np.isfinite(p.y) & np.isfinite(p.wav) & np.isfinite(p.pl_point)
            rows.append((k, m))
        self.pix_masks = rows
        d, e_idx, lam = [], [], []
        for k, m in rows:
            e = self.exps[k]
            d.append(np.nan_to_num(e.diff[m]))
            e_idx.append(np.full(int(m.sum()), k))
            lam.append(e.wav[m])
        self.pix = dict(d=np.concatenate(d), v=np.ones(sum(x.size for x in d)), e=np.concatenate(e_idx),
                        lam=np.concatenate(lam))
        self.npix = self.pix['d'].size
        self.use = np.ones(self.npix, bool)


def model_images(fit, result=None):
    """{canonical file name: the fitted model on its cutout, in the canonical's units}, on every pixel
    of the slitlet (0 off it): each component's light at its fitted shape times its fitted spectrum,
    plus the master-background scale term on single frames. ``fit`` a ``forward.Fit``; ``result`` its
    ``FitResult`` (default: the fit at its current parameters)."""
    M = fit.model
    if result is not None:
        fit.set([result.params[k] for k in fit.labels])
        sol = result.solution
    else:
        sol = fit.solve()
    comps = [c.resolved() for c in fit.components]
    W = _WholeSlit(M.exps, grid=M.lam_grid, optics=M.optics, centre_law_tables=M.centre_law_tables,
                   nod_law=M.nod_law, c_prior=M.c_prior)
    values = W.design(comps) @ np.asarray(sol.spectra, float).ravel()
    out, n = {}, 0
    for k, m in W.pix_masks:
        e = M.exps[k]
        img = np.zeros(m.shape)
        img[m] = values[n:n + int(m.sum())]
        n += int(m.sum())
        if sol.scale and e.mbkg is not None:
            img = img + sol.scale * np.nan_to_num(np.asarray(e.mbkg, float))
        pl = np.broadcast_to(np.asarray(e.pl_applied, float), m.shape)
        img = np.where(m, img / np.where(m, pl, 1.0), 0.0)        # the fit's data are the canonical x pl_applied
        r0, c0, ny, nx = e.cutout
        out[e.name] = img[r0:r0 + ny, c0:c0 + nx]
    return out


def model_2d(fit, result, grid):
    """The fitted model on the product's 2-D grid (``product_grid``), drizzled with the masks and
    weights of the data it was fitted to (``data_2d`` with ``frames`` as the fit's exposures:
    single frames or nod differences), so ``data_2d(...)['data'] - model_2d(...)`` is the residual
    on the combined 2-D. Returns the 2-D image in the product's units (MJy)."""
    fitted = [fit.model.exps[k] for k, _ in fit.model.pix_masks]
    frames = not any(e.partners for e in fitted)
    inputs = _inputs(grid, frames)
    images = model_images(fit, result)
    unknown = sorted(set(images) - set(grid.names))
    if unknown:
        raise ValueError(f'forward.model_2d: exposures not in the grid: {unknown}')
    out = {}
    for x in grid.exposures:
        a = dict(inputs[x.name])
        img = images.get(x.name)
        a['data'] = (np.zeros(np.shape(a['data'])) if img is None else img).astype(np.float32)
        out[x.name] = a
    return grid.drizzle(out)['data']
