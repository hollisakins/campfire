"""
The master-background spectrum: the secondary product of every source (extraction v2;
user decisions 2026-10-29 and 2026-11-01).

The nodded spectrum stays the preferred product. This one is the same extraction on the
exposures with their master background subtracted instead of the other exposures, so the
two differ only in how the background was removed (parity):

1. ``write_frame``: each science exposure's calibrated frame (``PRE_BKGSUB_SCI``, or the
   live SCI before stage 2b) minus its master background (``stage2.master_background_on``),
   written as a copy of its canonical in ``{workspace}/mbkg/``. The copy's live SCI / ERR /
   VAR_* hold the subtracted frame (the master background's variance added to the
   Poisson term); its ``PRE_BKGSUB_*`` arrays are the canonical's, which the pixel
   calibration (``profile_contam.calibration``) and the frame's own variance
   (``profile_fit.own_variance``) read.
2. ``run_spec3``: jwst's Spec3Pipeline on those copies under ``{product}_mbkg``: the 2-D
   spectrum is resampled exactly as the nodded one (the same exposures, so the same
   grid; stage 3 checks it).
3. ``fit_product``: the copies as single frames (``profile_fit.load_source``,
   ``single_frames``: nothing subtracted from them), the forward model at the shape the
   nodded chain chose, the spectrum solved. Other sources' light is predicted from their
   pass 1s, once and positive where each is in that exposure (``profile_contam.predict``),
   catalogued neighbors in the target's own slitlet included (``include``): no
   background exposure is dropped for them here. Light through failed-open shutters on
   the trace is solved with the source and added to it (``failed_open``; cards
   ``PFNFOPEN`` / ``PFFOSIG``). ``PROF2D`` / ``CONTAM2D`` /
   ``CONTERR2D`` are drizzled through the master-background crf into
   ``{product}_mbkg_pf.fits``.
4. stage 3 extracts it exactly like the nodded product (``stage3.extract_columns``) into
   ``SPEC1D_MBKG``, ``SCI_MBKG``, ``ERR_MBKG`` and ``PROF1D_MBKG`` of the same
   ``_spec.fits`` (``stage3.opt_ext_single_source``), with the primary cards ``MBKGPREF``
   / ``MBKGWHY`` (``preferred``) and ``MBKGNEXP``.

Named conditions (no master background for an exposure, no crf, a profile-fit fallback,
a grid that differs from the nodded one) leave the product without the
master-background spectrum or with its 3-pixel boxcar as ``fnu``, logged. Anything else
fails the stage.
"""

import glob
import os

import numpy as np

MBKG_SUBDIR = 'mbkg'            # the copies and the Spec3 products, inside the workspace
SUFFIX = '_mbkg'                # product name suffix of the master-background Spec3 run
COPY_SUFFIX = '_mbkgsub.fits'   # a copy's name: the canonical's with this ending (never a canonical name)
WHY = ('no_clear_background', 'single_exposure', 'extended')
FALLBACK_NO_MBKG = 'no_master_background'


class NoMasterBackground(RuntimeError):
    """An exposure of the product has no ``_mbkg.fits`` (named: no master-background spectrum)."""


def subdir(workspace):
    return os.path.join(workspace, MBKG_SUBDIR)


def copy_path(workspace, canonical):
    return os.path.join(subdir(workspace), os.path.basename(canonical).replace('.fits', COPY_SUFFIX))


def product_paths(workspace, product_name, source_id):
    """The master-background Spec3 and profile-fit files of a product."""
    d, p = subdir(workspace), f'{product_name}{SUFFIX}'
    return dict(s2d=os.path.join(d, f'{p}_s2d.fits'), x1d=os.path.join(d, f'{p}_x1d.fits'),
                crf=os.path.join(d, f'{p}_s{int(source_id):09d}_crf.fits'), pf=os.path.join(d, f'{p}_pf.fits'))


