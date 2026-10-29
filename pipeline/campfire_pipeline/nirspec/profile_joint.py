"""
Joint solve of strongly coupled overlap groups (prototype, extraction v2).

The one-pass handling of other slitlets' light (``profile_contam``) predicts every
contaminant from its own pass-1 spectrum, solved with the others' pixels masked and
then smoothed. Where two traces share much of their light (5215 and 2976 in
ember_cosmos_p14 G395M; 7420 and its two contaminants; 5301, 6723 and 8427, which
cover each other) that is lossy: the masking removes most of the contaminant's
pixels, its lines fall below the smoothing's line threshold and survive in the
target, and a contaminant solved over the target's pixels holds the target's own
light (the echo, ``profile_contam.echo_corrected``, an approximation).

Here the spectra of such a group are solved together, once, in one linear
least-squares system:

* **members**: sources whose traces put significant light on each other
  (``pair_metrics`` per product, ``select_groups``); every other source stays on the
  one-pass path, and its predicted light is subtracted from the members' data first.
* **shapes**: FIXED, each member's chain shape from the one-pass result (own fit,
  PRISM shape or default, each behind its gate), read from its ``_pf.fits``.
* **data**: every pixel of every member's fit region (open shutters, nod copies
  included), each detector pixel once (``owned_pixels``), in the units and with the
  nod bookkeeping of the member whose pixel it is.
* **model**: at each pixel, the light of every member in that nod difference, from
  its own frames per unit spectrum (+1 for its frame in this nod, -1/n for the
  partners), converted from its calibration to the pixel owner's (as
  ``profile_contam.predict`` moves pass-1 frames).
* **unknowns**: each member's spectrum on its own native grid, full resolution; no
  masking and no smoothing of the members' light. Pixels clipped at 5 sigma, twice.

What is computed once: the one-pass products (pass 1, pass 1b, each product's
shape chain); then the groups; then one joint solve per group; then each member's
product is redone with the other members' light from the joint solve (subtracted,
never masked) and the non-members' from pass 1: its shape chain runs again on that
data (the joint solve itself used the one-pass shape; the change is reported, it is
not iterated), and its CONTAM2D carries the joint light. Nothing feeds back into the
joint solve.

Identifiability: the joint covariance gives, per wavelength of each member, its
error against its error with the others' spectra known (the inflation), and its
most negative correlation with any other member's spectrum point. Where that
correlation is near -1 the split of the shared light is not determined by the data;
those wavelengths are flagged (``joint_flag``), not guessed.
"""

import os
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_contam as CT

JOINT_VERSION = '1'
CHUNK = 512          # right-hand sides per sparse solve when forming the covariance


def joint_settings(sc):
    """The joint-solve knobs of ``[nirspec.stage3]``."""
    g = sc.get
    return dict(enabled=bool(g('contam_joint', False)),
                min_sigma=float(g('contam_joint_min_sigma', 10.0)),
                min_frac=float(g('contam_joint_min_frac', 0.3)),
                max_members=int(g('contam_joint_max_members', 6)),
                smooth=float(g('contam_joint_smooth', 0.0)),
                degenerate=float(g('contam_joint_degenerate', 0.9)),
                clip=float(g('contam_joint_clip', 5.0)),
                refit=bool(g('contam_joint_refit', False)),
                sens=(float(g('contam_joint_sens_centre', 0.02)), float(g('contam_joint_sens_log_width', 0.1))),
                gate=str(g('contam_joint_gate', 'noise')).lower(),
                gate_max=float(g('contam_joint_gate_max', 2.0)))


# ---------------------------------------------------------------------------
# Group selection
# ---------------------------------------------------------------------------
def shape_at(params, eoff, M):
    """Shape vector (centre, drift, ln width, width change) of a fitted shape
    (``FitResult.params`` style, at its own pivot) re-expressed at model M's pivot."""
    dx = np.log2(M.pivot / float(params['pivot']))
    c = float(params['centre']) + float(params['drift']) * dx
    w = max(float(params['width']) + float(params['width_change']) * dx, PF.W_FLOOR)
    p = np.array([c, float(params['drift']), np.log(w), float(params['width_change'])])
    e = np.zeros(len(M.exps))
    if eoff is not None:
        e[:min(len(eoff), e.size)] = np.asarray(eoff, float)[:e.size]
    return p, e


def pair_metrics(exps0, R, P):
    """Each contaminant's predicted light in a product's fixed-shape spectrum.

    exps0 : the product's exposures before any contamination handling (all pixels).
    R : the product's chosen fit (its shape); P : its ``Prediction`` made with
        ``keep_sources=True`` (every contaminant's light per exposure, unreliable ones
        included, echo-corrected).
    The contaminant's light is solved like data with the target's shape on all the
    target's pixels: c_i per spectrum point, against the target's errors s_i.
    Returns {sid: dict(sig, frac, reliable, pix)}: ``sig`` = |sum c_i / s_i^2| /
    sqrt(sum 1 / s_i^2), the light in units of the noise of the integrated
    (inverse-variance) flux, as ``PFCONSIG``; ``frac`` = sum c_i / s_i^2 over
    sum f_i / s_i^2, the contaminant's light over the target's own (inf when the
    target's is not positive); ``pix`` the largest light / noise on one pixel of the
    target's main shutter."""
    if not P.by_source:
        return {}
    M = PF.ProfileModel(exps0, chromatic=R.model.chromatic, priors=R.model.priors, bounds=R.model.bounds_cfg,
                        grid=R.lam, c_prior=R.model.c_prior)
    p, eoff = shape_at(R.params, R.eoff, M)
    f0, cov, good, A = M.solve(p, eoff, covariance=True)
    w = 1.0 / M.pix['v']
    s2 = np.diag(cov)[good]
    ok = np.isfinite(s2) & (s2 > 0)
    iv = np.where(ok, 1.0 / np.where(ok, s2, 1.0), 0.0)
    ft = np.interp(M.lam_grid, R.lam, np.nan_to_num(R.flux))[good]
    den = float(np.sum(ft * iv))
    c_plan = M.c_prior
    out = {}
    for csid, per in P.by_source.items():
        L = []
        pmax = 0.0
        for k, m in M.pix_masks:
            a = per[k] if (k < len(per) and per[k] is not None) else np.zeros(m.shape)
            L.append(a[m])
            if per[k] is not None:
                sig = CT.diff_sigma(exps0, k)
                ms = CT.main_shutter(exps0[k], c_plan) & exps0[k].ok
                with np.errstate(invalid='ignore', divide='ignore'):
                    v = np.where(ms, np.abs(per[k]) / sig, np.nan)
                if np.isfinite(v).any():
                    pmax = max(pmax, float(np.nanmax(v)))
        L = np.concatenate(L)
        x = cov[np.ix_(good, good)] @ (A.T @ (w * L))[good]
        num = float(np.sum(np.where(ok, x, 0.0) * iv))
        sig = abs(num) / np.sqrt(np.sum(iv)) if np.sum(iv) > 0 else 0.0
        frac = num / den if den > 0 else np.inf
        out[int(csid)] = dict(sig=float(sig), frac=float(frac), reliable=int(csid) not in P.unreliable,
                              pix=float(pmax))
    return out


