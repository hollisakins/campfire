"""NIRSpec rate-exposure triage upsert (P2, design §3.2).

Populates the ``nirspec_rate_exposures`` table — the detector-grain analogue of
``nircam_exposures`` — from the rate files ``discover_rate_files`` finds. Split
ownership mirrors ``nircam._upsert_exposures``: a re-deploy UPDATE writes ONLY the
identity/render columns and OMITS ``review_status``/``mask_regions``/``notes`` so
it never clobbers web triage; new rows seed ``review_status='pending'``. Mask state
is entirely web-owned — it lives in ``mask_regions`` and deploy never touches it.

Pure helpers (``parse_rate_filename``, ``partition_rate_records``) are unit-tested
without a DB; the full insert/upsert round-trip is exercised live against local
Supabase in CI (``campfire deploy … --local``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from campfire_layout import KeyScheme, Scope, storage_key

_RATE_SUFFIX = "_rate.fits"


def parse_rate_filename(name: str) -> tuple[str, str]:
    """``jw07076020001_04101_00001_nrs1_rate.fits`` → (exposure_root, detector).

    ``exposure_root`` is the rate rootname stem WITHOUT the detector token
    (``jw07076020001_04101_00001``); ``detector`` is ``nrs1``|``nrs2``. Note this
    differs from the registry ``exposure_ref`` (P1), which keeps the full stem
    including detector + ``_rate``. Unambiguous because the detector token is
    always exactly ``nrs1``/``nrs2``.
    """
    if not name.endswith(_RATE_SUFFIX):
        raise ValueError(f"not a rate filename: {name!r}")
    stem = name[: -len(_RATE_SUFFIX)]           # jw..._nrs1
    root, detector = stem.rsplit("_", 1)
    if detector not in ("nrs1", "nrs2"):
        raise ValueError(f"unexpected detector token {detector!r} in {name!r}")
    return root, detector


def read_rate_metadata(path: Path) -> tuple[str | None, int | None, int | None]:
    """Best-effort ``(grating, image_width, image_height)`` from a rate FITS.

    ``GRATING`` from the primary header (mirrors the pipeline, stage1.py); dims
    from the SCI extension's NAXIS1/NAXIS2. Any failure (missing card/extension,
    unreadable file) degrades to all-None, exactly like ``_read_exposure_provenance``.
    """
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            grating = hdul[0].header.get("GRATING")
            sci_hdr = hdul["SCI"].header
            return grating, sci_hdr.get("NAXIS1"), sci_hdr.get("NAXIS2")
    except Exception:
        return None, None, None


# What was observed: column -> (primary-header card, type).
_OBSERVING_CARDS = {
    "filter": ("FILTER", str),
    "exposure_time": ("EFFEXPTM", float),
    "date_obs": ("DATE-OBS", str),
    "readout_pattern": ("READPATT", str),
    "n_groups": ("NGROUPS", int),
    "n_integrations": ("NINTS", int),
    "dither_position": ("PATT_NUM", int),
    "n_dither_positions": ("NUMDTHPT", int),
}


def read_observing_metadata(path: Path) -> dict:
    """The exposures-table columns of one rate file (what was observed), from its
    primary header. A card that is absent or unreadable leaves its column out, so a
    re-deploy never blanks it; an unreadable file gives nothing."""
    try:
        from astropy.io import fits
        h = fits.getheader(path, 0)
    except Exception:
        return {}
    out = {}
    for col, (card, cast) in _OBSERVING_CARDS.items():
        value = h.get(card)
        if value is None or (cast is str and not str(value).strip()):
            continue
        try:
            out[col] = cast(value).strip() if cast is str else cast(value)
        except (TypeError, ValueError):
            continue
    return out


_MBKG_SUFFIX = "_mbkg.fits"

# The pipeline's labels for which master background an exposure uses (MBKGSRC on the
# _mbkg.fits primary, MBKGPSRC on the rate file's MBKG1DR header) -> the portal's.
# 'pool' is the master background combined over the exposures that agree; 'group' is
# that combination used because the exposure has too few empty shutters of its own.
_MBKG_SOURCE = {"pool": "combined", "group": "combined", "exposure": "exposure"}


def _mbkg_source(label) -> str | None:
    return _MBKG_SOURCE.get(str(label).strip()) if label is not None else None


def read_mbkg_metadata(rate_path: Path, scope: Scope) -> dict:
    """The master-background columns of one rate file's row, from the
    ``{root}_{nrsN}_mbkg.fits`` beside it (end of stage 2a) and the step-1 table
    header in the rate file itself (``MBKG1DR``). Columns whose source is absent are
    left out (so a re-deploy without the product never blanks them); any read failure
    degrades the same way."""
    out: dict = {}
    try:
        from astropy.io import fits
    except Exception:
        return out
    try:
        with fits.open(rate_path, memmap=False) as hdul:
            if "MBKG1DR" in hdul:
                src = _mbkg_source(hdul["MBKG1DR"].header.get("MBKGPSRC"))
                if src:
                    out["mbkg_rate_source"] = src
    except Exception:
        pass
    mbkg = rate_path.with_name(rate_path.name[: -len(_RATE_SUFFIX)] + _MBKG_SUFFIX)
    if not mbkg.exists():
        return out
    try:
        h = fits.getheader(mbkg, 0)
    except Exception:
        return out
    out["mbkg_storage_key"] = storage_key(
        "nirspec_mbkg", scope, mbkg.name, scheme=KeyScheme.CANONICAL)
    label = h.get("MBKGSRC")
    if label is not None:
        out["mbkg_source"] = _mbkg_source(label)
    if str(label).strip() == "group":
        out["mbkg_test"] = "too few empty shutters"
    elif h.get("MBKGPDEC") is not None:
        out["mbkg_test"] = str(h["MBKGPDEC"])
    members = h.get("MBKGPMEM")
    if members is not None:
        out["mbkg_exposures"] = [] if str(members) == "none" else str(members).split()
    if h.get("MBKGNSH") is not None:
        out["mbkg_n_empty_shutters"] = int(h["MBKGNSH"])
    if h.get("CFMBKGV") is not None:
        out["mbkg_version"] = str(h["CFMBKGV"])
    return out


def build_rate_exposure_records(
    rate_files: list[Path], *, observation: str, scope: Scope,
) -> list[dict]:
    """One triage record per rate file (pre-partition; identity + render columns)."""
    records = []
    for path in rate_files:
        root, detector = parse_rate_filename(path.name)
        grating, width, height = read_rate_metadata(path)
        records.append({
            "observation": observation,
            "exposure_root": root,
            "detector": detector,
            "filename": path.name,
            "grating": grating,
            "image_width": width,
            "image_height": height,
            "storage_key": storage_key(
                "nirspec_rate", scope, path.name, scheme=KeyScheme.CANONICAL),
            "stage": "rate",
            **read_observing_metadata(path),
            **read_mbkg_metadata(path, scope),
        })
    return records


# Columns deploy owns on a re-register (everything else is web-owned triage).
_IDENTITY_COLS = ("observation", "exposure_root", "detector", "filename", "stage")
_RENDER_COLS = ("grating", "image_width", "image_height", "storage_key")
# What was observed (read_observing_metadata): deploy-owned like the render columns.
_OBSERVING_COLS = tuple(_OBSERVING_CARDS)
# The master background (read from the _mbkg.fits beside the rate file): deploy-owned
# like the render columns. A column a record does not carry is left untouched on its row
# (upsert_rate_exposures sends rows with the same columns together, since postgrest-py
# sends one column list per batch and NULL where a row lacks a key).
_MBKG_COLS = ("mbkg_storage_key", "mbkg_source", "mbkg_rate_source", "mbkg_test",
              "mbkg_exposures", "mbkg_n_empty_shutters", "mbkg_version")


def partition_rate_records(
    records: list[dict], existing_keys: set[tuple[str, str, str]], now: str,
) -> tuple[list[dict], list[dict]]:
    """Split into (new_rows, update_rows) with split ownership.

    New rows seed ``review_status='pending'``. Update rows carry ONLY identity +
    non-null render columns (+ ``updated_at``) — never the web-owned triage
    columns — so a re-deploy leaves reviewer decisions intact.
    """
    new_records, update_records = [], []
    for r in records:
        key = (r["observation"], r["exposure_root"], r["detector"])
        if key in existing_keys:
            update = {c: r[c] for c in _IDENTITY_COLS}
            update["updated_at"] = now
            for c in _RENDER_COLS + _OBSERVING_COLS + _MBKG_COLS:
                if r.get(c) is not None:
                    update[c] = r[c]
            update_records.append(update)
        else:
            new_records.append({
                **r,
                "review_status": "pending",
                "created_at": now,
                "updated_at": now,
            })
    return new_records, update_records


def upsert_rate_exposures(client, records: list[dict], batch_size: int = 500) -> int:
    """Upsert triage rows, preserving web triage on existing rows. Returns the row count.

    Mirrors ``nircam._upsert_exposures``: batched SELECT to find existing conflict
    tuples, then a plain ``.insert()`` for new rows and an ``.upsert(on_conflict=…)``
    for existing rows with the identity/render-only update dict.
    """
    if not records:
        return 0

    filenames = [r["filename"] for r in records]
    existing: set[tuple[str, str, str]] = set()
    for i in range(0, len(filenames), batch_size):
        batch = filenames[i:i + batch_size]
        resp = (client.table("nirspec_rate_exposures")
                .select("observation, exposure_root, detector, filename")
                .in_("filename", batch)
                .execute())
        for row in resp.data:
            existing.add((row["observation"], row["exposure_root"], row["detector"]))

    now = datetime.now(timezone.utc).isoformat()
    new_records, update_records = partition_rate_records(records, existing, now)

    for i in range(0, len(new_records), batch_size):
        client.table("nirspec_rate_exposures").insert(new_records[i:i + batch_size]).execute()
    # A batch upsert sends the union of its rows' keys and NULL where a row lacks one,
    # which would blank a column whose card this file lacks: send rows with the same
    # columns together.
    by_columns: dict[tuple[str, ...], list[dict]] = {}
    for r in update_records:
        by_columns.setdefault(tuple(sorted(r)), []).append(r)
    for group in by_columns.values():
        for i in range(0, len(group), batch_size):
            client.table("nirspec_rate_exposures").upsert(
                group[i:i + batch_size],
                on_conflict="observation,exposure_root,detector",
            ).execute()

    return len(records)
