"""Data models for the CAMPFIRE Python client."""

from dataclasses import dataclass
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
    header : dict
        FITS primary header as a dict.
    grating : str
        Grating name (e.g., 'PRISM', 'G395M').
    object_id : str
        CAMPFIRE object ID.
    fits_path : str or None
        Local file path if loaded from disk, None if from API.
    """

    wavelength: np.ndarray
    flux: np.ndarray
    flux_err: np.ndarray
    header: dict
    grating: str
    object_id: str
    fits_path: Optional[str] = None

    def __repr__(self) -> str:
        n = len(self.wavelength)
        wmin = self.wavelength.min() if n > 0 else 0
        wmax = self.wavelength.max() if n > 0 else 0
        return (
            f"SpectrumData({self.object_id}, {self.grating}, "
            f"{n} pixels, {wmin:.2f}-{wmax:.2f} μm)"
        )

    @classmethod
    def from_fits(cls, fits_path: str, object_id: str = "", grating: str = "") -> "SpectrumData":
        """Create a SpectrumData from a local FITS file.

        Parameters
        ----------
        fits_path : str
            Path to the FITS file.
        object_id : str, optional
            Object ID (inferred from header if not provided).
        grating : str, optional
            Grating name (inferred from header if not provided).

        Returns
        -------
        SpectrumData
        """
        from astropy.io import fits

        with fits.open(fits_path) as hdul:
            header = dict(hdul[0].header)

            # Try to find the spectrum data — CAMPFIRE FITS layout:
            # HDU 0: Primary header
            # HDU 1: 1D extracted spectrum (WAVELENGTH, FLUX, FLUX_ERR columns or arrays)
            if len(hdul) > 1:
                data = hdul[1].data
                if hasattr(data, "columns"):
                    # Table HDU with named columns
                    col_names = [c.name.upper() for c in data.columns]
                    if "WAVELENGTH" in col_names:
                        wavelength = np.array(data["WAVELENGTH"], dtype=float)
                    elif "WAVE" in col_names:
                        wavelength = np.array(data["WAVE"], dtype=float)
                    else:
                        wavelength = np.array(data.field(0), dtype=float)

                    if "FLUX" in col_names:
                        flux = np.array(data["FLUX"], dtype=float)
                    elif "FNU" in col_names:
                        flux = np.array(data["FNU"], dtype=float)
                    else:
                        flux = np.array(data.field(1), dtype=float)

                    if "FLUX_ERR" in col_names:
                        flux_err = np.array(data["FLUX_ERR"], dtype=float)
                    elif "FNU_ERR" in col_names:
                        flux_err = np.array(data["FNU_ERR"], dtype=float)
                    elif "ERR" in col_names:
                        flux_err = np.array(data["ERR"], dtype=float)
                    else:
                        flux_err = np.zeros_like(flux)
                else:
                    # Image HDU — assume rows are [wave, flux, err]
                    if data.ndim == 2 and data.shape[0] >= 2:
                        wavelength = np.array(data[0], dtype=float)
                        flux = np.array(data[1], dtype=float)
                        flux_err = np.array(data[2], dtype=float) if data.shape[0] > 2 else np.zeros_like(flux)
                    else:
                        wavelength = np.arange(len(data), dtype=float)
                        flux = np.array(data, dtype=float)
                        flux_err = np.zeros_like(flux)
            else:
                raise ValueError(f"FITS file has no data extensions: {fits_path}")

            # Infer metadata from header if not provided
            if not object_id:
                object_id = header.get("OBJECT", header.get("SRCNAME", "unknown"))
            if not grating:
                grating = header.get("GRATING", header.get("FILTER", "unknown"))

        return cls(
            wavelength=wavelength,
            flux=flux,
            flux_err=flux_err,
            header=header,
            grating=grating,
            object_id=object_id,
            fits_path=fits_path,
        )