def select_groups(pairs, products, min_sigma=10.0, min_frac=0.3, max_members=6):
    """Overlap groups from per-product pair metrics.

    pairs : {target sid: {contaminant sid: pair_metrics entry}}; products : the source
        ids that can be members (every source of the disperser, ``stage3._joint_phase``).
    A pair is strongly coupled when the contaminant's light in the target's spectrum
    is at least ``min_sigma`` times the noise of the target's integrated flux AND at
    least ``min_frac`` of the target's own light in size (negative light, from a
    contaminant's copies in the partner nods, counts as much: 2426 on 7420). Groups are the connected sets of
    strongly coupled pairs, joined strongest first; a pair that would make a group
    larger than ``max_members`` is left out (that contaminant stays on the one-pass
    path for that target). Returns a list of sorted sid lists (two members or more)."""
    edges = []
    for t, d in pairs.items():
        for c, m in (d or {}).items():
            if int(t) == int(c) or int(c) not in products or int(t) not in products:
                continue
            if m['sig'] >= min_sigma and abs(m['frac']) >= min_frac:
                edges.append((m['sig'], int(t), int(c)))
    edges.sort(key=lambda x: (-x[0], x[1], x[2]))
    parent, size = {}, {}

    def find(a):
        parent.setdefault(a, a)
        size.setdefault(a, 1)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for _, t, c in edges:
        a, b = find(t), find(c)
        if a == b or size[a] + size[b] > max_members:
            continue
        if size[a] < size[b]:
            a, b = b, a
        parent[b] = a
        size[a] += size[b]
    comp = {}
    for x in list(parent):
        comp.setdefault(find(x), []).append(x)
    return sorted((sorted(v) for v in comp.values() if len(v) > 1), key=lambda g: g[0])


def receivers(pairs, group, min_sigma=10.0, min_frac=0.3):
    """The members of a group that receive strongly coupled light from another member
    (``select_groups``' pair rule, target side). Only their products are redone from
    the joint solve: a member that only contaminates the others (2976 for 5215, 5301
    for 8427) is solved with them, for their sake, but keeps its one-pass product,
    which the joint solve would only make noisier (5301: errors x1.9)."""
    g = {int(x) for x in group}
    out = set()
    for t in g:
        for c, m in (pairs.get(t) or {}).items():
            if int(c) in g and int(c) != t and m['sig'] >= min_sigma and abs(m['frac']) >= min_frac:
                out.add(t)
    return out


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
def shape_from_header(h):
    """(params dict at its pivot, per-exposure offsets, shape source) from a header with
    the one-pass shape cards (``profile_product.shape_header``), or None. A header given
    as text (``Header.tostring``) is parsed first."""
    from astropy.io import fits
    if h is None:
        return None
    if isinstance(h, str):
        h = fits.Header.fromstring(h)
    if 'PFCEN' not in h:
        return None
    params = dict(centre=float(h['PFCEN']), drift=float(h['PFDRIFT']), width=float(h['PFWIDTH']),
                  width_change=float(h['PFDWIDTH']), pivot=float(h['PFPIVOT']), centre_prior=float(h['PFCENPR']))
    n = int(h.get('PFNEXP', 0))
    eoff = np.array([float(h.get(f'PFOFF{k}', 0.0)) for k in range(n)])
    return params, eoff, str(h.get('PFSHAPE', ''))


def read_shape(workspace, product):
    """The one-pass chain shape of a product from its ``_pf.fits`` (this run) or
    ``_spec.fits``: (params dict at its pivot, per-exposure offsets, shape source)."""
    from astropy.io import fits
    for s in ('_pf.fits', '_spec.fits'):
        path = os.path.join(workspace, f'{product}{s}')
        if not os.path.exists(path):
            continue
        out = shape_from_header(fits.getheader(path))
        if out is not None:
            return out
    return None


@dataclass
class Member:
    sid: int
    product: str
    exps: list
    model: object                 # ProfileModel of the member's data (non-members' light subtracted / masked)
    p: np.ndarray                 # fixed shape vector at the model's pivot
    eoff: np.ndarray
    shape_src: str = ''
    params: dict = None           # the one-pass shape as read (its own pivot)
    basis: dict = field(default_factory=dict, repr=False)   # exposure key -> frame per unit spectrum
    nosrc: set = field(default_factory=set)
    cal: dict = field(default_factory=dict, repr=False)     # exposure index -> calibration (lazy)
    workspace: str = ''
    owned: np.ndarray = None      # per row of the model's pixel table: this member's pixel in the joint data
    col0: int = 0                 # first unknown of the member's spectrum

    def calibration(self, j):
        if j not in self.cal:
            self.cal[j] = CT.calibration(self.exps[j], self.workspace)
        return self.cal[j]


def fixed_result(M, p, eoff, flux=None, flux_err=None):
    """A ``FitResult`` of model M at a fixed shape (for frame images)."""
    params = dict(centre=float(p[0]), drift=float(p[1]), width=float(np.exp(p[2])), width_change=float(p[3]),
                  pivot=M.pivot, centre_prior=M.c_prior)
    n = M.lam_grid.size
    R = PF.FitResult(params=params, eoff=np.asarray(eoff, float), lam=M.lam_grid,
                     flux=np.ones(n) if flux is None else flux,
                     flux_err=np.zeros(n) if flux_err is None else flux_err,
                     chi2=0.0, npix=0, nfree=0, chromatic=True, model=M)
    R.bound_bits = 0
    R.use = M.use.copy()
    return R


