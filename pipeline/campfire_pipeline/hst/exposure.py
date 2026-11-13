"""HST exposures as :class:`~campfire_pipeline.imaging.exposure.Chip` objects.

``open_exposure(path)`` returns one :class:`HstChip` per detector chip of a
calibrated HST file: two for an ACS/WFC ``_flc.fits`` (``[SCI,1]`` is chip 2,
``[SCI,2]`` is chip 1), one for a WFC3/IR ``_flt.fits``. The file must already
carry its distortion model in the headers (``stwcs.updatewcs``; an FLC straight
out of ``calacs`` has a linear WCS only, and is refused).

What the adapter turns HST conventions into (docs/hst/design.md, section 3.2):

**Units.** HST calibrated pixels are electrons (ACS) or electrons per second
(WFC3/IR) per native pixel, flat-fielded so that a uniform sky is flat. The
adapter returns surface brightness in MJy/sr::

    rate [e-/s]  = SCI / EXPTIME            (ACS)   or   SCI   (WFC3/IR)
    PHOTFNU [Jy s/e-] = 3.33564e4 * PHOTFLAM * PHOTPLAM**2   (if not in header)
    SB [MJy/sr]  = (rate - sky) * PHOTFNU * 1e-6 / (IDCSCALE arcsec)**2 [sr]

``PHOTFLAM`` is the exposure's own, so the time dependence of the sensitivity is
applied per exposure. ``IDCSCALE`` is the reference pixel scale of the
distortion solution, i.e. the pixel size at which the flat-fielded count rate is
a surface brightness.

**Sky.** A constant per chip, taken from the ``CFSKY`` keyword of the chip's
SCI header when the sky step has written it, otherwise estimated here as a
sigma-clipped median of the good pixels. It is subtracted from the returned
``sci``; the file is never modified.

**Variance.** HST gives one ``ERR`` array, which includes the Poisson noise of
sources and is computed from the noisy counts. The adapter splits it into the
three components the drizzle expects. ``var_rnoise``, which sets the drizzle
weight, is a smooth background level: one number per chip (sky + dark + read
noise), scaled by each pixel's effective exposure time; it does not follow the
pixel's own value. ``var_poisson`` is whatever ``ERR**2`` holds above that level
(source Poisson noise and local excesses), so the mosaic ``ERR`` still reflects
the per-pixel errors. ``var_flat`` is zero. See
:meth:`HstChip._background_variance`.

**Good pixels.** No DQ bit set outside the detector's tolerated set
(``hst/instruments.py``), finite SCI, positive finite ERR.
"""

import os
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats

from campfire_pipeline.hst.instruments import Detector, detector_for, filter_name
from campfire_pipeline.imaging.exposure import ChipArrays

_ARCSEC_TO_RAD = np.pi / (180.0 * 3600.0)
SKY_KEY = 'CFSKY'          # per-chip sky level, native SCI units (sky step)
VARSCALE_KEY = 'CFVARSC'   # measured / modelled background variance (sky step)


def photfnu(header):
    """Inverse sensitivity in Jy per e-/s from an HST SCI (or primary) header."""
    if header.get('PHOTFNU'):
        return float(header['PHOTFNU'])
    flam, plam = header.get('PHOTFLAM'), header.get('PHOTPLAM')
    if not flam or not plam:
        raise ValueError('header has neither PHOTFNU nor PHOTFLAM/PHOTPLAM')
    return 3.33564e4 * float(flam) * float(plam) ** 2


