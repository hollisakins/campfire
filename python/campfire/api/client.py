"""Typed API endpoint methods for the CAMPFIRE REST API.

Centralizes all URL construction and response parsing. Used by both the
``Campfire`` client class and the CLI.
"""

from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

from ..exceptions import (
    APIError,
    AuthenticationError,
    DownloadError,
    NotFoundError,
)
from ..flags import (
    DQFlags,
    FlagQuery,
    SpectralFeatures,
    parse_flag_input,
)
from .session import APISession


def _build_query_params(
    programs: Optional[List[Union[int, str]]] = None,
    fields: Optional[List[str]] = None,
    gratings: Optional[List[str]] = None,
    observations: Optional[List[str]] = None,
    redshift_range: Optional[Tuple[float, float]] = None,
    redshift_quality: Optional[List[int]] = None,
    max_snr_range: Optional[Tuple[float, float]] = None,
    spectral_features=None,
    dq_flags=None,
    tags: Optional[List[str]] = None,
    inspected_only: Optional[bool] = None,
    search: Optional[str] = None,
    cone_search: Optional[Tuple[float, float, float]] = None,
    limit: int = 1000,
    offset: int = 0,
    sort: str = "target_id",
    sort_dir: str = "asc",
) -> dict:
    """Build query parameters dict from filter arguments."""
    params: Dict[str, Union[str, int, float]] = {
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "sort_dir": sort_dir,
    }

    if programs:
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

    # Process flag parameters
    sf_query = parse_flag_input(spectral_features, SpectralFeatures)
    if sf_query:
        params.update(sf_query.to_params("spectral_features"))

    dq_query = parse_flag_input(dq_flags, DQFlags)
    if dq_query:
        params.update(dq_query.to_params("dq_flags"))

    if tags:
        params["lists"] = ",".join(tags)

    if inspected_only is not None:
        params["inspected_only"] = "true" if inspected_only else "false"
    if search:
        params["search"] = search
    if cone_search:
        ra, dec, radius_arcsec = cone_search
        params["ra"] = ra
        params["dec"] = dec
        params["radius"] = radius_arcsec

    return params


def _handle_response_error(response, context: str = "") -> None:
    """Raise appropriate exception for non-200 responses."""
    if response.status_code == 401:
        raise AuthenticationError(
            "Invalid or expired token. Run 'campfire login' to re-authenticate."
        )
    elif response.status_code == 403:
        raise AuthenticationError(f"Access denied{': ' + context if context else ''}")
    elif response.status_code == 404:
        raise NotFoundError(context or "Resource not found")
    elif response.status_code != 200:
        raise APIError(f"API error: {response.status_code} - {response.text}")


