"""Main CAMPFIRE API client."""

import hashlib
import logging
import os
import warnings
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

import requests
from astropy.table import Table

from .api.client import APIClient
from .api.session import APISession
from .exceptions import DownloadError, NotFoundError, ValidationError
from .flags import (
    DQFlags,
    FlagQuery,
    RedshiftQuality,
    parse_flag_input,
)
from .models import Object, SpectrumData

__version__ = "0.4.0"

logger = logging.getLogger(__name__)


def _safe_cache_path(cache_dir: Path, filename: str, key: str) -> Path:
    """Resolve a cache path and ensure it stays within cache_dir."""
    dest = (cache_dir / filename).resolve()
    if not str(dest).startswith(str(cache_dir.resolve())):
        raise ValueError(f"Invalid key produces unsafe cache path: {key!r}")
    return dest


class Campfire:
    """CAMPFIRE Python API Client.

    Query and download NIRSpec spectroscopic data from the CAMPFIRE archive.
    Objects and spectra are the two primary query surfaces: ``query_objects``
    returns one row per sky position (with inspection state and aggregate
    properties) and ``query_spectra`` returns one row per spectrum (for
    download-level metadata).

    When locally synced data is available (from ``campfire sync``), queries
    are served from the local SQLite database for speed. Otherwise, falls
    back to the remote API.

    Authentication uses stored credentials from ``campfire login``.

    Parameters
    ----------
    base_url : str, optional
        Base URL for the API. Defaults to ``$CAMPFIRE_API_URL`` or the
        production CAMPFIRE server.
    data_dir : str or Path, optional
        Root data directory (contains ``products/`` and ``meta/``). Defaults
        to ``$CAMPFIRE_ROOT`` or ``~/campfire``.
    auto_refresh : bool, optional
        If True (default), automatically refresh OAuth tokens on expiry.

    Examples
    --------
    >>> from campfire import Campfire
    >>> cf = Campfire()
    >>> objects = cf.query_objects(programs=['ember-uds'], redshift_range=(2, 4))
    >>> spectra = cf.query_spectra(gratings=['PRISM'])
    """

    DEFAULT_BASE_URL = "https://campfire.hollisakins.com/api/v1"

    def __init__(
        self,
        base_url: Optional[str] = None,
        data_dir: Optional[Union[str, Path]] = None,
        auto_refresh: bool = True,
    ):
        self._api_session = APISession(base_url=base_url, auto_refresh=auto_refresh)
        self._api = APIClient(self._api_session)
        self.base_url = self._api_session.base_url

        self._local = None
        self._products_dir: Optional[Path] = None
        self._meta_dir: Optional[Path] = None
        self._local_logged = False
        self._api_download_count = 0

        self._open_local_catalog(data_dir)

    def _open_local_catalog(self, data_dir: Optional[Union[str, Path]]) -> None:
        """Attempt to open the local SQLite catalog; warn if it can't be used.

        Without a local catalog, queries fall back to the remote API and
        ``get_object`` returns no embedded spectra — a common source of
        confusion when ``$CAMPFIRE_ROOT`` points somewhere stale (e.g.
        a path that exists on another machine but not this one).
        """
        from .config import resolve_data_dir

        if data_dir:
            resolved = Path(data_dir).expanduser()
            source = f"data_dir={str(data_dir)!r}"
        else:
            env = os.environ.get("CAMPFIRE_ROOT")
            resolved = resolve_data_dir()
            source = f"$CAMPFIRE_ROOT={env!r}" if env else f"default {resolved}"

        db_path = resolved / "meta" / "campfire.db"
        reason: Optional[str] = None

        if not db_path.exists():
            reason = f"no campfire.db at {db_path} (from {source})"
        else:
            from .db.store import LocalStore, SchemaMismatchError
            try:
                self._local = LocalStore(db_path)
            except SchemaMismatchError as exc:
                reason = (
                    f"local catalog at {db_path} has schema v{exc.found_version}, "
                    f"client expects v{exc.expected_version} — "
                    f"run `campfire sync --full` to rebuild"
                )
            else:
                self._products_dir = resolved / "products"
                self._meta_dir = resolved / "meta"

        if self._local is None:
            warnings.warn(
                f"No local CAMPFIRE catalog ({reason}); "
                f"falling back to remote API. Some methods (e.g. "
                f"`get_object().spectra`) return less data over the API. "
                f"Run `campfire sync` to enable local-first queries.",
                stacklevel=3,
            )

    @staticmethod
    def _resolve_data_dir(data_dir: Optional[Union[str, Path]]) -> Optional[Path]:
        if data_dir:
            return Path(data_dir).expanduser()
        from .config import resolve_data_dir
        resolved = resolve_data_dir()
        if (resolved / "meta" / "campfire.db").exists():
            return resolved
        return None

    def _log_local_use(self) -> None:
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
        return self._local is not None

    @property
    def last_synced(self) -> Optional[str]:
        if self._local:
            return self._local.get_last_synced_at()
        return None

    # -------------------------------------------------------------------------
    # Sync / download
    # -------------------------------------------------------------------------
    def sync(self, show_progress: bool = False, full: bool = False) -> dict:
        """Sync the objects + spectra catalog from the server."""
        from .sync import sync_metadata

        if self._meta_dir is None:
            from .config import ensure_data_dir, resolve_data_dir
            resolved = self._resolve_data_dir(None)
            if resolved is None:
                resolved = resolve_data_dir()
            ensure_data_dir(resolved)
            self._products_dir = resolved / "products"
            self._meta_dir = resolved / "meta"

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

        return sync_metadata(
            self._api, self._local, self._meta_dir,
            show_progress=show_progress, full=full,
        )

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
        """Download FITS files for matching spectra (final products)."""
        from .api.session import create_download_session
        from .sync import download_objects
        from .db.store import FINAL_PRODUCT_TYPES

        if self._local is None:
            raise ValidationError("No local catalog. Run cf.sync() first.")

        target_obs = set()

        if stale_only:
            stale_files = self._local.get_stale_objects()
            target_obs = set(f["observation"] for f in stale_files if f.get("observation"))
            if not target_obs:
                return {"downloaded": 0, "failed": 0, "bytes": 0, "message": "All files up to date"}
        else:
            if observations:
                target_obs.update(observations)
            if programs:
                for prog in programs:
                    spectra = self._local.query_spectra(programs=[prog], limit=999999)
                    target_obs.update(s["observation"] for s in spectra if s.get("observation"))
            if fields:
                for fld in fields:
                    spectra = self._local.query_spectra(fields=[fld], limit=999999)
                    target_obs.update(s["observation"] for s in spectra if s.get("observation"))

            if not target_obs:
                raise ValidationError(
                    "Specify at least one of: observations, programs, fields, or stale_only=True"
                )

        dl_session = create_download_session(max_workers)
        self._api_session._ensure_valid_token()
        stats = download_objects(
            self._api,
            sorted(target_obs),
            list(FINAL_PRODUCT_TYPES),
            self._local,
            self._products_dir,
            max_workers=max_workers,
            download_session=dl_session,
            gratings=gratings,
        )
        return {
            "downloaded": stats.get("downloaded", 0),
            "failed": stats.get("failed", 0),
            "bytes": stats.get("download_bytes", 0),
        }

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
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

    @staticmethod
    def _normalize_quality(redshift_quality):
        if not redshift_quality:
            return redshift_quality
        return [
            int(RedshiftQuality(q)) if isinstance(q, str) else q
            for q in redshift_quality
        ]

    # -------------------------------------------------------------------------
    # Objects
    # -------------------------------------------------------------------------
    def query_objects(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[Union[int, str]]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[Union[int, str]]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        dq_flags: Optional[Union[int, str, List[str], DQFlags, FlagQuery]] = None,
        tags: Optional[List[str]] = None,
        inspected_only: Optional[bool] = None,
        staleness: Optional[bool] = None,
        has_photometry: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        sort: str = "object_id",
        sort_dir: str = "asc",
        remote: bool = False,
    ) -> Table:
        """Query objects (cross-program grouped sky positions)."""
        if fields:
            fields = [f.lower() for f in fields]
        if gratings:
            gratings = [g.upper() for g in gratings]
        if observations:
            observations = [o.lower() for o in observations]
        redshift_quality = self._normalize_quality(redshift_quality)

        use_local = self._local is not None and not remote

        pagination: dict = {}
        if use_local:
            self._log_local_use()
            dq_dict = self._flag_to_dict(dq_flags, DQFlags)
            objects = self._local.query_objects(
                fields=fields,
                programs=[str(p) for p in programs] if programs else None,
                gratings=gratings,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                dq_flags=dq_dict,
                tags=tags,
                inspected_only=inspected_only,
                staleness=staleness,
                has_photometry=has_photometry,
                search=search,
                cone_search=cone_search,
                sort=sort,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        else:
            remote_limit = limit if limit is not None else 1000
            objects, pagination = self._api.query_objects(
                programs=programs,
                fields=fields,
                gratings=gratings,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                dq_flags=dq_flags,
                tags=tags,
                inspected_only=inspected_only,
                has_photometry=has_photometry,
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

    def iter_objects(self, **filters) -> Iterator[dict]:
        """Iterate over all matching objects with automatic pagination."""
        remote = filters.pop("remote", False)
        use_local = self._local is not None and not remote

        if use_local:
            self._log_local_use()
            if "dq_flags" in filters:
                filters["dq_flags"] = self._flag_to_dict(filters["dq_flags"], DQFlags)
            if filters.get("redshift_quality"):
                filters["redshift_quality"] = self._normalize_quality(filters["redshift_quality"])
            filters.setdefault("limit", 999999)
            yield from self._local.query_objects(**filters)
            return

        yield from self._api.iter_objects(**filters)

    def get_object(self, object_id: str) -> Optional[Object]:
        """Return a single :class:`Object` (with spectra + photometry) by object_id.

        Returns ``None`` if no object matches. The returned object's
        :attr:`Object.spectra` have ``.open()`` wired to this client for
        lazy FITS loading.
        """
        if self._local is not None:
            raw = self._local.get_object(object_id)
            if raw:
                return self._build_object(raw)
        # Remote fallback: search on object_id (no spectra embedded, no photometry)
        objects, _ = self._api.query_objects(search=object_id, limit=1)
        for raw in objects:
            if raw.get("object_id") == object_id:
                return self._build_object(raw)
        return None

    def _build_object(self, raw: dict) -> Object:
        """Construct an :class:`Object` from a store dict, wiring opener + photometry."""
        photometry_record = None
        if self._local is not None:
            object_id = raw.get("object_id")
            if object_id:
                photometry_record = self._local.get_photometry_for_object(object_id)
        return Object.from_dict(
            raw,
            opener=self.open_spectrum,
            photometry_record=photometry_record,
        )

    # -------------------------------------------------------------------------
    # Spectra
    # -------------------------------------------------------------------------
    def query_spectra(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[Union[int, str]]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[Union[int, str]]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        dq_flags: Optional[Union[int, str, List[str], DQFlags, FlagQuery]] = None,
        crds_context: Optional[Union[str, List[str]]] = None,
        cfpipe_version: Optional[Union[str, List[str]]] = None,
        reduced_after: Optional[str] = None,
        tags: Optional[List[str]] = None,
        inspected_only: Optional[bool] = None,
        has_photometry: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        sort: str = "spectrum_id",
        sort_dir: str = "asc",
        remote: bool = False,
    ) -> Table:
        """Query spectra (flat, one row per spectrum) with object-level filters.

        Inspection state (``redshift_range``, ``redshift_quality``,
        ``inspected_only``) is resolved through the parent object.

        Provenance filters carve a calibration-homogeneous subsample without
        opening any FITS: ``crds_context`` and ``cfpipe_version`` accept a
        string or list of strings; ``reduced_after`` is an ISO-8601 string
        keeping only spectra reduced on or after it. These are evaluated in SQL
        against a synced local catalog (``cf.sync()``); on a remote query they
        filter the returned page client-side, so sync locally to filter the
        full catalog.
        """
        if isinstance(crds_context, str):
            crds_context = [crds_context]
        if isinstance(cfpipe_version, str):
            cfpipe_version = [cfpipe_version]
        if fields:
            fields = [f.lower() for f in fields]
        if gratings:
            gratings = [g.upper() for g in gratings]
        if observations:
            observations = [o.lower() for o in observations]
        redshift_quality = self._normalize_quality(redshift_quality)

        use_local = self._local is not None and not remote

        pagination: dict = {}
        if use_local:
            self._log_local_use()
            dq_dict = self._flag_to_dict(dq_flags, DQFlags)
            spectra = self._local.query_spectra(
                fields=fields,
                programs=[str(p) for p in programs] if programs else None,
                gratings=gratings,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                dq_flags=dq_dict,
                crds_context=crds_context,
                cfpipe_version=cfpipe_version,
                reduced_after=reduced_after,
                tags=tags,
                inspected_only=inspected_only,
                has_photometry=has_photometry,
                search=search,
                cone_search=cone_search,
                sort=sort,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
        else:
            remote_limit = limit if limit is not None else 1000
            spectra, pagination = self._api.query_spectra(
                programs=programs,
                fields=fields,
                gratings=gratings,
                observations=observations,
                redshift_range=redshift_range,
                redshift_quality=redshift_quality,
                max_snr_range=max_snr_range,
                dq_flags=dq_flags,
                tags=tags,
                inspected_only=inspected_only,
                has_photometry=has_photometry,
                search=search,
                cone_search=cone_search,
                limit=remote_limit,
                offset=offset,
                sort=sort,
                sort_dir=sort_dir,
            )
            # Provenance filters aren't server-side on the remote feed; apply
            # them to the returned page so the kwargs behave consistently.
            if crds_context or cfpipe_version or reduced_after:
                spectra = [
                    s for s in spectra
                    if (not crds_context or s.get("crds_context") in crds_context)
                    and (not cfpipe_version or s.get("cfpipe_version") in cfpipe_version)
                    and (not reduced_after or (s.get("reduced_at") or "") >= reduced_after)
                ]

        if not use_local and pagination:
            total = pagination.get("total", 0)
            if total > len(spectra):
                import warnings
                warnings.warn(
                    f"Query returned {len(spectra)} of {total} matching spectra. "
                    f"Use limit/offset to paginate, iter_spectra() to stream all, "
                    f"or sync the catalog locally with cf.sync() for unlimited queries.",
                    stacklevel=2,
                )

        if len(spectra) == 0:
            return Table()

        return Table(rows=spectra)

    def iter_spectra(self, **filters) -> Iterator[dict]:
        """Iterate over all matching spectra with automatic pagination."""
        remote = filters.pop("remote", False)
        use_local = self._local is not None and not remote

        if use_local:
            self._log_local_use()
            if "dq_flags" in filters:
                filters["dq_flags"] = self._flag_to_dict(filters["dq_flags"], DQFlags)
            if filters.get("redshift_quality"):
                filters["redshift_quality"] = self._normalize_quality(filters["redshift_quality"])
            filters.setdefault("limit", 999999)
            yield from self._local.query_spectra(**filters)
            return

        yield from self._api.iter_spectra(**filters)

    def get_spectrum(self, spectrum_id: str) -> Optional[dict]:
        """Return a single spectrum row by spectrum_id."""
        if self._local is not None:
            row = self._local.get_spectrum(spectrum_id)
            if row:
                return row
        # Remote fallback: query by search on spectrum_id
        rows, _ = self._api.query_spectra(search=spectrum_id, limit=1)
        for row in rows:
            if row.get("spectrum_id") == spectrum_id:
                return row
        return None

    # -------------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------------
    def get_metadata(self) -> dict:
        return self._api.get_metadata()

    def get_programs(self) -> Table:
        metadata = self._api.get_metadata()
        programs = metadata.get("programs", [])
        if len(programs) == 0:
            return Table()
        return Table(rows=programs)

    def get_fields(self) -> List[str]:
        return self._api.get_metadata().get("fields", [])

    def get_gratings(self) -> List[str]:
        return self._api.get_metadata().get("gratings", [])

    def get_observations(self) -> List[str]:
        return self._api.get_metadata().get("observations", [])

    def get_tags(self) -> Table:
        if self._local is not None:
            rows = self._local.get_tags()
        else:
            rows = self._api.fetch_tags()
        if not rows:
            return Table()
        return Table(rows=rows)

    # -------------------------------------------------------------------------
    # Spectrum data (for plotting)
    # -------------------------------------------------------------------------
    def get_spectrum_data(self, spectrum_id: str) -> dict:
        """Fetch spectrum JSON data for plotting, keyed by spectrum_id."""
        return self._api.get_spectrum_data(spectrum_id)

    def get_redshift_fit_data(self, spectrum_id: str) -> dict:
        """Fetch redshift fitting results, keyed by spectrum_id."""
        return self._api.get_redshift_fit_data(spectrum_id)

    # -------------------------------------------------------------------------
    # Spectrum FITS access
    # -------------------------------------------------------------------------
    def open_spectrum(self, spectrum_id: str) -> SpectrumData:
        """Open a spectrum (identified by spectrum_id) as a SpectrumData.

        Looks up ``fits_path`` in the local store first, then the API if
        not synced. Downloaded files are cached in the managed data
        directory (when available) so subsequent calls are instant.
        """
        spec_info = self._resolve_spectrum_info(spectrum_id)
        fits_path = spec_info["fits_path"]
        grating = spec_info.get("grating", "")

        # Try local first
        if self._local and self._products_dir:
            local_path = self._local.find_local_path(spectrum_id)
            if local_path:
                full_path = self._products_dir / local_path
                if full_path.exists():
                    self._api_download_count = 0
                    return SpectrumData.from_fits(
                        str(full_path), spectrum_id=spectrum_id, grating=grating,
                    )

        self._api_download_count += 1
        if self._api_download_count == 3:
            import warnings
            warnings.warn(
                "Downloading spectra one at a time from the API. "
                "For bulk access, use cf.download() first, then open_spectrum() "
                "will read from local files.",
                stacklevel=2,
            )

        signed_url = self._api.get_signed_url(fits_path)
        filename = Path(fits_path).name

        if self._local and self._products_dir:
            from .config import products_relpath
            # Land the file where the pipeline writes and deploy reads it, via the
            # shared layout contract (products/nirspec/<obs>/…), not products/<obs>/.
            dest = self._products_dir / products_relpath(fits_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            import tempfile
            dest = Path(tempfile.mkdtemp(prefix="campfire_")) / filename

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

        if self._local and self._products_dir:
            from .config import products_relpath
            local_rel_path = products_relpath(fits_path)
            st = dest.stat()
            # fits_path is the storage key for the final product, so record local
            # state directly on its storage_objects mirror row.
            self._local.mark_object_synced(
                storage_key=fits_path,
                local_path=local_rel_path,
                local_file_hash=f"sha256:{sha256.hexdigest()}",
                local_file_size=st.st_size,
                local_file_mtime=st.st_mtime,
            )

        return SpectrumData.from_fits(
            str(dest), spectrum_id=spectrum_id, grating=grating,
        )

    def _resolve_spectrum_info(self, spectrum_id: str) -> dict:
        """Resolve fits_path + observation + grating for a spectrum_id."""
        if self._local is not None:
            row = self._local.get_spectrum(spectrum_id)
            if row:
                return row

        rows, _ = self._api.query_spectra(search=spectrum_id, limit=5)
        for row in rows:
            if row.get("spectrum_id") == spectrum_id:
                return row

        raise NotFoundError(f"Spectrum not found: {spectrum_id}")

    # -------------------------------------------------------------------------
    # Imaging (cutouts + shutters)
    # -------------------------------------------------------------------------
    def get_cutout(
        self,
        object_id: str,
        size: Optional[int] = None,
        fov: float = 5.0,
        cache: bool = True,
    ) -> Path:
        """Download a cutout PNG for an object.

        Returns the path to the cached PNG file.
        """
        fov_str = format(fov, "g")
        size_tag = f"_s{size}" if size is not None else ""
        filename = f"{object_id}_fov{fov_str}{size_tag}.png"

        from .config import resolve_data_dir
        cutouts = resolve_data_dir() / "cutouts"

        dest = _safe_cache_path(cutouts, filename, object_id)
        if cache and dest.exists():
            return dest

        png_data = self._api.get_cutout(object_id, size=size, fov=fov)

        cutouts.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        try:
            tmp.write_bytes(png_data)
            tmp.rename(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return dest

    def get_fits_cutout(
        self,
        field: str,
        ra: float,
        dec: float,
        fov: float = 10.0,
        bands: Optional[List[str]] = None,
        scale: Optional[float] = None,
        cache: bool = True,
    ):
        """Download a science FITS cutout from a field's FitsGL tile pyramid.

        A direct crop of the tiles at the requested (default native) pyramid
        level — no resampling, no stretch. The returned ``HDUList`` has one
        float32 IMAGE extension per band (``EXTNAME`` = band, e.g.
        ``hdul['F444W']``), each carrying the pyramid level's WCS. Pixels are
        the display pyramid's RICE-quantized values (~0.03%
        photometry-faithful; flagged in the FITS headers).

        Parameters
        ----------
        field : str
            Field name (must have a deployed FitsGL dataset).
        ra, dec : float
            ICRS centre in degrees.
        fov : float, optional
            Square field of view in arcseconds (default 10, max 600).
        bands : list of str, optional
            Band subset (e.g. ``["f277w", "f444w"]``). Default: every band.
        scale : float, optional
            Output pixel scale in arcsec/px — selects a coarser pyramid level
            for wide fields. Default: native.
        cache : bool, optional
            Reuse a previously downloaded file when present (default True).

        Returns
        -------
        astropy.io.fits.HDUList
            Opened from the cached file (memory-mapped).

        Examples
        --------
        >>> cf = Campfire()
        >>> hdul = cf.get_fits_cutout('cosmos', ra=150.1, dec=2.2, fov=6)
        >>> data, header = hdul['F444W'].data, hdul['F444W'].header
        """
        from astropy.io import fits

        band_tag = f"_{'-'.join(bands)}" if bands else ""
        scale_tag = f"_s{format(scale, 'g')}" if scale is not None else ""
        # repr() is the shortest lossless float text — rounded keys (e.g. .5f)
        # would silently alias nearby centres (~a native pixel at 1e-5 deg)
        # to the same cached cutout.
        filename = (
            f"{field}_{ra!r}_{dec!r}_fov{format(fov, 'g')}{band_tag}{scale_tag}.fits"
        )

        from .config import resolve_data_dir
        cutouts = resolve_data_dir() / "cutouts"

        dest = _safe_cache_path(cutouts, filename, field)
        if cache and dest.exists():
            return fits.open(dest)

        fits_data = self._api.get_fits_cutout(
            field, ra, dec, fov=fov, bands=bands, scale=scale,
        )

        cutouts.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        try:
            tmp.write_bytes(fits_data)
            tmp.rename(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return fits.open(dest)

    def get_cutout_figure(
        self,
        field: str,
        ra: float,
        dec: float,
        fov: float = 10.0,
        bands: Optional[List[str]] = None,
        size: int = 300,
        cols: Optional[int] = None,
        stretch: str = "asinh",
        colormap: str = "gray",
        cache: bool = True,
    ) -> Path:
        """Download a multi-band cutout figure PNG (one labeled panel per band).

        Rendered server-side from the field's FitsGL pyramid with the same
        transfer functions as the web map; per-panel percentile stretch.
        Returns the path to the cached PNG.

        Parameters
        ----------
        field : str
            Field name (must have a deployed FitsGL dataset).
        ra, dec : float
            ICRS centre in degrees.
        fov : float, optional
            Square field of view in arcseconds (default 10, max 600).
        bands : list of str, optional
            Band subset; default every band, in deployed inventory order.
        size : int, optional
            Panel edge in pixels (default 300, max 1024).
        cols : int, optional
            Panels per row (default: all in one row).
        stretch : str, optional
            One of ``linear``, ``log``, ``sqrt``, ``asinh`` (default).
        colormap : str, optional
            e.g. ``gray`` (default), ``viridis``, ``magma``, ``inferno``.
        cache : bool, optional
            Reuse a previously downloaded file when present (default True).
        """
        band_tag = f"_{'-'.join(bands)}" if bands else ""
        cols_tag = f"_c{cols}" if cols is not None else ""
        # repr() keys: lossless coordinates, no near-centre cache aliasing.
        filename = (
            f"{field}_{ra!r}_{dec!r}_fov{format(fov, 'g')}{band_tag}"
            f"_p{size}{cols_tag}_{stretch}_{colormap}.png"
        )

        from .config import resolve_data_dir
        cutouts = resolve_data_dir() / "cutouts"

        dest = _safe_cache_path(cutouts, filename, field)
        if cache and dest.exists():
            return dest

        png_data = self._api.get_cutout_figure(
            field, ra, dec, fov=fov, bands=bands, size=size, cols=cols,
            stretch=stretch, colormap=colormap,
        )

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
        """Get shutter geometry near an object."""
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

    def get_shutters_region(
        self,
        object_id: str,
        fov: float = 5.0,
        output: Optional[Union[str, Path]] = None,
        cache: bool = True,
    ) -> Path:
        """Download nearby shutter geometry as a DS9 region (.reg) file.

        Member-spectrum slitlets are coloured green and labelled with their
        ``target_id`` (``{observation}_{source_id}``); other nearby shutters
        are grey. Stuck-closed shutters are drawn red dashed.

        Parameters
        ----------
        object_id : str
            Object identifier (IAU name).
        fov : float, optional
            Search radius in arcseconds (default 5).
        output : str or Path, optional
            Destination path. Defaults to the cutout cache directory.
        cache : bool, optional
            Reuse a cached .reg file if present (default True).

        Returns
        -------
        Path
            Path to the written .reg file.
        """
        from .config import resolve_data_dir
        from .imaging import shutters_to_ds9_region

        fov_str = format(fov, "g")
        filename = f"{object_id}_fov{fov_str}_shutters.reg"

        if output is not None:
            dest = Path(output)
        else:
            cutouts = resolve_data_dir() / "cutouts"
            dest = _safe_cache_path(cutouts, filename, object_id)

        if cache and dest.exists():
            return dest

        shutter_data = self.get_shutters(object_id, fov=fov, cache=cache)
        region_text = shutters_to_ds9_region(
            shutter_data, target_object_id=object_id
        )

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            tmp.write_text(region_text)
            tmp.rename(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return dest

    def plot_cutout(
        self,
        object_id: str,
        fov: float = 5.0,
        size: Optional[int] = None,
        shutters: Union[bool, str] = True,
        ax=None,
        **kwargs,
    ):
        """Plot a cutout image with optional vector shutter overlay."""
        from .imaging import plot_cutout

        path = self.get_cutout(object_id, size=size, fov=fov)

        shutter_data = None
        if shutters and shutters is not False:
            result = self.get_shutters(object_id, fov=fov)
            if shutters == "target":
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
