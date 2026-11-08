"""The boundary between CAMPFIRE's NIRSpec MSA calibrations and jwst's reference products.

Two CRDS references describe how much of a source's light gets through the MSA: the path loss
(``pathloss``: point and uniform sources per aperture, as a function of the source's position in its
shutter and wavelength) and the bar shadow (``barshadow``: the profile of uniform light across a
slitlet). jwst applies both in stage 2. CAMPFIRE replaces parts of them with its own calibrations,
each listed with its inputs and the references it depends on in ``data/nirspec_calibrations.toml``
(``manifest``):

* the slit-direction path loss: the profile fit's diffraction model of the MSA (``optics``) gives
  the light of a source of any width and position along the slit, and the observation's bar offset
  (``bar_offset``) places its shutters. jwst's point path loss keeps the overall level and its
  dependence along the dispersion, times a correction fitted to stars (``Optics.x_correction``).
  Switches: ``[nirspec.stage3].slit_loss_y`` = "optics" | "jwst" and ``slit_loss_x`` =
  "corrected" | "jwst" (``SlitLoss``), recorded as SLOSSY / SLOSSX (the path-loss reference follows from CRDS_CTX).

* the bar shadow: not applied to science slits (stage 2 skips the step); the master background and
  stage 2a's background slitlets take the empirical tables (``barshadow_calibration``) for every
  grating that has one, the CRDS reference for the others (the H gratings).

This module is the only place that reads those two references or interprets what jwst wrote from
them into a cal file, and it checks the assumptions the calibrations make about them
(``pathloss_cube``: the MOS1x3 aperture, its grid and units). When CRDS delivers a new path-loss or
bar-shadow reference, ``check_references`` says which calibrations were derived against another
file, and ``compare`` measures how the new file differs from the model and the empirical tables.
Every other CRDS read (stage 1 and stage 2 references, the extended-wavelength flats) is a
calibration CAMPFIRE uses as delivered and stays where it is.

Units: slit-frame units (one unit = the open height of a shutter) unless noted; jwst's SRCYPOS and the
path-loss grid are in shutter pitches (``slit_position``).
"""
import os
import tomllib
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

DATA_DIR = Path(__file__).parent.parent / 'data'
MANIFEST_FILE = DATA_DIR / 'nirspec_calibrations.toml'
MOS_APERTURE = 'MOS1x3'          # jwst's MOS path-loss aperture: three shutters about the source's shutter


# ---------------------------------------------------------------------------
# jwst's conventions
# ---------------------------------------------------------------------------
def slit_position(srcypos):
    """A position along the slit in slit-frame units from jwst's SRCYPOS (shutter pitches)."""
    return srcypos * MSA_SLIT_PITCH


# ---------------------------------------------------------------------------
# What jwst applied to a cal file
# ---------------------------------------------------------------------------
def applied_pathloss(model, slit):
    """The path-loss correction jwst applied to ``slit`` of ``model`` (point or uniform, as its
    ``do_correction_mos`` chose), or ones when none was applied."""
    kind = str(getattr(slit, 'pathloss_correction_type', '') or '').upper()
    applied = 'COMPLETE' in str(model.meta.cal_step.pathloss).upper()
    if applied and kind == 'UNIFORM':
        return np.asarray(slit.pathloss_uniform, float)
    if applied:
        return np.asarray(slit.pathloss_point, float)
    return np.ones(slit.data.shape)


def point_pathloss(slit):
    """jwst's point-source path loss on ``slit``'s pixels, ones when it wrote none."""
    p = getattr(slit, 'pathloss_point', None)
    return np.asarray(p, float) if p is not None and np.size(p) else np.ones(slit.data.shape)


def pathloss_ref(model):
    """The path-loss reference jwst used for ``model``, as its cal file names it ('' when none)."""
    try:
        return str(model.meta.ref_file.pathloss.name or '')
    except AttributeError:
        return ''


