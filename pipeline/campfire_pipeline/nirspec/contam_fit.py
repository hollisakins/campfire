"""
Two-pass profile fit of an MSA configuration, removing other sources' light (prototype).

Other slitlets' spectra land on a source's trace (``trace_overlap``). Every such
contaminant is itself a source of the mask that the profile fit models, so its
light on the target's pixels can be predicted and subtracted instead of masked
(masking cannot work in G395M: a contaminating slitlet nodded with the target
lights one shutter per frame, so across the nods it covers the whole slitlet).

Pass 0 fits every source alone. Pass N fits every source again after subtracting
each contaminant's pass N-1 model of the same nod difference. Each pass is a set of
independent per-source fits; the only coupling is that pass N reads the
contaminants' pass N-1 results. This converges when a source's light on its
neighbours is small next to their own signal, which it almost always is; a further
pass that changes nothing beyond the noise is the check.

Units. A pixel's calibrated value depends on the slitlet it is assigned to (flat
field and photometric conversion at that slitlet's wavelength), so a contaminant's
model, in its own units, is converted to the target's with the ratio of the two
cutouts' calibration at that pixel: the pre-subtraction frame (path loss removed)
over the rate frame.

Contaminants with no model (failed-open shutters, sources without a canonical
cutout or whose fit failed) are masked.
"""

import glob
import os
import re
from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import trace_overlap as TO

_NOD = re.compile(r'_(\d{5})_(nrs[12])_')


def source_groups(products, root, sid):
    """Canonical-file groups (one per detector) of a source in one configuration."""
    return [fs for det in ('nrs1', 'nrs2')
            if (fs := sorted(glob.glob(os.path.join(products, f'{root}_0000?_{det}_{sid}.fits'))))]


def nod_key(path):
    """('00001', 'nrs1') of a canonical or rate file."""
    m = _NOD.search(os.path.basename(path))
    return m.group(1), m.group(2)


def maps_for(maps_dir, root):
    """Trace maps of a configuration, keyed by (nod, detector)."""
    out = {}
    for f in glob.glob(os.path.join(maps_dir, f'{root}_*_tracemap.fits')):
        out[nod_key(f.replace('_tracemap', '_x'))] = TO.TraceMap.read(f)
    return out


def calibration(e, rate_dir):
    """Per-pixel calibration of exposure e on its common frame: calibrated (path loss
    removed) over rate. NaN where either is unusable."""
    from astropy.io import fits

    nod, det = nod_key(e.name)
    root = e.name.split(f'_{nod}_')[0]
    rate = fits.getdata(os.path.join(rate_dir, f'{root}_{nod}_{det}_rate.fits'), 'SCI')
    with fits.open(e.path) as h:
        pre = np.asarray(h['PRE_BKGSUB_SCI'].data, float)
    r0, c0, ny, nx = e.cutout
    ys, xs = e.origin[0] + r0, e.origin[1] + c0
    raw = rate[ys - 1:ys - 1 + ny, xs - 1:xs - 1 + nx]
    cal = np.full(e.y.shape, np.nan)
    with np.errstate(divide='ignore', invalid='ignore'):
        cal[r0:r0 + ny, c0:c0 + nx] = np.where(raw != 0, pre * e.pl_applied[r0:r0 + ny, c0:c0 + nx] / raw, np.nan)
    return cal


@dataclass
class Contamination:
    """Other sources' light in one source's nod differences, from the trace maps."""
    mask: list                      # per exposure: bool on the common frame, any foreign source-shutter light
    sources: list                   # per exposure: {contaminating source id: its pixels (bool)}
    unmodelled: list = field(default_factory=list)   # per exposure: bool, failed-open shutters


def predict_contamination(exps, sid, maps):
    """Where other sources' light falls in each of ``exps``' nod differences."""
    n = len(exps)
    out = Contamination(mask=[np.zeros(e.y.shape, bool) for e in exps], sources=[{} for _ in exps],
                        unmodelled=[np.zeros(e.y.shape, bool) for e in exps])
    for g in sorted({e.group for e in exps}):
        ks = [k for k in range(n) if exps[k].group == g]
        try:
            mlist = [maps[nod_key(exps[k].name)] for k in ks]
        except KeyError:
            continue
        e0 = exps[ks[0]]
        ny, nx = e0.y.shape
        region = (e0.origin[1] - 1, e0.origin[0] - 1, nx, ny)
        partners = [[ks.index(j) for j in exps[k].partners] if exps[k].partners is not None else []
                    for k in ks]
        for r in TO.nod_contamination(mlist, int(sid), region, partners):
            k = ks[r.exposure]
            out.mask[k][r.iy, r.ix] = True
            if r.kind == 'slitlet':
                px = out.sources[k].setdefault(r.source_id, np.zeros(exps[k].y.shape, bool))
                px[r.iy, r.ix] = True
            else:
                out.unmodelled[k][r.iy, r.ix] = True
    return out


@dataclass
class SourceModel:
    """A fitted source's model of each of its nod differences, on the detector."""
    sid: int
    frames: dict                    # (nod, det) -> (model in the source's units, calibration, (ystart, xstart))
    result: object = None


def source_model(R, rate_dir):
    frames = {}
    for k, e in enumerate(R.model.exps):
        if not e.use_data or e.partners is None:
            continue
        m = PF.model_frame(R, k, applied_pathloss=False, common=True)
        frames[nod_key(e.name)] = (m, calibration(e, rate_dir), e.origin)
    return frames


def subtract_contaminants(exps, contam, models, rate_dir):
    """Subtract the contaminants' models from ``exps``' nod differences in place.
    Returns, per exposure, a mask of the pixels whose contaminant has no model."""
    keep_masked = []
    for k, e in enumerate(exps):
        missing = contam.unmodelled[k].copy()
        if not contam.sources[k]:
            keep_masked.append(missing)
            continue
        key = nod_key(e.name)
        cal_t = calibration(e, rate_dir)
        y0, x0 = e.origin
        ny, nx = e.y.shape
        total = np.zeros((ny, nx))
        for sid, px in contam.sources[k].items():
            sm = models.get(sid)
            if sm is None or key not in sm.frames:
                missing |= px                      # cannot model: keep this source's pixels masked
                continue
            m, cal_n, (cy0, cx0) = sm.frames[key]
            # overlap of the two common frames on the detector
            ra, rb = max(y0, cy0), min(y0 + ny, cy0 + m.shape[0])
            ca, cb = max(x0, cx0), min(x0 + nx, cx0 + m.shape[1])
            if ra >= rb or ca >= cb:
                continue
            t = (slice(ra - y0, rb - y0), slice(ca - x0, cb - x0))
            c = (slice(ra - cy0, rb - cy0), slice(ca - cx0, cb - cx0))
            with np.errstate(invalid='ignore', divide='ignore'):
                conv = cal_t[t] / cal_n[c]
            add = np.where(np.isfinite(m[c]) & np.isfinite(conv), m[c] * conv, 0.0)
            total[t] += add
        e.diff = e.diff - total
        keep_masked.append(missing)
    return keep_masked


def fit_masked(exps, masks, **kw):
    """Profile fit with extra pixels masked (``masks`` per exposure, common frame)."""
    saved = [e.ok.copy() for e in exps]
    try:
        for e, m in zip(exps, masks):
            e.ok = e.ok & ~m
        return PF.fit(exps, **kw)
    finally:
        for e, ok in zip(exps, saved):
            e.ok = ok