@dataclass
class HstChip:
    """One chip of one calibrated HST exposure."""
    path: str
    extver: int
    detector: Detector
    key: str
    filter: str
    exptime: float
    shape: tuple
    photfnu: float                 # Jy per e-/s, this exposure's own
    idcscale: float                # arcsec, reference pixel scale
    is_rate: bool                  # True when SCI is already e-/s
    header_sky: float = None       # CFSKY, native SCI units, if present
    var_scale: float = 1.0         # CFVARSC: measured / modelled sky variance
    tolerated_bits: int = None
    rootname: str = ''
    proposid: int = 0
    date_obs: str = ''
    wcsname: str = ''
    cal_ver: str = ''
    _wcs: object = field(default=None, repr=False)
    _sky_used: float = field(default=None, repr=False)

    @property
    def instrument(self):
        return self.detector.token

    @property
    def xposure(self):
        return self.exptime

    @property
    def pixel_area_arcsec2(self):
        return self.idcscale ** 2

    @property
    def sb_per_rate(self):
        """MJy/sr per (e-/s per native pixel)."""
        return self.photfnu * 1e-6 / (self.idcscale * _ARCSEC_TO_RAD) ** 2

    def pixel_to_world(self, x, y):
        return self._wcs.all_pix2world(x, y, 0)

    def load(self):
        bits = (self.detector.tolerated_bits if self.tolerated_bits is None
                else self.tolerated_bits)
        with fits.open(self.path, memmap=False) as hdul:
            sci = np.asarray(hdul['SCI', self.extver].data, dtype=np.float64)
            err = np.asarray(hdul['ERR', self.extver].data, dtype=np.float64)
            dq = np.asarray(hdul['DQ', self.extver].data).astype(np.int64)
            time = None
            if self.is_rate and ('TIME', self.extver) in hdul:
                time = np.asarray(hdul['TIME', self.extver].data, dtype=np.float64)

        good = ((dq & ~np.int64(bits)) == 0) & np.isfinite(sci) \
            & np.isfinite(err) & (err > 0)

        sky = self.header_sky
        if sky is None:
            sky = float(sigma_clipped_stats(sci[good], sigma=3.0, maxiters=5)[1]) \
                if good.any() else 0.0
        self._sky_used = sky

        rate, var_rate, t_eff = self.to_rate(sci, err, time, sky)
        good &= t_eff > 0
        var_background = self.var_scale * self._background_variance(
            rate, var_rate, t_eff, good)
        # Whatever ERR**2 holds above the background level: the Poisson noise
        # of sources, and any local excess (e.g. dark noise of a warm pixel).
        var_excess = np.clip(var_rate - var_background, 0.0, None)

        f = self.sb_per_rate
        return ChipArrays(
            sci=(rate * f).astype(np.float32),
            var_rnoise=(var_background * f * f).astype(np.float32),
            var_poisson=(var_excess * f * f).astype(np.float32),
            var_flat=np.zeros(sci.shape, dtype=np.float32),
            good=good,
        )

    def to_rate(self, sci, err, time, sky):
        """Native SCI/ERR to sky-subtracted count rate (e-/s), its variance and
        the effective exposure time per pixel. Shared with the sky step."""
        if self.is_rate:
            t_eff = np.full(sci.shape, self.exptime) if time is None else time
            return sci - sky, err ** 2, t_eff
        t_eff = np.full(sci.shape, self.exptime)
        return (sci - sky) / self.exptime, (err / self.exptime) ** 2, t_eff

    @staticmethod
    def _background_variance(rate, var_rate, t_eff, good):
        """Background-limited variance per pixel, in (e-/s)**2.

        This array sets the drizzle weight, so it must not depend on the pixel's
        own noise or on source flux: a weight that follows the pixel value
        biases the weighted mean (HST ``ERR`` is computed from the noisy counts,
        so ``1/ERR**2`` weighting is biased; DrizzlePac Handbook 5.2 quotes 1-2
        percent for sources). It is therefore one background level per chip,

            C = median over good pixels of (ERR**2 - source Poisson) * t_eff

        applied as ``C / t_eff``. For a CCD exposure ``t_eff`` is the exposure
        time and the result is a constant: sky + dark + read noise. For WFC3/IR
        ``t_eff`` is the per-pixel ``TIME`` array, so a pixel whose ramp was cut
        short by a cosmic ray or saturation gets proportionally more variance
        and less weight. The per-pixel structure of ``ERR`` is not lost: it goes
        into ``var_poisson`` and so into the mosaic ``ERR``.
        """
        with np.errstate(divide='ignore', invalid='ignore'):
            var_source = np.where(t_eff > 0, np.clip(rate, 0, None) / t_eff, 0.0)
        resid = (var_rate - var_source) * t_eff
        ok = good & np.isfinite(resid) & (resid > 0)
        level = float(np.median(resid[ok])) if ok.any() else 0.0
        with np.errstate(divide='ignore', invalid='ignore'):
            return np.where(t_eff > 0, level / t_eff, np.inf)

    def provenance(self):
        return {
            'key': self.key, 'rootname': self.rootname, 'extver': self.extver,
            'instrument': self.instrument, 'filter': self.filter,
            'proposid': self.proposid, 'date_obs': self.date_obs,
            'exptime': self.exptime, 'photfnu': self.photfnu,
            'idcscale': self.idcscale,
            'sky': np.nan if self._sky_used is None else self._sky_used,
            'sky_src': 'header' if self.header_sky is not None else 'median',
            'var_scale': self.var_scale,
            'wcsname': self.wcsname, 'cal_ver': self.cal_ver,
        }