class APIClient:
    """Typed methods for every CAMPFIRE /api/v1/ endpoint.

    Parameters
    ----------
    session : APISession
        An authenticated session to use for requests.
    """

    def __init__(self, session: APISession):
        self._session = session

    def query_targets(self, **filters) -> Tuple[List[dict], dict]:
        """Query targets with filters.

        Parameters match those of ``Campfire.query_targets()``.

        Returns
        -------
        tuple of (list[dict], dict)
            (targets_list, pagination_dict)
        """
        params = _build_query_params(**filters)
        response = self._session.get("/targets", params=params)
        _handle_response_error(response)

        data = response.json()
        return data.get("data", []), data.get("pagination", {})

    def iter_targets(self, **filters) -> Iterator[dict]:
        """Auto-paginating iterator over all matching targets.

        Yields individual target dicts. Handles pagination automatically.
        Accepts the same filter parameters as ``query_targets()``, except
        ``offset`` is managed internally.

        Parameters
        ----------
        **filters
            Same filters as ``query_targets()``. ``limit`` controls page
            size (default 1000). ``offset`` is ignored.
        """
        filters.pop("offset", None)
        limit = filters.get("limit", 1000)
        offset = 0

        while True:
            filters["offset"] = offset
            filters["limit"] = limit
            objects, pagination = self.query_targets(**filters)

            yield from objects

            total = pagination.get("total", 0)
            offset += len(objects)
            if offset >= total or not objects:
                break

    def get_metadata(self) -> dict:
        """Get available filter options (programs, fields, gratings, observations).

        Returns
        -------
        dict
            Keys: programs, fields, gratings, observations.
        """
        response = self._session.get("/metadata")
        _handle_response_error(response)
        return response.json()

    def get_observations(self) -> List[dict]:
        """List observations with aggregate stats.

        Returns
        -------
        list of dict
            Each with observation, program_name, field, object_count, etc.
        """
        response = self._session.get("/observations", timeout=30)
        _handle_response_error(response)
        return response.json().get("observations", [])

    def fetch_manifest(self, obs_name: str) -> dict:
        """Fetch the download manifest for an observation.

        Returns
        -------
        dict
            Manifest with observation metadata and list of spectra with
            signed download URLs.
        """
        response = self._session.get(
            f"/observations/{obs_name}/manifest", timeout=60
        )
        if response.status_code == 404:
            raise NotFoundError(
                f"Observation '{obs_name}' not found or you don't have access"
            )
        _handle_response_error(response)
        return response.json()

    def _paginate_sync_endpoint(
        self,
        path: str,
        updated_since: Optional[str] = None,
        on_page_complete: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """Paginate through a /sync/* endpoint.

        Returns (items, total_accessible_count).
        """
        all_items: List[dict] = []
        total_accessible_count = 0
        offset = 0
        while True:
            self._session._ensure_valid_token()
            params: dict = {"limit": 1000, "offset": offset}
            if updated_since:
                params["updated_since"] = updated_since
            response = self._session.get(path, params=params, timeout=60)
            _handle_response_error(response, f"fetching {path}")
            data = response.json()
            items = data.get("data", [])
            all_items.extend(items)
            total = data.get("pagination", {}).get("total", 0)
            total_accessible_count = data.get("total_accessible_count", 0)
            offset += len(items)
            if on_page_complete:
                on_page_complete(offset, total)
            if offset >= total or not items:
                break
        return all_items, total_accessible_count

    def fetch_all_targets(
        self,
        observations: Optional[List[str]] = None,
        updated_since: Optional[str] = None,
        on_page_complete: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """Fetch all targets via the lightweight sync endpoint.

        Uses ``/sync/catalog`` which is optimized for bulk fetches
        (no complex sorting or window functions).

        Parameters
        ----------
        observations : list of str, optional
            Not used directly (the sync endpoint returns all accessible
            targets). Kept for API compatibility.
        updated_since : str, optional
            ISO 8601 timestamp. Only fetch targets updated after this time.
        on_page_complete : callable, optional
            Callback ``(fetched_so_far, total)`` called after each page.

        Returns
        -------
        (targets, total_accessible_count)
            The fetched targets and the total number of accessible targets
            on the server (regardless of ``updated_since`` filter).
        """
        return self._paginate_sync_endpoint(
            "/sync/catalog", updated_since, on_page_complete,
        )

    def get_spectrum_data(self, object_id: str, grating: str) -> dict:
        """Fetch spectrum JSON data for plotting.

        Returns
        -------
        dict
            Keys: wave, fnu, fnu_err, snr_2d, n_spatial, n_wave,
            profile, profile_fit, profile_pix.
        """
        response = self._session.get(
            "/spectrum", params={"object_id": object_id, "grating": grating}
        )
        _handle_response_error(
            response, f"No {grating} spectrum found for {object_id}"
        )
        return response.json()

    def get_redshift_fit_data(self, object_id: str, grating: str) -> dict:
        """Fetch redshift fitting results for plotting.

        Returns
        -------
        dict
            Keys: redshift, chi2_min, confidence, z_grid, chi2_grid,
            model_wave, model_fnu.
        """
        response = self._session.get(
            "/redshift-fit", params={"object_id": object_id, "grating": grating}
        )
        _handle_response_error(
            response, f"No redshift fit data found for {object_id} ({grating})"
        )
        return response.json()

    def get_signed_url(self, fits_path: str) -> str:
        """Get a signed download URL for a FITS file.

        Returns
        -------
        str
            Presigned URL for direct download.
        """
        response = self._session.get("/spectra", params={"path": fits_path})
        _handle_response_error(response, f"File not found: {fits_path}")

        data = response.json()
        url = data.get("url")
        if not url:
            raise DownloadError("No download URL returned from API")
        return url

    def get_cutout(
        self,
        object_id: str,
        size: Optional[int] = None,
        fov: float = 5.0,
    ) -> bytes:
        """Fetch a PNG cutout image for an object.

        Parameters
        ----------
        object_id : str
            Object identifier.
        size : int, optional
            Output size in pixels. Defaults to native resolution for the
            requested FOV. Maximum 2048.
        fov : float, optional
            Field of view in arcseconds (default 5).

        Returns
        -------
        bytes
            Raw PNG image data.
        """
        params: Dict[str, Union[str, int, float]] = {
            "object_id": object_id,
            "fov": fov,
        }
        if size is not None:
            params["size"] = size
        response = self._session.get("/cutout", params=params, timeout=30)
        _handle_response_error(response, f"Cutout for {object_id}")
        return response.content

    def fetch_all_sky_objects(
        self,
        updated_since: Optional[str] = None,
        on_page_complete: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """Fetch all sky-objects via the /sync/objects endpoint.

        Parameters
        ----------
        updated_since : str, optional
            ISO 8601 timestamp. Only fetch objects updated after this time.
        on_page_complete : callable, optional
            Callback ``(fetched_so_far, total)`` called after each page.

        Returns
        -------
        (objects, total_accessible_count)
            The fetched sky-objects and the total number accessible on the
            server (regardless of ``updated_since`` filter).
        """
        return self._paginate_sync_endpoint(
            "/sync/objects", updated_since, on_page_complete,
        )

    def fetch_tags(self) -> List[dict]:
        """Fetch all tag metadata via the /sync/lists endpoint.

        Returns
        -------
        list of dict
            Tag metadata (slug, name, description, visibility, etc.).
        """
        self._session._ensure_valid_token()
        response = self._session.get("/sync/lists", timeout=30)
        _handle_response_error(response, "fetching tags")
        return response.json().get("data", [])

    # Deprecated aliases (old names → new names)
    query_objects = query_targets
    iter_objects = iter_targets
    fetch_all_objects = fetch_all_targets

    def get_shutters(
        self,
        object_id: str,
        radius: float = 5.0,
    ) -> dict:
        """Fetch nearby shutter geometry for an object.

        Parameters
        ----------
        object_id : str
            Object identifier. RA/Dec/field are looked up on the server.
        radius : float, optional
            Search radius in arcseconds (default 5).

        Returns
        -------
        dict
            Keys: ``shutters`` (list of shutter dicts), ``meta`` (dict with
            shutter dimensions, center coordinates, and search radius).
        """
        params: Dict[str, Union[str, float]] = {
            "object_id": object_id,
            "fov": radius,
        }
        response = self._session.get("/shutters", params=params, timeout=30)
        _handle_response_error(response, "Shutter query")
        return response.json()