def basis_frames(member):
    """Each of the member's exposures that holds its source: its frame per unit
    spectrum (path loss removed; zero off the spectrum grid), the pixels' wavelengths
    and calibration, on the member's common frame."""
    M = member.model
    R = fixed_result(M, member.p, member.eoff)
    out, nosrc = {}, set()
    ones = np.ones(M.lam_grid.size)
    for j, e in enumerate(member.exps):
        key = CT.exposure_key(e.name)
        if not e.has_source:
            nosrc.add(key)
            continue
        prof = PF.frame_image(R, j, spectrum=ones)
        if not (np.isfinite(prof) & (prof != 0)).any():
            nosrc.add(key)
            continue
        out[key] = dict(origin=np.array(e.origin), prof=prof, plam=e.wav, cal=member.calibration(j))
    member.basis, member.nosrc = out, nosrc
    return out


_READ = object()


def setup_member(product, workspace, groups, overrides, science, sid, stage_config, members, pass1_for,
                 maps=None, exps=None, log=None, shape=_READ):
    """Load one member, subtract (or mask) the predicted light of every non-member as
    the one-pass final step does, and fix its shape to its one-pass chain shape.

    pass1_for : sid -> one-pass ``Pass1`` (the non-members' predictions, the
        member's own pass 1 for the echo correction of unreliable non-members).
    shape : the one-pass shape cards (text, ``profile_product.one_pass_metrics``) of a
        member whose product this run does not write; None when it has none. By
        default read from the member's ``_pf.fits`` / ``_spec.fits``."""
    from campfire_pipeline.nirspec import profile_chain as PC
    from campfire_pipeline.nirspec.profile_product import stage_flags

    log = log or (lambda *a: None)
    fl = stage_flags(stage_config)
    cfg = PC.ChainConfig.from_stage_config(stage_config)
    if exps is None:
        exps = PF.load_source(groups, bkg_overrides=overrides, science=set(science))
    maps = CT.maps_for_exposures(workspace, exps) if maps is None else maps
    P = CT.predict(exps, sid, maps, pass1_for, workspace, own_pass1=pass1_for(sid), pair_ratio=fl['pair_ratio'],
                   echo=fl['echo'], exclude=set(members) - {int(sid)})
    CT.apply(exps, P, mode=fl['contam_mode'], kappa=fl['kappa'], dominance_kappa=fl['dominance_kappa'])
    shape = read_shape(workspace, product) if shape is _READ else shape_from_header(shape)
    if shape is None:
        raise ValueError(f'{product}: no one-pass shape (_pf.fits / _spec.fits) for the joint solve')
    params, eoff, src = shape
    M = PF.ProfileModel(exps, chromatic=True, priors=cfg.priors, bounds=cfg.bounds)
    p, e = shape_at(params, eoff, M)
    m = Member(sid=int(sid), product=product, exps=exps, model=M, p=p, eoff=e, shape_src=src, params=params,
               workspace=workspace)
    m.prediction = P
    own = pass1_for(sid)
    m.ext_frames = ext_frames(own)
    m.ext_reliable = bool(getattr(own, 'reliable', False))
    basis_frames(m)
    return m


def ext_frames(own):
    """A member's light past its cutout (pass 1, ``profile_extend``): not among the joint
    unknowns, it is added to the member's joint frames as pass 1 has it. From an
    unreliable pass 1 it only masks (``JointSolution.pass1``: ``ext_mask_only``): that
    pass 1 holds other sources' light, and so does its extension (fitted on the same
    nod differences), and the one-pass path masks all of it too (on 7420, 2426's
    extension put -1.6 sigma per pixel of light that the data do not show; on 8198,
    1736's put -3.5 sigma)."""
    from campfire_pipeline.nirspec import profile_extend as PE
    out = {}
    if own is None:
        return out
    for key, fr in own.frames.items():
        x = PE.ext_part(fr)
        if x is not None:
            out[key] = x
    return out


# ---------------------------------------------------------------------------
# The joint system
# ---------------------------------------------------------------------------
def _pixel_keys(member, tags):
    """Per row of a member's pixel table: (exposure index, row, column on its common
    frame) and an integer key of the physical datum (nod difference = exposure and its
    partners, detector pixel). ``tags`` numbers the nod differences across members."""
    M = member.model
    ks, iys, ixs, keys = [], [], [], []
    for k, m in M.pix_masks:
        e = M.exps[k]
        iy, ix = np.nonzero(m)
        tag = CT.exposure_key(e.name) + '|' + ','.join(sorted(CT.exposure_key(M.exps[j].name) for j in e.partners))
        tid = tags.setdefault(tag, len(tags))
        det = (iy + int(e.origin[0])).astype(np.int64) * 100000 + (ix + int(e.origin[1]))
        ks.append(np.full(iy.size, k))
        iys.append(iy)
        ixs.append(ix)
        keys.append(np.int64(tid) * 10 ** 10 + det)
    return np.concatenate(ks), np.concatenate(iys), np.concatenate(ixs), np.concatenate(keys)


def owned_pixels(members):
    """Each detector datum enters the joint data once: a pixel in several members' fit
    regions (the same nod difference, i.e. the same partners) belongs to the first
    member in the list (sorted by source id). Its value and noise are that member's,
    in its calibration. Sets ``member.owned`` and returns the number of joint data."""
    claimed = np.zeros(0, np.int64)
    tags = {}
    n = 0
    for m in members:
        k, iy, ix, keys = _pixel_keys(m, tags)
        own = ~np.isin(keys, claimed)
        claimed = np.union1d(claimed, keys[own])
        m.pix_k, m.pix_iy, m.pix_ix = k, iy, ix
        m.owned = own
        n += int(own.sum())
    return n