def pathloss_arrays(hdul, ver=1):
    """(kind, point, uniform) of slit ``ver`` of an open cal file: the PTHLOSS card ('POINT' when
    absent) and the PATHLOSS_PS / PATHLOSS_UN arrays (None when absent)."""
    def arr(name):
        try:
            return np.array(hdul[name, ver].data, float)
        except KeyError:
            return None
    kind = str(hdul['SCI', ver].header.get('PTHLOSS', 'POINT')).upper()
    return kind, arr('PATHLOSS_PS'), arr('PATHLOSS_UN')


def point_to_uniform(kind, point, uniform):
    """The factor that turns a point-calibrated slit into a uniform-calibrated one (point /
    uniform where jwst applied the point correction), or None when there is nothing to change."""
    if kind == 'POINT' and point is not None and uniform is not None:
        return point / uniform
    return None


# ---------------------------------------------------------------------------
# The path-loss reference
# ---------------------------------------------------------------------------
def _locate(name):
    import crds
    return crds.locate_file(os.path.basename(str(name).replace('crds://', '')), 'jwst')


@dataclass(frozen=True)
class PathlossCube:
    """jwst's point-source path loss of the MOS1x3 aperture: ``cube`` (wavelength, y, x), ``lam``
    [um], ``y`` and ``x`` [shutter pitches from the source shutter's centre]."""
    name: str
    lam: np.ndarray
    y: np.ndarray
    x: np.ndarray
    cube: np.ndarray

    def at_x_centre(self):
        """(wavelength, y) at the column nearest x = 0."""
        return self.cube[:, :, int(np.argmin(np.abs(self.x)))]


@lru_cache(maxsize=8)
def pathloss_cube(ref_name):
    """The MOS1x3 point path loss of reference ``ref_name`` (as a cal file names it), with the
    assumptions the profile fit makes about it checked: a PS extension with APERTURE = MOS1x3, three
    axes, y over three shutters (-1.5 to +1.5 pitches) and x over one (-0.5 to +0.5), wavelengths in
    metres. Raises ``ValueError`` naming the assumption that fails."""
    from astropy.io import fits
    path = _locate(ref_name)
    name = os.path.basename(str(ref_name))
    with fits.open(path) as h:
        hdus = [x for x in h if x.name == 'PS' and str(x.header.get('APERTURE', '')).upper() == MOS_APERTURE.upper()]
        if len(hdus) != 1:
            raise ValueError(f'{name}: expected one PS extension with APERTURE = {MOS_APERTURE}, found {len(hdus)}')
        hd, data = hdus[0].header, np.asarray(hdus[0].data, float)
    if data.ndim != 3:
        raise ValueError(f'{name}: the {MOS_APERTURE} point path loss has {data.ndim} axes, expected 3')

    def axis(n):                       # as jwst's pathloss step reads it (CRPIX is 1 in every reference so far)
        return hd[f'CRVAL{n}'] + hd[f'CDELT{n}'] * (np.arange(hd[f'NAXIS{n}']) + 1 - hd.get(f'CRPIX{n}', 1.0))
    lam, y, x = axis(3) * 1e6, axis(2), axis(1)
    if not (0.3 < lam.min() and lam.max() < 10.0):           # metres in the file; a unit mix-up is off by 1e6
        raise ValueError(f'{name}: wavelengths {lam.min():.3g}-{lam.max():.3g} um after metres -> um')
    if abs(y[0] + 1.5) > 0.05 or abs(y[-1] - 1.5) > 0.05:
        raise ValueError(f'{name}: y runs {y[0]:+.3f} to {y[-1]:+.3f}, expected three shutter pitches about 0')
    if abs(x[0] + 0.5) > 0.05 or abs(x[-1] - 0.5) > 0.05:
        raise ValueError(f'{name}: x runs {x[0]:+.3f} to {x[-1]:+.3f}, expected one shutter pitch about 0')
    return PathlossCube(name=name, lam=lam, y=y, x=x, cube=data)


