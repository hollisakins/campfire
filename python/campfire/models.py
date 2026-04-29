"""Data models for the CAMPFIRE Python client."""

import re
from collections import namedtuple
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Callable, List, Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from astropy.table import Table


# ---------------------------------------------------------------------------
# SpectrumData — a loaded 1D spectrum with flux arrays
# ---------------------------------------------------------------------------

@dataclass
class SpectrumData:
    """Container for a 1D extracted spectrum.

    Provides clean attribute access to wavelength, flux, and error arrays
    extracted from a CAMPFIRE FITS file.

    Attributes
    ----------
    wavelength : np.ndarray
        Wavelength array in microns.
    fnu : np.ndarray
        Flux density f_ν in microjansky (μJy).
    fnu_err : np.ndarray
        Flux density error f_ν in microjansky (μJy).
    flam : np.ndarray
        Flux density f_λ in erg/s/cm²/Å. Auto-computed from fnu if not
        provided.
    flam_err : np.ndarray
        Flux density error f_λ in erg/s/cm²/Å. Auto-computed from fnu_err
        if not provided.
    header : dict
        FITS primary header as a dict.
    grating : str
        Grating name (e.g., 'PRISM', 'G395M').
    spectrum_id : str
        Stable per-spectrum identifier (matches ``spectra.spectrum_id`` in
        the catalog, e.g. 'ember_cosmos_p1_prism_clear_920424'). For a
        stacked output this may be a synthetic ID like
        ``'stack:<object_id>:PRISM'``.
    fits_path : str or None
        Local file path if loaded from disk, None if from API.
    fnu_units : str
        Unit string for fnu (default ``'uJy'``).
    flam_units : str
        Unit string for flam (default ``'erg/s/cm2/A'``).
    wave_units : str
        Unit string for wavelength (default ``'um'``).
    """

    wavelength: np.ndarray
    fnu: np.ndarray
    fnu_err: np.ndarray
    header: dict
    grating: str
    spectrum_id: str
    flam: Optional[np.ndarray] = dc_field(default=None, repr=False)
    flam_err: Optional[np.ndarray] = dc_field(default=None, repr=False)
    fits_path: Optional[str] = None
    fnu_units: str = dc_field(default="uJy", repr=False)
    flam_units: str = dc_field(default="erg/s/cm2/A", repr=False)
    wave_units: str = dc_field(default="um", repr=False)

    def __post_init__(self):
        # Auto-compute flam from fnu if not provided in FITS.
        # Conversion f_λ = f_ν * c / λ² with c expressed so that:
        #   fnu in μJy, λ in μm → flam in erg/s/cm²/Å
        if self.flam is None:
            self.flam = self.fnu * 2.998e-19 / self.wavelength**2
        if self.flam_err is None:
            self.flam_err = self.fnu_err * 2.998e-19 / self.wavelength**2

    def __repr__(self) -> str:
        n = len(self.wavelength)
        wmin = self.wavelength.min() if n > 0 else 0
        wmax = self.wavelength.max() if n > 0 else 0
        return (
            f"SpectrumData({self.spectrum_id}, {self.grating}, "
            f"{n} pixels, {wmin:.2f}-{wmax:.2f} μm)"
        )

    def plot(
        self,
        flux_unit: str = "fnu",
        show_errors: bool = True,
        ax=None,
        **kwargs,
    ):
        """Quick-look matplotlib plot of the spectrum.

        Parameters
        ----------
        flux_unit : str
            ``'fnu'`` for f_ν in μJy (default), ``'flam'`` or
            ``'flambda'`` for f_λ in erg/s/cm²/Å.
        show_errors : bool
            Show shaded 1-sigma error band (default True).
        ax : matplotlib.axes.Axes, optional
            Axes to draw on. If *None*, a new figure is created.
        **kwargs
            Passed to ``ax.step()``.

        Returns
        -------
        matplotlib.axes.Axes
        """
        import matplotlib.pyplot as plt

        if ax is None:
            _, ax = plt.subplots()

        wave = self.wavelength
        if flux_unit in ("flam", "flambda"):
            flux = self.flam
            flux_err = self.flam_err
        else:
            flux = self.fnu
            flux_err = self.fnu_err

        valid = np.isfinite(flux)
        w = wave[valid]
        f = flux[valid]

        line, = ax.step(w, f, where="mid", **kwargs)

        if show_errors:
            e = flux_err[valid]
            color = line.get_color()
            ax.fill_between(w, f - e, f + e, alpha=0.15, color=color, step="mid")

        ax.set_xlabel("Wavelength (μm)")
        if flux_unit in ("flam", "flambda"):
            ax.set_ylabel("f_λ (erg/s/cm²/Å)")
        else:
            ax.set_ylabel("f_ν (μJy)")
        ax.set_title(f"{self.spectrum_id}  {self.grating}")

        return ax

    @staticmethod
    def _parse_spectrum_id_from_filename(filename: str) -> str:
        """Extract spectrum_id from a CAMPFIRE FITS filename.

        Mirrors the server-side generated `spectra.spectrum_id` column:
        strips the leading directory and the trailing `_spec.fits` suffix.

        Examples
        --------
        >>> SpectrumData._parse_spectrum_id_from_filename(
        ...     'ember_cosmos_p1_prism_clear_920424_spec.fits')
        'ember_cosmos_p1_prism_clear_920424'
        """
        stem = Path(filename).name  # drop directory
        stem = re.sub(r'_spec\.fits$', '', stem, flags=re.IGNORECASE)
        return stem

    @classmethod
    def from_fits(cls, fits_path: str, spectrum_id: str = "", grating: str = "") -> "SpectrumData":
        """Create a SpectrumData from a local FITS file.

        Reads the SPEC1D extension (HDU 1) from a CAMPFIRE pipeline
        output file. The 1D spectrum columns are: ``wave``, ``fnu``,
        ``fnu_err``, ``flam``, ``flam_err``.

        Parameters
        ----------
        fits_path : str
            Path to the FITS file.
        spectrum_id : str, optional
            Spectrum ID. If not provided, parsed from the FITS filename.
        grating : str, optional
            Grating name. If not provided, read from the GRATING header.

        Returns
        -------
        SpectrumData
        """
        from astropy.io import fits

        with fits.open(fits_path) as hdul:
            header = dict(hdul[0].header)

            if len(hdul) < 2:
                raise ValueError(f"FITS file has no data extensions: {fits_path}")

            data = hdul[1].data

            if hasattr(data, "columns"):
                col_names = [c.name.lower() for c in data.columns]

                if "wave" in col_names:
                    wavelength = np.array(data["wave"], dtype=float)
                elif "wavelength" in col_names:
                    wavelength = np.array(data["wavelength"], dtype=float)
                else:
                    wavelength = np.array(data.field(0), dtype=float)

                if "fnu" in col_names:
                    fnu = np.array(data["fnu"], dtype=float)
                elif "flux" in col_names:
                    fnu = np.array(data["flux"], dtype=float)
                else:
                    fnu = np.array(data.field(1), dtype=float)

                if "fnu_err" in col_names:
                    fnu_err = np.array(data["fnu_err"], dtype=float)
                elif "flux_err" in col_names:
                    fnu_err = np.array(data["flux_err"], dtype=float)
                elif "err" in col_names:
                    fnu_err = np.array(data["err"], dtype=float)
                else:
                    fnu_err = np.zeros_like(fnu)

                flam = None
                if "flam" in col_names:
                    flam = np.array(data["flam"], dtype=float)

                flam_err = None
                if "flam_err" in col_names:
                    flam_err = np.array(data["flam_err"], dtype=float)

            else:
                if data.ndim == 2 and data.shape[0] >= 2:
                    wavelength = np.array(data[0], dtype=float)
                    fnu = np.array(data[1], dtype=float)
                    fnu_err = np.array(data[2], dtype=float) if data.shape[0] > 2 else np.zeros_like(fnu)
                else:
                    wavelength = np.arange(len(data), dtype=float)
                    fnu = np.array(data, dtype=float)
                    fnu_err = np.zeros_like(fnu)
                flam = None
                flam_err = None

            if not spectrum_id:
                filename = header.get("FILENAME", Path(fits_path).name)
                spectrum_id = cls._parse_spectrum_id_from_filename(filename)
            if not grating:
                grating = header.get("GRATING", "unknown")

        return cls(
            wavelength=wavelength,
            fnu=fnu,
            fnu_err=fnu_err,
            flam=flam,
            flam_err=flam_err,
            header=header,
            grating=grating,
            spectrum_id=spectrum_id,
            fits_path=fits_path,
        )