def design(members):
    """Sparse design A (joint data x all members' spectrum points), data d, variance v.

    Row order: members in order, each member's owned pixels in its pixel-table order.
    The light of member c on member s's pixel in nod difference k: for each term j of
    s's difference (+1 for k, -1/n for each of s's partners), c's frame j per unit
    spectrum at that detector pixel, times c's spectrum at c's wavelength there
    (linear between grid points), converted from c's calibration of frame j to s's
    (1 for c = s)."""
    col = 0
    for c in members:
        c.col0 = col
        col += c.model.lam_grid.size
    rows, cols, vals, d, v = [], [], [], [], []
    row0 = 0
    for s in members:
        M = s.model
        own = s.owned
        loc = np.cumsum(own) - 1               # row of each owned pixel within this member's block
        d.append(M.pix['d'][own])
        v.append(M.pix['v'][own])
        for k in np.unique(s.pix_k[own]):
            sel = own & (s.pix_k == k)
            iy, ix, r = s.pix_iy[sel], s.pix_ix[sel], row0 + loc[sel]
            e = M.exps[k]
            terms = [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]
            for c in members:
                g = c.model.lam_grid
                for j, w in terms:
                    b = c.basis.get(CT.exposure_key(M.exps[j].name))
                    if b is None:
                        continue
                    cy = iy + int(e.origin[0]) - int(b['origin'][0])
                    cx = ix + int(e.origin[1]) - int(b['origin'][1])
                    ny, nx = b['prof'].shape
                    ins = (cy >= 0) & (cy < ny) & (cx >= 0) & (cx < nx)
                    if not ins.any():
                        continue
                    cy_, cx_, rr = cy[ins], cx[ins], r[ins]
                    prof = b['prof'][cy_, cx_]
                    lam = b['plam'][cy_, cx_]
                    if c is s:
                        conv = np.ones(prof.size)
                    else:
                        with np.errstate(divide='ignore', invalid='ignore'):
                            conv = s.calibration(j)[iy[ins], ix[ins]] / b['cal'][cy_, cx_]
                    ok = np.isfinite(prof) & (prof != 0) & np.isfinite(lam) & (lam >= g[0]) & (lam <= g[-1]) \
                        & np.isfinite(conv)
                    if not ok.any():
                        continue
                    val = w * conv[ok] * prof[ok]
                    lam = lam[ok]
                    i = np.clip(np.searchsorted(g, lam) - 1, 0, g.size - 2)
                    t = np.clip((lam - g[i]) / (g[i + 1] - g[i]), 0.0, 1.0)
                    rows += [rr[ok], rr[ok]]
                    cols += [c.col0 + i, c.col0 + i + 1]
                    vals += [val * (1 - t), val * t]
        row0 += int(own.sum())
    A = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(row0, col)) \
        if vals else sp.csr_matrix((row0, col))
    return A, np.concatenate(d), np.concatenate(v)


def _second_difference(n):
    return sp.diags([np.ones(n - 2), -2 * np.ones(n - 2), np.ones(n - 2)], [0, 1, 2], shape=(n - 2, n))


@dataclass
class JointSolution:
    members: list
    lam: dict                     # sid -> spectrum grid
    flux: dict                    # sid -> joint spectrum (NaN unconstrained)
    err: dict                     # sid -> its error from the joint covariance
    err_iso: dict                 # sid -> its error with the other members' spectra known
    corr: dict                    # sid -> most negative correlation with any other member's point
    corr_sid: dict                # sid -> which member
    npix: int = 0
    nclip: int = 0
    chi2: float = np.nan
    smooth: float = 0.0
    seconds: float = 0.0
    sens: dict = field(default_factory=dict)   # sid -> change of its spectrum for the others' shape errors

    def inflation(self, sid):
        with np.errstate(invalid='ignore', divide='ignore'):
            return self.err[sid] / self.err_iso[sid]

    def pass1(self, sid):
        """A member's joint spectrum as a ``Pass1`` (``joint = True``) for
        ``profile_contam.predict``: its frames per unit spectrum times the spectrum,
        sparse, with their calibration."""
        m = next(x for x in self.members if x.sid == int(sid))
        lam, f, ef = self.lam[m.sid], np.nan_to_num(self.flux[m.sid]), np.nan_to_num(self.err[m.sid])
        frames, nosrc = {}, set(m.nosrc)
        for key, b in m.basis.items():
            prof, plam = b['prof'], b['plam']
            with np.errstate(invalid='ignore'):
                img = prof * np.interp(np.nan_to_num(plam), lam, f, left=0.0, right=0.0)
                err = np.abs(prof) * np.interp(np.nan_to_num(plam), lam, ef, left=0.0, right=0.0)
            good = np.isfinite(img) & (np.abs(img) > 0) & np.isfinite(plam)
            if not good.any():
                nosrc.add(key)
                continue
            idx = np.flatnonzero(good)
            frames[key] = dict(origin=b['origin'], shape=np.array(prof.shape), idx=idx.astype(np.int64),
                               img=img.ravel()[idx], err=err.ravel()[idx], cal=b['cal'].ravel()[idx],
                               prof=prof.ravel()[idx], plam=plam.ravel()[idx])
        from campfire_pipeline.nirspec import profile_extend as PE
        for key, x in (getattr(m, 'ext_frames', None) or {}).items():       # past its cutout: pass 1's
            frames[key] = PE.merge_sparse(frames[key], x) if key in frames else dict(x)
            nosrc.discard(key)
        P1 = CT.Pass1(sid=m.sid, lam=lam, flux=self.flux[m.sid], flux_err=self.err[m.sid], frames=frames,
                      reliable=True, nosrc=nosrc)
        P1.joint, P1.why, P1.shape_src, P1.n_smooth = True, 'joint', m.shape_src, 0
        P1.ext_mask_only = not getattr(m, 'ext_reliable', True)      # past the cutout: masks only
        return P1


