"""Content hashing — the single implementation for both transfer directions.

Three kinds of content identity live here:

* **Whole-file** (``hash_file`` / ``compute_file_hash``): the authoritative
  ``content_hash`` stored in the ``storage_objects`` registry and verified on
  download. Byte-exact, so it churns whenever a FITS is re-saved with fresh
  header timestamps.
* **Science-only** (``sci_dq_hash``): SHA-256 over the SCI+DQ+CFMASK arrays of
  a FITS file — the pixel half of the *change-detection* identity for push
  dedup (epic #261, D1). Stable across a science-identical re-save, which is
  exactly why whole-file hashes can't drive "should I re-upload this exposure".
* **Astrometric** (``wcs_hash``): SHA-256 over the WCS-defining header cards.
  The array digests alone are blind to a re-*alignment*: ``align`` (and
  ``wcs_shift``) rewrite an exposure's WCS without touching a single SCI or DQ
  pixel, so a re-aligned exposure used to dedup as "unchanged" and the cloud
  copy kept its pre-alignment astrometry forever. The two digests together are
  the exposure identity — see :func:`exposure_identity` and
  ``campfire.storage.plan``.

Historically ``campfire/sync.py`` and ``campfire/deploy/registry.py`` each had
their own streamer (64 KB vs 1 MB chunks, hash-only vs hash+size); both now
delegate here.
"""

from __future__ import annotations

import hashlib
import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional, Tuple

_HASH_CHUNK = 1 << 20  # 1 MB: one sequential read beats many small reads on NFS


def hash_file(path: Path) -> tuple[str, int]:
    """Return ``('sha256:<hex>', size_bytes)`` for a local file, streamed."""
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return f"sha256:{h.hexdigest()}", size


def compute_file_hash(path: Path) -> str:
    """SHA-256 of a file as ``'sha256:<hex>'`` (hash only, no size)."""
    return hash_file(Path(path))[0]


def default_hash_workers() -> int:
    """Thread count for parallel local-file hashing.

    Whole-file (and NIRCam SCI+DQ) hashing of a field's worth of exposures is
    I/O-bound — the reads overlap well across threads, so a small pool cuts the
    serial hash time to a fraction. Sized off the CPU count and capped so we
    don't thrash a spinning disk with too many concurrent large reads.
    """
    return min(16, (os.cpu_count() or 4) * 2)


def hash_files_parallel(
    paths: Iterable[Path], *, max_workers: Optional[int] = None,
    progress_desc: Optional[str] = None,
) -> dict[Path, tuple[str, int]]:
    """Hash many local files concurrently → ``{path: ('sha256:<hex>', size)}``.

    Order-independent (callers key by path). De-duplicates repeated paths so a
    file is hashed once. Falls back to a serial loop for a single file.
    ``progress_desc`` shows a tqdm bar — hashing hundreds of GB off NFS takes
    minutes, and silence there is indistinguishable from a hang.
    """
    unique = list({Path(p) for p in paths})
    if not unique:
        return {}
    workers = max(1, min(max_workers or default_hash_workers(), len(unique)))
    if workers == 1 and not progress_desc:
        return {p: hash_file(p) for p in unique}
    pbar = None
    if progress_desc:
        from tqdm import tqdm
        pbar = tqdm(total=len(unique), desc=progress_desc, unit='file')
    out: dict[Path, tuple[str, int]] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(hash_file, p): p for p in unique}
            for fut in as_completed(futures):
                out[futures[fut]] = fut.result()
                if pbar:
                    pbar.update(1)
    finally:
        if pbar:
            pbar.close()
    return out


# --------------------------------------------------------------------------
# Astrometric identity
# --------------------------------------------------------------------------