def point_y_ratio(ref_name, lam, srcy):
    """jwst's MOS1x3 point path loss at the shutter centre over its value at ``srcy`` (SRCYPOS,
    shutter pitches), at the central x: divides jwst's slit-direction dependence out of the applied
    path loss, so the model's own takes its place (the level at the centre and the dependence along
    the dispersion stay jwst's). Exact when the reference is a product of an x part and a y part
    (``separability``)."""
    pc = pathloss_cube(ref_name)
    prof = pc.at_x_centre()
    at0 = np.array([np.interp(0.0, pc.y, p) for p in prof])
    aty = np.array([np.interp(srcy, pc.y, p) for p in prof])
    return np.interp(np.asarray(lam, float), pc.lam, at0 / aty)


def separability(ref_name, y_max=0.4, x_max=0.45):
    """Per wavelength of the reference, the largest |ln P(x, y) + ln P(0, 0) - ln P(0, y) -
    ln P(x, 0)| over |y| <= y_max and |x| <= x_max (pitches): zero when the point path loss is a
    product of an x part and a y part, which ``point_y_ratio`` assumes. Returns (lam, value)."""
    pc = pathloss_cube(ref_name)
    iy, ix = int(np.argmin(np.abs(pc.y))), int(np.argmin(np.abs(pc.x)))
    my, mx = np.abs(pc.y) <= y_max, np.abs(pc.x) <= x_max
    out = []
    for P in np.log(np.clip(pc.cube, 1e-12, None)):
        d = P + P[iy, ix] - P[:, [ix]] - P[[iy], :]
        out.append(float(np.abs(d[np.ix_(my, mx)]).max()))
    return pc.lam, np.array(out)


# ---------------------------------------------------------------------------
# The bar-shadow reference
# ---------------------------------------------------------------------------
def barshadow_path(canonical_path):
    """The CRDS bar-shadow reference the pipeline would use for this exposure."""
    import crds
    from astropy.io import fits
    hdr = fits.getheader(canonical_path)
    params = {'INSTRUME': 'NIRSPEC', 'DETECTOR': hdr['DETECTOR'], 'EXP_TYPE': hdr.get('EXP_TYPE', 'NRS_MSASPEC'),
              'FILTER': hdr['FILTER'], 'GRATING': hdr['GRATING'],
              'DATE-OBS': hdr.get('DATE-OBS', '2023-01-01'), 'TIME-OBS': hdr.get('TIME-OBS', '00:00:00')}
    return crds.getreferences(params, reftypes=['barshadow'], observatory='jwst')['barshadow']


# ---------------------------------------------------------------------------
# The switches
# ---------------------------------------------------------------------------
SLIT_LOSS_Y = ('optics', 'jwst')
SLIT_LOSS_X = ('corrected', 'jwst')


@dataclass(frozen=True)
class SlitLoss:
    """Whose path loss the profile fit takes, per direction.

    y : "optics" (the model's slit-direction transmission for the source's width and fitted
        position; jwst's slit-direction dependence divided out, ``point_y_ratio``) or "jwst" (jwst's
        point path loss at the planned position; the model gives the image's shape only).
    x : "corrected" (jwst's dependence along the dispersion times ``Optics.x_correction``) or "jwst"
        (jwst's as delivered). The model's extended-source ratio along the dispersion
        (``Optics.x_extended_ratio``) applies either way: jwst's correction is for a point."""
    y: str = 'optics'
    x: str = 'corrected'

    def __post_init__(self):
        if self.y not in SLIT_LOSS_Y:
            raise ValueError(f'slit_loss_y = {self.y!r}: one of {SLIT_LOSS_Y}')
        if self.x not in SLIT_LOSS_X:
            raise ValueError(f'slit_loss_x = {self.x!r}: one of {SLIT_LOSS_X}')

    @classmethod
    def from_config(cls, stage_config):
        g = (stage_config or {}).get
        return cls(y=str(g('slit_loss_y', 'optics')).lower(), x=str(g('slit_loss_x', 'corrected')).lower())

    @property
    def is_default(self):
        return self == SlitLoss()