def open_exposure(path, tolerated_bits=None):
    """The chips of a calibrated HST exposure file, as :class:`HstChip` objects."""
    from astropy.wcs import WCS

    chips = []
    with fits.open(path, memmap=False) as hdul:
        primary = hdul[0].header
        det = detector_for(primary.get('INSTRUME'), primary.get('DETECTOR'))
        rootname = str(primary.get('ROOTNAME') or
                       os.path.basename(path).split('_')[0]).strip().lower()
        filt = filter_name(primary)
        exptime = float(primary['EXPTIME'])
        for extver in det.sci_extvers:
            hdu = hdul['SCI', extver]
            h = hdu.header
            wcs = WCS(h, fobj=hdul)
            if wcs.sip is None:
                raise ValueError(
                    f"{os.path.basename(path)} [SCI,{extver}] has no distortion "
                    f"model in its header (WCSNAME={h.get('WCSNAME')!r}). Run the "
                    f"wcs_init step (stwcs.updatewcs) before using it.")
            idcscale = h.get('IDCSCALE')
            if not idcscale:
                raise ValueError(
                    f"{os.path.basename(path)} [SCI,{extver}] has no IDCSCALE")
            bunit = str(h.get('BUNIT', '')).upper().replace(' ', '')
            if bunit not in ('ELECTRONS', 'ELECTRONS/S'):
                raise ValueError(
                    f"{os.path.basename(path)} [SCI,{extver}]: unsupported "
                    f"BUNIT {h.get('BUNIT')!r}")
            try:
                fnu = photfnu(h)
            except ValueError:
                fnu = photfnu(primary)
            chips.append(HstChip(
                path=str(path), extver=extver, detector=det,
                key=f'{rootname}_sci{extver}', filter=filt, exptime=exptime,
                shape=hdu.data.shape, photfnu=fnu, idcscale=float(idcscale),
                is_rate=(bunit == 'ELECTRONS/S'),
                header_sky=(float(h[SKY_KEY]) if SKY_KEY in h else None),
                # recorded for every detector, applied only where the pixel
                # scatter is the noise (see instruments.py)
                var_scale=(float(h.get(VARSCALE_KEY, 1.0))
                           if det.apply_var_scale else 1.0),
                tolerated_bits=tolerated_bits, rootname=rootname,
                proposid=int(primary.get('PROPOSID') or 0),
                date_obs=str(primary.get('DATE-OBS') or ''),
                wcsname=str(h.get('WCSNAME') or ''),
                cal_ver=str(primary.get('CAL_VER') or ''),
                _wcs=wcs,
            ))
    return chips
