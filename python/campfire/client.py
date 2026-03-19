"""Main CAMPFIRE API client."""

import logging
import tempfile
from pathlib import Path
from typing import Iterator, Optional, List, Union, Tuple

import requests
from astropy.table import Table
from tqdm import tqdm

from .api.session import APISession
from .api.client import APIClient
from .exceptions import (
    AuthenticationError,
    NotFoundError,
    DownloadError,
    ValidationError,
    APIError,
)
from .flags import (
    FlagQuery,
    QueryableFlag,
    SpectralFeatures,
    ObjectFlags,
    DQFlags,
    parse_flag_input,
)
from .models import SpectrumData

__version__ = "0.2.0"

logger = logging.getLogger(__name__)


class Campfire:
    """
    CAMPFIRE Python API Client.

    Query and download NIRSpec spectroscopic data from the CAMPFIRE archive.

    When locally synced data is available (from ``campfire sync``), queries
    are served from the local SQLite database for speed. Otherwise, falls
    back to the remote API.

    Authentication uses stored credentials from 'campfire login'. Run one of:
    - ``campfire login`` for browser-based authentication (recommended)
    - ``campfire login --api-key`` to paste an API key (for headless systems)

    Parameters
    ----------
    base_url : str, optional
        Base URL for the API. If not provided, uses CAMPFIRE_API_URL environment
        variable or defaults to production CAMPFIRE server.
    data_dir : str or Path, optional
        Path to the local data directory. If not provided, auto-detected from
        ``~/.campfire/config.toml``.
    auto_refresh : bool, optional
        If True (default), automatically refresh OAuth tokens when they expire.

    Examples
    --------
    >>> # First, authenticate via command line:
    >>> # $ campfire login
    >>>
    >>> # Then use the client:
    >>> from campfire import Campfire
    >>> cf = Campfire()
    >>> results = cf.query_objects(programs=['EMBER-UDS'], redshift_range=(2.0, 4.0))
    """

    DEFAULT_BASE_URL = "https://campfire.hollisakins.com/api/v1"

    def __init__(
        self,
        base_url: Optional[str] = None,
        data_dir: Optional[Union[str, Path]] = None,
        auto_refresh: bool = True,
    ):
        """Initialize the CAMPFIRE client."""
        # Create shared API session and client
        self._api_session = APISession(base_url=base_url, auto_refresh=auto_refresh)
        self._api = APIClient(self._api_session)
        self.base_url = self._api_session.base_url

        # Detect local data store
        self._local = None
        self._local_data_dir: Optional[Path] = None
        self._local_logged = False  # For one-time "Using local catalog" message

        resolved_dir = self._resolve_data_dir(data_dir)
        if resolved_dir:
            db_path = resolved_dir / ".campfire_meta" / "campfire.db"
            if db_path.exists():
                from .db.store import LocalStore
                self._local = LocalStore(db_path)
                self._local_data_dir = resolved_dir

    @staticmethod
    def _resolve_data_dir(data_dir: Optional[Union[str, Path]]) -> Optional[Path]:
        """Resolve the local data directory from argument, config, or default."""
        if data_dir:
            return Path(data_dir).expanduser()
        try:
            from .config import Config
            config = Config()
            if config.exists():
                return config.data_dir
        except Exception:
            pass
        return None

    def _log_local_use(self) -> None:
        """Log a one-time message when using local data."""
        if self._local_logged or self._local is None:
            return
        self._local_logged = True
        last = self._local.get_last_synced_at()
        if last:
            from datetime import datetime, timezone
            try:
                synced = datetime.fromisoformat(last.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - synced
                if delta.days > 0:
                    ago = f"{delta.days}d ago"
                elif delta.seconds > 3600:
                    ago = f"{delta.seconds // 3600}h ago"
                else:
                    ago = f"{delta.seconds // 60}m ago"
                logger.info(f"Using local catalog (last synced {ago})")
            except (ValueError, TypeError):
                logger.info("Using local catalog")
        else:
            logger.info("Using local catalog")

    @property
    def is_local(self) -> bool:
        """Whether a local data store is available."""
        return self._local is not None

    @property
    def last_synced(self) -> Optional[str]:
        """Timestamp of last sync, or None if no local data."""
        if self._local:
            return self._local.get_last_synced_at()
        return None

    @staticmethod
    def _flag_to_dict(flag_input, flag_class):
        """Convert a flag input to a dict for local query."""
        query = parse_flag_input(flag_input, flag_class)
        if query is None:
            return None
        return {
            "include_any": query.include_any,
            "include_all": query.include_all,
            "exclude": query.exclude,
        }

    def query_objects(
        self,
        programs: Optional[List[Union[int, str]]] = None,
        fields: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[int]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        spectral_features: Optional[
            Union[int, str, List[str], SpectralFeatures, FlagQuery]
        ] = None,
        object_flags: Optional[
            Union[int, str, List[str], ObjectFlags, FlagQuery]
        ] = None,
        dq_flags: Optional[Union[int, str, List[str], DQFlags, FlagQuery]] = None,
        inspected_only: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        limit: int = 1000,
        offset: int = 0,
        sort: str = "object_id",
        sort_dir: str = "asc",
        remote: bool = False,
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
        spectral_features : int, str, list, SpectralFeatures, or FlagQuery, optional
            Filter by spectral features. Accepts:
            - int: Legacy bitmask (match any)
            - str: Single flag name
            - list of str: Multiple flag names (match any)
            - SpectralFeatures: Single flag enum
            - FlagQuery: Complex query with |, &, ~ operators
        object_flags : int, str, list, ObjectFlags, or FlagQuery, optional
            Filter by object flags. Same input types as spectral_features.
        dq_flags : int, str, list, DQFlags, or FlagQuery, optional
            Filter by data quality flags. Same input types as spectral_features.
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
        >>> from campfire.flags import ObjectFlags, DQFlags, SpectralFeatures
        >>> cf = Campfire()
        >>>
        >>> # Query high-z galaxies with good redshift quality
        >>> results = cf.query_objects(
        ...     redshift_range=(3.0, 6.0),
        ...     redshift_quality=[2, 3],
        ...     inspected_only=True
        ... )
        >>>
        >>> # Filter by flags using numpy-style operators
        >>> # Has LRD OR LAE, but NOT broad line
        >>> results = cf.query_objects(
        ...     object_flags=(ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
        ... )
        >>>
        >>> # Simple string-based filtering (like web app)
        >>> results = cf.query_objects(object_flags=['LRD', 'LYA_EMITTER'])
        >>>
        >>> # Exclude contaminated objects
        >>> results = cf.query_objects(dq_flags=~DQFlags.CONTAMINATION)
        """
        # Determine whether to use local store
        use_local = False
        if self._local and not remote:
            # Check if the requested observations are all available locally
            if observations:
                synced_obs = set(self._local.get_synced_observations())
                use_local = all(o in synced_obs for o in observations)
            else:
                # No observation filter — use local if we have any data
                synced_obs = self._local.get_synced_observations()
                use_local = len(synced_obs) > 0

        if use_local:
            self._log_local_use()
            # Convert flag inputs to dicts for local query
            sf_dict = self._flag_to_dict(spectral_features, SpectralFeatures)
            of_dict = self._flag_to_dict(object_flags, ObjectFlags)
            dq_dict = self._flag_to_dict(dq_flags, DQFlags)

            objects = self._local.query_objects(
                programs=programs,
                fields=fields,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                spectral_features=sf_dict,
                object_flags=of_dict,
                dq_flags=dq_dict,
                inspected_only=inspected_only,
                search=search,
                cone_search=cone_search,
                sort=sort,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        else:
            objects, pagination = self._api.query_objects(
                programs=programs,
                fields=fields,
                gratings=gratings,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                spectral_features=spectral_features,
                object_flags=object_flags,
                dq_flags=dq_flags,
                inspected_only=inspected_only,
                search=search,
                cone_search=cone_search,
                limit=limit,
                offset=offset,
                sort=sort,
                sort_dir=sort_dir,
            )

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
        # Check if file is available locally via sync
        if self._local and self._local_data_dir:
            # Try to find by fits_path in the spectra table
            rows = self._local._conn.execute(
                "SELECT local_path FROM spectra WHERE fits_path = ? AND local_path IS NOT NULL",
                (fits_path,),
            ).fetchall()
            if rows:
                local_file = self._local_data_dir / rows[0][0]
                if local_file.exists():
                    if output_path is None:
                        return str(local_file)
                    # Copy to requested output_path
                    output_path = Path(output_path)
                    if not output_path.exists() or overwrite:
                        import shutil
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(local_file, output_path)
                    return str(output_path)

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
        signed_url = self._api.get_signed_url(fits_path)

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
            Object ID(s) to download. Must also provide ``table`` parameter.
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
        return self._api.get_metadata()

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
        metadata = self._api.get_metadata()
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
        metadata = self._api.get_metadata()
        return metadata.get("fields", [])

    def get_gratings(self) -> List[str]:
        """
        List available grating types.

        Returns
        -------
        list of str
            List of grating names (e.g., ['PRISM', 'G395M']).
        """
        metadata = self._api.get_metadata()
        return metadata.get("gratings", [])

    def get_observations(self) -> List[str]:
        """
        List available observation names.

        Returns
        -------
        list of str
            List of observation names (e.g., ['ember_uds_p4']).
        """
        metadata = self._api.get_metadata()
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
        return self._api.get_spectrum_data(object_id, grating)

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
        return self._api.get_redshift_fit_data(object_id, grating)

    # -------------------------------------------------------------------------
    # Spectrum Access Methods
    # -------------------------------------------------------------------------

    def open_spectrum(
        self,
        object_id: str,
        grating: str,
    ) -> SpectrumData:
        """
        Open a spectrum as a SpectrumData object.

        Checks for locally synced FITS files first. If the file exists
        locally, opens it directly. Otherwise, downloads via the API to
        a temporary location.

        Parameters
        ----------
        object_id : str
            Object ID.
        grating : str
            Grating type (e.g., 'PRISM', 'G395M').

        Returns
        -------
        SpectrumData
            Spectrum with .wavelength, .flux, .flux_err, .header attributes.

        Examples
        --------
        >>> cf = Campfire()
        >>> spec = cf.open_spectrum('ember_uds_p4_123456', 'PRISM')
        >>> print(spec.wavelength.shape, spec.flux.shape)
        """
        # Check local store first
        if self._local and self._local_data_dir:
            local_path = self._local.find_local_path(object_id, grating)
            if local_path:
                full_path = self._local_data_dir / local_path
                if full_path.exists():
                    return SpectrumData.from_fits(
                        str(full_path), object_id=object_id, grating=grating
                    )

        # Need to find the fits_path for this object + grating
        fits_path = self._resolve_fits_path(object_id, grating)

        # Download to temp file
        signed_url = self._api.get_signed_url(fits_path)
        tmp_dir = Path(tempfile.mkdtemp(prefix="campfire_"))
        tmp_file = tmp_dir / Path(fits_path).name

        try:
            with requests.get(signed_url, stream=True) as r:
                r.raise_for_status()
                with open(tmp_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
        except requests.RequestException as e:
            raise DownloadError(f"Failed to download spectrum: {e}")

        return SpectrumData.from_fits(
            str(tmp_file), object_id=object_id, grating=grating
        )

    def _resolve_fits_path(self, object_id: str, grating: str) -> str:
        """Resolve the FITS path for an object + grating."""
        # Check local store first
        if self._local:
            spectra = self._local.get_spectra_for_object(object_id, grating)
            if spectra:
                return spectra[0]["fits_path"]

        # Fall back to API query
        objects, _ = self._api.query_objects(
            search=object_id, limit=1
        )
        if not objects:
            raise NotFoundError(f"Object not found: {object_id}")

        for spec in objects[0].get("spectra", []):
            if spec.get("grating") == grating:
                return spec["fits_path"]

        raise NotFoundError(f"No {grating} spectrum found for {object_id}")

    def iter_objects(self, **filters) -> Iterator[dict]:
        """
        Iterate over all matching objects with automatic pagination.

        Yields individual object dicts. When local data is available and
        covers the requested observations, iterates from SQLite. Otherwise,
        auto-paginates through the remote API.

        Parameters
        ----------
        **filters
            Same filters as ``query_objects()``. ``limit`` controls page
            size for remote queries (default 1000).

        Yields
        ------
        dict
            Individual object records.

        Examples
        --------
        >>> cf = Campfire()
        >>> for obj in cf.iter_objects(redshift_range=(2.0, 4.0)):
        ...     print(obj['object_id'], obj['redshift'])
        """
        remote = filters.pop("remote", False)

        if self._local and not remote:
            observations = filters.get("observations")
            use_local = False
            if observations:
                synced_obs = set(self._local.get_synced_observations())
                use_local = all(o in synced_obs for o in observations)
            else:
                use_local = len(self._local.get_synced_observations()) > 0

            if use_local:
                self._log_local_use()
                # Convert flag inputs for local query
                for flag_name, flag_class in [
                    ("spectral_features", SpectralFeatures),
                    ("object_flags", ObjectFlags),
                    ("dq_flags", DQFlags),
                ]:
                    if flag_name in filters:
                        filters[flag_name] = self._flag_to_dict(
                            filters[flag_name], flag_class
                        )
                # Local query — no pagination needed, SQLite handles it
                filters["limit"] = filters.get("limit", 999999)
                yield from self._local.query_objects(**filters)
                return

        # Remote auto-pagination
        yield from self._api.iter_objects(**filters)
