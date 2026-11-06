"""Diffraction model of the MSA in the slit direction: the profile fit's PSF and shutter for every
disperser (FIT_VERSION 6).

A point source at slit position c: the telescope's field at the MSA is the Fourier transform of
its pupil, whose amplitude across the slit direction is (1 - u^2)^ALPHA (u the pupil coordinate
over its half-width). The open shutters pass that field (open half-height H_OPEN, shutters
PITCH_AS apart); the spectrograph's pupil stop removes spatial frequencies beyond rho times the
telescope pupil's edge, which is where light diffracted at the shutter edges is lost. The
detector intensity is the squared modulus, blurred by a Gaussian of s0 + s1 (lambda - 3 um)
arcsec (detector, charge diffusion, spectrograph aberrations) and integrated over a detector row.

Before the shutter, each point is spread by an empirical kernel: a non-negative table over source
offsets (31 points within +-2 slit units) per detector at the centres of the calibration's
wavelength ranges, linear in ln(lambda) between them (the telescope's wavefront error and pointing
jitter, which uniform light does not feel). The kernel and the diffraction numbers were fitted to
the PRISM calibration stars' nod differences and master-background single frames
(``data/nirspec_optics.json``; the tests are in the design doc, docs/design-nirspec-pipeline-v2.md).
The gratings use the same model: the light before the shutter is the same, and the gratings' bar
shadow and calibration stars fit it as well as their own tables did.

Along the dispersion the data do not show the source's shape, so only the light passed is
modelled: jwst's point-source path loss times a correction fitted to stars (``x_correction``),
times the model's ratio for an extended source over a point there (``x_extended_ratio``: the
same diffraction through one shutter X_HALF wide each side, the source taken to be round).

A Gaussian source of width w (the profile fit's galaxy) is the sum of point images across it:
for w <= W_COMPACT exactly over the tables interpolated linearly in source position
(``_compact_weights``), above it in uniform steps.

Tables per open-shutter pattern (offsets from the source's shutter), per detector: light per
detector row at slit positions Y for a point source at C, at wavelengths LAM, and the light a
point source sends through the pattern. They are built on first use (about a minute each) and
cached under ``$CAMPFIRE_ROOT/cache/optics``, keyed by the model's numbers and the kernel's
content, and read memory-mapped so parallel workers share them.

The openings are symmetric, of half-height H_OPEN. A model file may move every opening's lower and upper edge
('shutter'); the shipped one does not: the empty-shutter background of five survey fields and 1133 puts the two
edges where the symmetric opening has them, to 0.002 slit units (design doc 3.7). The cache key carries
ENGINE_VERSION: bump it whenever the computation of a table changes, or old tables are read back unchanged.

Units: slit-frame units (one unit = the open height, 0.46 arcsec) unless noted.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates
from scipy.special import ndtr

H_OPEN = 0.23                 # open half-height of a shutter [arcsec]
PITCH_AS = 0.53               # shutter pitch along the slit [arcsec]
ROW = 0.105                   # detector row [arcsec]
SLIT = 2 * H_OPEN             # one slit-frame unit [arcsec]
D_TEL = 6.5                   # telescope pupil [m]
RAD = np.pi / 180 / 3600
LAM = np.exp(np.linspace(np.log(0.55), np.log(5.5), 30))       # table wavelengths [um]
C = np.round(np.arange(-5.5, 5.5001, 0.0125), 5)               # source positions
Y = np.round(np.arange(-5.5, 5.5001, 0.025), 5)                # detector positions
DY_ROW = ROW / SLIT
# Along the dispersion, fitted to the NGC 2506 stars' fluxes against SRCXPOS (design doc, check 1): the open
# half-width, and the scatter of a source's true position about its planned one (acquisition, catalogue
# astrometry). Without that scatter a point's transmission falls from 0.9 to 0.3 within 0.05 arcsec of the edge,
# and the ratio for an extended source there swings by factors of two.
X_HALF = 0.110                # open half-width of a shutter along the dispersion [arcsec]
X_BLUR = 0.030                # a source's position scatter along the dispersion [arcsec]
X_PITCH = 0.27                # shutter pitch along the dispersion [arcsec] (the unit of SRCXPOS)
XC = np.round(np.arange(-2.5, 2.5001, 0.005), 4)               # source x [arcsec]
W_COMPACT = 0.06              # widths up to this: exact weights over the tables (``_compact_weights``)
K_MAX = 5                     # shutters beyond this offset do not change the tables' +-5.5 range
ENGINE_VERSION = '2'          # part of the cache key: bump when the table computation changes (2: continuous edges)
OPTICS_FILE = Path(__file__).parent.parent / 'data' / 'nirspec_optics.json'
LOCK_WAIT = 900.0             # seconds to wait for another process building the same table


def default_cache_dir():
    """``$CAMPFIRE_ROOT/cache/optics``."""
    root = os.environ.get('CAMPFIRE_ROOT') or os.path.expanduser('~/campfire-data')
    return os.path.join(root, 'cache', 'optics')


# ---------------------------------------------------------------------------
# One point source through the shutters (arcsec)
# ---------------------------------------------------------------------------
def _grid(n=4096, step=0.0025):
    y = (np.arange(n) - n // 2) * step
    f = np.fft.fftfreq(n, d=step * RAD)                         # cycles per radian
    return y, f


def opening_mask(y, shutters, edges=(0.0, 0.0)):
    """The open area of shutters ``shutters`` (offsets from the source's shutter) on the grid ``y`` [arcsec]: 1 between
    each opening's edges, ramping linearly over the one grid step an edge falls in, so an edge moves the light
    continuously and every opening passes the same light wherever it falls on the grid (an edge on a grid point gives
    the plain 0 / 1 mask). ``edges`` as in ``point_image``."""
    mask = np.zeros_like(y)
    lo, hi = edges
    dy = y[1] - y[0]
    for k in shutters:
        a, b = k * PITCH_AS - H_OPEN + lo, k * PITCH_AS + H_OPEN + hi
        mask = np.maximum(mask, np.clip((y - a) / dy, 0.0, 1.0) * np.clip((b - y) / dy, 0.0, 1.0))
    return mask


def point_image(c, lam_um, shutters, rho, sigma_det, alpha, y, f, edges=(0.0, 0.0)):
    """Detector intensity per arcsec (before row integration) of a point source at ``c`` arcsec
    with unit light entering the telescope, through open shutters ``shutters`` (offsets from the
    source's shutter): (intensity, transmitted fraction). ``edges``: how far every opening's lower
    and upper edge sit from the symmetric opening of half-height H_OPEN [arcsec, + toward +y]."""
    fmax = D_TEL / (2 * lam_um * 1e-6)
    pupil = np.clip(1.0 - (f / fmax) ** 2, 0.0, None) ** alpha if alpha > 0 else (np.abs(f) < fmax).astype(float)
    field = np.fft.fftshift(np.fft.ifft(pupil * np.exp(-2j * np.pi * f * (c * RAD))))
    norm = np.sum(np.abs(field) ** 2)
    spec = np.fft.fft(np.fft.ifftshift(field * opening_mask(y, shutters, edges)))
    spec[np.abs(f) > rho * fmax] = 0.0
    inten = np.abs(np.fft.fftshift(np.fft.ifft(spec))) ** 2 / norm
    if sigma_det > 0:
        ker = np.exp(-0.5 * (y / sigma_det) ** 2)
        ker /= ker.sum()
        inten = np.real(np.fft.fftshift(np.fft.ifft(np.fft.fft(np.fft.ifftshift(inten))
                                                    * np.fft.fft(np.fft.ifftshift(ker)))))
    return inten / (y[1] - y[0]), float(np.sum(inten))


def row_integrate(y, inten):
    """Mean of the intensity over a detector row centred at each y."""
    n = max(int(round(ROW / (y[1] - y[0]))), 1)
    return np.convolve(inten, np.ones(n) / n, mode='same')


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def _cached(path, build):
    """The array saved at ``path``, built by ``build()`` and saved first if missing; memory-
    mapped. One process builds while the others wait for it (a lock file beside it); a lock
    older than LOCK_WAIT is taken to be left by a process that died."""
    if os.path.exists(path):
        return np.load(path, mmap_mode='r')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = path + '.lock'
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        owner = True
    except FileExistsError:
        owner = False
        t0 = time.time()
        while time.time() - t0 < LOCK_WAIT:
            if os.path.exists(path):
                return np.load(path, mmap_mode='r')
            time.sleep(1.0)
    try:
        tab = build()
        tmp = f'{path}.{os.getpid()}.tmp.npy'
        np.save(tmp, tab)
        os.replace(tmp, path)
    finally:
        if owner and os.path.exists(lock):
            os.remove(lock)
    return np.load(path, mmap_mode='r')


class Optics:
    """The model for one disperser and detector: diffraction numbers ``rho`` (stop size over the
    telescope pupil's image), ``s0`` / ``s1`` (detector blur [arcsec] at 3 um and per um),
    ``alpha`` (pupil amplitude profile), and ``kernel`` (list of dict(lam [um], delta [slit
    units], k): the table before the shutter at each wavelength node), ``x_loss``
    (dict(b, lam0), or None: the correction to jwst's along-dispersion point loss,
    ``x_correction``) and ``edges`` (lower, upper: where every opening's two edges sit against the
    symmetric opening [slit units, + toward +y]; ``SHUTTER_EDGES``)."""

    def __init__(self, rho, s0, s1, alpha, kernel, cache_dir=None, x_loss=None, edges=(0.0, 0.0)):
        self.params = dict(rho=float(rho), s0=float(s0), s1=float(s1), alpha=float(alpha))
        if any(edges):
            # in the cache key only when set, so the tables of the symmetric opening keep theirs
            self.params.update(edge_lower=float(edges[0]), edge_upper=float(edges[1]))
        self.kernel = [dict(lam=float(n['lam']), delta=[float(x) for x in n['delta']], k=[float(x) for x in n['k']])
                       for n in kernel]
        self.x_loss = None if x_loss is None else dict(b=float(x_loss['b']), lam0=float(x_loss['lam0']))
        self.cache_dir = cache_dir
        self._img, self._tr, self._tx = {}, {}, None

    # -- along the dispersion -------------------------------------------------
    def x_correction(self, lam, srcx):
        """The factor on jwst's point-source path loss for a source at SRCXPOS ``srcx`` (shutter
        pitches along the dispersion): exp(-b (lambda - lam0)+ srcx^2), one at the centre and
        below lam0. jwst's table loses too little light off centre in the red; fitted to the
        NGC 2506 stars seen at several positions (design doc). One without ``x_loss``."""
        lam = np.asarray(lam, float)
        if self.x_loss is None or not np.isfinite(srcx):
            return np.ones(lam.shape)
        return np.exp(-self.x_loss['b'] * np.maximum(lam - self.x_loss['lam0'], 0.0) * float(srcx) ** 2)

    def _raw_x_transmission(self):
        p = self.params
        y, f = _grid(n=8192, step=0.00125)
        mask = (np.abs(y) < X_HALF).astype(float)
        tab = np.zeros((LAM.size, XC.size))
        for i, L in enumerate(LAM):
            fmax = D_TEL / (2 * L * 1e-6)
            pupil = np.clip(1.0 - (f / fmax) ** 2, 0.0, None) ** p['alpha'] if p['alpha'] > 0 \
                else (np.abs(f) < fmax).astype(float)
            for j, x in enumerate(XC):
                field = np.fft.fftshift(np.fft.ifft(pupil * np.exp(-2j * np.pi * f * (x * RAD))))
                spec = np.fft.fft(np.fft.ifftshift(field * mask))
                spec[np.abs(f) > p['rho'] * fmax] = 0.0
                tab[i, j] = np.sum(np.abs(np.fft.ifft(spec)) ** 2) / np.sum(np.abs(field) ** 2)
        return tab

    def x_transmission_table(self):
        """(LAM, XC) the light of a point source planned at x (arcsec from the shutter centre
        along the dispersion) one shutter passes: the same pupil and stop as in the slit
        direction, the shutter X_HALF wide each side, spread before the shutter by the kernel
        (the telescope's blur, taken to be round) and by the position scatter X_BLUR. Its
        neighbours along the dispersion belong to other slitlets."""
        if self._tx is None:
            kind = f'xtr{X_HALF:g}_{XC[0]:g}_{XC.size}'

            def smear():
                raw = np.asarray(_cached(self._path(kind, (0,), False), self._raw_x_transmission), float)
                offs = np.round(np.arange(-2.0, 2.0001, C[1] - C[0]), 5)
                g = np.exp(-0.5 * (XC / X_BLUR) ** 2)
                out = np.empty(raw.shape)
                for i, L in enumerate(LAM):
                    k = np.interp(XC, offs * SLIT, self.kernel_at(L, offs), left=0.0, right=0.0)
                    k = np.convolve(k, g, mode='same')
                    out[i] = np.convolve(raw[i], k / k.sum(), mode='same')
                return out
            self._tx = _cached(self._path(f'{kind}_b{X_BLUR:g}', (0,), True), smear)
        return self._tx

    def x_extended_ratio(self, lam, srcx, width):
        """The light a Gaussian source of ``width`` (slit units, per ``lam``, taken as its width
        along the dispersion too: a round source) at SRCXPOS ``srcx`` passes along the
        dispersion, over a point source's there (``x_transmission_table``). One for a point."""
        lam = np.asarray(lam, float)
        if lam.size == 0:
            return np.ones(lam.shape)
        tab = self.x_transmission_table()
        x = (float(srcx) if np.isfinite(srcx) else 0.0) * X_PITCH
        o = np.argsort(lam)
        sig = np.interp(np.log(LAM), np.log(lam[o]), np.broadcast_to(np.asarray(width, float), lam.shape)[o]) * SLIT
        r = np.ones(LAM.size)
        for i in range(LAM.size):
            if sig[i] < 2 * (XC[1] - XC[0]):          # narrower than the grid resolves: a point
                continue
            g = np.exp(-0.5 * ((x - XC) / sig[i]) ** 2)
            r[i] = (g @ tab[i] / g.sum()) / max(np.interp(x, XC, tab[i]), 1e-9)
        return np.interp(np.log(lam), np.log(LAM), r)

    # -- the kernel before the shutter -------------------------------------
    def kernel_at(self, lam, offsets):
        """The kernel at ``lam`` over source offsets ``offsets`` (slit units), summing to one:
        each node's table linear between its points and zero beyond, the nodes linear in
        ln(lambda) and held beyond the end ones."""
        lams = np.array([n['lam'] for n in self.kernel])

        def prof(n):
            k = np.interp(offsets, n['delta'], n['k'], left=0.0, right=0.0)
            return k / k.sum() if k.sum() > 0 else k
        if lam <= lams[0]:
            return prof(self.kernel[0])
        if lam >= lams[-1]:
            return prof(self.kernel[-1])
        i = int(np.searchsorted(lams, lam)) - 1
        t = (np.log(lam) - np.log(lams[i])) / (np.log(lams[i + 1]) - np.log(lams[i]))
        k = (1 - t) * prof(self.kernel[i]) + t * prof(self.kernel[i + 1])
        return k / k.sum()

    def _smear(self, tab):
        """Each source position's light spread by the kernel along the C axis: the light of a
        source at C[j] lands at C[j] + delta with weight k(delta)."""
        out = np.empty(tab.shape, np.float32)
        idx = np.arange(C.size)
        sh = idx[None, :] - idx[:, None] + C.size // 2
        ok = (sh >= 0) & (sh < C.size)
        offsets = (idx - C.size // 2) * (C[1] - C[0])
        for i, L in enumerate(LAM):
            k = self.kernel_at(L, offsets)
            W = np.where(ok, k[np.clip(sh, 0, C.size - 1)], 0.0)
            out[i] = W @ np.asarray(tab[i], float)
        return out

    # -- cache ---------------------------------------------------------------
    def _path(self, kind, mask, with_kernel):
        content = dict(engine=ENGINE_VERSION, params=self.params, kind=kind, mask=list(mask),
                       lam=[float(LAM[0]), float(LAM[-1]), int(LAM.size)], c=[float(C[0]), float(C[-1]), int(C.size)],
                       y=[float(Y[0]), float(Y[-1]), int(Y.size)], kernel=self.kernel if with_kernel else None)
        h = hashlib.sha1(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]
        return os.path.join(self.cache_dir or default_cache_dir(), f'{kind}_{h}.npy')

    @staticmethod
    def _mask(mask):
        return tuple(int(k) for k in mask if abs(int(k)) <= K_MAX)

    @property
    def edges_as(self):
        """The openings' lower and upper edges against the symmetric opening [arcsec]."""
        return (self.params.get('edge_lower', 0.0) * SLIT, self.params.get('edge_upper', 0.0) * SLIT)

    def _raw_image(self, mask):
        p = self.params
        y, f = _grid()
        tab = np.zeros((LAM.size, C.size, Y.size), dtype=np.float32)
        for i, L in enumerate(LAM):
            sd = max(p['s0'] + p['s1'] * (L - 3.0), 1e-4)
            for j, c in enumerate(C):
                I, _ = point_image(c * SLIT, L, mask, p['rho'], sd, p['alpha'], y, f, edges=self.edges_as)
                tab[i, j] = np.interp(Y * SLIT, y, row_integrate(y, I) * ROW)     # light per detector row
        return tab

    def _raw_transmission(self, mask):
        p = self.params
        y, f = _grid()
        return np.array([[point_image(c * SLIT, L, mask, p['rho'], max(p['s0'] + p['s1'] * (L - 3.0), 1e-4),
                                      p['alpha'], y, f, edges=self.edges_as)[1] for c in C] for L in LAM])

    def raw_image_table(self, mask):
        """``image_table`` without the kernel: a point source's light through the diffraction
        alone (what the kernel calibration spreads, ``optics_calibration``)."""
        mask = self._mask(mask)
        return _cached(self._path('img', mask, False), lambda: self._raw_image(mask))

    def image_table(self, mask):
        """(LAM, C, Y) light per detector row (DY_ROW slit units high) of a point source."""
        mask = self._mask(mask)
        if mask not in self._img:
            self._img[mask] = _cached(self._path('img', mask, True), lambda: self._smear(self.raw_image_table(mask)))
        return self._img[mask]

    def transmission_table(self, mask):
        """(LAM, C) the light of a point source the open shutters ``mask`` pass."""
        mask = self._mask(mask)
        if mask not in self._tr:
            raw = lambda: _cached(self._path('tr', mask, False), lambda: self._raw_transmission(mask))  # noqa: E731
            self._tr[mask] = _cached(self._path('tr', mask, True), lambda: self._smear(raw()[:, :, None])[:, :, 0])
        return self._tr[mask]

    # -- sources -------------------------------------------------------------
    @staticmethod
    def _li(lam):
        return np.interp(np.log(lam), np.log(LAM), np.arange(LAM.size))

    @staticmethod
    def _ci(c):
        return (np.asarray(c, float) - C[0]) / (C[1] - C[0])

    @staticmethod
    def _wide_nodes(width):
        """(x, weight) over a wide source's Gaussian in units of its width: uniform steps of at
        most 0.025 slit units over +-4 widths (a point image changes sharply as the source
        crosses a shutter edge)."""
        n = int(np.ceil(8.0 * float(np.max(width)) / 0.025)) + 1
        x = np.linspace(-4.0, 4.0, n)
        w = np.exp(-0.5 * x * x)
        return x, w / w.sum()

    @staticmethod
    def _compact_weights(centre, width):
        """(table index along C, weight) pairs for a compact source, exact for a Gaussian source
        over the table interpolated linearly in source position: a triangle of half-width h
        convolved with a Gaussian of width w is (R(u + h) - 2 R(u) + R(u - h)) / h, with
        R(x) = x Phi(x / w) + w phi(x / w). At w -> 0 it is linear interpolation, and it stays
        smooth in w below the table step (a star's width of 0.01-0.03 slit units can move)."""
        h = C[1] - C[0]
        base = np.floor((centre - C[0]) / h).astype(int)
        w = np.maximum(width, 1e-6)
        m = int(np.ceil(4.0 * float(np.max(w)) / h)) + 1

        def R(x):
            z = x / w
            return x * ndtr(z) + w * np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)
        for k in range(-m, m + 2):
            j = base + k
            u = centre - C[0] - j * h
            wt = (R(u + h) - 2.0 * R(u) + R(u - h)) / h
            if np.max(np.abs(wt)) > 1e-7:
                yield j, wt

    def image(self, mask, y, lam, centre, width):
        """Light per DY_ROW-high detector row at slit positions ``y`` (wavelengths ``lam``) of a
        Gaussian source of the given centre and width (arrays broadcast per pixel), unit total
        flux entering the telescope."""
        tab = self.image_table(mask)
        arrs = np.broadcast_arrays(np.asarray(y, float), np.asarray(lam, float), np.asarray(centre, float),
                                   np.asarray(width, float))
        shape = arrs[0].shape
        y, lam, centre, width = (a.ravel() for a in arrs)
        li = self._li(lam)
        yi = (y - Y[0]) / (Y[1] - Y[0])
        out = np.zeros(y.size)
        if width.size == 0:
            return out.reshape(shape)
        if np.max(width) <= W_COMPACT:
            for j, wt in self._compact_weights(centre, width):
                out += wt * map_coordinates(tab, np.vstack([li, j, yi]), order=1, mode='constant', cval=0.0)
        else:
            for x, wt in zip(*self._wide_nodes(width)):
                out += wt * map_coordinates(tab, np.vstack([li, self._ci(centre + width * x), yi]), order=1,
                                            mode='constant', cval=0.0)
        return out.reshape(shape)

    def transmission(self, mask, lam, centre, width):
        """The light of a Gaussian source the open shutters ``mask`` pass."""
        tab = self.transmission_table(mask)
        lam, centre, width = (np.asarray(a, float) for a in np.broadcast_arrays(lam, centre, width))
        shape = lam.shape
        lam, centre, width = lam.ravel(), centre.ravel(), width.ravel()
        out = np.zeros(lam.size)
        if width.size == 0:
            return out.reshape(shape)
        li = self._li(lam)
        if np.max(width) <= W_COMPACT:
            for j, wt in self._compact_weights(centre, width):
                out += wt * map_coordinates(tab, np.vstack([li, j]), order=1, mode='constant', cval=0.0)
        else:
            for x, wt in zip(*self._wide_nodes(width)):
                out += wt * map_coordinates(tab, np.vstack([li, self._ci(centre + width * x)]), order=1,
                                            mode='constant', cval=0.0)
        return out.reshape(shape)


# ---------------------------------------------------------------------------
# The calibrated models
# ---------------------------------------------------------------------------
_MODELS = {}


def load_optics(path=OPTICS_FILE):
    """{'version': str, detector: Optics} from the model file (layout: 'meta' with 'version',
    'diffraction' (rho, s0, s1, alpha), a kernel per detector and, optionally, 'x_loss' and
    'shutter' (lower, upper: the openings' edges against the symmetric opening, slit units))."""
    with open(path) as fh:
        raw = json.load(fh)
    d = raw['diffraction']
    sh = raw.get('shutter') or {}
    edges = (float(sh.get('lower', 0.0)), float(sh.get('upper', 0.0)))
    out = {'version': str(raw.get('meta', {}).get('version', '?'))}
    for det in ('NRS1', 'NRS2'):
        out[det] = Optics(d['rho'], d['s0'], d['s1'], d['alpha'], raw[det], x_loss=raw.get('x_loss'), edges=edges)
    return out


def models():
    """The shipped model, or the file ``CAMPFIRE_NIRSPEC_OPTICS`` names (for testing a candidate calibration across
    worker processes; products record which through PFOPTIC, the file's version)."""
    if 'default' not in _MODELS:
        _MODELS['default'] = load_optics(os.environ.get('CAMPFIRE_NIRSPEC_OPTICS') or OPTICS_FILE)
    return _MODELS['default']


def optics_for(detector):
    """The shipped model of an exposure's detector ('NRS1' / 'NRS2')."""
    return models()[str(detector).upper()]


def version():
    return models()['version']
