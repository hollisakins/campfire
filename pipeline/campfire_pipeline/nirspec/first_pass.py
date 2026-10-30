"""
First pass on master-background-subtracted frames (extraction v2 + master background).

A source's pass 1 (``profile_contam``) is solved on single exposures with the master
background subtracted, instead of on nod differences. A single frame holds the
source once and every other source only where that exposure's own shutters put it,
with no negative copies from the partners. On ember_cosmos_p14 / p9 G395M the
sources whose own-shutter pixels are less than 30 % clean drop from 147 / 114 in nod
differences to 38 / 28 in single frames (geometry only, merge plan T0).

The frames are made on demand and never stored: the stage 2a calibrated arrays the
canonical keeps (``PRE_BKGSUB_*``) minus the sky in the slit's own units
(``sky_for``; in production ``master_bkg.sky_for_slit``), with the path loss
removed as ``profile_fit.load_group`` removes it. ``PRE_BKGSUB_*`` stays the single
calibrated truth.

The output is an ordinary pass-1 file (``profile_contam.pass1_path``), so the
prediction of other light on the final nodded fit reads it unchanged: the frames
are the source's own light (``profile_fit.frame_image``), which does not depend on
how the sky was removed. Its light past the cutouts (``profile_extend``) is still
solved on the nod differences of the rate files (the master background flags
those pixels), so each exposure keeps its nodded partner list in
``Exposure.nod_partners``.

Not yet here (merge plan section 2.5): the sky-template nuisance term per exposure
and shutter, the native 3-row boxcar (sanity reference and fallback), the wiring
into ``stage3.run_profile_fit_phase``. The sanity guard against the nodded stage-3
spectrum is not applied: that reference self-subtracts what this pass is meant to
see.
"""

import os

import numpy as np

from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec import profile_fit as PF

FIRST_PASS_VERSION = '1'
SPEC_SRC = 'master'


def master_sky(workspace, slitlet_scale=False):
    """``sky_for`` from the master background (``master_bkg.sky_for_slit``): each
    canonical's sky from its exposure's ``{workspace}/<root>_<nod>_<nrsN>_mbkg.fits``.

    The sky is the painted total on the cutout (every slitlet whose trace crosses a
    pixel; on the gratings several), in the slit's units. Pixels a footprint covers
    but no slit was painted for (``MBKG2DDQ`` bit 2: an unprocessed source, a
    failed-open shutter) get NaN, so the loader drops them: the total is incomplete
    there by construction. One ``load_product`` dict per exposure, shared by its slits.

    ``slitlet_scale``: rescale the slit's own slitlet's share of the sky by the median
    scale of its own clean empty shutters (``master_bkg.slitlet_scale``; the other
    traces' share untouched). Off: whether to use it is an open decision."""
    from campfire_pipeline.nirspec import master_bkg as MB

    cache = {}

    def product(key, canonical):
        if key not in cache:
            path = os.path.join(workspace, f'{key}_mbkg.fits')
            if not os.path.exists(path):
                raise FileNotFoundError(f'first pass: no master background {path}')
            prod = MB.load_product(path)
            cache[key] = (prod, MB.resolve_barshadow_table(prod['bs_ref'], canonical))
        return cache[key]

    def sky_for(path, slit):
        prod, table = product(CT.exposure_key(path), path)
        d = MB.sky_for_slit(prod, path, table=table, slitlet_scale_on=slitlet_scale)
        sky, var = np.array(d['sky'], float), np.array(d['var'], float)
        if d.get('dq') is not None:
            bad = (np.asarray(d['dq']) & MB.DQ_UNPAINTED) > 0
            sky[bad], var[bad] = np.nan, np.nan
        return sky, var

    return sky_for