# The header cards that define an exposure's sky mapping. Deliberately a
# whitelist, not "every card except a denylist": the digest must change when the
# astrometry changes and NEVER on a plain re-save, and only an explicit set can
# promise the second half (a denylist silently admits every new volatile keyword
# the calibration software starts writing, and each one costs a spurious
# re-upload of a ~40 MB exposure over a slow link).
#
# Covered: the core FITS WCS (CTYPE/CRPIX/CRVAL/CDELT/CD/PC/CROTA + celestial
# frame), the SIP distortion coefficients ``update_fits_wcsinfo`` refreshes from
# the gwcs after a solve, the JWST aperture-reference values, and S_REGION (the
# recorded sky footprint). None of these are touched by a re-save that does not
# move the WCS.
_WCS_KEY_RE = re.compile(
    r'^(?:'
    r'WCSAXES|RADESYS|EQUINOX|LONPOLE|LATPOLE'
    r'|C(?:TYPE|UNIT|RPIX|RVAL|DELT|ROTA)\d+'
    r'|CD\d+_\d+|PC\d+_\d+'
    r'|[AB]P?_ORDER|[AB]P?_DMAX|[AB]P?_\d+_\d+'
    r'|V[23]_REF|VPARITY|VA_SCALE|ROLL_REF|RA_REF|DEC_REF'
    r'|S_REGION'
    r')$'
)

# Bumped if the recipe below ever changes, so a digest computed under an old
# rule can never compare equal to one computed under a new rule.
_WCS_DIGEST_VERSION = b'campfire-wcs-1\n'

# Headers scanned for WCS cards: the primary (S_REGION and friends live there)
# and SCI (where the datamodel writes the FITS WCS + SIP).
_WCS_HEADER_EXTS = (0, 'SCI')


def _wcs_digest_from_hdul(hdul) -> Optional[str]:
    """``'sha256:<hex>'`` over the WCS cards of an open HDUList, or None.

    None means *no WCS cards at all* (a product with no sky mapping, or a
    header we could not read) — callers must treat that as "this file has no
    astrometric component", never as a match.
    """
    h = hashlib.sha256()
    h.update(_WCS_DIGEST_VERSION)
    found = False
    for ext in _WCS_HEADER_EXTS:
        try:
            header = hdul[ext].header
        except (KeyError, IndexError):
            continue
        # Sorted, so card ORDER in the file can never move the digest; the
        # value's repr() carries a float's full precision and round-trips
        # exactly, so a re-save that rewrites the same numbers hashes the same.
        for key in sorted(k for k in header if _WCS_KEY_RE.match(k)):
            h.update(f'{ext}|{key}={header[key]!r}\n'.encode('utf-8', 'replace'))
            found = True
    return f'sha256:{h.hexdigest()}' if found else None


def wcs_hash(path) -> Optional[str]:
    """Return ``'sha256:<hex>'`` over a FITS file's WCS header cards, or None.

    The astrometric half of the exposure identity (see
    :func:`exposure_identity`). Header-only, so it costs a header parse rather
    than a data read.

    **Known limit.** This digests the *FITS* WCS — the SIP approximation
    ``jwst.assign_wcs.util.update_fits_wcsinfo`` writes from the authoritative
    gwcs on every solve — not the gwcs in the embedded ASDF extension. The ASDF
    bytes cannot be hashed directly (a datamodel save stamps a fresh
    ``meta.date`` into them, so they churn on every re-save and would defeat
    the whole point of a partial digest). The two disagree only when
    ``update_fits_wcsinfo`` fails, which the align step logs loudly as
    "FITS SIP keywords not refreshed".
    """
    from astropy.io import fits

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with fits.open(path, memmap=False, lazy_load_hdus=True) as hdul:
                return _wcs_digest_from_hdul(hdul)
    except Exception:
        return None


def sci_dq_hash(path) -> Optional[str]:
    """Return ``'sha256:<hex>'`` over the SCI+DQ+CFMASK arrays, or None.

    The science-only change-detection digest for the push dedup (epic #261,
    D1). Reproduces ``campfire_pipeline.nircam.manifest.compute_file_hash``
    client-side (the client must not import the pipeline):
    ``do_not_scale_image_data=True`` hashes the raw stored bytes regardless of
    BZERO/BSCALE, ``memmap=False`` forces a real sequential read.

    CFMASK (the user manual-mask extension) is included so a mask edit — which
    the N7 freeze records as CFMASK on the canonical without touching SCI/DQ —
    still re-uploads the exposure. It is hashed *last*, and the ``not in hdul``
    guard skips it when absent, so an un-masked exposure hashes byte-identically
    to the old SCI+DQ-only digest (no spurious re-upload on the first
    post-N7 deploy).

    Returns None if none of the arrays are present or the file is unreadable
    (never dedup on an empty hash). This is the *pixel* half of an exposure's
    identity only — pair it with :func:`wcs_hash`, or call
    :func:`exposure_identity` to get both from one open.
    """
    from astropy.io import fits

    h = hashlib.sha256()
    hashed_any = False
    try:
        with fits.open(path, memmap=False, do_not_scale_image_data=True) as hdul:
            for extname in ('SCI', 'DQ', 'CFMASK'):
                if extname not in hdul:
                    continue
                data = hdul[extname].data
                if data is not None:
                    h.update(data.tobytes())
                    hashed_any = True
    except Exception:
        return None
    return f'sha256:{h.hexdigest()}' if hashed_any else None


