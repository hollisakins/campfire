"""A target's exposures for the forward model, from the deployed files alone: its canonicals
(``campfire pull --obs X --intermediate``) and, for single frames, each exposure's ``_mbkg.fits``
beside them.

Stage 3 leaves in each canonical what its fits took from files it does not keep
(``canonical.write_stage3``): Spec3's outlier flags in DQ bit 4, the master-background frames' own
flags in bit 4 of ``PRE_BKGSUB_DQ``, the other sources' predicted light with the level above which
a pixel is left out (``CONTAM``), and the bar offset (``BAROFFS``). ``load`` reads them back and
prepares the exposures as stage 3 prepared them for its fit, so a model built on them
(``ForwardModel(load(files))``) sees the pixels stage 3's fit saw.

    files = forward.files_for('products/nirspec/zenith_p15', 547856)
    exps = forward.load(files)                        # the nod differences
    F = forward.ForwardModel(exps)
    s = F.solve([forward.Point()], covariance=True)
"""
import glob
import os
import warnings

import numpy as np
from astropy.io import fits

from campfire_pipeline.nirspec import canonical as CN
from campfire_pipeline.nirspec import profile_fit as PF


def files_for(directory, source_id, disperser=None):
    """The canonicals of one source in an observation's products directory
    (``<root>_<nrsN>_<source>.fits``), optionally of one disperser (``'PRISM/CLEAR'``, ``'G395M/F290LP'``)."""
    out = []
    for p in sorted(glob.glob(os.path.join(directory, f'jw*_nrs[12]_{int(source_id)}.fits'))):
        if disperser is not None:
            h = fits.getheader(p)
            if f"{h.get('GRATING', '')}/{h.get('FILTER', '')}".upper() != disperser.upper():
                continue
        out.append(p)
    return out


def groups_of(files):
    """The files in their background groups (one exposure group of one detector, the cards
    ``CFEXPGRP`` and ``DETECTOR``), as stage 2b subtracted them: [[path, ...], ...]."""
    by = {}
    for p in files:
        h = fits.getheader(p)
        g = h.get(CN.EXP_GROUP_KEYWORD)
        if g is None:
            raise ValueError(f'{os.path.basename(p)}: no {CN.EXP_GROUP_KEYWORD} card (written before stage 2a '
                             'stamped it); its background group is not known')
        by.setdefault((int(g), str(h.get('DETECTOR', ''))), []).append(p)
    return [by[k] for k in sorted(by)]


def science_names(files):
    """The names stage 3 combines: background-subtracted and containing the source."""
    out = set()
    for p in files:
        h = fits.getheader(p)
        v = h.get('CFP_BKG')
        if bool(v) and not str(v).startswith(('skipped', 'excluded')) and str(h.get('SRCFLUX', 'T')) == 'T':
            out.add(os.path.basename(p))
    return out


def _apply_bar_offsets(exps):
    """Each exposure's bar offset from its file (``BAROFFS``), as ``profile_fit.load_group`` applies
    a measured one: added to every pixel's slit position and to the planned position."""
    for e in exps:
        bar = fits.getheader(e.path).get('BAROFFS')
        if bar is None:
            continue
        e.y = e.y + float(bar)
        e.src_y = e.src_y + float(bar)
        e.bar_offset = float(bar)


def _placed(e, a):
    """An array on exposure ``e``'s cutout placed on its group's common frame (0 outside)."""
    r0, c0, ny, nx = e.cutout
    out = np.zeros(e.y.shape)
    out[r0:r0 + ny, c0:c0 + nx] = a
    return out


