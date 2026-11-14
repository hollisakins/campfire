"""
Automatic diffraction-spike masking for NIRCam (M3/M4 of
``docs/design-nircam-spike-masking.md``).

**Beta.** Developed and checked on one field (MACS0647, two roll angles, mostly
F200W). The arm width, the threshold and the flux prediction have not been
tested on many fields or filters.

What is computed, per filter, over the whole exposure set:

1. **Stars.** Gaia DR3 stars around the exposures, positions moved to the
   observation epoch. Each star's in-band flux is *predicted* from its Gaia
   G magnitude and BP-RP color (:func:`band_ab_mag`); nothing is fitted to the
   exposure, so stars off the detector are handled the same way as stars on it.
2. **Predicted spike surface brightness.** The published mask-grade spike model
   (fraction of the star's total flux per raw pixel) is scaled by the predicted
   flux and placed on each exposure at the star's sky position, rotated by the
   telescope roll (the pattern is fixed to the telescope V3 axis). Beyond the
   model raster (120") each arm continues as the model's own power law.
3. **Raw spike mask.** Pixels where the predicted surface brightness exceeds
   ``threshold_sigma`` times the exposure's pixel noise, grown by ``grow`` px.
4. **Other-roll-angle check.** A raw spike pixel is masked only where exposures
   at another roll angle have a usable pixel on the same sky with less predicted
   spike light: none at all if possible; where two spikes cross, the brighter
   one is masked and the fainter kept. Without such a pixel it is left alone: a
   spike is never traded for a hole.

Nothing here touches SCI or DQ. The step module (``steps/spike_mask.py``)
records the result as a ``CFSPIKE`` extension on the canonical exposure.
"""

import math

import numpy as np

# NIRCam filter pivot wavelengths [um] (JDox).
PIVOT_UM = {
    'F070W': 0.704, 'F090W': 0.901, 'F115W': 1.154, 'F140M': 1.404,
    'F150W': 1.501, 'F162M': 1.626, 'F164N': 1.644, 'F150W2': 1.671,
    'F182M': 1.845, 'F187N': 1.874, 'F200W': 1.990, 'F210M': 2.093,
    'F212N': 2.120, 'F250M': 2.503, 'F277W': 2.786, 'F300M': 2.996,
    'F322W2': 3.247, 'F323N': 3.237, 'F335M': 3.365, 'F356W': 3.563,
    'F360M': 3.621, 'F405N': 4.055, 'F410M': 4.092, 'F430M': 4.280,
    'F444W': 4.421, 'F460M': 4.630, 'F466N': 4.654, 'F470N': 4.707,
    'F480M': 4.834,
}

ARCSEC2_TO_SR = (math.pi / 180.0 / 3600.0) ** 2

# The model image's +y axis is the telescope V3 axis. Measured on MACS0647
# F200W (two visits, stars at G = 8.0, 11.3 and 12.9, 8" to 300" from the star):
# the real spikes peak within 0.1-0.2 deg of that, with no trend in distance.
# (The models were simulated for NRCA1, but they are NOT rotated by that
# aperture's V3IdlYAngle of -0.55 deg; assuming so misplaces every spike.)

# Radial power-law slope of the spike arms beyond the model raster (the source
# model's own fit, header FIT2, quoted valid to 170").
ARM_SLOPE = -2.631

GAIA_EPOCH = 2016.0
KS_UM = 2.159
G_UM = 0.622
KS_VEGA_TO_AB = 1.85
G_VEGA_TO_AB = 0.11


# ---------------------------------------------------------------------------
# star fluxes