# ---------------------------------------------------------------------------
# Band namedtuple for photometry band access
# ---------------------------------------------------------------------------

Band = namedtuple("Band", ["flux", "flux_err", "wavelength"])


# ---------------------------------------------------------------------------
# Photometry — flat parallel-array structure for a single object
# ---------------------------------------------------------------------------

@dataclass
class Photometry:
    """Photometric measurements for a sky-object.

    Stores band-level data as parallel arrays for easy plotting and
    analysis. Individual bands are accessible by name::

        phot = obj.photometry
        phot['f444w']              # Band(flux=0.42, flux_err=0.03, wavelength=4.44)
        phot.flux                  # full array
        plt.errorbar(phot.wavelength, phot.flux, phot.flux_err)

    Attributes
    ----------
    bands : list of str
        Band names in wavelength order.
    flux : np.ndarray
        Flux density for each band (microjansky).
    flux_err : np.ndarray
        Flux error for each band (microjansky).
    wavelength : np.ndarray
        Effective wavelength for each band (microns).
    flux_unit : str
        Unit string (typically 'uJy').
    catalog : str
        Source catalog name.
    catalog_id : str or None
        ID of this object in the source catalog.
    match_distance_arcsec : float or None
        Cross-match distance in arcsec.
    photo_z : float or None
        Photometric redshift from this catalog.
    photo_z_err_lo : float or None
        Photo-z lower error bound.
    photo_z_err_hi : float or None
        Photo-z upper error bound.
    """

    bands: List[str]
    flux: np.ndarray
    flux_err: np.ndarray
    wavelength: np.ndarray
    flux_unit: str = "uJy"
    catalog: str = ""
    catalog_id: Optional[str] = None
    match_distance_arcsec: Optional[float] = None
    photo_z: Optional[float] = None
    photo_z_err_lo: Optional[float] = None
    photo_z_err_hi: Optional[float] = None

    def __getitem__(self, band_name: str) -> Band:
        """Get a single band by name.

        Raises
        ------
        KeyError
            If band name not found.
        """
        try:
            idx = self.bands.index(band_name)
        except ValueError:
            raise KeyError(
                f"Band '{band_name}' not found. Available: {', '.join(self.bands)}"
            )
        return Band(
            flux=self.flux[idx],
            flux_err=self.flux_err[idx],
            wavelength=self.wavelength[idx],
        )

    def to_table(self) -> "Table":
        """Convert to an astropy Table with columns: band, flux, flux_err, wavelength."""
        from astropy.table import Table as AstropyTable

        return AstropyTable(
            {
                "band": self.bands,
                "flux": self.flux,
                "flux_err": self.flux_err,
                "wavelength": self.wavelength,
            }
        )

    def __len__(self) -> int:
        return len(self.bands)

    def __repr__(self) -> str:
        n = len(self.bands)
        cat = f", {self.catalog}" if self.catalog else ""
        pz = f", photo_z={self.photo_z:.2f}" if self.photo_z is not None else ""
        return f"Photometry({n} bands{cat}{pz})"

    @classmethod
    def from_record(cls, record: dict) -> "Photometry":
        """Build from a store photometry record.

        Parameters
        ----------
        record : dict
            A row from ``LocalStore.query_photometry()`` (or similar) with
            a ``photometry`` key containing a dict of ``{band: {flux, flux_err, wav}}``.
        """
        phot_data = record.get("photometry") or {}
        bands_dict = phot_data.get("bands", {}) if isinstance(phot_data, dict) else {}

        # Sort bands by wavelength
        band_items = sorted(
            bands_dict.items(),
            key=lambda kv: kv[1].get("wav", 0) or 0,
        )

        band_names = [k for k, _ in band_items]
        flux = np.array([v.get("flux", np.nan) for _, v in band_items])
        flux_err = np.array([v.get("flux_err", np.nan) for _, v in band_items])
        wavelength = np.array([v.get("wav", np.nan) for _, v in band_items])

        return cls(
            bands=band_names,
            flux=flux,
            flux_err=flux_err,
            wavelength=wavelength,
            flux_unit=phot_data.get("flux_unit", "uJy") if isinstance(phot_data, dict) else "uJy",
            catalog=record.get("catalog_name", "") or "",
            catalog_id=record.get("catalog_id"),
            match_distance_arcsec=record.get("match_distance_arcsec"),
            photo_z=record.get("photo_z"),
            photo_z_err_lo=record.get("photo_z_err_lo"),
            photo_z_err_hi=record.get("photo_z_err_hi"),
        )