def write_frame(canonical, workspace):
    """Write the master-background-subtracted copy of one canonical (module doc, 1).
    Raises ``NoMasterBackground`` when its exposure has no ``_mbkg.fits``."""
    from jwst.datamodels import MultiSlitModel
    from campfire_pipeline.nirspec import canonical as C
    from campfire_pipeline.nirspec.profile_fit import DO_NOT_USE
    from campfire_pipeline.nirspec.stage2 import master_background_on

    bg = master_background_on(canonical)
    if bg is None:
        raise NoMasterBackground(f'{os.path.basename(canonical)}: no master background beside it')
    sky, sky_var = bg
    pre = C.read_slit_arrays(canonical, prefix=C.PRE_BKGSUB_PREFIX)
    out = copy_path(workspace, canonical)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with MultiSlitModel(canonical) as m:
        s = m.slits[0]
        sci = np.asarray(pre.get('SCI', s.data), float)
        vp = np.asarray(pre.get('VAR_POISSON', s.var_poisson), float)
        vr = np.asarray(pre.get('VAR_RNOISE', s.var_rnoise), float)
        dq = np.asarray(pre.get('DQ', s.dq)).astype(s.dq.dtype)
        known = np.isfinite(sky)
        sci = np.where(known, sci - sky, np.nan)
        vp = np.where(known & np.isfinite(sky_var), vp + sky_var, vp)
        dq = np.where(known, dq, dq | DO_NOT_USE).astype(s.dq.dtype)
        vf = np.asarray(s.var_flat, float) if s.var_flat is not None and np.size(s.var_flat) else 0.0
        s.data = sci.astype(s.data.dtype)
        s.var_poisson = vp.astype(s.var_poisson.dtype)
        s.var_rnoise = vr.astype(s.var_rnoise.dtype)
        s.err = np.sqrt(vp + vr + vf).astype(s.err.dtype)
        s.dq = dq
        m.meta.cal_step.bkg_subtract = 'COMPLETE'
        m.save(out)                                # on_save names the copy after its own file
    C.append_extras(out, extra_hdus=C.make_prefixed_hdus(pre, C.PRE_BKGSUB_PREFIX),
                    header_updates={'CFMBKGSB': (True, 'master background subtracted (mbkg_extraction)')})
    return out


def run_spec3(product_name, cal_files, workspace, source_id):
    """The master-background copies of a product's science exposures and Spec3 on them
    (module doc, 2). Returns the ``product_paths``. Raises ``NoMasterBackground``."""
    from jwst.associations import asn_from_list as AL
    from jwst.pipeline import Spec3Pipeline

    copies = [write_frame(os.path.join(workspace, os.path.basename(f)), workspace) for f in cal_files]
    name = f'{product_name}{SUFFIX}'
    prev = os.getcwd()
    os.chdir(subdir(workspace))
    try:
        asn = AL.asn_from_list([(os.path.basename(c), 'science') for c in copies],
                               with_exptype=True, product_name=name)
        _, text = asn.dump()
        asn_file = f'{name}_spec3.json'
        with open(asn_file, 'w') as fh:
            fh.write(text)
        Spec3Pipeline.call(asn_file, save_results=True)
        for ext in ('s2d', 'x1d'):
            target = f'{name}_{ext}.fits'
            msa = f'{name}_s{int(source_id):09d}_{ext}.fits'
            if os.path.exists(msa):
                os.replace(msa, target)
                continue
            fs = sorted(glob.glob(f'{name}_*_s{int(source_id):09d}_{ext}.fits'))
            if fs:
                os.replace(fs[0], target)
        os.remove(asn_file)
    finally:
        os.chdir(prev)
    return product_paths(workspace, product_name, source_id)


def cleanup(workspace, product_name, cal_files):
    """Remove a product's master-background intermediates (copies, crf, Spec3 and
    profile-fit files) once its ``_spec.fits`` is written."""
    d, name = subdir(workspace), f'{product_name}{SUFFIX}'
    paths = [copy_path(workspace, f) for f in cal_files]
    paths += [c.replace('.fits', '_a3001_crf.fits') for c in paths]
    paths += glob.glob(os.path.join(d, f'{name}_*'))
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def same_slitlet_sources(canonical, workspace, source_id):
    """Catalogue sources within reach of the source's slitlet at some dither point
    (``slitlet_neighbours.scene``): the light single frames hold next to the target."""
    from campfire_pipeline.nirspec import slitlet_neighbours as SN
    _, nbs = SN.scene_for(canonical, workspace, int(source_id), {})
    return {n.source_id for n in nbs}


