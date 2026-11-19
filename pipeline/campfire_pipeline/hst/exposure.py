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

**Background.** The ``bkg`` step (``hst/steps/bkg.py``) subtracts it from the
file and records the level it removed as ``CFBKG`` in the chip's SCI header;
such a chip is returned as it is. A file the step has not run on yet (the
cosmic-ray and trail steps open exposures before it) still has its background:
a sigma-clipped median of the good pixels is subtracted from the returned
``sci``, and the file is not modified.

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
(``hst/instruments.py``), finite SCI, positive finite ERR, not marked in the
``CFOUT`` extension the outlier pass writes (``hst/steps/outlier.py``), and
not inside a region of the exposure's region file (the ``CFMASK`` extension,
``hst/steps/apply_masks.py``: hand-drawn masks and the satellite trails of
``hst/steps/trails.py``).
"""

import os
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats

from campfire_pipeline.hst.instruments import Detector, detector_for, filter_name
from campfire_pipeline.imaging.exposure import ChipArrays

_ARCSEC_TO_RAD = np.pi / (180.0 * 3600.0)
BKG_KEY = 'CFBKG'          # background level the bkg step removed from SCI,
                           # native SCI units; present = SCI is subtracted
VARSCALE_KEY = 'CFVARSC'   # measured / modelled background variance (bkg step)
OUTLIER_EXT = 'CFOUT'      # per-chip image extension, non-zero at an outlier
                           # found by the field-level pass (outlier step)
MASK_EXT = 'CFMASK'        # per-chip image extension, non-zero inside a
                           # hand-drawn region (apply_masks step)


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
    bkg_level: float = None        # CFBKG: the level the bkg step removed
                                   # (native SCI units); None = not subtracted
    var_scale: float = 1.0         # CFVARSC: measured / modelled sky variance
    tolerated_bits: int = None
    use_outlier_flags: bool = True  # honor the CFOUT extension when present
    rootname: str = ''
    proposid: int = 0
    date_obs: str = ''
    wcsname: str = ''
    cal_ver: str = ''
    crds_ctx: str = ''             # pinned HST context of the calibration
    expstart: float = float('nan')  # MJD of the exposure's start and end
    expend: float = float('nan')
    photplam: float = float('nan')  # pivot wavelength, Angstrom
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
            outlier = None
            if self.use_outlier_flags and (OUTLIER_EXT, self.extver) in hdul:
                outlier = np.asarray(hdul[OUTLIER_EXT, self.extver].data) != 0
            masked = None
            if (MASK_EXT, self.extver) in hdul:
                masked = np.asarray(hdul[MASK_EXT, self.extver].data) != 0

        good = ((dq & ~np.int64(bits)) == 0) & np.isfinite(sci) \
            & np.isfinite(err) & (err > 0)
        if outlier is not None:
            good &= ~outlier
        if masked is not None:
            good &= ~masked

        if self.bkg_level is not None:
            sky = 0.0                    # the bkg step has subtracted it
        else:
            sky = float(sigma_clipped_stats(sci[good], sigma=3.0, maxiters=5)[1]) \
                if good.any() else 0.0
        self._sky_used = sky

        rate, var_rate, t_eff = self.to_rate(sci, err, time, sky)
        good &= t_eff > 0
        var_model = self._background_variance(rate, var_rate, t_eff, good)
        # The background term follows the measured pixel scatter (CFVARSC).
        var_background = self.var_scale * var_model
        # Whatever ERR**2 holds above the modelled background level: the
        # Poisson noise of sources, and any local excess (e.g. dark noise of a
        # warm pixel). Taken against the unscaled model, so that scaling the
        # background term neither eats into the source term (scale above 1)
        # nor moves background variance into it (scale below 1).
        var_excess = np.clip(var_rate - var_model, 0.0, None)

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
        the effective exposure time per pixel. Shared with the bkg step."""
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
            # the background level taken out of this chip, native units: by
            # the bkg step ('bkg'), or here on load ('median')
            'sky': (self.bkg_level if self.bkg_level is not None
                    else np.nan if self._sky_used is None else self._sky_used),
            'sky_src': 'bkg' if self.bkg_level is not None else 'median',
            'var_scale': self.var_scale,
            'wcsname': self.wcsname, 'cal_ver': self.cal_ver,
            'crds_ctx': self.crds_ctx,
            'expstart': self.expstart, 'expend': self.expend,
        }

    @staticmethod
    def mosaic_header(chips):
        """Header cards of a mosaic drizzled from ``chips``, beyond the ones
        the mosaic writer derives from the output grid.

        Returns ``(primary, sci)``, two lists of ``(keyword, value, comment)``.
        The mosaic writer calls this for any chip class that has it
        (``imaging/mosaic_io.py``); the cards are the ones a NIRCam mosaic
        carries from the JWST pipeline, under the names an HST user expects
        where the two differ.

        * Identity: ``TELESCOP``, ``INSTRUME`` / ``DETECTOR`` / ``FILTER`` as
          the archive names them (``ACS`` / ``WFC`` / ``F814W``), on both
          headers so a split file identifies itself; ``PROPOSID``.
        * Time: ``DATE-OBS`` / ``TIME-OBS`` / ``DATE-BEG`` / ``DATE-END`` and
          ``EXPSTART`` / ``EXPMID`` / ``EXPEND`` on the primary, ``MJD-BEG`` /
          ``MJD-AVG`` / ``MJD-END`` and ``TELAPSE`` on SCI, from the first
          start to the last end; the middle is weighted by exposure time.
        * Photometry (SCI): ``PHOTMJSR`` / ``PHOTUJA2``, the surface
          brightness that gives 1 e-/s in a native pixel, and ``PHOTFNU``
          (Jy per e-/s); the exposure-time-weighted mean over the inputs,
          whose own values differ slightly (the sensitivity changes with
          time), with the spread in ``PHOTSPRD``. ``PHOTPLAM`` is the pivot
          wavelength.
        * Calibration: ``CAL_VER`` and ``CRDS_CTX`` of the inputs.

        A quantity is counted once per exposure, not once per chip.
        """
        from astropy.time import Time

        if not chips:
            return [], []
        first = {}
        for c in chips:                      # one chip stands for its exposure
            first.setdefault(c.path, c)
        exposures = list(first.values())

        def joined(values):
            return ','.join(sorted({str(v) for v in values if str(v)}))

        def one(values):
            unique = sorted({str(v) for v in values})
            return unique[0] if len(unique) == 1 else 'MULTIPLE'

        identity = [
            ('TELESCOP', 'HST', 'telescope'),
            ('INSTRUME', one(c.detector.instrume for c in exposures), 'instrument'),
            ('DETECTOR', one(c.detector.detector for c in exposures), 'detector'),
            ('FILTER', one(c.filter.upper() for c in exposures), 'filter'),
        ]
        primary = list(identity)
        sci = list(identity)
        primary.append(('PROPOSID', joined(c.proposid for c in exposures if c.proposid),
                        'HST proposal ID(s) of the inputs'))
        primary.append(('CAL_VER', joined(c.cal_ver for c in exposures),
                        'calibration software version of the inputs'))
        primary.append(('CRDS_CTX', joined(c.crds_ctx for c in exposures),
                        'CRDS context of the inputs'))

        t = np.array([c.exptime for c in exposures], dtype=float)
        start = np.array([c.expstart for c in exposures], dtype=float)
        end = np.array([c.expend for c in exposures], dtype=float)
        if np.all(np.isfinite(start)) and np.all(np.isfinite(end)):
            beg, fin = float(start.min()), float(end.max())
            mid = float(np.sum(0.5 * (start + end) * t) / np.sum(t))
            iso_beg = Time(beg, format='mjd', scale='utc').isot
            iso_end = Time(fin, format='mjd', scale='utc').isot
            primary += [
                ('DATE-OBS', iso_beg[:10], '[yyyy-mm-dd] UTC date of the first exposure'),
                ('TIME-OBS', iso_beg[11:], '[hh:mm:ss.sss] UTC start of the first exposure'),
                ('DATE-BEG', iso_beg, 'UTC start of the first exposure'),
                ('DATE-END', iso_end, 'UTC end of the last exposure'),
                ('EXPSTART', beg, '[d] MJD start of the first exposure'),
                ('EXPMID', mid, '[d] MJD exposure-time-weighted mid-point'),
                ('EXPEND', fin, '[d] MJD end of the last exposure'),
            ]
            sci += [
                ('MJD-BEG', beg, '[d] start of the first exposure in MJD'),
                ('MJD-AVG', mid, '[d] exposure-time-weighted mid-point in MJD'),
                ('MJD-END', fin, '[d] end of the last exposure in MJD'),
                ('TELAPSE', float(np.sum(end - start) * 86400.0),
                 '[s] summed elapsed time of the exposures'),
            ]

        photmjsr = np.array([c.sb_per_rate for c in exposures], dtype=float)
        photfnu_ = np.array([c.photfnu for c in exposures], dtype=float)
        mean_mjsr = float(np.sum(photmjsr * t) / np.sum(t))
        sci += [
            ('PHOTMJSR', mean_mjsr, 'MJy/sr producing 1 e-/s per native pixel (mean)'),
            # 1 MJy/sr = 1e12 uJy / (206264.806...)**2 arcsec2
            ('PHOTUJA2', mean_mjsr * 1e12 * _ARCSEC_TO_RAD ** 2,
             'uJy/arcsec2 producing 1 e-/s per native pixel'),
            ('PHOTFNU', float(np.sum(photfnu_ * t) / np.sum(t)),
             'Jy producing 1 e-/s (exposure-time-weighted mean)'),
            ('PHOTSPRD', float((photmjsr.max() - photmjsr.min()) / mean_mjsr),
             'fractional spread of PHOTMJSR over the inputs'),
        ]
        plam = np.array([c.photplam for c in exposures], dtype=float)
        if np.all(np.isfinite(plam)):
            sci.append(('PHOTPLAM', float(np.mean(plam)), '[Angstrom] pivot wavelength'))
        return primary, sci