# ---------------------------------------------------------------------------
# The manifest of CAMPFIRE's calibrations
# ---------------------------------------------------------------------------
def manifest(path=MANIFEST_FILE):
    """``data/nirspec_calibrations.toml``: one table per shipped calibration (its file, version,
    sha256, what it replaces, what it was derived from, the CRDS references it depends on, the
    switch that turns it off, its code and tests)."""
    with open(path, 'rb') as fh:
        return tomllib.load(fh)


def depends_on(reftype, path=MANIFEST_FILE):
    """{calibration name: reference file} of every calibration in the manifest derived against a
    reference of ``reftype`` (e.g. 'pathloss')."""
    out = {}
    for name, entry in manifest(path).items():
        ref = (entry.get('depends_on') or {}).get(reftype)
        if ref:
            out[name] = ref
    return out


_WARNED = set()


def check_references(pathloss=None, barshadow=None, grating=None, path=MANIFEST_FILE, warn=True):
    """The calibrations derived against another reference than the ones given (names as a cal file
    or CRDS gives them; the bar shadow per ``grating``): a list of messages, each also warned once
    per process with ``warn``. The model still runs (its slit-direction loss does not use the
    reference's), but the named parts were fitted against the old file and should be re-derived
    (the path loss can also be switched off, ``SlitLoss``)."""
    out = []
    for reftype, name in (('pathloss', pathloss), ('barshadow', barshadow)):
        if not name:
            continue
        have = os.path.basename(str(name).replace('crds://', ''))
        for cal, ref in depends_on(reftype, path).items():
            if reftype == 'barshadow' and grating and not cal.endswith(str(grating).lower()):
                continue
            if os.path.basename(ref) != have:
                out.append(f'{cal} was derived against {ref}; the reference now is {have}')
    if warn:
        for msg in out:
            if msg not in _WARNED:
                _WARNED.add(msg)
                warnings.warn(f'nirspec calibrations: {msg} (data/nirspec_calibrations.toml; '
                              'crds_refs.compare measures the difference)')
    return out


# ---------------------------------------------------------------------------
# The standing comparison
# ---------------------------------------------------------------------------
def compare(pathloss='jwst_nirspec_pathloss_0010.fits', lams=(1.0, 3.0, 5.0), detector='NRS1'):
    """How the path-loss reference ``pathloss`` relates to CAMPFIRE's model: a dict of plain
    numbers (also the basis of the ``crds`` tests and of ``cfpipe nirspec crds-compare``).

    * ``assumptions``: the checks of ``pathloss_cube`` (raises if one fails) and the largest
      departure from an x part times a y part (``separability``; ``point_y_ratio`` is exact at 0).
    * ``y_profile``: at each wavelength of ``lams``, jwst's point path loss along y (central x,
      relative to its centre) against the model's transmission of a point source through the
      MOS1x3 aperture, relative to its centre, at y = 0.1 ... 0.4 pitches: the slit-direction
      difference the profile fit makes for a compact source.
    * ``x_correction``: the model's correction to jwst's dependence along the dispersion at SRCXPOS
      -0.3 and +0.3, and jwst's own dependence there (central y, relative to x = 0).
    """
    from campfire_pipeline.nirspec import optics as O
    pc = pathloss_cube(pathloss)
    lam_s, sep = separability(pathloss)
    out = dict(pathloss=pc.name, separability_max=float(sep.max()), y_profile={}, x_correction={})
    o = O.optics_for(detector)
    prof = pc.at_x_centre()
    iy = int(np.argmin(np.abs(pc.y)))
    ix = int(np.argmin(np.abs(pc.x)))
    for lam in lams:
        i = int(np.argmin(np.abs(pc.lam - lam)))
        rows = {}
        for yp in (0.1, 0.2, 0.3, 0.4):
            jw = float(np.interp(yp, pc.y, prof[i]) / prof[i, iy])
            t = o.transmission((-1, 0, 1), np.array([lam, lam]), np.array([slit_position(yp), 0.0]), np.zeros(2))
            rows[yp] = dict(jwst=jw, model=float(t[0] / t[1]))
        out['y_profile'][float(lam)] = rows
        xr = {}
        for xp in (-0.3, 0.3):
            jw = float(np.interp(xp, pc.x, pc.cube[i, iy]) / pc.cube[i, iy, ix])
            xr[xp] = dict(jwst=jw, correction=float(o.x_correction(np.array([lam]), xp)[0]))
        out['x_correction'][float(lam)] = xr
    return out