def band_ab_mag(g, bp_rp, pivot_um, default_color=1.5):
    """Predicted AB magnitude of a star at ``pivot_um`` from Gaia G and BP-RP.

    2MASS Ks comes from the Gaia DR3 color relation
    ``G - Ks = -0.0981 + 2.089 (BP-RP) - 0.1579 (BP-RP)^2``. Redward of Ks the
    stellar spectrum is taken as Rayleigh-Jeans (F_nu ~ lambda^-2); blueward the
    AB magnitude is interpolated in log(lambda) between G and Ks, which is crude
    (it ignores the 1.6 um bump of cool stars, so it under-predicts there).
    Stars with no measured color get ``default_color`` (red, i.e. bright in the
    near-IR, so the prediction errs toward masking).
    """
    g = np.asarray(g, dtype=float)
    c = np.asarray(bp_rp, dtype=float)
    c = np.where(np.isfinite(c), c, default_color)
    c = np.clip(c, 0.25, 5.0)
    ks_ab = g - (-0.0981 + 2.089 * c - 0.1579 * c * c) + KS_VEGA_TO_AB
    if pivot_um >= KS_UM:
        return ks_ab + 5.0 * math.log10(pivot_um / KS_UM)
    g_ab = g + G_VEGA_TO_AB
    t = math.log(pivot_um / G_UM) / math.log(KS_UM / G_UM)
    return g_ab + t * (ks_ab - g_ab)


def star_amplitude(ab_mag, pixarea0_arcsec2):
    """Scale from model units (fraction of total flux per raw pixel) to MJy/sr."""
    f_mjy = 3631.0e-6 * 10.0 ** (-0.4 * np.asarray(ab_mag, dtype=float))
    return f_mjy / (pixarea0_arcsec2 * ARCSEC2_TO_SR)


def query_gaia(ra, dec, radius_deg, g_max=19.5, max_rows=2000):
    """Gaia DR3 cone query (synchronous; the archive's async queue can hang).

    Returns a dict of float arrays: ra, dec [deg, epoch 2016.0], g, bp_rp,
    pmra, pmdec [mas/yr; 0 where Gaia has none].
    """
    from astroquery.gaia import Gaia
    adql = (
        f"select top {int(max_rows)} ra, dec, phot_g_mean_mag, bp_rp, pmra, pmdec "
        "from gaiadr3.gaia_source where 1=contains(point(ra,dec),"
        f"circle({ra:.7f},{dec:.7f},{radius_deg:.5f})) "
        f"and phot_g_mean_mag < {g_max:.2f} order by phot_g_mean_mag"
    )
    tbl = Gaia.launch_job(adql).get_results()

    def col(name):
        return np.ma.filled(np.ma.asarray(tbl[name], dtype=float), np.nan)

    return dict(
        ra=col('ra'), dec=col('dec'), g=col('phot_g_mean_mag'),
        bp_rp=col('bp_rp'),
        pmra=np.nan_to_num(col('pmra')), pmdec=np.nan_to_num(col('pmdec')),
        truncated=len(tbl) >= max_rows,
    )


def stars_at_epoch(stars, year):
    """Star positions moved from the Gaia epoch to decimal ``year``."""
    dt = year - GAIA_EPOCH
    dec = stars['dec'] + stars['pmdec'] * dt / 3.6e6
    ra = stars['ra'] + stars['pmra'] * dt / 3.6e6 / np.cos(np.radians(stars['dec']))
    return ra, dec


# ---------------------------------------------------------------------------
# model