# ---------------------------------------------------------------------------
# Spectrum — catalog-row handle; .open() materialises a SpectrumData
# ---------------------------------------------------------------------------

@dataclass
class Spectrum:
    """Catalog metadata for a single spectrum.

    This is the catalog row — spectrum_id, grating, SNR, etc. Call
    ``.open()`` to load the actual wavelength/flux arrays as a
    :class:`SpectrumData`.

    Attributes
    ----------
    spectrum_id : str
        Stable per-spectrum identifier.
    object_id : str
        Parent object ID.
    grating : str
        Grating name (e.g. 'PRISM', 'G395M').
    signal_to_noise : float or None
        Peak signal-to-noise ratio.
    exposure_time : float or None
        Total exposure time in seconds.
    reduction_version : str or None
        Pipeline reduction version.
    redshift_auto : float or None
        Automatic (zfit) redshift for this grating. May differ between
        gratings of the same object.
    dq_flags : int
        Per-spectrum data-quality bitmask. See :class:`campfire.flags.DQFlags`.
    fits_path : str or None
        Remote FITS path.
    local_path : str or None
        Local relative path to the downloaded FITS file (None if not downloaded).
    """

    spectrum_id: str
    object_id: str
    grating: str
    signal_to_noise: Optional[float] = None
    exposure_time: Optional[float] = None
    reduction_version: Optional[str] = None
    redshift_auto: Optional[float] = None
    dq_flags: int = 0
    fits_path: Optional[str] = None
    local_path: Optional[str] = None
    _opener: Optional[Callable] = dc_field(default=None, repr=False, compare=False)

    def open(self) -> SpectrumData:
        """Open this spectrum, returning a :class:`SpectrumData` with flux arrays.

        Reads from local FITS if downloaded, otherwise downloads from the API.
        Requires the parent :class:`Campfire` client (set automatically when
        returned by ``cf.get_object()``).
        """
        if self._opener is None:
            raise RuntimeError(
                "No client attached. Use cf.get_object() to get Spectrum "
                "instances with .open() support, or call "
                "cf.open_spectrum(spectrum_id) directly."
            )
        return self._opener(self.spectrum_id)

    @property
    def data(self) -> SpectrumData:
        """Load and return the spectrum data (wavelength, flux, etc.)."""
        return self.open()

    def plot(self, **kwargs):
        """Load the spectrum and create a quick-look matplotlib plot.

        All keyword arguments are forwarded to :meth:`SpectrumData.plot`.
        """
        return self.open().plot(**kwargs)

    @property
    def downloaded(self) -> bool:
        """Whether the FITS file is available locally."""
        return self.local_path is not None

    def __repr__(self) -> str:
        snr = f", SNR={self.signal_to_noise:.1f}" if self.signal_to_noise else ""
        dl = " ✓" if self.downloaded else ""
        return f"Spectrum({self.spectrum_id}, {self.grating}{snr}{dl})"