def load_group_sky(cal_files, sky_for, bkg_overrides=None, science=None, group=0):
    """Exposures of one stage-2b background group as single frames with the sky removed.

    ``sky_for(path, slit)`` returns ``(sky, sky_var)``: the sky and its variance in
    the units of the slit's stage 2a arrays (path loss as applied there), the
    slit's shape, NaN where there is none. Data: ``PRE_BKGSUB_SCI`` (or the live
    SCI of a canonical stage 2b has not touched) minus the sky, times the applied
    path loss. ``partners`` is ``[]`` (a single frame); ``nod_partners`` the
    nodded list. ``science`` as in ``profile_fit.load_group``; an exposure without
    the source is loaded but not fitted."""
    from astropy.io import fits
    from jwst.datamodels import MultiSlitModel
    from campfire_pipeline.nirspec.stage2 import nod_background_partners

    models = [MultiSlitModel(f) for f in cal_files]
    heads = [fits.getheader(f) for f in cal_files]
    srcflux = [str(h.get('SRCFLUX', 'T')).strip().upper() != 'F' for h in heads]
    slits = [m.slits[0] for m in models]
    y0, x0, shape = PF._common_frame(slits)
    nodded = PF.partners_from_headers(cal_files, heads)
    if nodded is None:
        nodded = nod_background_partners(cal_files, bkg_overrides)
    exps = []
    for f, m, s, part, has_src in zip(cal_files, models, slits, nodded, srcflux):
        sci, dq, var = _calibrated_arrays(f, m, s)
        ny, nx = sci.shape
        yy, xx = np.mgrid[:ny, :nx]
        _, ys, _ = s.meta.wcs.get_transform('detector', 'slit_frame')(xx, yy)
        pl_type = str(getattr(s, 'pathloss_correction_type', '') or '').upper()
        applied = 'COMPLETE' in str(m.meta.cal_step.pathloss).upper()
        if applied and pl_type == 'UNIFORM':
            pl = np.asarray(s.pathloss_uniform, float)
        elif applied:
            pl = np.asarray(s.pathloss_point, float)
        else:
            pl = np.ones((ny, nx))
        sky, sky_var = sky_for(str(f), s)
        sky, sky_var = np.asarray(sky, float), np.asarray(sky_var, float)
        if sky.shape != sci.shape or sky_var.shape != sci.shape:
            raise ValueError(f'first pass: sky for {f} has shape {sky.shape}, the slit {sci.shape}')
        data = (sci - sky) * pl
        v = (var + sky_var) * pl ** 2
        dq_bad = (dq & PF.DO_NOT_USE) > 0
        ok = (np.isfinite(data) & np.isfinite(v) & (v > 0) & ~dq_bad & np.isfinite(ys)
              & np.isfinite(s.wavelength) & np.isfinite(pl) & (pl > 0))
        name = str(f).split('/')[-1]
        exps.append(PF.Exposure(
            name=name, group=group, detector=str(m.meta.instrument.detector).upper(),
            grating=f'{m.meta.instrument.grating}/{m.meta.instrument.filter}'.upper(),
            quadrant=int(s.quadrant) if s.quadrant is not None else None,
            diff=PF._place(data, s, y0, x0, shape), var=PF._place(v, s, y0, x0, shape),
            ok=PF._place(ok.astype(float), s, y0, x0, shape, fill=0.0) > 0,
            wav=PF._place(np.asarray(s.wavelength, float), s, y0, x0, shape), y=PF._place(ys, s, y0, x0, shape),
            pl_applied=PF._place(pl, s, y0, x0, shape),
            pl_point=PF._place(np.asarray(s.pathloss_point, float), s, y0, x0, shape),
            src_y=float(s.source_ypos), open_k=PF.open_shutters(s.shutter_state, s.slit_ymin, s.slit_ymax),
            partners=[], use_data=bool(has_src) and (science is None or name in science),
            cutout=(s.ystart - y0, s.xstart - x0, ny, nx), path=str(f), origin=(y0, x0),
            has_source=bool(has_src), pa_v3=PF._pa_v3(f), nod_partners=part))
    for m in models:
        m.close()
    return exps