MOS_GRATINGS = (('PRISM', 'CLEAR'), ('G140M', 'F100LP'), ('G235M', 'F170LP'), ('G395M', 'F290LP'))


def active_references(grating='PRISM', filter_name='CLEAR', date='2026-01-01'):
    """{'pathloss': name, 'barshadow': name} the active CRDS context selects for an MOS exposure of
    this disperser on NRS1 (names only, nothing downloaded)."""
    import crds
    params = {'INSTRUME': 'NIRSPEC', 'DETECTOR': 'NRS1', 'EXP_TYPE': 'NRS_MSASPEC', 'GRATING': grating,
              'FILTER': filter_name, 'DATE-OBS': date, 'TIME-OBS': '00:00:00'}
    recs = crds.getrecommendations(params, reftypes=['pathloss', 'barshadow'], observatory='jwst', fast=True)
    return {k: os.path.basename(str(v)) for k, v in recs.items()}


def barshadow_edges(table_path, lams=(1.0, 3.0, 5.0)):
    """Where a bar-shadow table (CRDS layout: a CRDS reference or an empirical one) puts the edges of a
    1x3 slitlet: per wavelength, the half-light points (the profile at half its shutter-centre
    level) in shutter pitches from the source shutter's centre, lowest first."""
    from campfire_pipeline.nirspec import barshadow_calibration as B
    tab = B.load_table(table_path)
    ys = np.linspace(-2.2, 2.2, 8801)
    k = np.rint(ys)
    seg, yseg = B.segment_map((1 + k).astype(int), ys - k, '1x1')
    out = {}
    for lam in lams:
        t = np.full(ys.size, np.nan)
        ok = seg >= 0
        t[ok] = B.evaluate_table(tab, seg[ok], yseg[ok], np.full(ok.sum(), lam))
        t = t / np.nanmedian(t[np.abs(ys) < 0.15])
        s = np.sign(t - 0.5)
        i = np.flatnonzero(np.isfinite(s[:-1]) & np.isfinite(s[1:]) & (s[:-1] != s[1:]))
        out[float(lam)] = ys[i] + (0.5 - t[i]) * (ys[i + 1] - ys[i]) / (t[i + 1] - t[i])
    return out


