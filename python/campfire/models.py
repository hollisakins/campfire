"""Data models for the CAMPFIRE Python client."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class SpectrumData:
    """Container for a 1D extracted spectrum.

    Provides clean attribute access to wavelength, flux, and error arrays
    extracted from a CAMPFIRE FITS file.

    Attributes
    ----------
    wavelength : np.ndarray
        Wavelength array in microns.
    flux : np.ndarray
        Flux density (f_nu) in microjansky.
    flux_err : np.ndarray
        Flux density error in microjansky.
    flam : np.ndarray or None
        Flux density (f_lambda) in erg/s/cm2/A, if available.
    flam_err : np.ndarray or None
        Flux density error (f_lambda), if available.
    header : dict
        FITS primary header as a dict.
    grating : str
        Grating name (e.g., 'PRISM', 'G395M').
    spectrum_id : str
        Stable per-spectrum identifier derived from the FITS path
        (e.g., 'ember_cosmos_p1_prism_clear_920424').
    fits_path : str or None
        Local file path if loaded from disk, None if from API.
    """

    wavelength: np.ndarray
    flux: np.ndarray
    flux_err: np.ndarray
    header: dict
    grating: str
    spectrum_id: str
    flam: Optional[np.ndarray] = field(default=None, repr=False)
    flam_err: Optional[np.ndarray] = field(default=None, repr=False)
    fits_path: Optional[str] = None

    def __repr__(self) -> str:
        n = len(self.wavelength)
        wmin = self.wavelength.min() if n > 0 else 0
        wmax = self.wavelength.max() if n > 0 else 0
        return (
            f"SpectrumData({self.spectrum_id}, {self.grating}, "
            f"{n} pixels, {wmin:.2f}-{wmax:.2f} μm)"
        )

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
                    flux = np.array(data["fnu"], dtype=float)
                elif "flux" in col_names:
                    flux = np.array(data["flux"], dtype=float)
                else:
                    flux = np.array(data.field(1), dtype=float)

                if "fnu_err" in col_names:
                    flux_err = np.array(data["fnu_err"], dtype=float)
                elif "flux_err" in col_names:
                    flux_err = np.array(data["flux_err"], dtype=float)
                elif "err" in col_names:
                    flux_err = np.array(data["err"], dtype=float)
                else:
                    flux_err = np.zeros_like(flux)

                flam = None
                if "flam" in col_names:
                    flam = np.array(data["flam"], dtype=float)

                flam_err = None
                if "flam_err" in col_names:
                    flam_err = np.array(data["flam_err"], dtype=float)

            else:
                if data.ndim == 2 and data.shape[0] >= 2:
                    wavelength = np.array(data[0], dtype=float)
                    flux = np.array(data[1], dtype=float)
                    flux_err = np.array(data[2], dtype=float) if data.shape[0] > 2 else np.zeros_like(flux)
                else:
                    wavelength = np.arange(len(data), dtype=float)
                    flux = np.array(data, dtype=float)
                    flux_err = np.zeros_like(flux)
                flam = None
                flam_err = None

            if not spectrum_id:
                filename = header.get("FILENAME", Path(fits_path).name)
                spectrum_id = cls._parse_spectrum_id_from_filename(filename)
            if not grating:
                grating = header.get("GRATING", "unknown")

        return cls(
            wavelength=wavelength,
            flux=flux,
            flux_err=flux_err,
            flam=flam,
            flam_err=flam_err,
            header=header,
            grating=grating,
            spectrum_id=spectrum_id,
            fits_path=fits_path,
        )