def _calibrated_arrays(path, model, slit):
    """(SCI, DQ, variance) of the stage 2a calibrated frame: the ``PRE_BKGSUB_*``
    arrays when stage 2b has run, else the live ones."""
    from astropy.io import fits

    vflat = slit.var_flat if slit.var_flat is not None and np.size(slit.var_flat) else 0.0
    with fits.open(path) as h:
        if 'PRE_BKGSUB_SCI' in h:
            sci = np.asarray(h['PRE_BKGSUB_SCI'].data, float)
            dq = np.asarray(h['PRE_BKGSUB_DQ'].data, np.int64)
            var = (np.asarray(h['PRE_BKGSUB_VAR_RNOISE'].data, float)
                   + np.asarray(h['PRE_BKGSUB_VAR_POISSON'].data, float))
            if sci.shape != slit.data.shape:
                raise ValueError(f'first pass: {path}: PRE_BKGSUB_SCI {sci.shape} != slit {slit.data.shape}')
            return sci, dq, var + vflat
    if 'COMPLETE' in str(model.meta.cal_step.bkg_subtract).upper():
        raise ValueError(f'first pass: {path} is background-subtracted and has no PRE_BKGSUB_* arrays')
    return (np.asarray(slit.data, float), np.asarray(slit.dq, np.int64),
            np.asarray(slit.var_poisson, float) + np.asarray(slit.var_rnoise, float) + vflat)


def load_source_sky(groups, sky_for, bkg_overrides=None, science=None):
    """All exposures of a source as single frames (``load_group_sky`` per group);
    nodded partner indices made global as in ``profile_fit.load_source``."""
    out = []
    overrides = bkg_overrides if isinstance(bkg_overrides, (list, tuple)) else [bkg_overrides] * len(groups)
    for g, files in enumerate(groups):
        exps = load_group_sky(files, sky_for, bkg_overrides=overrides[g], science=science, group=g)
        base = len(out)
        for e in exps:
            if e.nod_partners is not None:
                e.nod_partners = [base + j for j in e.nod_partners]
        out.extend(exps)
    if len({e.grating for e in out}) > 1:
        raise ValueError(f'first pass: one disperser per fit, got {sorted({e.grating for e in out})}')
    return out


