"""Main CAMPFIRE API client."""

import hashlib
import logging
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
from .models import SpectrumData

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

        resolved_dir = self._resolve_data_dir(data_dir)
        if resolved_dir:
            meta_dir = resolved_dir / "meta"
            db_path = meta_dir / "campfire.db"
            if db_path.exists():
                from .db.store import LocalStore, SchemaMismatchError
                try:
                    self._local = LocalStore(db_path)
                except SchemaMismatchError:
                    self._local = None
                else:
                    self._products_dir = resolved_dir / "products"
                    self._meta_dir = meta_dir

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
        """Download FITS files for matching spectra."""
        from .api.session import create_download_session
        from .sync import download_observation

        if self._local is None:
            raise ValidationError("No local catalog. Run cf.sync() first.")

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

    def get_object(self, object_id: str) -> Optional[dict]:
        """Return a single object (with embedded spectra) by object_id."""
        if self._local is not None:
            obj = self._local.get_object(object_id)
            if obj:
                return obj
        # Remote fallback: search on object_id
        objects, _ = self._api.query_objects(search=object_id, limit=1)
        for obj in objects:
            if obj.get("object_id") == object_id:
                return obj
        return None

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
        """
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
            observation = spec_info.get("observation") or ""
            obs_dir = self._products_dir / observation if observation else self._products_dir
            obs_dir.mkdir(parents=True, exist_ok=True)
            dest = obs_dir / filename
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
            observation = spec_info.get("observation") or ""
            local_rel_path = f"{observation}/{filename}" if observation else filename
            st = dest.stat()
            self._local.mark_synced(
                spectrum_id=spectrum_id,
                local_path=local_rel_path,
                file_hash=f"sha256:{sha256.hexdigest()}",
                file_size=file_size,
                local_file_mtime=st.st_mtime,
                local_file_size=st.st_size,
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
