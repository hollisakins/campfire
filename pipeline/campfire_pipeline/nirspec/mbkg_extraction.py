"""
The master-background spectrum: the secondary product of every source (extraction v2;
user decisions 2026-10-29 and 2026-11-01).

The nodded spectrum stays the preferred product. This one is the same extraction on the
exposures with their master background subtracted instead of the other exposures, so the
two differ only in how the background was removed (parity). Everything is made in memory
on the nodded product's 2-D grid, through its pixel maps (``drizzle_map.DrizzleMap``, one
per product, built from the nodded Spec3 crf), before any 1-D extraction:

1. ``frame_arrays``: each science exposure's calibrated frame (``PRE_BKGSUB_SCI``, or the
   live SCI before stage 2b) minus its master background (``stage2.master_background_on``),
   with the master background's variance added to the Poisson term and DO_NOT_USE where
   it has none. Nothing is written.
2. ``drizzle_2d``: those frames drizzled as data onto the nodded grid, with their outlier
   flags (``outlier_flags``: the choice is there): the nodded Spec3's, plus the pixels
   fixed high on the detector in every exposure (``hot_pixels``), which cancel in the nod
   differences. Into ``{workspace}/mbkg/{product}_mbkg_s2d.fits`` (SCI, ERR, WHT). The
   grid is the nodded one by construction.
3. ``fit_product``: the nodded fit's exposures with their frame minus master background
   in place of the nod difference, as single frames (``single_frame_exposures``: nothing
   subtracted from them), the forward model at the shape the nodded chain chose, the
   spectrum solved. Other sources' light is predicted from their pass 1s, once and
   positive where each is in that exposure (``profile_contam.predict``), catalogued
   neighbors in the target's own slitlet included (``include``): no background exposure
   is dropped for them here. Light through failed-open shutters on the trace is solved
   with the source and added to it (``failed_open``; card ``NFOPEN``).
   ``PROF2D`` / ``CONTAM2D`` / ``CONTERR2D`` are drizzled through the same maps with the
   frames' own masks and weights (``drizzle_on_frames``: the nodded crf's would combine
   other exposures than the 2-D's at a pixel masked in only one of them) into
   ``{product}_mbkg_pf.fits``.
4. stage 3 extracts it exactly like the nodded product (``stage3.extract_columns``) into
   ``SPEC1D_MBKG``, ``SCI_MBKG``, ``ERR_MBKG`` and ``PROF1D_MBKG`` of the same
   ``_spec.fits`` (``stage3.opt_ext_single_source``), with the primary cards ``MBKGPREF``
   / ``MBKGWHY`` (``preferred``) and ``MBKGNEXP``.

Steps 1-3 run in the product's profile fit (``profile_product.run_product``); without the
profile fit, steps 1-2 run after its Spec3 (``write_2d``); a 1-D combined product of
several exposure groups gets its 2-D the same way from the stacked crf
(``stage3._combined_mbkg``).

Named conditions (no master background for an exposure, no crf, a profile-fit fallback,
a 2-D of another shape than the nodded one) leave the product without the
master-background spectrum or with its 3-pixel boxcar as ``fnu``, logged. Anything else
fails the stage.
"""

import glob
import os
import warnings
from dataclasses import dataclass

import numpy as np

MBKG_SUBDIR = 'mbkg'            # the 2-D and profile-fit files, inside the workspace
SUFFIX = '_mbkg'                # product name suffix of the master-background files
WHY = ('no_clear_background', 'single_exposure', 'extended')
FALLBACK_NO_MBKG = 'no_master_background'
OUTLIER = 16                    # jwst DQ OUTLIER
DO_NOT_USE = 1


class NoMasterBackground(RuntimeError):
    """An exposure of the product has no ``_mbkg.fits`` (named: no master-background spectrum)."""


def subdir(workspace):
    return os.path.join(workspace, MBKG_SUBDIR)