class SpikeModel:
    """A published mask-grade spike model, sampled in arcsec offsets.

    ``sample(x, y)`` takes offsets in the model frame (+y = telescope V3) and
    returns the fraction of total flux per raw pixel **in the spike arms only**:
    the smooth halo (the median over azimuth at each radius) is removed, because
    a mask is wanted for the narrow arms, not for the star's broad scattered
    light, which the background steps handle. The value comes from a table per
    arm (distance along the arm, offset across it) that falls monotonically
    away from the arm axis and with distance, so a threshold gives one clean
    strip per arm; beyond the raster the outermost cross-section is continued
    with the arm power law at constant width in arcsec.

    ``width_scale`` stretches every arm across its own axis: real arms are wider
    than this pre-flight model (see ``[nircam.spike_mask].width_scale``).
    """

    # arm directions [deg from +y toward +x]: six from the hexagonal segments,
    # two from the vertical secondary-mirror strut
    ARMS = np.array([0.0, 60.0, 120.0, 180.0, 240.0, 300.0, 90.0, 270.0])
    P_MAX = 15.0        # arcsec: tabulated half-width across an arm

    def __init__(self, img, pixscl, ctr, pixarea0, wave):
        img = np.asarray(img, dtype=np.float32)
        self.pixscl = float(pixscl)
        self.ctr = float(ctr)
        self.pixarea0 = float(pixarea0)
        self.wave = float(wave)
        n = img.shape[0]
        half_px = min(self.ctr, n - 1 - self.ctr)
        self.r_ring = 0.96 * half_px * self.pixscl

        # remove the halo: median over azimuth in one-pixel annuli
        yy, xx = np.indices(img.shape)
        rbin = np.hypot(yy - self.ctr, xx - self.ctr).astype(int).ravel()
        order = np.argsort(rbin, kind='stable')
        cuts = np.searchsorted(rbin[order], np.arange(rbin.max() + 2))
        flat = img.ravel()[order]
        halo = np.zeros(rbin.max() + 1, dtype=np.float32)
        for k in range(len(halo)):
            seg = flat[cuts[k]:cuts[k + 1]]
            if seg.size:
                halo[k] = np.median(seg)
        self.halo = halo
        self.img = np.maximum(img - halo[rbin].reshape(img.shape), 0.0)
        del yy, xx

        # Arm tables: each arm resampled to (distance along the arm, offset
        # across it), folded about its axis, then made monotone: falling away
        # from the axis (so a threshold gives one strip, without detached
        # speckles) and falling with distance (so a strip has no gaps). A
        # thresholded raster has ragged, dashed edges; these do not.
        from scipy.ndimage import maximum_filter
        self._dp = 0.5 * self.pixscl
        n_r = int(self.r_ring / self.pixscl) + 1
        n_p = int(self.P_MAX / self._dp) + 1
        rr = np.arange(n_r) * self.pixscl
        pp = np.arange(n_p) * self._dp
        tab = np.zeros((len(self.ARMS), n_r, n_p), dtype=np.float32)
        for k, a in enumerate(np.radians(self.ARMS)):
            for side in (-1.0, 1.0):
                x = rr[:, None] * math.sin(a) + side * pp[None, :] * math.cos(a)
                y = rr[:, None] * math.cos(a) - side * pp[None, :] * math.sin(a)
                ix = np.rint(self.ctr + x / self.pixscl).astype(int)
                iy = np.rint(self.ctr + y / self.pixscl).astype(int)
                ok = (ix >= 0) & (ix < n) & (iy >= 0) & (iy < n)
                v = np.where(ok, self.img[np.clip(iy, 0, n - 1),
                                          np.clip(ix, 0, n - 1)], 0.0)
                tab[k] = np.maximum(tab[k], v)
        tab = maximum_filter(tab, size=(1, 3, 3))
        tab = np.minimum.accumulate(tab, axis=2)                    # across
        tab = np.maximum.accumulate(tab[:, ::-1], axis=1)[:, ::-1]  # along
        self._tab = np.log(np.maximum(tab, 1e-30)).astype(np.float32)
        self._env = tab[:, :, 0].max(axis=0).astype(np.float64)

    @classmethod
    def from_fits(cls, path):
        from astropy.io import fits
        with fits.open(path) as hdul:
            h = hdul[0].header
            return cls(hdul[0].data, h['PIXELSCL'], h['CFSPKCTR'],
                       h['PIXAREA0'], h['CFSPKWAV'])

    def sample(self, x, y, width_scale=1.0):
        r = np.hypot(x, y)
        az = np.degrees(np.arctan2(x, y))
        d = (az[..., None] - self.ARMS + 180.0) % 360.0 - 180.0
        k = np.argmin(np.abs(d), axis=-1)
        dk = np.radians(np.take_along_axis(d, k[..., None], axis=-1)[..., 0])
        # an arm keeps its width in arcsec at every distance
        p = r * np.abs(np.sin(dk)) / width_scale
        n_r, n_p = self._tab.shape[1:]
        r_in = np.minimum(r, self.r_ring)
        fr = np.clip(r_in / self.pixscl, 0, n_r - 1.001)
        fp = p / self._dp
        on = fp < n_p - 1.001
        fp = np.where(on, fp, 0.0)
        i0, j0 = fr.astype(int), fp.astype(int)
        wr, wp = (fr - i0).astype(np.float32), (fp - j0).astype(np.float32)
        t = self._tab
        val = ((1 - wr) * (1 - wp) * t[k, i0, j0] + wr * (1 - wp) * t[k, i0 + 1, j0]
               + (1 - wr) * wp * t[k, i0, j0 + 1] + wr * wp * t[k, i0 + 1, j0 + 1])
        out = np.exp(val)
        out[out < 1e-25] = 0.0
        far = r > self.r_ring
        if far.any():
            out[far] *= (r[far] / self.r_ring) ** ARM_SLOPE
        return np.where(on, out, 0.0).astype(np.float32)

    def reach(self, level):
        """Largest radius [arcsec] at which any pixel is at or above ``level``."""
        env = self._env
        if env[-1] >= level:
            return self.r_ring * (level / env[-1]) ** (1.0 / ARM_SLOPE)
        above = np.nonzero(env >= level)[0]
        return (above[-1] + 1) * self.pixscl if above.size else 0.0