def exposure_identity(path) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(sci_dq_hash, wcs_hash)`` for one exposure, from a single open.

    The full change-detection identity of a NIRCam canonical exposure: what the
    pixels are *and* where they are on the sky. Computed together because the
    pixel digest already reads the whole file — pulling the WCS cards out of the
    same HDUList costs nothing, whereas a second :func:`wcs_hash` call would
    re-open (and on NFS, re-read) it.

    Either element is None when that component is absent or unreadable; see
    ``campfire.storage.plan`` for how each None is interpreted (never as a
    match).
    """
    from astropy.io import fits

    h = hashlib.sha256()
    hashed_any = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with fits.open(path, memmap=False,
                           do_not_scale_image_data=True) as hdul:
                for extname in ('SCI', 'DQ', 'CFMASK'):
                    if extname not in hdul:
                        continue
                    data = hdul[extname].data
                    if data is not None:
                        h.update(data.tobytes())
                        hashed_any = True
                wcs = _wcs_digest_from_hdul(hdul)
    except Exception:
        return None, None
    return (f'sha256:{h.hexdigest()}' if hashed_any else None), wcs


def sci_dq_hashes_parallel(
    paths: Iterable[Path], *, max_workers: Optional[int] = None,
    progress_desc: Optional[str] = None,
) -> dict[Path, Optional[str]]:
    """Compute :func:`sci_dq_hash` for many files concurrently → ``{path: hash}``.

    The reads are I/O-bound and overlap across threads. A per-file failure
    records ``None`` (never dedup on it). When ``progress_desc`` is given, a
    tqdm bar tracks the pass — computing these digests is the dominant local
    cost before a NIRCam upload starts.
    """
    unique = list({Path(p) for p in paths})
    if not unique:
        return {}
    workers = max(1, min(max_workers or default_hash_workers(), len(unique)))
    out: dict[Path, Optional[str]] = {}
    pbar = None
    if progress_desc:
        from tqdm import tqdm
        pbar = tqdm(total=len(unique), desc=progress_desc, unit='file')
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(sci_dq_hash, p): p for p in unique}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    out[path] = fut.result()
                except Exception:
                    out[path] = None
                if pbar:
                    pbar.update(1)
    finally:
        if pbar:
            pbar.close()
    return out


def exposure_identities_parallel(
    paths: Iterable[Path], *, max_workers: Optional[int] = None,
    progress_desc: Optional[str] = None,
) -> dict[Path, Tuple[Optional[str], Optional[str]]]:
    """:func:`exposure_identity` for many files concurrently.

    Returns ``{path: (sci_dq_hash, wcs_hash)}``. Same shape and failure
    semantics as :func:`sci_dq_hashes_parallel` — a per-file failure records
    ``(None, None)`` so it can never be mistaken for a match — and the same
    tqdm bar, since this pass is the dominant local cost before a NIRCam
    upload starts.
    """
    unique = list({Path(p) for p in paths})
    if not unique:
        return {}
    workers = max(1, min(max_workers or default_hash_workers(), len(unique)))
    out: dict[Path, Tuple[Optional[str], Optional[str]]] = {}
    pbar = None
    if progress_desc:
        from tqdm import tqdm
        pbar = tqdm(total=len(unique), desc=progress_desc, unit='file')
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(exposure_identity, p): p for p in unique}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    out[path] = fut.result()
                except Exception:
                    out[path] = (None, None)
                if pbar:
                    pbar.update(1)
    finally:
        if pbar:
            pbar.close()
    return out