def product_paths(workspace, product_name, source_id=0):
    """The master-background 2-D and profile-fit files of a product."""
    d, p = subdir(workspace), f'{product_name}{SUFFIX}'
    return dict(s2d=os.path.join(d, f'{p}_s2d.fits'), pf=os.path.join(d, f'{p}_pf.fits'))


@dataclass
class Frame:
    """One exposure's frame minus its master background (``frame_arrays``).

    arrays : ``data``, ``dq``, ``err``, ``var_poisson``, ``var_rnoise`` (and ``var_flat``)
        on the canonical's cutout, as the drizzle takes them (float32 / uint32).
    live_dq : the canonical's live DQ (``outlier_flags``).
    detector, origin : where the cutout sits: detector name and its (ystart, xstart),
        1-based like jwst (``hot_pixels``)."""
    arrays: dict
    live_dq: np.ndarray
    detector: str = ''
    origin: tuple = (1, 1)


def frame_arrays(canonical):
    """One canonical's frame minus its master background, in memory (module doc, 1): a
    ``Frame``. Raises ``NoMasterBackground`` when its exposure has no ``_mbkg.fits``."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import canonical as C
    from campfire_pipeline.nirspec.stage2 import master_background_on

    bg = master_background_on(canonical)
    if bg is None:
        raise NoMasterBackground(f'{os.path.basename(canonical)}: no master background beside it')
    sky, sky_var = bg
    live = C.read_slit_arrays(canonical, exts=('SCI', 'DQ', 'VAR_POISSON', 'VAR_RNOISE', 'VAR_FLAT'))
    pre = C.read_slit_arrays(canonical, prefix=C.PRE_BKGSUB_PREFIX)
    sci = np.asarray(pre.get('SCI', live['SCI']), float)
    vp = np.asarray(pre.get('VAR_POISSON', live['VAR_POISSON']), float)
    vr = np.asarray(pre.get('VAR_RNOISE', live['VAR_RNOISE']), float)
    dq = np.asarray(pre.get('DQ', live['DQ'])).astype(np.uint32)
    known = np.isfinite(sky)
    sci = np.where(known, sci - sky, np.nan)
    vp = np.where(known & np.isfinite(sky_var), vp + sky_var, vp)
    dq = np.where(known, dq, dq | DO_NOT_USE).astype(np.uint32)
    vf = np.asarray(live['VAR_FLAT'], float) if np.size(live.get('VAR_FLAT', [])) else 0.0
    frame = dict(data=sci.astype(np.float32), dq=dq, var_poisson=vp.astype(np.float32),
                 var_rnoise=vr.astype(np.float32), err=np.sqrt(vp + vr + vf).astype(np.float32))
    if np.size(live.get('VAR_FLAT', [])):
        frame['var_flat'] = np.asarray(live['VAR_FLAT']).astype(np.float32)
    h = fits.getheader(canonical, 'SCI')
    return Frame(frame, np.asarray(live['DQ']).astype(np.uint32), str(fits.getheader(canonical).get('DETECTOR', '')),
                 (int(h.get('SLTSTRT2', 1)), int(h.get('SLTSTRT1', 1))))


def frames_for(canonicals):
    """``frame_arrays`` of every canonical, keyed by file name: {name: Frame}. Raises
    ``NoMasterBackground``."""
    return {os.path.basename(c): frame_arrays(c) for c in canonicals}


# ---------------------------------------------------------------------------
# Outlier flags of the master-background frames: the choice is here, and only here
# ---------------------------------------------------------------------------
HOT_SIGMA = 5.0          # a hot pixel stands this many sigma above its row and its column neighbors ...
HOT_MIN_EXPOSURES = 2    # ... in every exposure that sees the detector pixel, at least this many
HOT_ROW_HALF = 3         # row neighbors: +-3 pixels along the dispersion
HOT_COL_HALF = 2         # column neighbors: +-2 pixels across it


def _shifted_median(a, axis, half):
    """Per pixel, the median of its neighbors within +-``half`` along ``axis`` (itself
    left out; NaN outside the array or unusable)."""
    n = a.shape[axis]
    stack = []
    for s in range(-half, half + 1):
        if s == 0:
            continue
        b = np.full(a.shape, np.nan)
        src = [slice(None)] * 2
        dst = [slice(None)] * 2
        src[axis] = slice(max(s, 0), n + min(s, 0))
        dst[axis] = slice(max(-s, 0), n + min(-s, 0))
        b[tuple(dst)] = a[tuple(src)]
        stack.append(b)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)          # all-NaN neighborhoods
        return np.nanmedian(np.array(stack), axis=0)


def local_contrast(frame):
    """How far each pixel of a frame stands above its surroundings in that frame, in
    sigma: the smaller of (value minus the median of its row neighbors within
    ``HOT_ROW_HALF``) and (value minus the median of its column neighbors within
    ``HOT_COL_HALF``), over its error. NaN where the frame is unusable.

    A single hot pixel stands above both. Light smooth along the dispersion (a trace, a
    failed-open shutter's band) does not stand above its row; light spread across a few
    rows (an extended line, a band's edge rows) does not stand above its column."""
    a = frame.arrays
    d = np.where((a['dq'] & DO_NOT_USE) > 0, np.nan, np.asarray(a['data'], float))
    with np.errstate(invalid='ignore', divide='ignore'):
        rx = d - _shifted_median(d, 1, HOT_ROW_HALF)
        ry = d - _shifted_median(d, 0, HOT_COL_HALF)
        return np.minimum(rx, ry) / np.asarray(a['err'], float)


def hot_pixels(frames):
    """Pixels fixed high on the detector: {name: boolean mask on its cutout}.

    A detector pixel is hot where ``local_contrast`` exceeds ``HOT_SIGMA`` in every
    exposure (on the same detector) that has a usable value there, and at least
    ``HOT_MIN_EXPOSURES`` do. Such a pixel cancels in the nod differences, so the nodded
    outlier detection never flags it, but it stays in a frame minus master background.
    Sky light, the source and neighbors move on the detector between nods, so they cannot
    be high at one detector pixel in every exposure (the source of a product with no nod
    at all could; its exposures would then count as one). With one exposure nothing tells
    a hot pixel from the source: no pixel is flagged (jwst's outlier detection also skips
    a single exposure). A cosmic ray hits one exposure and is not flagged here."""
    names = list(frames)
    out = {n: np.zeros(frames[n].arrays['data'].shape, bool) for n in names}
    for det in sorted({frames[n].detector for n in names}):
        mem = [n for n in names if frames[n].detector == det]
        if len(mem) < HOT_MIN_EXPOSURES:
            continue
        y0 = min(frames[n].origin[0] for n in mem)
        x0 = min(frames[n].origin[1] for n in mem)
        y1 = max(frames[n].origin[0] + frames[n].arrays['data'].shape[0] for n in mem)
        x1 = max(frames[n].origin[1] + frames[n].arrays['data'].shape[1] for n in mem)
        seen = np.zeros((y1 - y0, x1 - x0), int)
        high = np.zeros((y1 - y0, x1 - x0), int)
        cut, usable = {}, {}
        for n in mem:
            f = frames[n]
            r, c = f.origin[0] - y0, f.origin[1] - x0
            ny, nx = f.arrays['data'].shape
            cut[n] = (slice(r, r + ny), slice(c, c + nx))
            s = local_contrast(f)
            usable[n] = np.isfinite(s)
            seen[cut[n]] += usable[n]
            high[cut[n]] += usable[n] & (s > HOT_SIGMA)
        hot = (seen >= HOT_MIN_EXPOSURES) & (high == seen)
        for n in mem:
            out[n] = hot[cut[n]] & usable[n]
    return out


def outlier_flags(frame, crf_dq, hot):
    """A master-background frame's arrays with its outlier flags: the decision of where
    they come from is here, and only here (user, 2026-11-02).

    Flagged ``OUTLIER`` and DO_NOT_USE, on top of its own mask (no master background:
    DO_NOT_USE, ``frame_arrays``):
    - what the nodded Spec3's outlier detection flagged in this exposure (``OUTLIER`` in
      the nodded crf's DQ that the canonical's live DQ does not have);
    - its hot pixels (``hot``, ``hot_pixels``), which cancel in the nod differences.
    jwst's outlier detection is not run on the frames. Before 2026-11-02 they had a Spec3
    run of their own and its outlier detection on them."""
    a = frame.arrays
    new = ((np.asarray(crf_dq) & OUTLIER) > 0) & ((np.asarray(frame.live_dq) & OUTLIER) == 0)
    out = dict(a)
    out['dq'] = (a['dq'] | np.where(new | np.asarray(hot, bool), OUTLIER | DO_NOT_USE, 0)).astype(a['dq'].dtype)
    return out


def flagged_inputs(dmap, frames):
    """The drizzle inputs of the master-background frames: ``frames`` ({name: Frame},
    every exposure of the map) with their outlier flags (``outlier_flags``), {name: arrays}."""
    missing = [x.name for x in dmap.exposures if x.name not in frames]
    if missing:
        raise ValueError(f'master background: no frame for {missing}')
    hot = hot_pixels({x.name: frames[x.name] for x in dmap.exposures})
    return {x.name: outlier_flags(frames[x.name], x.arrays['dq'], hot[x.name]) for x in dmap.exposures}


def drizzle_2d(dmap, inputs):
    """The master-background 2-D spectrum (module doc, 2): the frames' ``inputs``
    (``flagged_inputs``) drizzled as data through ``dmap``. Returns dict(data, err, wht,
    ...) (``DrizzleMap.drizzle``)."""
    return dmap.drizzle(inputs, variances=True)


def drizzle_on_frames(dmap, inputs, images):
    """Model images ({name: image on the crf cutout}; a name not given: zeros) drizzled
    with the master-background frames' masks and weights (``inputs``), as their 2-D was
    (``drizzle_2d``), so a profile and the data it weights combine the same exposures at
    every pixel. Returns the 2-D image."""
    out = {}
    for x in dmap.exposures:
        img = images.get(x.name)
        a = dict(inputs[x.name])
        shape = np.shape(a['data'])
        a['data'] = np.nan_to_num(np.zeros(shape) if img is None else np.asarray(img, float)).astype(np.float32)
        out[x.name] = a
    return dmap.drizzle(out)['data']


def write_2d_file(path, d2):
    """``{product}_mbkg_s2d.fits``: SCI, ERR, WHT of ``drizzle_2d`` (MJy, the s2d's units)."""
    from astropy.io import fits
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(d2['data'], name='SCI'), fits.ImageHDU(d2['err'], name='ERR'),
                  fits.ImageHDU(d2['wht'], name='WHT')]).writeto(path, overwrite=True)