def load_model(channel, pivot_um):
    """Fetch (if needed) and load the mask-grade model nearest ``pivot_um``."""
    from pathlib import Path
    import campfire_pipeline
    from campfire_pipeline.common import ref_cache

    manifest = (Path(campfire_pipeline.__file__).parent / 'data'
                / 'spike_model_manifest.toml')
    import toml
    entries = [e for e in toml.load(manifest)['model']
               if e['channel'] == channel and e['grade'] == 'mask']
    if not entries:
        raise RuntimeError(f"no {channel} mask-grade spike model in the manifest")
    entry = min(entries, key=lambda e: abs(e['wavelength_um'] - pivot_um))
    engine = ref_cache.RefCache(
        'spike_models', lambda: ref_cache.load_manifest(manifest, 'model'),
        log_prefix='spike', log_noun='spike model')
    engine.ensure([entry['name']])
    path = engine.resolve(entry['name'])
    if path is None:
        raise RuntimeError(f"spike model {entry['name']} could not be fetched")
    return SpikeModel.from_fits(path)


# ---------------------------------------------------------------------------
# geometry

def gnomonic(ra, dec, ra0, dec0):
    """Tangent-plane offsets [arcsec] of (ra, dec) about (ra0, dec0): east, north."""
    ra, dec = np.radians(ra), np.radians(dec)
    ra0, dec0 = math.radians(ra0), math.radians(dec0)
    cosc = (math.sin(dec0) * np.sin(dec)
            + math.cos(dec0) * np.cos(dec) * np.cos(ra - ra0))
    xi = np.cos(dec) * np.sin(ra - ra0) / cosc
    eta = (math.cos(dec0) * np.sin(dec)
           - math.sin(dec0) * np.cos(dec) * np.cos(ra - ra0)) / cosc
    return np.degrees(xi) * 3600.0, np.degrees(eta) * 3600.0


class SkyGrid:
    """A coarse tangent-plane grid used to compare coverage between exposures."""

    def __init__(self, ra0, dec0, half_width_arcsec, cell_arcsec):
        self.ra0, self.dec0 = float(ra0), float(dec0)
        self.cell = float(cell_arcsec)
        self.n = int(math.ceil(2 * half_width_arcsec / self.cell)) + 2
        self.half = self.n * self.cell / 2.0

    def index(self, ra, dec):
        """Flat cell index of each position (-1 where off the grid)."""
        xi, eta = gnomonic(ra, dec, self.ra0, self.dec0)
        ix = np.floor((xi + self.half) / self.cell).astype(np.int64)
        iy = np.floor((eta + self.half) / self.cell).astype(np.int64)
        ok = (ix >= 0) & (ix < self.n) & (iy >= 0) & (iy < self.n)
        return np.where(ok, iy * self.n + ix, -1)


