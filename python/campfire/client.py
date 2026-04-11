"""Main CAMPFIRE API client."""

import hashlib
import logging
from pathlib import Path
from typing import Iterator, Optional, List, Union, Tuple

import requests
from astropy.table import Table

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
    RedshiftQuality,
    SpectralFeatures,
    DQFlags,
    parse_flag_input,
)
from .models import SpectrumData

__version__ = "0.2.0"

logger = logging.getLogger(__name__)


def _safe_cache_path(cache_dir: Path, filename: str, target_id: str) -> Path:
    """Resolve a cache path and ensure it stays within cache_dir."""
    dest = (cache_dir / filename).resolve()
    if not str(dest).startswith(str(cache_dir.resolve())):
        raise ValueError(f"Invalid target_id produces unsafe cache path: {target_id!r}")
    return dest


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
        Root data directory (contains ``products/`` and ``meta/``). If not
        provided, uses ``$CAMPFIRE_ROOT`` or ``~/campfire``.
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
        self._products_dir: Optional[Path] = None
        self._meta_dir: Optional[Path] = None
        self._local_logged = False  # For one-time "Using local catalog" message
        self._api_download_count = 0  # Track consecutive API downloads for warning

        resolved_dir = self._resolve_data_dir(data_dir)
        if resolved_dir:
            meta_dir = resolved_dir / "meta"
            db_path = meta_dir / "campfire.db"
            if db_path.exists():
                from .db.store import LocalStore, SchemaMismatchError
                try:
                    self._local = LocalStore(db_path)
                except SchemaMismatchError:
                    # Stale schema — will be recreated on next sync()
                    self._local = None
                else:
                    self._products_dir = resolved_dir / "products"
                    self._meta_dir = meta_dir

    @staticmethod
    def _resolve_data_dir(data_dir: Optional[Union[str, Path]]) -> Optional[Path]:
        """Resolve the root data directory.

        Resolution order: explicit arg → $CAMPFIRE_ROOT → ~/campfire.
        Returns None if the resolved directory has no meta/campfire.db.
        """
        if data_dir:
            return Path(data_dir).expanduser()
        from .config import resolve_data_dir
        resolved = resolve_data_dir()
        if (resolved / "meta" / "campfire.db").exists():
            return resolved
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

    # -------------------------------------------------------------------------
    # Sync and Download
    # -------------------------------------------------------------------------

    def sync(self, show_progress: bool = False, full: bool = False) -> dict:
        """Sync the object/spectra catalog from the server.

        Equivalent to ``campfire sync``. On first call, fetches the full
        catalog. On subsequent calls, only fetches objects modified since
        the last sync (incremental). Use ``full=True`` to force a
        complete re-sync.

        Parameters
        ----------
        show_progress : bool
            Show a progress bar during fetch.
        full : bool
            Force a full sync, ignoring incremental cache.

        Returns
        -------
        dict
            Summary with keys: observations, objects, spectra, stale_count.

        Examples
        --------
        >>> cf = Campfire()
        >>> result = cf.sync()
        >>> print(f"Synced {result['objects']} objects")
        """
        from .sync import sync_metadata

        # Ensure data dir and store exist
        if self._meta_dir is None:
            from .config import resolve_data_dir, ensure_data_dir
            resolved = self._resolve_data_dir(None)
            if resolved is None:
                resolved = resolve_data_dir()
            ensure_data_dir(resolved)
            self._products_dir = resolved / "products"
            self._meta_dir = resolved / "meta"

        # Open store (create if needed, delete stale schema automatically)
        if self._local is None:
            from .db.store import LocalStore, SchemaMismatchError
            db_path = self._meta_dir / "campfire.db"
            try:
                self._local = LocalStore(db_path)
            except SchemaMismatchError:
                db_path.unlink(missing_ok=True)
                db_path.with_suffix(".db-wal").unlink(missing_ok=True)
                db_path.with_suffix(".db-shm").unlink(missing_ok=True)
                self._local = LocalStore(db_path)

        result = sync_metadata(
            self._api, self._local, self._meta_dir,
            show_progress=show_progress, full=full,
        )
        return result

    def download(
        self,
        observations: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        stale_only: bool = False,
        max_workers: int = 4,
        show_progress: bool = True,
    ) -> dict:
        """Download FITS files for matching spectra.

        Equivalent to ``campfire download``. Requires a prior ``sync()``
        to populate the local catalog.

        Parameters
        ----------
        observations : list of str, optional
            Download by observation name.
        programs : list of str, optional
            Download by program slug.
        fields : list of str, optional
            Download by field name.
        gratings : list of str, optional
            Filter by grating type.
        stale_only : bool, optional
            Re-download only files updated on the server.
        max_workers : int, optional
            Parallel download workers (default 4).
        show_progress : bool, optional
            Show progress bars (default True).

        Returns
        -------
        dict
            Summary with total downloaded, failed, and bytes.
        """
        from .sync import download_observation
        from .api.session import create_download_session

        if self._local is None:
            raise ValidationError("No local catalog. Run cf.sync() first.")

        # Determine target observations
        target_obs = set()

        if stale_only:
            stale_files = self._local.get_stale_files()
            target_obs = set(f["observation"] for f in stale_files)
            if not target_obs:
                return {"downloaded": 0, "failed": 0, "bytes": 0, "message": "All files up to date"}
        else:
            if observations:
                target_obs.update(observations)
            if programs:
                for prog in programs:
                    results = self._local.query_targets(programs=[prog], limit=999999)
                    target_obs.update(r["observation"] for r in results)
            if fields:
                for fld in fields:
                    results = self._local.query_targets(fields=[fld], limit=999999)
                    target_obs.update(r["observation"] for r in results)

            if not target_obs:
                raise ValidationError(
                    "Specify at least one of: observations, programs, fields, or stale_only=True"
                )

        dl_session = create_download_session(max_workers)
        total_downloaded = 0
        total_failed = 0
        total_bytes = 0

        for obs in sorted(target_obs):
            self._api_session._ensure_valid_token()
            stats = download_observation(
                self._api, obs, self._products_dir, self._local,
                max_workers=max_workers,
                download_session=dl_session,
                grating_filter=gratings,
            )
            total_downloaded += stats.get("downloaded", 0)
            total_failed += stats.get("failed", 0)
            total_bytes += stats.get("download_bytes", 0)

        return {
            "downloaded": total_downloaded,
            "failed": total_failed,
            "bytes": total_bytes,
        }

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

    def query_targets(
        self,
        programs: Optional[List[Union[int, str]]] = None,
        fields: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[Union[int, str]]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        spectral_features: Optional[
            Union[int, str, List[str], SpectralFeatures, FlagQuery]
        ] = None,
        dq_flags: Optional[Union[int, str, List[str], DQFlags, FlagQuery]] = None,
        lists: Optional[List[str]] = None,
        inspected_only: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        sort: str = "target_id",
        sort_dir: str = "asc",
        remote: bool = False,
    ) -> Table:
        """
        Query targets with filters.

        Parameters
        ----------
        programs : list of int or str, optional
            Program IDs or names to filter by.
        fields : list of str, optional
            Field names (e.g., ['COSMOS', 'UDS']). Case-insensitive.
        gratings : list of str, optional
            Grating names (e.g., ['PRISM', 'G395M']).
        observations : list of str, optional
            Observation names.
        redshift_range : tuple of (float, float), optional
            (min, max) redshift range.
        redshift_quality : list of int or str, optional
            Quality codes to include. Accepts integers (0-4) or strings
            ('tentative', 'probable', 'secure', etc.).
        max_snr_range : tuple of (float, float), optional
            (min, max) maximum SNR range.
        spectral_features : int, str, list, SpectralFeatures, or FlagQuery, optional
            Filter by spectral features. Accepts:
            - int: Legacy bitmask (match any)
            - str: Single flag name
            - list of str: Multiple flag names (match any)
            - SpectralFeatures: Single flag enum
            - FlagQuery: Complex query with |, &, ~ operators
        dq_flags : int, str, list, DQFlags, or FlagQuery, optional
            Filter by data quality flags. Same input types as spectral_features.
        lists : list of str, optional
            Filter by list slugs (e.g., ['lrd', 'blagn']).
            Returns targets whose parent object belongs to any of the given lists.
        inspected_only : bool, optional
            If True, only return visually inspected targets.
        search : str, optional
            Text search on target_id.
        cone_search : tuple of (ra, dec, radius), optional
            (ra, dec, radius) in degrees, arcsec, arcsec for cone search.
        limit : int, optional
            Maximum number of results. Default: no limit (local), 1000 (remote).
        offset : int, optional
            Pagination offset (default: 0).
        sort : str, optional
            Sort column (default: 'target_id').
        sort_dir : str, optional
            Sort direction: 'asc' or 'desc' (default: 'asc').

        Returns
        -------
        astropy.table.Table
            Table of matching targets with columns: target_id, ra, dec, redshift, etc.

        Examples
        --------
        >>> from campfire.flags import DQFlags, SpectralFeatures
        >>> cf = Campfire()
        >>>
        >>> # Query high-z galaxies with good redshift quality
        >>> results = cf.query_targets(
        ...     redshift_range=(3.0, 6.0),
        ...     redshift_quality=['probable', 'secure'],  # or [3, 4]
        ...     inspected_only=True
        ... )
        >>>
        >>> # Filter by list membership (replaces object_flags)
        >>> results = cf.query_targets(lists=['lrd', 'blagn'])
        >>>
        >>> # Exclude contaminated objects
        >>> results = cf.query_targets(dq_flags=~DQFlags.CONTAMINATION)
        """
        # Normalize inputs to match DB conventions
        if fields:
            fields = [f.lower() for f in fields]
        if gratings:
            gratings = [g.upper() for g in gratings]
        if observations:
            observations = [o.lower() for o in observations]
        if redshift_quality:
            redshift_quality = [
                int(RedshiftQuality(q)) if isinstance(q, str) else q
                for q in redshift_quality
            ]

        # Use local store when available (full catalog after sync)
        use_local = self._local is not None and not remote

        if use_local:
            self._log_local_use()
            # Convert flag inputs to dicts for local query
            sf_dict = self._flag_to_dict(spectral_features, SpectralFeatures)
            dq_dict = self._flag_to_dict(dq_flags, DQFlags)

            objects = self._local.query_targets(
                programs=programs,
                fields=fields,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                spectral_features=sf_dict,
                dq_flags=dq_dict,
                lists=lists,
                inspected_only=inspected_only,
                search=search,
                cone_search=cone_search,
                sort=sort,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        else:
            # Remote queries need a concrete limit for pagination
            remote_limit = limit if limit is not None else 1000
            objects, pagination = self._api.query_targets(
                programs=programs,
                fields=fields,
                gratings=gratings,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                spectral_features=spectral_features,
                dq_flags=dq_flags,
                lists=lists,
                inspected_only=inspected_only,
                search=search,
                cone_search=cone_search,
                limit=remote_limit,
                offset=offset,
                sort=sort,
                sort_dir=sort_dir,
            )

        if not use_local and pagination:
            total = pagination.get("total", 0)
            if total > len(objects):
                import warnings
                warnings.warn(
                    f"Query returned {len(objects)} of {total} matching objects. "
                    f"Use limit/offset to paginate, iter_objects() to stream all, "
                    f"or sync the catalog locally with cf.sync() for unlimited queries.",
                    stacklevel=2,
                )

        if len(objects) == 0:
            return Table()

        return Table(rows=objects)

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

        Checks for locally downloaded FITS files first. If not found
        locally, downloads from the API and caches in the managed data
        directory so subsequent calls are instant.

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
        if self._local and self._products_dir:
            local_path = self._local.find_local_path(object_id, grating)
            if local_path:
                full_path = self._products_dir / local_path
                if full_path.exists():
                    self._api_download_count = 0
                    return SpectrumData.from_fits(
                        str(full_path), object_id=object_id, grating=grating,
                    )

        # Warn if downloading many spectra one at a time
        self._api_download_count += 1
        if self._api_download_count == 3:
            import warnings
            warnings.warn(
                "Downloading spectra one at a time from the API. "
                "For bulk access, use cf.download() first, then open_spectrum() "
                "will read from local files.",
                stacklevel=2,
            )

        # Resolve spectrum metadata from store or API
        spec_info = self._resolve_spectrum_info(object_id, grating)
        fits_path = spec_info["fits_path"]

        # Download the file
        signed_url = self._api.get_signed_url(fits_path)
        filename = Path(fits_path).name

        # Determine where to save: managed data dir if available, else temp
        if self._local and self._products_dir:
            observation = spec_info.get("observation", object_id.rsplit("_", 1)[0])
            obs_dir = self._products_dir / observation
            obs_dir.mkdir(parents=True, exist_ok=True)
            dest = obs_dir / filename
        else:
            import tempfile
            dest = Path(tempfile.mkdtemp(prefix="campfire_")) / filename

        # Download to temp file then rename (atomic)
        tmp_dest = dest.with_suffix(".tmp")
        sha256 = hashlib.sha256()
        file_size = 0

        try:
            with requests.get(signed_url, stream=True) as r:
                r.raise_for_status()
                tmp_dest.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp_dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        sha256.update(chunk)
                        file_size += len(chunk)
            tmp_dest.rename(dest)
        except requests.RequestException as e:
            tmp_dest.unlink(missing_ok=True)
            raise DownloadError(f"Failed to download spectrum: {e}")

        # Update the store so next call finds it locally
        if self._local and self._products_dir:
            observation = spec_info.get("observation", object_id.rsplit("_", 1)[0])
            local_rel_path = f"{observation}/{filename}"
            st = dest.stat()
            self._local.mark_synced(
                spectra_id=spec_info["spectra_id"],
                target_id=object_id,
                observation=observation,
                grating=grating,
                fits_path=fits_path,
                local_path=local_rel_path,
                file_hash=f"sha256:{sha256.hexdigest()}",
                file_size=file_size,
                local_file_mtime=st.st_mtime,
                local_file_size=st.st_size,
            )

        return SpectrumData.from_fits(
            str(dest), object_id=object_id, grating=grating
        )

    def _resolve_spectrum_info(self, object_id: str, grating: str) -> dict:
        """Resolve spectrum metadata (fits_path, spectra_id, observation) for a target + grating."""
        # Check local store first
        if self._local:
            spectra = self._local.get_spectra_for_object(object_id, grating)
            if spectra:
                spec = spectra[0]
                # Get observation from the targets table
                row = self._local._conn.execute(
                    "SELECT observation FROM targets WHERE target_id = ?",
                    (object_id,),
                ).fetchone()
                if row:
                    spec["observation"] = row["observation"]
                return spec

        # Fall back to API query
        objects, _ = self._api.query_targets(
            search=object_id, limit=1
        )
        if not objects:
            raise NotFoundError(f"Target not found: {object_id}")

        obj = objects[0]
        for spec in obj.get("spectra", []):
            if spec.get("grating") == grating:
                spec["observation"] = obj.get("observation", "")
                return spec

        raise NotFoundError(f"No {grating} spectrum found for {object_id}")

    def iter_targets(self, **filters) -> Iterator[dict]:
        """
        Iterate over all matching targets with automatic pagination.

        Yields individual target dicts. When local data is available and
        covers the requested observations, iterates from SQLite. Otherwise,
        auto-paginates through the remote API.

        Parameters
        ----------
        **filters
            Same filters as ``query_targets()``. ``limit`` controls page
            size for remote queries (default 1000).

        Yields
        ------
        dict
            Individual target records.

        Examples
        --------
        >>> cf = Campfire()
        >>> for obj in cf.iter_targets(redshift_range=(2.0, 4.0)):
        ...     print(obj['target_id'], obj['redshift'])
        """
        remote = filters.pop("remote", False)
        use_local = self._local is not None and not remote

        if use_local:
            self._log_local_use()
            # Convert flag inputs for local query
            for flag_name, flag_class in [
                ("spectral_features", SpectralFeatures),
                ("dq_flags", DQFlags),
            ]:
                if flag_name in filters:
                    filters[flag_name] = self._flag_to_dict(
                        filters[flag_name], flag_class
                    )
            # Local query — no pagination needed, SQLite handles it
            filters["limit"] = filters.get("limit", 999999)
            yield from self._local.query_targets(**filters)
            return

        # Remote auto-pagination
        yield from self._api.iter_targets(**filters)

    def query_objects(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[Union[int, str]]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[Union[int, str]]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        sort: str = "object_id",
        sort_dir: str = "asc",
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Table:
        """
        Query sky-objects (cross-program grouped positions).

        Requires a local sync (``cf.sync()``). Sky-objects group targets
        at the same sky position across programs, providing aggregate
        properties like best redshift and total spectra count.

        Parameters
        ----------
        fields : list of str, optional
            Field names to filter by.
        programs : list of str, optional
            Program slugs to filter by.
        redshift_range : tuple of (float, float), optional
            (min, max) best redshift range.
        redshift_quality : list of int or str, optional
            Quality codes to include.
        max_snr_range : tuple of (float, float), optional
            (min, max) maximum SNR range.
        search : str, optional
            Text search on object_id.
        cone_search : tuple of (ra, dec, radius), optional
            (ra_deg, dec_deg, radius_arcsec) for cone search.
        sort : str, optional
            Sort column (default: 'object_id').
        sort_dir : str, optional
            Sort direction: 'asc' or 'desc'.
        limit : int, optional
            Maximum number of results.
        offset : int, optional
            Pagination offset.

        Returns
        -------
        astropy.table.Table
            Table of matching sky-objects.

        Examples
        --------
        >>> cf = Campfire()
        >>> cf.sync()
        >>> objects = cf.query_objects(redshift_range=(2.0, 6.0))
        >>> print(objects['object_id', 'best_redshift', 'n_targets'])
        """
        if self._local is None:
            raise ValidationError(
                "No local catalog. Run cf.sync() first to query sky-objects."
            )

        self._log_local_use()

        if fields:
            fields = [f.lower() for f in fields]
        if programs:
            programs = [str(p) for p in programs]
        if redshift_quality:
            redshift_quality = [
                int(RedshiftQuality(q)) if isinstance(q, str) else q
                for q in redshift_quality
            ]

        objects = self._local.query_sky_objects(
            fields=fields,
            programs=programs,
            redshift_range=redshift_range,
            redshift_quality=redshift_quality,
            max_snr_range=max_snr_range,
            search=search,
            cone_search=cone_search,
            sort=sort,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )

        if len(objects) == 0:
            return Table()

        return Table(rows=objects)

    # -------------------------------------------------------------------------
    # Imaging Methods (cutouts + shutters)
    # -------------------------------------------------------------------------

    def get_cutout(
        self,
        object_id: str,
        size: Optional[int] = None,
        fov: float = 5.0,
        cache: bool = True,
    ) -> Path:
        """
        Download a cutout PNG for an object.

        Returns the path to the cached PNG file. When ``cache=True``
        (default), subsequent calls return instantly from local cache.

        Parameters
        ----------
        object_id : str
            Object ID.
        size : int, optional
            Output size in pixels. Defaults to native resolution.
        fov : float, optional
            Field of view in arcseconds (default 5).
        cache : bool, optional
            Cache the cutout locally (default True).

        Returns
        -------
        Path
            Path to the PNG file.

        Examples
        --------
        >>> cf = Campfire()
        >>> path = cf.get_cutout('cosmos_ddt_66964', fov=3.2)
        >>> # Use with imaging module
        >>> from campfire.imaging import plot_cutout
        >>> fig = plot_cutout(path)
        """
        # Build cache filename (use format(g) to avoid float repr oddities)
        fov_str = format(fov, "g")
        size_tag = f"_s{size}" if size is not None else ""
        filename = f"{object_id}_fov{fov_str}{size_tag}.png"

        # Check cache
        from .config import resolve_data_dir
        cutouts = resolve_data_dir() / "cutouts"

        dest = _safe_cache_path(cutouts, filename, object_id)
        if cache and dest.exists():
            return dest

        # Fetch from API
        png_data = self._api.get_cutout(object_id, size=size, fov=fov)

        # Save to cache
        cutouts.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        try:
            tmp.write_bytes(png_data)
            tmp.rename(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return dest

    def get_shutters(
        self,
        object_id: str,
        fov: float = 5.0,
        cache: bool = True,
    ) -> dict:
        """
        Get shutter geometry near an object.

        Parameters
        ----------
        object_id : str
            Object ID.
        fov : float, optional
            Search radius in arcseconds (default 5).
        cache : bool, optional
            Cache the result locally (default True).

        Returns
        -------
        dict
            Keys: ``shutters`` (list of shutter dicts), ``meta`` (dict with
            shutter_width_arcsec, shutter_height_arcsec, center_ra, center_dec,
            radius_arcsec, field).

        Examples
        --------
        >>> cf = Campfire()
        >>> result = cf.get_shutters('cosmos_ddt_66964', fov=3.2)
        >>> print(f"Found {len(result['shutters'])} nearby shutters")
        """
        import json
        from .config import resolve_data_dir

        fov_str = format(fov, "g")
        filename = f"{object_id}_fov{fov_str}_shutters.json"
        cutouts = resolve_data_dir() / "cutouts"
        dest = _safe_cache_path(cutouts, filename, object_id)

        if cache and dest.exists():
            return json.loads(dest.read_text())

        result = self._api.get_shutters(object_id=object_id, radius=fov)

        if cache:
            cutouts.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(result))
                tmp.rename(dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

        return result

    def plot_cutout(
        self,
        object_id: str,
        fov: float = 5.0,
        size: Optional[int] = None,
        shutters: Union[bool, str] = True,
        ax=None,
        **kwargs,
    ):
        """
        Plot a cutout image with optional vector shutter overlay.

        Convenience method that fetches the cutout and shutter geometry
        (with local caching) and renders them using
        :func:`campfire.imaging.plot_cutout`.

        Parameters
        ----------
        object_id : str
            Object ID.
        fov : float, optional
            Field of view in arcseconds (default 5).
        size : int, optional
            Output size in pixels. Defaults to native resolution.
        shutters : bool or str, optional
            Control shutter overlay. ``True`` or ``'all'`` shows all
            shutters (default). ``'target'`` shows only the current
            object's shutters. ``False`` disables the overlay.
        ax : matplotlib.axes.Axes, optional
            Axes to plot on. If None, uses ``plt.gca()``.
        **kwargs
            Additional keyword arguments passed to
            :func:`campfire.imaging.plot_cutout` (e.g.
            ``shutter_style``, ``scalebar``, ``scalebar_length``).

        Returns
        -------
        matplotlib.axes.Axes
            The axes with the plot.

        Examples
        --------
        >>> import matplotlib.pyplot as plt
        >>> cf = Campfire()
        >>> fig, ax = plt.subplots(figsize=(5, 5))
        >>> cf.plot_cutout('cosmos_ddt_66964', fov=3.2, ax=ax)
        >>> fig.savefig('cutout.pdf')
        >>>
        >>> # Target shutters only:
        >>> cf.plot_cutout('cosmos_ddt_66964', fov=3.2, shutters='target', ax=ax)
        """
        from .imaging import plot_cutout

        path = self.get_cutout(object_id, size=size, fov=fov)

        shutter_data = None
        if shutters and shutters != False:  # noqa: E712
            result = self.get_shutters(object_id, fov=fov)
            if shutters == 'target':
                # Filter to only this object's shutters
                result = {
                    **result,
                    "shutters": [
                        s for s in result["shutters"]
                        if s.get("object_id") == object_id
                    ],
                }
            shutter_data = result

        return plot_cutout(
            path,
            shutters=shutter_data,
            object_id=object_id,
            fov=fov,
            ax=ax,
            **kwargs,
        )