def compare_barshadow(grating, crds_name=None):
    """The empirical table of ``grating`` against the CRDS bar shadow (``crds_name``, default the one
    the active context selects): each one's half-light edge points and their differences (empirical
    minus CRDS, pitches; positive = moved up), plus the mean of the differences (a shift of the
    whole slitlet) and half the change of the outer edges' separation (a width change). None when
    there is no empirical table for the grating."""
    from campfire_pipeline.config import get_empirical_barshadow_path
    from campfire_pipeline.nirspec.constants import GRATING_LIMITS
    emp = get_empirical_barshadow_path(grating)
    if emp is None:
        return None
    filt = dict(MOS_GRATINGS).get(grating.upper(), 'CLEAR')
    name = crds_name or active_references(grating.upper(), filt)['barshadow']
    lo, hi = GRATING_LIMITS[grating.lower()][:2]
    lams = tuple(np.round(np.geomspace(lo, hi, 5)[1:4], 2))            # inside the disperser's own range
    a, b = barshadow_edges(emp, lams), barshadow_edges(_locate(name), lams)
    out = dict(grating=grating.upper(), empirical=os.path.basename(emp), crds=name, edges={})
    for lam in a:
        if a[lam].size != b[lam].size:
            out['edges'][lam] = dict(empirical=a[lam].tolist(), crds=b[lam].tolist(), shift=np.nan, width=np.nan)
            continue
        d = a[lam] - b[lam]
        out['edges'][lam] = dict(empirical=a[lam].tolist(), crds=b[lam].tolist(), shift=float(d.mean()),
                                 width=float(0.5 * (d[-1] - d[0])))
    return out


def compare_text(result):
    """``compare`` as lines of text."""
    r = result
    lines = [f"path-loss reference {r['pathloss']}: largest departure from x part x y part "
             f"{100 * r['separability_max']:.1f} % (|y| <= 0.4, |x| <= 0.45 pitches)"]
    for lam, rows in r['y_profile'].items():
        lines.append(f'  {lam:.1f} um, point source along y (relative to the centre): '
                     + '; '.join(f"y {yp:.1f}: jwst {v['jwst']:.3f} model {v['model']:.3f}" for yp, v in rows.items()))
    for lam, rows in r['x_correction'].items():
        lines.append(f'  {lam:.1f} um, along the dispersion: '
                     + '; '.join(f"x {xp:+.1f}: jwst {v['jwst']:.3f} x correction {v['correction']:.3f}"
                                 for xp, v in rows.items()))
    return lines


def compare_barshadow_text(result):
    """``compare_barshadow`` as lines of text."""
    r = result
    lines = [f"bar shadow {r['grating']}: {r['empirical']} against {r['crds']} (half-light edges of a 1x3 slitlet, "
             'pitches; empirical minus CRDS)']
    for lam, v in r['edges'].items():
        lines.append(f"  {lam:.1f} um: shift {v['shift']:+.4f}, width change per side {v['width']:+.4f}; edges "
                     + ' '.join(f'{a:+.3f}/{b:+.3f}' for a, b in zip(v['empirical'], v['crds'])))
    return lines


def report(pathloss=None):
    """The standing comparison as text: the references the active context selects for each MOS
    disperser, the calibrations derived against other files (``check_references``), the path-loss
    reference against the model (``compare``) and each empirical bar-shadow table against its CRDS
    reference (``compare_barshadow``). ``cfpipe nirspec crds-compare`` prints it; run it when
    ``CRDS_CONTEXT`` changes and put the result in the changelog entry."""
    import crds
    try:
        context = crds.get_context_name('jwst')
    except Exception:                          # noqa: BLE001 - no server: the configured name
        context = os.environ.get('CRDS_CONTEXT', '<default>')
    lines = [f'CRDS context {context}; CAMPFIRE calibrations: {MANIFEST_FILE.name}']
    active = {g: active_references(g, f) for g, f in MOS_GRATINGS}
    for g, refs in active.items():
        msgs = check_references(pathloss=refs.get('pathloss'), barshadow=refs.get('barshadow'), grating=g, warn=False)
        lines.append(f"  {g}: pathloss {refs.get('pathloss')}, barshadow {refs.get('barshadow')}: "
                     + ('; '.join(msgs) if msgs else 'the calibrations were derived against these'))
    lines += compare_text(compare(pathloss or active['PRISM']['pathloss']))
    for g, _ in MOS_GRATINGS:
        r = compare_barshadow(g, active[g].get('barshadow'))
        if r is not None:
            lines += compare_barshadow_text(r)
    return lines