def _inverse_pieces(lu, n, blocks, want_corr=True):
    """diag(N^-1) and, per unknown, its most negative correlation with an unknown of
    another block (and which block), from chunked solves of the factorised N."""
    diag = np.zeros(n)
    for a in range(0, n, CHUNK):
        b = min(a + CHUNK, n)
        E = np.zeros((n, b - a))
        E[np.arange(a, b), np.arange(b - a)] = 1.0
        X = lu.solve(E)
        diag[a:b] = X[np.arange(a, b), np.arange(b - a)]
    cmin = np.zeros(n)
    cblk = np.full(n, -1)
    if want_corr and len(set(blocks.tolist())) > 1:
        sd = np.sqrt(np.abs(diag))
        for a in range(0, n, CHUNK):
            b = min(a + CHUNK, n)
            E = np.zeros((n, b - a))
            E[np.arange(a, b), np.arange(b - a)] = 1.0
            X = lu.solve(E)
            C = X / (sd[:, None] * sd[None, a:b])
            same = blocks[:, None] == blocks[None, a:b]
            C = np.where(same, np.inf, C)
            i = np.argmin(C, axis=0)
            cmin[a:b] = C[i, np.arange(b - a)]
            cblk[a:b] = blocks[i]
    return diag, cmin, cblk


def _shape_sensitivity(members, sens, use, dz, vz, gi, blocks, f, smooth, target):
    """Per unknown: how far its solution moves when another member's fixed shape is off
    by a typical error, the centre by ``sens[0]`` slit units or the width by a factor
    exp(``sens[1]``) (one member and one parameter at a time; the largest change is
    kept). The covariance only describes the noise; where a faint member sits inside a
    bright one's profile, a percent-level error of the bright one's shape moves the
    faint one's spectrum by more than its noise (5215 inside 2976), and only this says so.
    Same pixels and weights as the solve; no clipping."""
    out = np.zeros(f.size)
    w = np.where(use, 1.0 / vz, 0.0)
    for ci, c in enumerate(members):
        p0, b0 = c.p.copy(), c.basis
        for dp in (np.array([sens[0], 0, 0, 0]), np.array([0, 0, sens[1], 0])):
            try:
                c.p = p0 + dp
                basis_frames(c)
                A1, _, _ = design(members)
            finally:
                c.p, c.basis = p0, b0
            AtW = A1.T.multiply(w).tocsr()
            N1 = (AtW @ A1).tocsc()
            b1 = AtW @ dz
            if smooth > 0:
                for i, m in enumerate(members):
                    if target is not None and m.sid == int(target):
                        continue
                    sel = np.flatnonzero(blocks == i)
                    if sel.size < 3:
                        continue
                    dg = N1.diagonal()[sel]
                    med = np.median(dg[dg > 0]) if (dg > 0).any() else 0.0
                    Q = (smooth * med * (_second_difference(sel.size).T @ _second_difference(sel.size))).tocoo()
                    N1 = N1 + sp.csc_matrix((Q.data, (Q.row + sel[0], Q.col + sel[0])), shape=N1.shape)
            Ng = N1[gi][:, gi] + sp.identity(gi.size, format='csc') * 1e-9 * np.median(N1.diagonal()[gi])
            f1 = np.zeros(f.size)
            f1[gi] = splu(Ng.tocsc()).solve(b1[gi])
            other = blocks != ci
            out[other] = np.maximum(out[other], np.abs(f1 - f)[other])
    res = np.full(f.size, np.nan)
    res[gi] = out[gi]
    return res


def solve(members, clip=5.0, n_clip=2, smooth=0.0, target=None, sens=(0.02, 0.1)):
    """The joint solve of a group (``owned_pixels`` and ``basis_frames`` done).

    smooth : weak second-difference prior on the spectra of every member except
        ``target`` (the target stays unregularised): weight ``smooth`` times the
        median data weight of that member's spectrum points. 0 (default): none, one
        solve serves every member.
    sens : (centre slit units, ln width) of a typical shape error, for the shape
        sensitivity (``_shape_sensitivity``); None: not computed.
    Pixels deviating by more than ``clip`` sigma are dropped and the solve repeated,
    ``n_clip`` times (outlier rejection only; the model is linear and solved once)."""
    import time
    from campfire_pipeline.nirspec import profile_timing as TM
    t0 = time.time()
    with TM.step('joint_design'):
        A, d, v = design(members)
    t_mark = TM.start()
    n = A.shape[1]
    blocks = np.concatenate([np.full(m.model.lam_grid.size, i) for i, m in enumerate(members)])
    use = np.isfinite(d) & np.isfinite(v) & (v > 0)
    dz = np.where(use, d, 0.0)
    vz = np.where(use, v, 1.0)
    for it in range(n_clip + 1):
        w = np.where(use, 1.0 / vz, 0.0)
        AtW = A.T.multiply(w).tocsr()
        N = (AtW @ A).tocsc()
        bvec = AtW @ dz
        diag = N.diagonal()
        good = np.zeros(n, bool)
        for i in range(len(members)):
            sel = blocks == i
            dpos = diag[sel][diag[sel] > 0]
            if dpos.size:
                good[sel] = diag[sel] > 1e-3 * np.median(dpos)
        if smooth > 0:
            for i, m in enumerate(members):
                if target is not None and m.sid == int(target):
                    continue
                sel = np.flatnonzero(blocks == i)          # contiguous
                if sel.size < 3:
                    continue
                D = _second_difference(sel.size)
                med = np.median(diag[sel][diag[sel] > 0]) if (diag[sel] > 0).any() else 0.0
                Q = (smooth * med * (D.T @ D)).tocoo()
                N = N + sp.csc_matrix((Q.data, (Q.row + sel[0], Q.col + sel[0])), shape=(n, n))
        gi = np.flatnonzero(good)
        Ng = N[gi][:, gi] + sp.identity(gi.size, format='csc') * 1e-9 * np.median(diag[good])
        lu = splu(Ng.tocsc())
        f = np.zeros(n)
        f[gi] = lu.solve(bvec[gi])
        r = (dz - A @ f) / np.sqrt(vz)
        if it == n_clip:
            break
        new = use & (np.abs(r) < clip)
        if np.array_equal(new, use):
            break
        use = new
    chi2 = float(np.sum(r[use] ** 2))
    TM.stop('joint_lsq', t_mark)
    t_mark = TM.start()
    dg, cmin, cblk = _inverse_pieces(lu, gi.size, blocks[gi])
    err_all = np.full(n, np.nan)
    err_all[gi] = np.sqrt(np.abs(dg))
    corr_all = np.full(n, np.nan)
    corr_all[gi] = cmin
    cb_all = np.full(n, -1)
    cb_all[gi] = cblk
    # isolated: each member's own block of the same normal matrix, the others known
    iso_all = np.full(n, np.nan)
    for i in range(len(members)):
        sel = gi[blocks[gi] == i]
        if sel.size == 0:
            continue
        Nb = N[sel][:, sel] + sp.identity(sel.size, format='csc') * 1e-9 * np.median(diag[good])
        db, _, _ = _inverse_pieces(splu(Nb.tocsc()), sel.size, np.zeros(sel.size, int), want_corr=False)
        iso_all[sel] = np.sqrt(np.abs(db))
    TM.stop('joint_covariance', t_mark)
    flux_all = np.where(good, f, np.nan)
    sens_all = np.full(n, np.nan)
    if sens is not None and len(members) > 1:
        with TM.step('joint_sensitivity'):
            sens_all = _shape_sensitivity(members, sens, use, dz, vz, gi, blocks, f, smooth, target)
    sol = JointSolution(members=members, lam={}, flux={}, err={}, err_iso={}, corr={}, corr_sid={},
                        npix=int(use.sum()), nclip=int((~use & np.isfinite(d)).sum()), chi2=chi2, smooth=smooth)
    for i, m in enumerate(members):
        sel = slice(m.col0, m.col0 + m.model.lam_grid.size)
        sol.lam[m.sid] = m.model.lam_grid
        sol.flux[m.sid] = flux_all[sel]
        sol.err[m.sid] = err_all[sel]
        sol.err_iso[m.sid] = iso_all[sel]
        sol.corr[m.sid] = corr_all[sel]
        cb = cb_all[sel]
        sol.corr_sid[m.sid] = np.where(cb >= 0, np.array([members[x].sid for x in np.clip(cb, 0, None)]), -1)
        sol.sens[m.sid] = sens_all[sel]
    sol.seconds = time.time() - t0
    sol.A, sol.d, sol.v, sol.use, sol.f = A, d, v, use, f
    return sol