def write_2d(product_name, workspace, source_id, dmap, log=None):
    """Steps 1-2 of the module doc for a product: its master-background 2-D through
    ``dmap`` (the frames of the map's exposures, the canonicals in ``workspace``).
    Returns (frames {name: Frame}, their drizzle inputs ``flagged_inputs``), or None
    (logged) when an exposure has no master background (no master-background spectrum)."""
    log = log or (lambda *a: None)
    try:
        frames = frames_for([os.path.join(workspace, n) for n in dmap.names])
    except NoMasterBackground as e:
        log(f'{product_name}: no master-background spectrum ({e})')
        return None
    inputs = flagged_inputs(dmap, frames)
    write_2d_file(product_paths(workspace, product_name, source_id)['s2d'], drizzle_2d(dmap, inputs))
    return frames, inputs


def cleanup(workspace, product_name):
    """Remove a product's master-background files (2-D, profile fit) once its
    ``_spec.fits`` is written."""
    for p in glob.glob(os.path.join(subdir(workspace), f'{product_name}{SUFFIX}_*')):
        os.remove(p)


def single_frame_exposures(exps, frames):
    """The fit's master-background exposures, in memory (module doc, 3): every science
    exposure of the nodded fit (``exps``, ``use_data``) with its frame minus master
    background (``frames``) in place of the nod difference, nothing subtracted
    (``partners == []``). Geometry, variance and path loss are the nodded load's; the
    usable pixels are recomputed from the frame (``profile_fit.load_group``). Each
    group's science exposures are placed on their own common frame, as
    ``profile_fit.load_source(single_frames=True)`` on those exposures alone places them."""
    import copy
    from campfire_pipeline.nirspec import profile_fit as PF

    sci = [e for e in exps if e.use_data]
    out = []
    for g, group in enumerate(sorted({e.group for e in sci})):
        mem = [e for e in sci if e.group == group]
        det = [(e.origin[0] + e.cutout[0], e.origin[1] + e.cutout[1], e.cutout[2], e.cutout[3]) for e in mem]
        y0, x0 = min(d[0] for d in det), min(d[1] for d in det)
        shape = (max(d[0] + d[2] for d in det) - y0, max(d[1] + d[3] for d in det) - x0)
        for e, (ys, xs, ny, nx) in zip(mem, det):
            r0, c0 = e.cutout[0], e.cutout[1]
            cut = (slice(r0, r0 + ny), slice(c0, c0 + nx))
            r1, c1 = ys - y0, xs - x0

            def place(a, fill=np.nan):
                p = np.full(shape, fill, dtype=float)
                p[r1:r1 + ny, c1:c1 + nx] = a
                return p
            frame = frames[e.name].arrays
            pl = e.pl_applied[cut]
            var = e.var[cut]                      # var x pl^2: positive and finite where var is (pl > 0)
            diff = np.asarray(frame['data'], float) * pl
            dq_bad = (np.asarray(frame['dq']) & PF.DO_NOT_USE) > 0
            ok = (np.isfinite(diff) & np.isfinite(var) & (var > 0) & ~dq_bad & np.isfinite(e.y[cut])
                  & np.isfinite(e.wav[cut]) & np.isfinite(pl) & (pl > 0))
            x = copy.copy(e)
            x.group, x.partners, x.use_data = g, [], True
            x.diff, x.var, x.ok = place(diff), place(var), place(ok.astype(float), fill=0.0) > 0
            x.wav, x.y = place(e.wav[cut]), place(e.y[cut])
            x.pl_applied, x.pl_point = place(pl), place(e.pl_point[cut])
            x.cutout, x.origin = (r1, c1, ny, nx), (y0, x0)
            out.append(x)
    return out


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


