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
    ObjectFlags,
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
    object_flags=None,
    dq_flags=None,
    inspected_only: Optional[bool] = None,
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

    # Process flag parameters
    sf_query = parse_flag_input(spectral_features, SpectralFeatures)
    if sf_query:
        params.update(sf_query.to_params("spectral_features"))

    of_query = parse_flag_input(object_flags, ObjectFlags)
    if of_query:
        params.update(of_query.to_params("object_flags"))

    dq_query = parse_flag_input(dq_flags, DQFlags)
    if dq_query:
        params.update(dq_query.to_params("dq_flags"))

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

    def query_objects(self, **filters) -> Tuple[List[dict], dict]:
        """Query objects with filters.

        Parameters match those of ``Campfire.query_objects()``.

        Returns
        -------
        tuple of (list[dict], dict)
            (objects_list, pagination_dict)
        """
        params = _build_query_params(**filters)
        response = self._session.get("/objects", params=params)
        _handle_response_error(response)

        data = response.json()
        return data.get("data", []), data.get("pagination", {})

    def iter_objects(self, **filters) -> Iterator[dict]:
        """Auto-paginating iterator over all matching objects.

        Yields individual object dicts. Handles pagination automatically.
        Accepts the same filter parameters as ``query_objects()``, except
        ``offset`` is managed internally.

        Parameters
        ----------
        **filters
            Same filters as ``query_objects()``. ``limit`` controls page
            size (default 1000). ``offset`` is ignored.
        """
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

    def fetch_all_objects(
        self,
        observations: Optional[List[str]] = None,
        updated_since: Optional[str] = None,
        on_observation_complete: Optional[Callable[[str, int], None]] = None,
    ) -> List[dict]:
        """Fetch all objects, handling pagination.

        Parameters
        ----------
        observations : list of str, optional
            Observation names to fetch. If None, fetches all accessible
            observations.
        updated_since : str, optional
            ISO 8601 timestamp. Only fetch objects updated after this time.
            When set, uses a single query across all observations instead
            of per-observation iteration (much faster when few objects changed).
        on_observation_complete : callable, optional
            Callback ``(obs_name, obj_count)`` called after each observation.
            Only used for full (non-incremental) fetches.
        """
        if observations is None:
            obs_list = self.get_observations()
            observations = [o["observation"] for o in obs_list]

        # Incremental sync: single query across all observations
        if updated_since:
            return self._fetch_updated_objects(updated_since)

        # Full sync: iterate per observation (supports progress callback)
        all_objects = []
        for obs in observations:
            self._session._ensure_valid_token()
            obs_count = 0
            offset = 0
            while True:
                params = {"observations": obs, "limit": 1000, "offset": offset}
                response = self._session.get(
                    "/objects",
                    params=params,
                    timeout=60,
                )
                _handle_response_error(response, f"fetching objects for {obs}")
                data = response.json()
                objects = data.get("data", [])
                all_objects.extend(objects)
                obs_count += len(objects)
                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                offset += len(objects)
                if offset >= total or not objects:
                    break
            if on_observation_complete:
                on_observation_complete(obs, obs_count)
        return all_objects

    def _fetch_updated_objects(self, updated_since: str) -> List[dict]:
        """Fetch all objects updated since a timestamp in a single paginated query."""
        all_objects = []
        offset = 0
        while True:
            self._session._ensure_valid_token()
            params = {
                "updated_since": updated_since,
                "limit": 1000,
                "offset": offset,
            }
            response = self._session.get("/objects", params=params, timeout=60)
            _handle_response_error(response, "fetching updated objects")
            data = response.json()
            objects = data.get("data", [])
            all_objects.extend(objects)
            pagination = data.get("pagination", {})
            total = pagination.get("total", 0)
            offset += len(objects)
            if offset >= total or not objects:
                break
        return all_objects

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