def compute_first_pass(exps, sid, maps, workspace, is_prism, stage_config, prism_shape=None):
    """Pass 1 of one source on its single frames (in memory; ``run_first_pass`` writes it).

    As the first round of ``profile_contam.compute_pass1``, on single frames: every
    pixel another source's source shutter lights in that exposure is masked (in a
    single frame only its own shutters, not its partners'); when that leaves less
    than 30 % of the source's main-shutter pixels the solve is unmasked and marked
    unreliable (``why = 'covered'``). Shape: the PRISM shape for a grating when the
    PRISM product took its own fit; else the source's own fit above its gate; else
    the default. Spectrum: the fixed-shape solve, smoothed as pass 1 is."""
    from campfire_pipeline.nirspec import profile_chain as PC

    if any(e.partners for e in exps):
        raise ValueError('first pass: exposures must be single frames (load_source_sky)')
    st = CT.pass1_settings(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    geo = CT.geometry(exps, sid, maps)
    c_plan = PF.planned_position(exps)[0]
    saved = [e.ok.copy() for e in exps]
    before = sum(int((e.ok & CT.main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
    for e, m in zip(exps, geo.mask):
        e.ok = e.ok & ~m
    after = sum(int((e.ok & CT.main_shutter(e, c_plan)).sum()) for e in exps if e.use_data)
    reliable, why = True, ''
    if before == 0 or after < 0.3 * before:
        for e, ok in zip(exps, saved):
            e.ok = ok.copy()
        reliable, why = False, 'covered'
    try:
        M = PF.ProfileModel(exps, chromatic=True, priors=cfg.priors, bounds=cfg.bounds)
        snr, _ = PF.detection_snr(M, scale=st['gate_noise'])
        shape, shape_src = None, 'default'
        if not is_prism and prism_shape is not None and prism_shape.shape == 'own':
            shape, shape_src = PF.map_shape(prism_shape.params, M.pivot), 'prism'
        elif st['shape'] == 'chain' and reliable and snr >= (cfg.gate_prism if is_prism else cfg.gate_grating):
            try:
                Ro = PF.fit(exps, model=M)
                if not Ro.bound_bits and PF.planned_position(exps, cfg.srcypos_tolerance)[1] != 2:
                    shape = dict(centre_offset=Ro.params['centre'] - Ro.params['centre_prior'],
                                 drift=Ro.params['drift'], width=Ro.params['width'],
                                 width_change=Ro.params['width_change'])
                    shape_src = 'own'
            except Exception:                        # noqa: BLE001 - default shape then
                pass
        R = PF.fit_fixed(model=M, shape=shape)
    finally:
        for e, ok in zip(exps, saved):
            e.ok = ok
    raw, raw_err = R.flux.copy(), R.flux_err.copy()
    n_smooth = st['smooth_prism'] if is_prism else st['smooth_grating']
    n_lines = 0
    if n_smooth > 1:
        R.flux, R.flux_err, _ = CT.smooth_spectrum(R.flux, R.flux_err, n=n_smooth, keep_lines=st['lines'])
        n_lines = int(np.sum(np.isfinite(raw) & (R.flux == raw)))
    rep = np.abs(R.flux * PF.main_shutter_factor(R, R.lam)) * 1e12
    rep = rep[np.isfinite(rep)]
    # the light past the cutouts is solved on the rate files' nod differences
    for e in exps:
        e.partners = e.nod_partners
    try:
        frames, nosrc, ext = CT._frames(R, exps, sid, maps, workspace, n_smooth, st['lines'])
    finally:
        for e in exps:
            e.partners = []
    P1 = CT.Pass1(sid=int(sid), lam=R.lam, flux=R.flux, flux_err=R.flux_err, frames=frames, reliable=reliable,
                  nosrc=set(nosrc))
    P1.ext, P1.nband = ext, None
    P1.n_smooth, P1.keep_lines = n_smooth, bool(st['lines'])
    P1.clean = after / before if before else 0.0
    P1.why, P1.shape_src, P1.spec_src, P1.snr_det = why, shape_src, SPEC_SRC, snr
    P1.flux_raw, P1.err_raw = raw, raw_err
    P1.rep_p95 = float(np.percentile(rep, 95)) if rep.size > 10 else np.nan
    P1.ref_p95, P1.n_lines = np.nan, n_lines
    P1.result = R
    return P1


def run_first_pass(product, workspace, groups, overrides, science, sid, is_prism, stage_config, sky_for,
                   prism_product=None, overwrite=False):
    """First pass of one source, written as its pass-1 file. Returns the path, or None
    on a named condition (``profile_fit.ProfileFitFallback``), whose reason goes to
    ``profile_contam.missing_path`` as for a nodded pass 1."""
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import trace_overlap as TO

    out = CT.pass1_path(workspace, product)
    st = CT.pass1_settings(stage_config)
    prism = CT.read_prism_shape(workspace, prism_product) if (not is_prism and prism_product) else None
    pshape = 'none' if prism is None else (
        f"{prism.shape}:" + ','.join(f'{k}={prism.params[k]:.4f}' for k in sorted(prism.params)))
    sig = CT._signature(dict(st, prism_shape=pshape, first_pass=FIRST_PASS_VERSION))
    keys = {CT.exposure_key(f) for g in groups for f in g}
    mpaths = [TO.trace_map_path(os.path.join(workspace, CT.MAPS_SUBDIR), f'{k}_rate.fits') for k in sorted(keys)]
    mpaths = [p for p in mpaths if os.path.exists(p)]
    if not overwrite and CT._pass1_valid(out, list(groups) + [mpaths], len(mpaths), sig) is not None:
        return out
    try:
        exps = load_source_sky(groups, sky_for, bkg_overrides=overrides, science=set(science))
        maps = CT.maps_for_exposures(workspace, exps)
        P1 = compute_first_pass(exps, sid, maps, workspace, is_prism, stage_config, prism_shape=prism)
    except PF.ProfileFitFallback as e:
        log(f'{product}: no first pass ({e.reason}: {e})')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(CT.missing_path(workspace, product), 'w') as f:
            f.write(f'{e.reason}: {e}\n')
        return None
    CT.write_pass1(out, P1, sig, len(mpaths), SPEC_SRC, '1')
    if os.path.exists(CT.missing_path(workspace, product)):
        os.remove(CT.missing_path(workspace, product))
    return out