def solve_group(members, clip=5.0, smooth=0.0, sens=(0.02, 0.1)):
    """``owned_pixels`` + ``solve``; with ``smooth`` one solve per member (each member
    unregularised in its own). Returns {sid: JointSolution} (the same object for all
    members without smoothing)."""
    owned_pixels(members)
    if smooth <= 0:
        s = solve(members, clip=clip, sens=sens)
        return {m.sid: s for m in members}
    return {m.sid: solve(members, clip=clip, smooth=smooth, target=m.sid, sens=sens) for m in members}


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@dataclass
class JointView:
    """What one member's product needs from its group's joint solve."""
    sid: int
    group: list                   # member sids
    solution: object              # JointSolution
    pass1_of: dict                # other member sid -> joint ``Pass1``
    params_in: dict = None        # the one-pass shape the joint solve used
    eoff_in: np.ndarray = None    # its per-exposure pointing offsets
    shape_src: str = ''           # own | prism | default (one-pass chain)
    refit: bool = False           # rerun the shape chain on the jointly subtracted data (else keep the solve's shape)
    degenerate: float = 0.9
    nwin: int = 31
    use: bool = True              # the gate's decision: deliver the joint result (else the one-pass one)
    gate: str = 'off'             # the gate quantity (``GATE_QUANTITIES``) or 'off'
    gate_max: float = np.nan
    gate_value: float = np.nan
    gate_why: str = ''

    def quantities(self):
        """The candidate gate quantities of this member's joint result (``GATE_QUANTITIES``)."""
        s = self.solution
        corr, err, flux = s.corr[self.sid], s.err[self.sid], s.flux[self.sid]
        infl = s.inflation(self.sid)
        fin = np.isfinite(corr)
        sens, sensf = _integrated(s.sens.get(self.sid), err, flux)
        return dict(sensf=abs(sensf) if np.isfinite(sensf) else np.nan,
                    sens=abs(sens) if np.isfinite(sens) else np.nan,
                    fshape=float(np.mean((self.flags() & FLAG_SHAPE) > 0)) if flux.size else np.nan,
                    cdeg=float(np.mean(corr[fin] < -self.degenerate)) if fin.any() else np.nan,
                    infl=float(np.nanmedian(infl)) if np.isfinite(infl).any() else np.nan)

    def decide(self, gate='off', gate_max=np.nan):
        """The per-product gate: deliver the joint result when its gate quantity is at
        most ``gate_max``; otherwise the one-pass result (``use`` False). An
        undefined quantity (no shape sensitivity computed, no flux) fails the gate."""
        self.gate, self.gate_max = str(gate).lower(), float(gate_max)
        if self.gate == 'off':
            self.use, self.gate_value, self.gate_why = True, np.nan, 'gate_off'
            return self.use
        if self.gate == 'noise':
            # decided at the extraction (stage3._profile_fit_columns), where both results'
            # 1-D noise is known: the joint result is written as the primary one meanwhile
            self.use, self.gate_value, self.gate_why = True, np.nan, 'noise_at_extraction'
            return self.use
        if self.gate not in GATE_QUANTITIES:
            raise ValueError(f"contam_joint_gate must be off, noise or one of {sorted(GATE_QUANTITIES)}, "
                             f"got {gate!r}")
        v = self.quantities()[self.gate]
        self.gate_value = float(v)
        if not np.isfinite(v):
            self.use, self.gate_why = False, f'{self.gate}_undefined'
        elif v <= self.gate_max:
            self.use, self.gate_why = True, f'{self.gate}<={self.gate_max:g}'
        else:
            self.use, self.gate_why = False, f'{self.gate}>{self.gate_max:g}'
        return self.use

    def flags(self):
        s = self.solution
        return joint_flags(s.corr[self.sid], s.err[self.sid], s.sens.get(self.sid), self.degenerate, self.nwin)

    def table(self):
        """JOINT table: the member's spectrum grid, joint flux and error, isolated
        error, most negative correlation and with which member, the shape sensitivity
        and the flags (``joint_flags``)."""
        from astropy.io import fits
        s = self.solution
        cols = [fits.Column(name='lam', format='D', unit='um', array=s.lam[self.sid]),
                fits.Column(name='flux', format='D', array=s.flux[self.sid]),
                fits.Column(name='err', format='D', array=s.err[self.sid]),
                fits.Column(name='err_iso', format='D', array=s.err_iso[self.sid]),
                fits.Column(name='corr', format='D', array=s.corr[self.sid]),
                fits.Column(name='corr_sid', format='K', array=s.corr_sid[self.sid]),
                fits.Column(name='sens', format='D',
                            array=s.sens.get(self.sid, np.full(s.lam[self.sid].size, np.nan))),
                fits.Column(name='flag', format='I', array=self.flags())]
        h = fits.BinTableHDU.from_columns(cols, name='JOINT')
        h.header['COMMENT'] = 'Joint solve of the overlap group: true spectrum (MJy) on the native grid'
        return h

    def cards(self, R=None):
        s = self.solution
        corr = s.corr[self.sid]
        infl = s.inflation(self.sid)
        fin = np.isfinite(corr)
        c = {
            'PFJOINT': (len(self.group), 'Members of the joint-solve group (0: none)'),
            'PFJGRP': (','.join(str(x) for x in self.group)[:68], 'Joint-solve group (source ids)'),
            'PFJVER': (JOINT_VERSION, 'Joint-solve version'),
            'PFJCMIN': (_f(np.nanmin(corr)) if fin.any() else -999.0, 'Most negative correlation with a member'),
            'PFJCDEG': (_f(np.mean(corr[fin] < -self.degenerate)) if fin.any() else -999.0,
                        f'Fraction of points with correlation < -{self.degenerate:g}'),
            'PFJDEGT': (float(self.degenerate), 'Correlation below minus this: split not determined'),
            'PFJFSHP': (_f(np.mean((self.flags() & FLAG_SHAPE) > 0)), 'Fraction of points flagged shape-limited'),
            'PFJNWIN': (int(self.nwin), 'Points per noise bin of the shape flag'),
            'PFJINFL': (_f(np.nanmedian(infl)), 'Median error inflation over isolated'),
            'PFJINF90': (_f(np.nanpercentile(infl, 90)) if np.isfinite(infl).any() else -999.0,
                         '90th pct of the error inflation'),
            'PFJSMTH': (_f(s.smooth), 'Smoothness prior on the other members (0: none)'),
            'PFJSENS': (_f(_integrated(s.sens.get(self.sid), s.err[self.sid])[0]),
                        'Shift for the others shape errors / noise, integrated'),
            'PFJSENSF': (_f(_integrated(s.sens.get(self.sid), s.err[self.sid], s.flux[self.sid])[1]),
                         'Same shift / the integrated flux'),
            'PFJNPIX': (int(s.npix), 'Pixels in the joint solve'),
            'PFJSEC': (_f(s.seconds), 'Joint solve time [s]'),
            'PFJUSE': (bool(self.use), 'fnu_prof is the joint result (F: one-pass)'),
            'PFJGATE': (self.gate, 'Gate quantity on the joint result (off: none)'),
            'PFJGVAL': (_f(self.gate_value), 'Its value for this product'),
            'PFJGMAX': (_f(self.gate_max), 'Joint result delivered when the value <= this'),
            'PFJWHY': (self.gate_why[:68], 'Why the joint result is (not) delivered'),
        }
        if R is not None and self.params_in is not None:
            p = R.params
            q = self.params_in
            dx = np.log2(p['pivot'] / q['pivot'])
            c_in = q['centre'] + q['drift'] * dx
            w_in = max(q['width'] + q['width_change'] * dx, PF.W_FLOOR)
            c['PFJDCEN'] = (_f(p['centre'] - c_in), 'Refit centre minus the joint-solve centre')
            c['PFJDLW'] = (_f(np.log(p['width'] / w_in)), 'Refit ln width minus the joint-solve one')
        return c


