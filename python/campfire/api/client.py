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
    dq_flags=None,
    tags: Optional[List[str]] = None,
    inspected_only: Optional[bool] = None,
    has_photometry: Optional[bool] = None,
    search: Optional[str] = None,
    cone_search: Optional[Tuple[float, float, float]] = None,
    limit: int = 1000,
    offset: int = 0,
    sort: str = "object_id",
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

    dq_query = parse_flag_input(dq_flags, DQFlags)
    if dq_query:
        params.update(dq_query.to_params("dq_flags"))

    if tags:
        params["lists"] = ",".join(tags)

    if inspected_only is not None:
        params["inspected_only"] = "true" if inspected_only else "false"
    if has_photometry is not None:
        params["has_photometry"] = "true" if has_photometry else "false"
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

    # ------------------------------------------------------------------
    # Objects
    # ------------------------------------------------------------------
    def query_objects(self, **filters) -> Tuple[List[dict], dict]:
        """Query objects with filters.

        Returns
        -------
        tuple of (list[dict], dict)
            (objects_list, pagination_dict)
        """
        filters.setdefault("sort", "object_id")
        params = _build_query_params(**filters)
        response = self._session.get("/objects", params=params)
        _handle_response_error(response)
        data = response.json()
        return data.get("data", []), data.get("pagination", {})

    def iter_objects(self, **filters) -> Iterator[dict]:
        """Auto-paginating iterator over all matching objects."""
        filters.pop("offset", None)
        limit = filters.get("limit", 1000)
        offset = 0
        while True:
            filters["offset"] = offset
            filters["limit"] = limit
            objects, pagination = self.query_objects(**filters)
            yield from objects
            total = pagination.get("total", 0)
            offset += len(objects)
            if offset >= total or not objects:
                break

    def fetch_all_objects(
        self,
        updated_since: Optional[str] = None,
        on_page_complete: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """Fetch all objects via the lightweight /sync/objects endpoint."""
        return self._paginate_sync_endpoint(
            "/sync/objects", updated_since, on_page_complete,
        )

    # ------------------------------------------------------------------
    # Spectra
    # ------------------------------------------------------------------
    def query_spectra(self, **filters) -> Tuple[List[dict], dict]:
        """Query spectra (flat, one row per spectrum) with filters.

        Returns
        -------
        tuple of (list[dict], dict)
            (spectra_list, pagination_dict)
        """
        filters.setdefault("sort", "spectrum_id")
        params = _build_query_params(**filters)
        response = self._session.get("/spectra/list", params=params)
        _handle_response_error(response)
        data = response.json()
        return data.get("data", []), data.get("pagination", {})

    def iter_spectra(self, **filters) -> Iterator[dict]:
        """Auto-paginating iterator over all matching spectra."""
        filters.pop("offset", None)
        limit = filters.get("limit", 1000)
        offset = 0
        while True:
            filters["offset"] = offset
            filters["limit"] = limit
            spectra, pagination = self.query_spectra(**filters)
            yield from spectra
            total = pagination.get("total", 0)
            offset += len(spectra)
            if offset >= total or not spectra:
                break

    def fetch_all_spectra(
        self,
        updated_since: Optional[str] = None,
        on_page_complete: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """Fetch all spectra via the /sync/spectra endpoint."""
        return self._paginate_sync_endpoint(
            "/sync/spectra", updated_since, on_page_complete,
        )

    # ------------------------------------------------------------------
    # Metadata / observations
    # ------------------------------------------------------------------
    def get_metadata(self) -> dict:
        """Get available filter options (programs, fields, gratings, observations)."""
        response = self._session.get("/metadata")
        _handle_response_error(response)
        return response.json()

    def get_observations(self) -> List[dict]:
        """List observations with aggregate stats."""
        response = self._session.get("/observations", timeout=30)
        _handle_response_error(response)
        return response.json().get("observations", [])

    def fetch_manifest(self, obs_name: str) -> dict:
        """Fetch the download manifest for an observation."""
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
        total = 0
        offset = 0
        first_page = True
        while True:
            self._session._ensure_valid_token()
            # Only request counts on the first page; the server skips the
            # count CTEs when include_counts=false, saving a full scan per
            # subsequent page.
            params: dict = {
                "limit": 1000,
                "offset": offset,
                "include_counts": "true" if first_page else "false",
            }
            if updated_since:
                params["updated_since"] = updated_since
            response = self._session.get(path, params=params, timeout=60)
            _handle_response_error(response, f"fetching {path}")
            data = response.json()
            items = data.get("data", [])
            all_items.extend(items)
            if first_page:
                total = data.get("pagination", {}).get("total", 0)
                total_accessible_count = data.get("total_accessible_count", 0)
                first_page = False
            offset += len(items)
            if on_page_complete:
                on_page_complete(offset, total)
            if offset >= total or not items:
                break
        return all_items, total_accessible_count

    # ------------------------------------------------------------------
    # Spectrum JSON / redshift fit
    # ------------------------------------------------------------------
    def get_spectrum_data(self, spectrum_id: str) -> dict:
        """Fetch spectrum JSON data for plotting."""
        response = self._session.get(
            "/spectrum", params={"spectrum_id": spectrum_id}
        )
        _handle_response_error(
            response, f"No spectrum found for {spectrum_id}"
        )
        return response.json()

    def get_redshift_fit_data(self, spectrum_id: str) -> dict:
        """Fetch redshift fitting results for plotting."""
        response = self._session.get(
            "/redshift-fit", params={"spectrum_id": spectrum_id}
        )
        _handle_response_error(
            response, f"No redshift fit data found for {spectrum_id}"
        )
        return response.json()

    def get_signed_url(self, fits_path: str) -> str:
        """Get a signed download URL for a FITS file."""
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
            Object identifier (IAU name from objects.object_id).
        size : int, optional
            Output size in pixels. Defaults to native resolution for the
            requested FOV. Maximum 2048.
        fov : float, optional
            Field of view in arcseconds (default 5).
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

    def fetch_all_photometry(
        self,
        updated_since: Optional[str] = None,
        on_page_complete: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[dict], int]:
        """Fetch all photometry records via the /sync/photometry endpoint."""
        all_items: List[dict] = []
        offset = 0
        total_count = 0
        while True:
            self._session._ensure_valid_token()
            params: dict = {"limit": 1000, "offset": offset}
            if updated_since:
                params["updated_since"] = updated_since
            response = self._session.get("/sync/photometry", params=params, timeout=60)
            _handle_response_error(response, "fetching photometry")
            data = response.json()
            items = data.get("data", [])
            all_items.extend(items)
            total = data.get("pagination", {}).get("total", 0)
            total_count = total
            offset += len(items)
            if on_page_complete:
                on_page_complete(offset, total)
            if offset >= total or not items:
                break
        return all_items, total_count

    def fetch_tags(self) -> List[dict]:
        """Fetch all tag metadata via the /sync/lists endpoint."""
        self._session._ensure_valid_token()
        response = self._session.get("/sync/lists", timeout=30)
        _handle_response_error(response, "fetching tags")
        return response.json().get("data", [])

    def get_shutters(
        self,
        object_id: str,
        radius: float = 5.0,
    ) -> dict:
        """Fetch nearby shutter geometry for an object.

        Parameters
        ----------
        object_id : str
            Object identifier (IAU name). RA/Dec/field are looked up on the server.
        radius : float, optional
            Search radius in arcseconds (default 5).
        """
        params: Dict[str, Union[str, float]] = {
            "object_id": object_id,
            "fov": radius,
        }
        response = self._session.get("/shutters", params=params, timeout=30)
        _handle_response_error(response, "Shutter query")
        return response.json()