def write_fallback(product_name, workspace, source_id, reason, dmap, log=None):
    """The master-background files of a product whose nodded fit fell back
    (``profile_product.write_fallback``, same reason): its 2-D through ``dmap`` and a
    profile-fit file holding only the reason, so its ``fnu`` is the 3-pixel boxcar too.
    Nothing when an exposure has no master background."""
    from campfire_pipeline.nirspec import profile_product as PP
    if write_2d(product_name, workspace, source_id, dmap, log=log) is None:
        return
    PP.write_fallback(product_paths(workspace, product_name, source_id)['pf'], reason)


def fit_product(product_name, workspace, exps, science, source_id, chain, stage_config, dmap,
                pass1_products=None, log=None, stuck_keys=None):
    """The master-background 2-D and ``{product}_mbkg_pf.fits`` of a product (module doc,
    1-3), after its nodded chain ``chain`` (``profile_chain.ChainResult``) chose the shape
    on the nodded exposures ``exps``; everything drizzled through the product's map
    ``dmap``. A profile-fit fallback writes a fallback file (the master-background
    ``fnu`` is then its 3-pixel boxcar). Returns the file's path, or None when an
    exposure has no master background (no master-background spectrum at all).
    ``stuck_keys``: the stuck physical shutters left out of the trace maps
    (``profile_contam.trace_maps``)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import failed_open as FO
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec import profile_product as PP

    log = log or (lambda *a: None)
    paths = product_paths(workspace, product_name, source_id)
    science = set(science)
    made = write_2d(product_name, workspace, source_id, dmap, log=log)
    if made is None:
        return None
    frames, inputs = made
    try:
        mexps = single_frame_exposures([e for e in exps if e.name in science], frames)
        if not mexps:
            raise PF.NoUsablePixels('master background: no science exposure')
        R, shape = PF.refit_at_shape(chain.result, mexps)
    except PF.ProfileFitFallback as e:
        log(f'{product_name}: master-background fit: {e}; its fnu is the 3-pixel boxcar ({e.reason})')
        PP.write_fallback(paths['pf'], e.reason)
        return paths['pf']

    # every model image on the frames' own masks and weights, as their 2-D (drizzle_on_frames)
    M = R.model
    by_name = {e.name: k for k, e in enumerate(M.exps)}
    hdus = []
    prof = drizzle_on_frames(dmap, inputs, {n: PF.model_frame(R, by_name[n], unit=True) for n in dmap.names
                                            if n in by_name})
    hdus.append(fits.ImageHDU(np.asarray(prof, float) / 1e12, name='PROF2D'))
    fo_cards = (0, 0.0)
    fl = PP.stage_flags(stage_config)
    if fl['contam']:
        maps = CT.maps_for_exposures(workspace, mexps, stuck_keys)
        if maps:
            pass1_for, why_missing = CT.pass1_lookup(workspace, pass1_products)
            near = same_slitlet_sources(exps[0].path, workspace, source_id)
            P = CT.predict(mexps, source_id, maps, pass1_for, workspace, why_missing=why_missing, include=near)
            s1 = CT.pass1_settings(stage_config)
            knot = max(s1['smooth_prism'] if mexps[0].grating.startswith('PRISM') else s1['smooth_grating'], 3)
            p_shape = PF.shape_vector(shape, M.c_prior, M.priors.width_median)
            L = FO.solve(M, p_shape, mexps, source_id, maps, knot, minus=P.pred)
            if L is not None:
                for k in range(len(mexps)):
                    P.pred[k] = P.pred[k] + L.pred[k]
                    P.dpred[k] = P.dpred[k] + L.dpred[k]
                fo_cards = (len({n for _, n in L.shutters}), max(L.shutters.values(), default=0.0))
                log(f'{product_name}: master background: {fo_cards[0]} failed-open shutter(s) on the trace, '
                    f'their light up to {fo_cards[1]:.1f} x the noise on the main shutter')
            for what, name, unit in (('pred', 'CONTAM2D', 'Predicted other-source light, as SCI'),
                                     ('dpred', 'CONTERR2D', 'Uncertainty of CONTAM2D (systematic), as SCI')):
                d2 = drizzle_on_frames(dmap, inputs, CT.live_frames(mexps, P, what=what))
                h = fits.ImageHDU(np.asarray(d2, float), name=name)
                h.header['BUNIT'] = ('MJy', unit)
                hdus.append(h)
    from campfire_pipeline.nirspec import extraction_cards as XC
    hdr = fits.Header()
    for k, v in chain.header_cards().items():
        XC.put(hdr, k, v)
    XC.put(hdr, 'NFOPEN', int(fo_cards[0]))
    fits.HDUList([fits.PrimaryHDU(header=hdr)] + hdus).writeto(paths['pf'], overwrite=True)
    return paths['pf']