def self_subtraction(R, exps):
    """Share of the source's own light in its main shutter that the nod subtraction
    removes: per fitted exposure, the mean of its background exposures' model light
    on its main-shutter pixels over its own model light there, weighted by that light.
    0 for a compact source; large for one wider than the nod step (``extended``)."""
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import profile_fit as PF

    c_plan = R.model.c_prior
    num = den = 0.0
    frames = {}

    def frame(j):
        if j not in frames:
            frames[j] = np.nan_to_num(PF.frame_image(R, j))
        return frames[j]

    for k, e in enumerate(exps):
        if not e.use_data or not e.partners:
            continue
        ms = CT.main_shutter(e, c_plan) & e.ok
        own = float(np.sum(frame(k)[ms]))
        if own <= 0:
            continue
        sub = float(np.mean([np.sum(frame(j)[ms]) for j in e.partners]))
        num += sub
        den += own
    return num / den if den > 0 else 0.0


def preferred(exposures, self_sub, extended_frac):
    """(``MBKGPREF``, ``MBKGWHY``) of a product from its ``EXPOSURES`` rows (``bkg_why``,
    ``bkg_nods``) and its self-subtraction share (``self_subtraction``): the first of
    ``WHY`` that applies; (False, '') when none does.

    - ``no_clear_background``: an exposure kept a bright catalogued neighbor in every
      background exposure (``BKGWHY`` 'every background exposure: ...');
    - ``single_exposure``: an exposure had no background exposure (``n_exp_bkg`` 0);
    - ``extended``: the nod subtraction removes more than ``extended_frac`` of the
      source's own light in its main shutter."""
    why = [str(w) for w in exposures.get('bkg_why', [])]
    if any(w.startswith('every background exposure') for w in why):
        return True, 'no_clear_background'
    nbkg = exposure_counts(exposures)[1]
    if nbkg is not None and nbkg == 0:
        return True, 'single_exposure'
    if self_sub is not None and np.isfinite(self_sub) and self_sub > extended_frac:
        return True, 'extended'
    return False, ''


def exposure_counts(exposures):
    """(``NEXP``, ``NEXPBKG``) of a product's ``EXPOSURES`` rows: the exposures combined,
    and the fewest background exposures subtracted from any one of them (``bkg_nods``;
    'excluded' counts 0). ``NEXPBKG`` is None when no row records them."""
    nods = [str(v).strip() for v in exposures.get('bkg_nods', [])]
    counts = []
    for v in nods:
        if v.lower() == 'excluded':
            counts.append(0)
        elif v and v.upper() != 'N/A':
            counts.append(len([x for x in v.split(',') if x.strip()]))
    n = len(exposures.get('filename', nods))
    return int(n), (min(counts) if counts else None)