def _f(x):
    x = float(x)
    return x if np.isfinite(x) else -999.0


def _integrated(v, err, flux=None):
    """An inverse-variance integral of ``v`` over the spectrum, in units of the noise of
    the integrated flux, and (with ``flux``) relative to the integrated flux."""
    if v is None:
        return np.nan, np.nan
    ok = np.isfinite(v) & np.isfinite(err) & (err > 0)
    if flux is not None:
        ok &= np.isfinite(flux)
    if not ok.any():
        return np.nan, np.nan
    iv = 1.0 / err[ok] ** 2
    a = float(np.sum(v[ok] * iv))
    rel = a / abs(float(np.sum(flux[ok] * iv))) if flux is not None and np.sum(flux[ok] * iv) != 0 else np.nan
    return a / np.sqrt(np.sum(iv)), rel


def views(members, sols, degenerate=0.9, nwin=31, refit=False):
    """One ``JointView`` per member."""
    group = [m.sid for m in members]
    out = {}
    for m in members:
        s = sols[m.sid]
        out[m.sid] = JointView(sid=m.sid, group=group, solution=s,
                               pass1_of={x.sid: s.pass1(x.sid) for x in members if x.sid != m.sid},
                               params_in=m.params, degenerate=degenerate, nwin=nwin,
                               eoff_in=np.asarray(m.eoff, float), shape_src=m.shape_src, refit=refit)
    return out


FLAG_DEGENERATE = 1     # the split of the shared light is not determined by the data (correlation)
FLAG_SHAPE = 2          # the split depends on the others' shapes more than the noise allows

# per-product gate quantities. 'noise' (the default, ``contam_joint_gate``): the integrated
# noise of the receiver's joint result over its one-pass one, decided at the extraction
# (``stage3._joint_noise_ratio``) where both are known. The others (``JointView.quantities``),
# decided here, were the candidates checked against PRISM (round 6):
#   sensf  how far the product's integrated flux moves for a typical shape error of the
#          other members, over that flux (PFJSENSF)
#   sens   the same move in units of the integrated noise (PFJSENS)
#   fshape fraction of spectrum points flagged shape-limited (PFJFSHP)
#   cdeg   fraction of points whose split is not determined, correlation < -degenerate (PFJCDEG)
#   infl   median error inflation over the isolated error (PFJINFL)
GATE_QUANTITIES = ('sensf', 'sens', 'fshape', 'cdeg', 'infl')


