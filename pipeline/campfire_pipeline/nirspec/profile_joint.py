"""
The linear system of a joint solve of several sources' spectra (extraction v2): the
machinery ``joint_first_pass`` builds each target's neighbourhood solve from.

* **members** (``Member``): each source with its exposures, its profile model and
  its FIXED shape;
* **basis** (``basis_frames``): each member's frame per unit spectrum in every
  exposure that holds it;
* **data** (``owned_pixels``): every pixel of every member's fit region (open
  shutters, nod copies included), each detector pixel once, in the units and with
  the nod bookkeeping of the member whose pixel it is;
* **model** (``design``): at each pixel, the light of every member in that nod
  difference, from its own frames per unit spectrum (+1 for its frame in this nod,
  -1/n for the partners), converted from its calibration to the pixel owner's (as
  ``profile_contam.predict`` moves pass-1 frames). The unknowns are each member's
  spectrum on its own native grid, full resolution.
"""

from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_contam as CT


@dataclass
class Member:
    sid: int
    product: str
    exps: list
    model: object                 # ProfileModel of the member's data, as they are (nothing subtracted or masked)
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
        tid = tags.setdefault(CT.nod_tag(M.exps, k), len(tags))
        det = (iy + int(e.origin[0])).astype(np.int64) * 100000 + (ix + int(e.origin[1]))
        ks.append(np.full(iy.size, k))
        iys.append(iy)
        ixs.append(ix)
        keys.append(np.int64(tid) * 10 ** 10 + det)
    return np.concatenate(ks), np.concatenate(iys), np.concatenate(ixs), np.concatenate(keys)


def owned_pixels(members):
    """Each detector datum enters the joint data once: a pixel in several members' fit
    regions (the same nod difference, i.e. the same partners) belongs to the first
    member in the list (the target, then by coupling strength, ``joint_first_pass.neighbourhood``).
    Its value and noise are that member's, in its calibration. Sets ``member.owned`` and
    returns the number of joint data."""
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
            terms = PF.nod_terms(M.exps, k)
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