def background_sources(filenames, workspace):
    """Per exposure the master background it used (``MBKGSRC`` of its ``_mbkg.fits``:
    pool and group are both combinations of the group's exposures, 'combined';
    'exposure' its own), and the algorithm version (``CFMBKGV``, of the first)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec.profile_contam import exposure_key

    src, version = [], ''
    for f in filenames:
        p = os.path.join(workspace, f'{exposure_key(str(f))}_mbkg.fits')
        if not os.path.exists(p):
            src.append('')
            continue
        h = fits.getheader(p)
        version = version or str(h.get('CFMBKGV', ''))
        src.append('exposure' if str(h.get('MBKGSRC', '')).lower() == 'exposure' else 'combined')
    return src, version


def write_fallback(product_name, workspace, source_id, reason):
    """The master-background profile-fit file of a product whose nodded fit fell back
    (``profile_product.write_fallback``, same reason): its ``fnu`` is the 3-pixel boxcar
    too. Nothing when its Spec3 did not run."""
    from campfire_pipeline.nirspec import profile_product as PP
    paths = product_paths(workspace, product_name, source_id)
    if os.path.exists(paths['s2d']):
        PP.write_fallback(paths['pf'], reason)


def fit_product(product_name, workspace, groups, science, source_id, chain, stage_config, pass1_products=None,
                log=None):
    """``{product}_mbkg_pf.fits`` of a product (module doc, 3), after its nodded chain
    ``chain`` (``profile_chain.ChainResult``) chose the shape. Named conditions write a
    fallback file (the master-background ``fnu`` is then its 3-pixel boxcar). Returns
    the file's path, or None when the master-background Spec3 did not run (no
    ``_mbkg_s2d.fits``: no master-background spectrum at all)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import failed_open as FO
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_product as PP

    log = log or (lambda *a: None)
    paths = product_paths(workspace, product_name, source_id)
    if not os.path.exists(paths['s2d']):
        return None
    if not os.path.exists(paths['crf']):
        log(f'{product_name}: no master-background crf; its fnu is the 3-pixel boxcar')
        PP.write_fallback(paths['pf'], PP.FALLBACK_NO_CRF)
        return paths['pf']
    science = set(science)
    mb_groups = [[copy_path(workspace, f) for f in g if os.path.basename(f) in science
                  and os.path.exists(copy_path(workspace, f))] for g in groups]
    mb_groups = [g for g in mb_groups if g]
    names = {os.path.basename(c) for g in mb_groups for c in g}
    try:
        exps = PF.load_source(mb_groups, science=names, single_frames=True)
        M0 = chain.result.model
        M = PF.ProfileModel(exps, chromatic=M0.chromatic, priors=M0.priors, bounds=M0.bounds_cfg,
                            c_prior=M0.c_prior, grid=M0.lam_grid)
        M.pivot = M0.pivot                    # the chain's shape is defined at its own pivot
        p = chain.result.params
        shape = dict(centre_offset=p['centre'] - M0.c_prior, drift=p['drift'], width=p['width'],
                     width_change=p['width_change'])
        R = PF.fit_fixed(model=M, shape=shape)
    except PF.ProfileFitFallback as e:
        log(f'{product_name}: master-background fit: {e}; its fnu is the 3-pixel boxcar ({e.reason})')
        PP.write_fallback(paths['pf'], e.reason)
        return paths['pf']

    hdus = []
    prof = PF.drizzle_frames(R, paths['crf'], unit=True)
    hdus.append(fits.ImageHDU(np.asarray(prof.data, float) / 1e12, name='PROF2D'))
    prof.close()
    fo_cards = (0, 0.0)
    fl = PP.stage_flags(stage_config)
    if fl['contam']:
        maps = CT.maps_for_exposures(workspace, exps)
        if maps:
            pass1_products = pass1_products or {}

            def pass1_for(sid):
                name = pass1_products.get(int(sid))
                path = CT.pass1_path(workspace, name) if name else None
                return CT.Pass1.read(path) if path and os.path.exists(path) else None

            def why_missing(sid):
                name = pass1_products.get(int(sid))
                return CT.missing_reason(workspace, name) if name else 'no pass 1 in this disperser'

            canonical = os.path.join(workspace, os.path.basename(groups[0][0]))
            near = same_slitlet_sources(canonical, workspace, source_id)
            P = CT.predict(exps, source_id, maps, pass1_for, workspace, why_missing=why_missing, include=near)
            s1 = CT.pass1_settings(stage_config)
            knot = max(s1['smooth_prism'] if exps[0].grating.startswith('PRISM') else s1['smooth_grating'], 3)
            p_shape = PF.shape_vector(shape, M.c_prior, M.priors.width_median)
            L = FO.solve(M, p_shape, exps, source_id, maps, knot, minus=P.pred)
            if L is not None:
                for k in range(len(exps)):
                    P.pred[k] = P.pred[k] + L.pred[k]
                    P.dpred[k] = P.dpred[k] + L.dpred[k]
                fo_cards = (len({n for _, n in L.shutters}), max(L.shutters.values(), default=0.0))
                log(f'{product_name}: master background: {fo_cards[0]} failed-open shutter(s) on the trace, '
                    f'their light up to {fo_cards[1]:.1f} x the noise on the main shutter')
            for what, name, unit in (('pred', 'CONTAM2D', 'Predicted other-source light, as SCI'),
                                     ('dpred', 'CONTERR2D', 'Uncertainty of CONTAM2D (systematic), as SCI')):
                live = CT.live_frames(exps, P, what=what)
                d2 = PF.drizzle_arrays(lambda n: live.get(n), paths['crf'])
                h = fits.ImageHDU(np.asarray(d2.data, float), name=name)
                h.header['BUNIT'] = ('MJy', unit)
                hdus.append(h)
                d2.close()
    hdr = fits.Header()
    for k, v in chain.header_cards().items():
        hdr[k] = v
    hdr['PFNEXP'] = (len(exps), 'Exposures in the model (master background subtracted)')
    hdr['PFNFOPEN'] = (int(fo_cards[0]), 'Failed-open shutters modeled on the trace')
    hdr['PFFOSIG'] = (float(fo_cards[1]), 'Their max light / noise on the main shutter')
    fits.HDUList([fits.PrimaryHDU(header=hdr)] + hdus).writeto(paths['pf'], overwrite=True)
    return paths['pf']