# ---------------------------------------------------------------------------
# SpectrumCollection — numpy-style filterable collection of Spectrum objects
# ---------------------------------------------------------------------------

class SpectrumCollection:
    """Filterable collection of :class:`Spectrum` objects.

    Supports numpy-style boolean indexing on any attribute::

        prism = obj.spectra[obj.spectra.grating == 'PRISM']
        high_snr = obj.spectra[obj.spectra.signal_to_noise > 10]

    Integer indexing returns a single :class:`Spectrum`. Iteration yields
    individual :class:`Spectrum` instances.
    """

    def __init__(self, spectra: List[Spectrum]):
        self._spectra = list(spectra)

    # --- Attribute access returns numpy arrays for filtering ---

    @property
    def spectrum_id(self) -> np.ndarray:
        return np.array([s.spectrum_id for s in self._spectra])

    @property
    def object_id(self) -> np.ndarray:
        return np.array([s.object_id for s in self._spectra])

    @property
    def grating(self) -> np.ndarray:
        return np.array([s.grating for s in self._spectra])

    @property
    def signal_to_noise(self) -> np.ndarray:
        return np.array([s.signal_to_noise for s in self._spectra], dtype=float)

    @property
    def exposure_time(self) -> np.ndarray:
        return np.array([s.exposure_time for s in self._spectra], dtype=float)

    @property
    def downloaded(self) -> np.ndarray:
        return np.array([s.downloaded for s in self._spectra])

    @property
    def gratings(self) -> List[str]:
        """Unique gratings available in this collection."""
        return sorted(set(s.grating for s in self._spectra))

    # --- Indexing ---

    def __getitem__(self, key):
        if isinstance(key, (int, np.integer)):
            return self._spectra[key]
        if isinstance(key, np.ndarray) and key.dtype == bool:
            return SpectrumCollection(
                [s for s, m in zip(self._spectra, key) if m]
            )
        if isinstance(key, slice):
            return SpectrumCollection(self._spectra[key])
        raise TypeError(f"Unsupported index type: {type(key)}")

    # --- Container protocol ---

    def __len__(self) -> int:
        return len(self._spectra)

    def __iter__(self):
        return iter(self._spectra)

    def __bool__(self) -> bool:
        return len(self._spectra) > 0

    def to_table(self) -> "Table":
        """Convert to an astropy Table."""
        from astropy.table import Table as AstropyTable

        if not self._spectra:
            return AstropyTable()
        rows = [
            {
                "spectrum_id": s.spectrum_id,
                "object_id": s.object_id,
                "grating": s.grating,
                "signal_to_noise": s.signal_to_noise,
                "exposure_time": s.exposure_time,
                "reduction_version": s.reduction_version,
                "local_path": s.local_path,
            }
            for s in self._spectra
        ]
        return AstropyTable(rows=rows)

    def __repr__(self) -> str:
        n = len(self._spectra)
        if n == 0:
            return "SpectrumCollection(empty)"

        sid_width = max(max(len(s.spectrum_id) for s in self._spectra), len("spectrum_id"))
        grat_width = max(max(len(s.grating) for s in self._spectra), len("grating"))

        header = (
            f"{'spectrum_id':<{sid_width}}  {'grating':<{grat_width}}"
            f"  {'SNR':>7}  {'exp_time':>8}  {'local':>5}"
        )
        sep = "-" * len(header)

        lines = [f"SpectrumCollection ({n} spectra)", header, sep]
        for s in self._spectra:
            snr = f"{s.signal_to_noise:7.1f}" if s.signal_to_noise is not None else "      -"
            exp = f"{s.exposure_time:8.0f}" if s.exposure_time is not None else "       -"
            dl = "    Y" if s.downloaded else "    -"
            lines.append(
                f"{s.spectrum_id:<{sid_width}}  {s.grating:<{grat_width}}"
                f"  {snr}  {exp}  {dl}"
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Object — sky-object with full context (cross-program)
# ---------------------------------------------------------------------------

@dataclass
class Object:
    """A sky-object — the primary CAMPFIRE query target.

    Groups all spectra at a single sky position across programs. Use
    :attr:`spectra` (a :class:`SpectrumCollection`) to access or filter
    the associated spectra, and :attr:`photometry` for broadband
    photometry.

    Attributes
    ----------
    object_id : str
        Sky-object identifier.
    ra : float
        Right ascension (degrees).
    dec : float
        Declination (degrees).
    field : str
        Field name.
    redshift : float or None
        Best redshift estimate (inspected > auto).
    redshift_auto : float or None
        Automated redshift-fit estimate.
    redshift_inspected : float or None
        User-inspected redshift, if set.
    redshift_quality : int
        Quality code (0=none, 1=tentative, … 4=secure).
    n_spectra : int
        Number of spectra associated with this object.
    programs : list of str
        Program slugs with data for this object.
    tags : list of str
        Tag / list slugs this object belongs to (e.g. ``['lrd', 'blagn']``).
    max_snr : float or None
        Highest per-spectrum SNR.
    max_exposure_time : float or None
        Longest per-spectrum exposure time.
    has_photometry : bool
        Whether broadband photometry is available.
    photo_z : float or None
    photo_z_err_lo : float or None
    photo_z_err_hi : float or None
    spectra : SpectrumCollection
        All spectra for this object.
    photometry : Photometry or None
        Photometric measurements, or None if not available.
    """

    object_id: str
    ra: float
    dec: float
    field: str = ""
    redshift: Optional[float] = None
    redshift_auto: Optional[float] = None
    redshift_inspected: Optional[float] = None
    redshift_quality: int = 0
    n_spectra: int = 0
    programs: List[str] = dc_field(default_factory=list)
    tags: List[str] = dc_field(default_factory=list)
    max_snr: Optional[float] = None
    max_exposure_time: Optional[float] = None
    has_photometry: bool = False
    photo_z: Optional[float] = None
    photo_z_err_lo: Optional[float] = None
    photo_z_err_hi: Optional[float] = None
    spectra: SpectrumCollection = dc_field(
        default_factory=lambda: SpectrumCollection([]), repr=False
    )
    photometry: Optional[Photometry] = dc_field(default=None, repr=False)

    def __repr__(self) -> str:
        z = f"z={self.redshift:.4f}, " if self.redshift is not None else ""
        gratings = ", ".join(self.spectra.gratings) if self.spectra else ""
        parts = [
            f"Object({self.object_id}, {z}{self.field})",
            f"  {self.n_spectra} spectra ({gratings})",
        ]
        if self.tags:
            parts.append(f"  tags: {', '.join(self.tags)}")
        if self.photometry:
            parts.append(f"  {self.photometry!r}")
        return "\n".join(parts)

    @classmethod
    def from_dict(
        cls,
        d: dict,
        opener: Optional[Callable] = None,
        photometry_record: Optional[dict] = None,
    ) -> "Object":
        """Build from a store object dict (with embedded spectra list).

        Parameters
        ----------
        d : dict
            Row from ``LocalStore.get_object()`` / ``query_objects()`` with a
            ``spectra`` list of per-spectrum dicts.
        opener : callable, optional
            Callable ``(spectrum_id) -> SpectrumData`` used for
            ``Spectrum.open()``. Typically ``client.open_spectrum``.
        photometry_record : dict, optional
            Photometry row from ``LocalStore.query_photometry()``; pass
            ``None`` to leave :attr:`photometry` unset.
        """
        spectra_dicts = d.get("spectra") or []
        obj_id = d.get("object_id", "")

        spectra = [
            Spectrum(
                spectrum_id=s.get("spectrum_id") or "",
                object_id=s.get("object_id") or obj_id,
                grating=s.get("grating", ""),
                signal_to_noise=s.get("signal_to_noise"),
                exposure_time=s.get("exposure_time"),
                reduction_version=s.get("reduction_version"),
                redshift_auto=s.get("redshift_auto"),
                dq_flags=s.get("dq_flags") or 0,
                fits_path=s.get("fits_path"),
                local_path=s.get("local_path"),
                _opener=opener,
            )
            for s in spectra_dicts
        ]

        photometry = Photometry.from_record(photometry_record) if photometry_record else None

        return cls(
            object_id=obj_id,
            ra=d.get("ra", 0.0) or 0.0,
            dec=d.get("dec", 0.0) or 0.0,
            field=d.get("field", "") or "",
            redshift=d.get("redshift"),
            redshift_auto=d.get("redshift_auto"),
            redshift_inspected=d.get("redshift_inspected"),
            redshift_quality=d.get("redshift_quality", 0) or 0,
            n_spectra=d.get("n_spectra") or len(spectra),
            programs=list(d.get("programs") or []),
            tags=list(d.get("tags") or []),
            max_snr=d.get("max_snr"),
            max_exposure_time=d.get("max_exposure_time"),
            has_photometry=bool(d.get("has_photometry")),
            photo_z=d.get("photo_z"),
            photo_z_err_lo=d.get("photo_z_err_lo"),
            photo_z_err_hi=d.get("photo_z_err_hi"),
            spectra=SpectrumCollection(spectra),
            photometry=photometry,
        )