def _subtract_contam(exps, planes_of, pred_key, mask_key):
    """Stage 3's use of the predicted light (``profile_contam.apply``) from the stored planes:
    subtracted, and the pixels whose masking level exceeds ``CONTKAPP`` times the noise left out.
    Returns {file name: the CONTAM header state} of the exposures it touched."""
    from campfire_pipeline.nirspec import profile_contam as CT
    pred, lev, kappas, states = [], [], set(), {}
    for e in exps:
        c = planes_of.get(e.name)
        if c is None or pred_key not in c['planes'] or str(c.get('state', '')).endswith('_undone'):
            pred.append(np.zeros(e.y.shape))
            lev.append(np.zeros(e.y.shape))
            continue
        pred.append(_placed(e, c['planes'][pred_key]) * e.pl_applied)
        lev.append(_placed(e, c['planes'][mask_key]) * e.pl_applied)
        kappas.add(c.get('kappa'))
        states[e.name] = c.get('state', '')
    if not states:
        return states
    if len(kappas) != 1 or None in kappas:
        raise ValueError(f'forward.load: the exposures carry different or no CONTKAPP ({sorted(map(str, kappas))})')
    P = CT.Prediction(pred=pred, dpred=lev)
    CT.apply(exps, P, kappa=float(kappas.pop()))
    return states


def load(files, frames=False, outliers=True, contam=True, slit_loss=None):
    """The exposures of one target (one disperser) prepared as stage 3 prepared them for its fit.

    files : every canonical of the target (``files_for``), background exposures included; they are
        put in their background groups from their headers.
    frames : False, the nod differences (stage 3's fit); True, the frames minus the master background
        (the master-background fit; each exposure's ``_mbkg.fits`` beside its canonical), with
        ``Exposure.mbkg`` set for the scale term (``ForwardModel(..., mbkg_sigma=...)``).
    outliers : leave out the pixels stage 3 flagged in DQ bit 4 (in a nod difference also those
        flagged in the frames it was differenced with).
    contam : subtract the other sources' predicted light and leave out the pixels where its
        uncertainty is large (``CONTAM``), as stage 3 did.
    slit_loss : whose path loss the model takes (``crds_refs.SlitLoss``; None: the pipeline's default).

    A file stage 3 has not written (no ``CFOUTL`` card) is used as it is, with a warning."""
    files = [str(f) for f in files]
    groups = groups_of(files)
    science = science_names(files)
    if not science:
        raise PF.NoUsablePixels('forward.load: no background-subtracted exposure with the source among the files')
    stale = [os.path.basename(p) for p in files if os.path.basename(p) in science and 'CFOUTL' not in fits.getheader(p)]
    if stale:
        warnings.warn(f'forward.load: stage 3 has not written {", ".join(stale)} (no outlier flags, no '
                      'predicted light, no bar offset): used as they are')
    exps = PF.load_source(groups, science=science, slit_loss=slit_loss)
    _apply_bar_offsets(exps)
    if outliers:
        dq = {e.name: np.asarray(fits.getdata(e.path, 'DQ')) for e in exps if e.use_data}
        PF.mask_crf_outliers(exps, dq)              # bit 4, the bit Spec3's crf carries
    planes_of = {e.name: CN.read_contam(e.path) for e in exps if e.use_data} if contam else {}
    planes_of = {k: v for k, v in planes_of.items() if v is not None}
    if not frames:
        if contam:
            _subtract_contam(exps, planes_of, 'NOD', 'NODMASK')
        return exps
    from campfire_pipeline.nirspec import mbkg_extraction as MX
    sci = [e for e in exps if e.use_data]
    fr = MX.frames_for([e.path for e in sci])
    inputs = {}
    for n, f in fr.items():
        a = dict(f.arrays)
        if outliers:
            # the frame's own flags (bit 4 of PRE_BKGSUB_DQ) leave the pixel out, as its 2-D did
            a['dq'] = np.where(a['dq'] & CN.OUTLIER_BIT, a['dq'] | MX.DO_NOT_USE, a['dq']).astype(a['dq'].dtype)
        inputs[n] = a
    mexps = MX.single_frame_exposures(sci, inputs, backgrounds={n: f.background for n, f in fr.items()})
    if contam:
        _subtract_contam(mexps, planes_of, 'FRAME', 'FRAMEMSK')
    return mexps
