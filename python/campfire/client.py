"""Main CAMPFIRE API client."""

import os
from pathlib import Path
from typing import Optional, List, Union, Tuple
import requests
from astropy.table import Table
from astropy.io import fits
from tqdm import tqdm

from .exceptions import (
    AuthenticationError,
    NotFoundError,
    DownloadError,
    ValidationError,
    APIError,
)

__version__ = "0.1.0"


class Campfire:
    """
    CAMPFIRE Python API Client.

    Query and download NIRSpec spectroscopic data from the CAMPFIRE archive.

    Parameters
    ----------
    api_key : str, optional
        API key for authentication. If not provided, reads from CAMPFIRE_API_KEY
        environment variable.
    base_url : str, optional
        Base URL for the API. Defaults to production CAMPFIRE server.

    Examples
    --------
    >>> from campfire import Campfire
    >>> cf = Campfire(api_key='sk_live_...')
    >>> results = cf.query_objects(programs=['EMBER-UDS'], redshift_range=(2.0, 4.0))
    >>> print(f"Found {len(results)} objects")
    """

    DEFAULT_BASE_URL = "https://campfire.vercel.app/api/v1"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        """Initialize the CAMPFIRE client."""
        self.api_key = api_key or os.environ.get("CAMPFIRE_API_KEY")
        if not self.api_key:
            raise AuthenticationError(
                "API key required. Provide via api_key parameter or CAMPFIRE_API_KEY environment variable."
            )

        if not self.api_key.startswith("sk_"):
            raise ValidationError(
                "Invalid API key format. Keys should start with 'sk_'"
            )

        self.base_url = base_url or self.DEFAULT_BASE_URL

        # Create session with auth header
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": f"campfire-python/{__version__}",
            }
        )

    def query_objects(
        self,
        programs: Optional[List[Union[int, str]]] = None,
        fields: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[int]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        spectral_features: Optional[int] = None,
        object_flags: Optional[int] = None,
        dq_flags: Optional[int] = None,
        inspected_only: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        limit: int = 1000,
        offset: int = 0,
        sort: str = "object_id",
        sort_dir: str = "asc",
    ) -> Table:
        """
        Query objects with filters.

        Parameters
        ----------
        programs : list of int or str, optional
            Program IDs or names to filter by.
        fields : list of str, optional
            Field names (e.g., ['COSMOS', 'UDS']).
        gratings : list of str, optional
            Grating names (e.g., ['PRISM', 'G395M']).
        observations : list of str, optional
            Observation names.
        redshift_range : tuple of (float, float), optional
            (min, max) redshift range.
        redshift_quality : list of int, optional
            Quality codes to include.
        max_snr_range : tuple of (float, float), optional
            (min, max) maximum SNR range.
        spectral_features : int, optional
            Bit mask for spectral features.
        object_flags : int, optional
            Bit mask for object flags.
        dq_flags : int, optional
            Bit mask for DQ flags.
        inspected_only : bool, optional
            If True, only return visually inspected objects.
        search : str, optional
            Text search on object_id.
        cone_search : tuple of (ra, dec, radius), optional
            (ra, dec, radius) in degrees, arcsec, arcsec for cone search.
        limit : int, optional
            Maximum number of results (default: 1000).
        offset : int, optional
            Pagination offset (default: 0).
        sort : str, optional
            Sort column (default: 'object_id').
        sort_dir : str, optional
            Sort direction: 'asc' or 'desc' (default: 'asc').

        Returns
        -------
        astropy.table.Table
            Table of matching objects with columns: object_id, ra, dec, redshift, etc.

        Examples
        --------
        >>> cf = Campfire()
        >>> # Query high-z galaxies with good redshift quality
        >>> results = cf.query_objects(
        ...     redshift_range=(3.0, 6.0),
        ...     redshift_quality=[2, 3],
        ...     inspected_only=True
        ... )
        >>> # Cone search around coordinates
        >>> nearby = cf.query_objects(
        ...     cone_search=(150.0, 2.5, 5.0)  # RA, Dec, radius in arcsec
        ... )
        """
        # Build query parameters
        params = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "sort_dir": sort_dir,
        }

        if programs:
            # Convert program names to IDs if needed (just pass as-is for now)
            params["programs"] = ",".join(str(p) for p in programs)

        if fields:
            params["fields"] = ",".join(fields)

        if gratings:
            params["gratings"] = ",".join(gratings)

        if observations:
            params["observations"] = ",".join(observations)

        if redshift_range:
            params["redshift_min"] = redshift_range[0]
            params["redshift_max"] = redshift_range[1]

        if max_snr_range:
            params["max_snr_min"] = max_snr_range[0]
            params["max_snr_max"] = max_snr_range[1]

        if redshift_quality:
            params["redshift_quality"] = ",".join(str(q) for q in redshift_quality)

        if spectral_features is not None:
            params["spectral_features"] = spectral_features

        if object_flags is not None:
            params["object_flags"] = object_flags

        if dq_flags is not None:
            params["dq_flags"] = dq_flags

        if inspected_only is not None:
            params["inspected_only"] = "true" if inspected_only else "false"

        if search:
            params["search"] = search

        if cone_search:
            ra, dec, radius_arcsec = cone_search
            params["ra"] = ra
            params["dec"] = dec
            params["radius"] = radius_arcsec

        # Make request
        url = f"{self.base_url}/objects"
        response = self.session.get(url, params=params)

        if response.status_code == 401:
            raise AuthenticationError("Invalid or expired API key")
        elif response.status_code == 403:
            raise AuthenticationError("Access denied")
        elif response.status_code != 200:
            raise Exception(f"API error: {response.status_code} - {response.text}")

        data = response.json()
        objects = data.get("data", [])

        # Convert to astropy Table
        if len(objects) == 0:
            return Table()

        return Table(rows=objects)

    def download_spectrum(
        self,
        fits_path: str,
        output_path: Optional[Union[str, Path]] = None,
        overwrite: bool = False,
        show_progress: bool = True,
    ) -> str:
        """
        Download a FITS spectrum file.

        Parameters
        ----------
        fits_path : str
            FITS file path from the objects query result (e.g., from spectra['fits_path']).
        output_path : str or Path, optional
            Local path to save the file. If not provided, uses the basename from fits_path.
        overwrite : bool, optional
            If True, overwrite existing file (default: False).
        show_progress : bool, optional
            If True, show download progress bar (default: True).

        Returns
        -------
        str
            Path to the downloaded file.

        Examples
        --------
        >>> cf = Campfire()
        >>> results = cf.query_objects(search='ember_uds_p4_123')
        >>> # Download all spectra for this object
        >>> for spectrum in results[0]['spectra']:
        ...     path = cf.download_spectrum(spectrum['fits_path'])
        ...     print(f"Downloaded to {path}")
        """
        # Default output path
        if output_path is None:
            output_path = Path(fits_path).name

        output_path = Path(output_path)

        # Check if file already exists
        if output_path.exists() and not overwrite:
            if show_progress:
                print(f"File already exists: {output_path} (use overwrite=True to replace)")
            return str(output_path)

        # Get signed URL from API
        url = f"{self.base_url}/spectra"
        params = {"path": fits_path}

        response = self.session.get(url, params=params)

        if response.status_code == 401:
            raise AuthenticationError("Invalid or expired API key")
        elif response.status_code == 403:
            raise AuthenticationError("Access denied to this file")
        elif response.status_code == 404:
            raise NotFoundError(f"File not found: {fits_path}")
        elif response.status_code != 200:
            raise DownloadError(
                f"Failed to get download URL: {response.status_code} - {response.text}"
            )

        data = response.json()
        signed_url = data.get("url")

        if not signed_url:
            raise DownloadError("No download URL returned from API")

        # Download the file from R2
        try:
            with requests.get(signed_url, stream=True) as r:
                r.raise_for_status()
                total_size = int(r.headers.get("content-length", 0))

                # Create parent directories if needed
                output_path.parent.mkdir(parents=True, exist_ok=True)

                with open(output_path, "wb") as f:
                    if show_progress and total_size > 0:
                        with tqdm(
                            total=total_size,
                            unit="B",
                            unit_scale=True,
                            desc=output_path.name,
                        ) as pbar:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                                pbar.update(len(chunk))
                    else:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)

        except requests.exceptions.RequestException as e:
            raise DownloadError(f"Download failed: {e}")

        return str(output_path)

    def download_spectra(
        self,
        object_ids: Union[str, List[str]] = None,
        table: Optional[Table] = None,
        download_dir: Union[str, Path] = ".",
        gratings: Optional[List[str]] = None,
        overwrite: bool = False,
        show_progress: bool = True,
    ) -> dict:
        """
        Download multiple spectra.

        Parameters
        ----------
        object_ids : str or list of str, optional
            Object ID(s) to download. Must also provide `table` parameter.
        table : astropy.table.Table, optional
            Table from query_objects() containing spectra to download.
        download_dir : str or Path, optional
            Directory to save files (default: current directory).
        gratings : list of str, optional
            Only download specific gratings (e.g., ['PRISM']).
        overwrite : bool, optional
            If True, overwrite existing files (default: False).
        show_progress : bool, optional
            If True, show download progress (default: True).

        Returns
        -------
        dict
            Dictionary mapping object_id to dict of {grating: filepath}.

        Examples
        --------
        >>> cf = Campfire()
        >>> results = cf.query_objects(programs=['EMBER-UDS'], limit=10)
        >>> # Download all spectra
        >>> paths = cf.download_spectra(table=results, download_dir='./spectra/')
        >>> # Download only PRISM spectra
        >>> paths = cf.download_spectra(table=results, gratings=['PRISM'])
        """
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        if table is None:
            raise ValidationError("Must provide 'table' parameter")

        # Filter by object_ids if provided
        if object_ids is not None:
            if isinstance(object_ids, str):
                object_ids = [object_ids]
            table = table[table["object_id"].isin(object_ids)]

        results = {}

        for row in table:
            object_id = row["object_id"]
            spectra = row.get("spectra", [])

            if not spectra:
                continue

            results[object_id] = {}

            for spectrum in spectra:
                grating = spectrum["grating"]

                # Skip if grating filter is active and doesn't match
                if gratings and grating not in gratings:
                    continue

                fits_path = spectrum["fits_path"]
                filename = Path(fits_path).name
                output_path = download_dir / filename

                try:
                    downloaded_path = self.download_spectrum(
                        fits_path,
                        output_path=output_path,
                        overwrite=overwrite,
                        show_progress=show_progress,
                    )
                    results[object_id][grating] = downloaded_path
                except Exception as e:
                    if show_progress:
                        print(f"Failed to download {fits_path}: {e}")

        return results

    # -------------------------------------------------------------------------
    # Metadata Methods
    # -------------------------------------------------------------------------

    def get_metadata(self) -> dict:
        """
        Get all available metadata in a single call.

        Returns
        -------
        dict
            Dictionary with keys: programs, fields, gratings, observations.

        Examples
        --------
        >>> cf = Campfire()
        >>> meta = cf.get_metadata()
        >>> print(meta['fields'])
        ['COSMOS', 'UDS', ...]
        """
        url = f"{self.base_url}/metadata"
        response = self.session.get(url)

        if response.status_code == 401:
            raise AuthenticationError("Invalid or expired API key")
        elif response.status_code != 200:
            raise APIError(f"API error: {response.status_code} - {response.text}")

        return response.json()

    def get_programs(self) -> Table:
        """
        List available programs with metadata.

        Returns
        -------
        astropy.table.Table
            Table with columns: program_id, program_name, pi_name, is_public.

        Examples
        --------
        >>> cf = Campfire()
        >>> programs = cf.get_programs()
        >>> print(programs)
        """
        metadata = self.get_metadata()
        programs = metadata.get("programs", [])

        if len(programs) == 0:
            return Table()

        return Table(rows=programs)

    def get_fields(self) -> List[str]:
        """
        List available field names.

        Returns
        -------
        list of str
            List of field names (e.g., ['COSMOS', 'UDS']).
        """
        metadata = self.get_metadata()
        return metadata.get("fields", [])

    def get_gratings(self) -> List[str]:
        """
        List available grating types.

        Returns
        -------
        list of str
            List of grating names (e.g., ['PRISM', 'G395M']).
        """
        metadata = self.get_metadata()
        return metadata.get("gratings", [])

    def get_observations(self) -> List[str]:
        """
        List available observation names.

        Returns
        -------
        list of str
            List of observation names (e.g., ['ember_uds_p4']).
        """
        metadata = self.get_metadata()
        return metadata.get("observations", [])

    # -------------------------------------------------------------------------
    # Spectrum Data Methods (for plotting)
    # -------------------------------------------------------------------------

    def get_spectrum_data(
        self,
        object_id: str,
        grating: str,
    ) -> dict:
        """
        Fetch spectrum JSON data for plotting.

        Parameters
        ----------
        object_id : str
            Object ID to fetch spectrum for.
        grating : str
            Grating type (e.g., 'PRISM', 'G395M').

        Returns
        -------
        dict
            Spectrum data with keys: wave, fnu, fnu_err, snr_2d, n_spatial,
            n_wave, profile, profile_fit, profile_pix.

        Examples
        --------
        >>> cf = Campfire()
        >>> data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')
        >>> # Use with plotting module
        >>> from campfire.plotting import plot_spectrum
        >>> fig = plot_spectrum(data, redshift=2.5)
        """
        url = f"{self.base_url}/spectrum"
        params = {"object_id": object_id, "grating": grating}

        response = self.session.get(url, params=params)

        if response.status_code == 401:
            raise AuthenticationError("Invalid or expired API key")
        elif response.status_code == 403:
            raise AuthenticationError("Access denied to this object")
        elif response.status_code == 404:
            raise NotFoundError(f"No {grating} spectrum found for {object_id}")
        elif response.status_code != 200:
            raise APIError(f"API error: {response.status_code} - {response.text}")

        return response.json()

    def get_redshift_fit_data(
        self,
        object_id: str,
        grating: str,
    ) -> dict:
        """
        Fetch redshift fitting results for plotting.

        Parameters
        ----------
        object_id : str
            Object ID to fetch fit for.
        grating : str
            Grating type (e.g., 'PRISM', 'G395M').

        Returns
        -------
        dict
            Fit data with keys: redshift, chi2_min, confidence, z_grid,
            chi2_grid, model_wave, model_fnu.

        Examples
        --------
        >>> cf = Campfire()
        >>> fit_data = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
        >>> print(f"Best-fit redshift: z={fit_data['redshift']:.4f}")
        """
        url = f"{self.base_url}/redshift-fit"
        params = {"object_id": object_id, "grating": grating}

        response = self.session.get(url, params=params)

        if response.status_code == 401:
            raise AuthenticationError("Invalid or expired API key")
        elif response.status_code == 403:
            raise AuthenticationError("Access denied to this object")
        elif response.status_code == 404:
            raise NotFoundError(
                f"No redshift fit data found for {object_id} ({grating})"
            )
        elif response.status_code != 200:
            raise APIError(f"API error: {response.status_code} - {response.text}")

        return response.json()