def cluster_position_angles(pa_deg, width_deg):
    """Group roll angles that give the same spike pattern on the sky.

    The pattern repeats under a 180 deg rotation, so angles are compared modulo
    180. Returns an integer label per input.
    """
    pa = np.asarray(pa_deg, dtype=float) % 180.0
    order = np.argsort(pa)
    labels = np.zeros(len(pa), dtype=int)
    lab = 0
    for a, b in zip(order[:-1], order[1:]):
        if pa[b] - pa[a] > width_deg:
            lab += 1
        labels[b] = lab
    if lab > 0 and (pa[order[0]] + 180.0 - pa[order[-1]]) <= width_deg:
        labels[labels == lab] = 0          # wrap-around: last group joins first
    return labels


def predict_spike_sb(ra, dec, star_ra, star_dec, amp, model, lam_scale,
                     roll_ref, ra_ref, dec_ref, level, max_radius, roll_tol=0.0,
                     width_scale=1.0):
    """Predicted spike surface brightness [MJy/sr] at sky positions (ra, dec).

    Sums nothing: returns, per position, the largest prediction over the stars
    (a mask needs "is any star's spike above the level here"). Stars whose
    brightest pixel at the nearest position is below ``level`` are skipped.
    Also returns the indices of the stars that contributed. With ``roll_tol``
    (deg) the pattern is also evaluated rotated by +-roll_tol and the largest
    value kept, so a spike is covered despite a small error in its direction.
    """
    sb = np.zeros(ra.shape, dtype=np.float32)
    used = []
    cosd = math.cos(math.radians(dec_ref))
    # cheap flat offsets from the reference point, to pre-select pixels per star
    px = ((ra - ra_ref) * cosd * 3600.0).astype(np.float32)
    py = ((dec - dec_ref) * 3600.0).astype(np.float32)
    for i in range(len(star_ra)):
        # flux is conserved under the radial rescale, so SB scales as 1/lam^2
        a = amp[i] / lam_scale ** 2
        reach = min(model.reach(level / a) * lam_scale, max_radius)
        if reach <= 0:
            continue
        sx = (star_ra[i] - ra_ref) * cosd * 3600.0
        sy = (star_dec[i] - dec_ref) * 3600.0
        sel = np.nonzero((np.abs(px - sx) < reach * 1.02 + 2)
                         & (np.abs(py - sy) < reach * 1.02 + 2))[0]
        if sel.size == 0:
            continue
        xi, eta = gnomonic(ra[sel], dec[sel], star_ra[i], star_dec[i])
        r = np.hypot(xi, eta)
        keep = r < reach
        if not keep.any():
            continue
        sel, xi, eta, r = sel[keep], xi[keep], eta[keep], r[keep]
        # roll at the star: the header value holds at the reference point, and
        # north turns by d(ra) * sin(dec) between the two
        roll = roll_ref + (star_ra[i] - ra_ref) * math.sin(math.radians(star_dec[i]))
        phi0 = np.arctan2(xi, eta) - math.radians(roll)
        val = None
        for d in ((0.0, roll_tol, -roll_tol) if roll_tol > 0 else (0.0,)):
            phi = phi0 - math.radians(d)
            v = a * model.sample(r * np.sin(phi) / lam_scale,
                                 r * np.cos(phi) / lam_scale, width_scale)
            val = v if val is None else np.maximum(val, v)
        if (val >= level).any():
            used.append(i)
        sb[sel] = np.maximum(sb[sel], val)
    return sb, used