def open_exposure(path, tolerated_bits=None, use_outlier_flags=True):
    """The chips of a calibrated HST exposure file, as :class:`HstChip` objects.

    ``use_outlier_flags=False`` ignores the ``CFOUT`` extension of the
    field-level outlier pass (that pass itself opens its inputs this way, so a
    re-run does not depend on the previous result).
    """
    from astropy.wcs import WCS

    chips = []
    with fits.open(path, memmap=False) as hdul:
        primary = hdul[0].header
        det = detector_for(primary.get('INSTRUME'), primary.get('DETECTOR'))
        rootname = str(primary.get('ROOTNAME') or
                       os.path.basename(path).split('_')[0]).strip().lower()
        filt = filter_name(primary)
        exptime = float(primary['EXPTIME'])
        # The pinned HST context: its own card, or (exposures calibrated
        # before the card was written) the last word of the calibrate stamp,
        # 'raw <CAL_VER> <context>.pmap'.
        crds_ctx = str(primary.get('CRDS_CTX') or '').strip()
        if not crds_ctx:
            last = str(primary.get('CFP_CAL') or '').split()[-1:]
            crds_ctx = last[0] if last and last[0].endswith('.pmap') else ''

        def mjd(key):
            try:
                return float(primary[key])
            except (KeyError, TypeError, ValueError):
                return float('nan')

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
                bkg_level=(float(h[BKG_KEY]) if BKG_KEY in h else None),
                # applied for every detector: the variance follows the
                # measured pixel scatter (see instruments.py)
                var_scale=float(h.get(VARSCALE_KEY, 1.0)),
                tolerated_bits=tolerated_bits,
                use_outlier_flags=use_outlier_flags, rootname=rootname,
                proposid=int(primary.get('PROPOSID') or 0),
                date_obs=str(primary.get('DATE-OBS') or ''),
                wcsname=str(h.get('WCSNAME') or ''),
                cal_ver=str(primary.get('CAL_VER') or ''),
                crds_ctx=crds_ctx,
                expstart=mjd('EXPSTART'), expend=mjd('EXPEND'),
                photplam=float(h.get('PHOTPLAM') or primary.get('PHOTPLAM')
                               or float('nan')),
                _wcs=wcs,
            ))
    return chips