def joint_flags(corr, err, sens, degenerate=0.9, nwin=31):
    """Per spectrum point: ``FLAG_DEGENERATE`` where the most negative correlation with a
    member is below -``degenerate``; ``FLAG_SHAPE`` where the change for a typical shape
    error of the others (``sens``, a smooth bias) exceeds the noise of a mean over
    ``nwin`` points (one pass-1 smoothing length: a bias that size is visible in a
    resolution-element-binned spectrum)."""
    f = np.zeros(np.size(corr), np.int16)
    with np.errstate(invalid='ignore'):
        f |= np.where(np.isfinite(corr) & (corr < -degenerate), FLAG_DEGENERATE, 0).astype(np.int16)
        if sens is not None:
            f |= np.where(np.isfinite(sens) & np.isfinite(err) & (sens > err / np.sqrt(max(nwin, 1))),
                          FLAG_SHAPE, 0).astype(np.int16)
    return f


def run_group(tasks, workspace, stage_config, pass1_products, redo=None, log=None, shapes=None):
    """Joint solve of one group and its members' products.

    tasks : the members' ``run_product`` argument tuples (product_name, workspace_dir,
        groups, overrides, science, source_id, stage_config, is_prism, prism_product,
        pass1_products, cleanup_crfs).
    redo : the members whose products are redone (``receivers``; default all).
    shapes : sid -> one-pass shape cards (text, or None) of the members whose product
        this run does not write; the others' are read from their ``_pf.fits``.
    Returns the redone members' ``run_product`` summaries (None for a member whose
    product failed). A failed joint solve leaves the one-pass products as they are."""
    from campfire_pipeline.common.io import log as _log
    from campfire_pipeline.nirspec.profile_product import run_product
    from campfire_pipeline.nirspec import profile_timing as TM

    log = log or _log
    js = joint_settings(stage_config)
    names = pass1_products or {}

    def pass1_for(sid):
        name = names.get(int(sid))
        path = CT.pass1_path(workspace, name) if name else None
        return CT.Pass1.read(path) if path and os.path.exists(path) else None
    sids = [int(t[5]) for t in tasks]
    tag = ','.join(str(s) for s in sids)
    TM.reset()
    try:
        with TM.step('joint_setup'):
            shapes = shapes or {}
            members = [setup_member(t[0], workspace, t[2], t[3], t[4], int(t[5]), stage_config, sids, pass1_for,
                                    **({'shape': shapes[int(t[5])]} if int(t[5]) in shapes else {}))
                       for t in tasks]
        with TM.step('joint_solve'):
            sols = solve_group(members, clip=js['clip'], smooth=js['smooth'], sens=js['sens'])
        s0 = sols[members[0].sid]
        log(f'joint solve {tag}: {s0.npix} pixels, {s0.nclip} clipped, {s0.seconds:.1f} s')
        st = CT.pass1_settings(stage_config)
        nwin = st['smooth_prism'] if tasks[0][7] else st['smooth_grating']
        V = views(members, sols, degenerate=js['degenerate'], nwin=nwin, refit=js['refit'])
        for sid, v in V.items():
            if redo is None or sid in redo:
                v.decide(js['gate'], js['gate_max'])
                log(f"joint solve {tag}: {sid} gets its {'joint' if v.use else 'one-pass'} result "
                    f"({v.gate_why}, {v.gate} = {v.gate_value:.3g})")
    except Exception as e:                       # noqa: BLE001 - the one-pass products stand
        import traceback
        log(f'joint solve {tag} failed ({type(e).__name__}: {e}); one-pass products kept')
        log(traceback.format_exc()[-1500:])
        TM.reset()
        return []
    TM.report(f'joint_group {tag}', log=log)
    out = []
    for t in tasks:
        if redo is None or int(t[5]) in redo:
            out.append(run_product(*t, joint=V[int(t[5])]))
    return out


ALT_EXTENSIONS = ('CONTAM2D', 'CONTERR2D')


def deliver(path, hdr, hdus, view, log=None):
    """The receiver's ``_pf.fits`` content after the gate (``JointView.decide``).

    hdr, hdus : the product as redone from the joint solve (PFJ* cards, JOINT table).
    path : the product's one-pass ``_pf.fits``, written before the joint phase.
    Joint result delivered: the joint product, plus the one-pass other light as
    ``CONTAM2D_1P`` / ``CONTERR2D_1P``. Otherwise: the one-pass product
    as it was, plus the PFJ* cards (PFJUSE false and why), the JOINT table and the
    joint other light as ``*_J``. Both go in the ``_pf.fits``; stage 3 extracts the
    delivered one (``fnu_prof`` / ``fnu_contam``) after its noise gate. A missing
    one-pass product: the joint one is delivered (PFJWHY says so).
    Returns (header, hdus)."""
    from astropy.io import fits
    log = log or (lambda *a: None)
    if not os.path.exists(path):
        if not view.use:
            log(f'{os.path.basename(path)}: no one-pass product to deliver; the joint result is kept')
            view.use, view.gate_why = True, 'no_onepass_product'
            hdr['PFJUSE'] = (True, hdr.comments['PFJUSE'])
            hdr['PFJWHY'] = (view.gate_why, hdr.comments['PFJWHY'])
        return hdr, hdus
    with fits.open(path) as h:
        one_hdr = h[0].header.copy()
        one = [(x.name, np.array(x.data), x.header.copy()) for x in h[1:] if x.data is not None]
    by_name = {x.name: x for x in hdus}
    if view.use:
        out = list(hdus)
        for n, data, xh in one:
            if n in ALT_EXTENSIONS:
                e = fits.ImageHDU(data, header=xh, name=n + '_1P')
                out.append(e)
        return hdr, out
    for k in hdr:
        if k.startswith('PFJ'):
            one_hdr[k] = (hdr[k], hdr.comments[k])
    out = [fits.ImageHDU(data, header=xh, name=n) for n, data, xh in one if not n.endswith(('_1P', '_J'))
           and n != 'JOINT']
    for n in ALT_EXTENSIONS:
        if n in by_name:
            out.append(fits.ImageHDU(np.asarray(by_name[n].data), header=by_name[n].header.copy(), name=n + '_J'))
    if 'JOINT' in by_name:
        out.append(by_name['JOINT'])
    return one_hdr, out
