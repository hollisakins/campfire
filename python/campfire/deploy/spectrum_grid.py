"""NIRSpec nods-grid population (P4, design §4.2).

Populates the revived ``spectrum_exposures`` table — the render grid the web nods
renderer (P5) reads — from the canonical per-source spectrum-exposure files deploy
already discovers/uploads. Rows are grouped ``rows=(exp_group, nod) × cols=detector``
per ``(observation, source_id)``, matching the pipeline's ``*_nods.pdf`` exactly
because ``exp_group`` comes from the pipeline-stamped ``CFEXPGRP`` header card (not
reconstructed here — it depends on the whole exposure set's dither pattern).

Split ownership mirrors ``rate_exposures`` / ``nircam._upsert_exposures``: a
re-deploy UPDATE writes ONLY identity/render columns and OMITS
``review_status``/``masking``/``notes`` so web triage is never clobbered; new rows
seed ``review_status='pending'``, ``masking='none'``.

Terminology note: ``spectrum_exposures.exposure_root`` is the pipeline's 2-token
root (``jw07076020001_04101``) with the exposure token broken out as ``nod`` — this
DIFFERS from ``nirspec_rate_exposures.exposure_root`` (3 tokens, detector-stripped).
The divergence is deliberate: it matches the pipeline's own ``root``/``nod`` split
(``observation.group_files``) and the nods grid key.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from campfire_layout import KeyScheme, Scope, storage_key

_RATE_SUFFIX = "_rate.fits"
_SPEC_SUFFIX = "_spec.fits"


def parse_spectrum_exposure_filename(name: str) -> tuple[str, str, str, int]:
    """``jw07076020001_04101_00001_nrs1_117757.fits`` → (exposure_root, nod, detector, source_id).

    Anchors on the ``nrs[12]`` token (the only unambiguous marker — source ids are
    numeric and could be mistaken for other tokens): ``exposure_root`` is everything
    before ``nod``, ``nod`` is the token before the detector, ``detector`` is
    ``nrs1``/``nrs2``, ``source_id`` is the token after. Raises ValueError for
    non-canonical names (rate files, finals, or a missing/int-unparseable source).
    """
    if not name.endswith(".fits") or name.endswith(_RATE_SUFFIX) or name.endswith(_SPEC_SUFFIX):
        raise ValueError(f"not a canonical spectrum-exposure filename: {name!r}")
    tokens = name[: -len(".fits")].split("_")
    det_idx = next((i for i, t in enumerate(tokens) if t in ("nrs1", "nrs2")), None)
    if det_idx is None or det_idx < 2 or det_idx + 1 >= len(tokens):
        raise ValueError(f"no nrs[12] token / malformed: {name!r}")
    detector = tokens[det_idx]
    nod = tokens[det_idx - 1]
    exposure_root = "_".join(tokens[:det_idx - 1])
    try:
        source_id = int(tokens[det_idx + 1])
    except ValueError:
        raise ValueError(f"non-integer source id in {name!r}") from None
    return exposure_root, nod, detector, source_id


def read_spectrum_metadata(path: Path) -> tuple[int | None, str | None, int | None, int | None]:
    """Best-effort ``(exp_group, grating, image_width, image_height)`` from a canonical FITS.

    ``exp_group`` from the ``CFEXPGRP`` primary card (None if the file predates the
    P4 stamp — the row is still created, the web falls back gracefully), ``grating``
    from ``GRATING``, dims from the ``S2D_SCI`` rectified view (what the renderer
    shows) falling back to ``SCI``. Any failure degrades to None, like
    ``rate_exposures.read_rate_metadata``.
    """
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            phdr = hdul[0].header
            eg = phdr.get("CFEXPGRP")
            exp_group = int(eg) if eg is not None else None
            grating = phdr.get("GRATING")
            width = height = None
            for ext in ("S2D_SCI", "SCI"):
                if ext in hdul:
                    h = hdul[ext].header
                    width, height = h.get("NAXIS1"), h.get("NAXIS2")
                    break
            return exp_group, grating, width, height
    except Exception:
        return None, None, None, None


def build_spectrum_exposure_records(
    exposure_files: list[Path], *, observation: str, scope: Scope,
) -> list[dict]:
    """One grid record per canonical file (pre-partition; identity + render columns)."""
    records = []
    for path in exposure_files:
        exposure_root, nod, detector, source_id = parse_spectrum_exposure_filename(path.name)
        exp_group, grating, width, height = read_spectrum_metadata(path)
        records.append({
            "observation": observation,
            "exposure_root": exposure_root,
            "nod": nod,
            "detector": detector,
            "source_id": source_id,
            "exp_group": exp_group,
            "grating": grating,
            "filename": path.name,
            "image_width": width,
            "image_height": height,
            "storage_key": storage_key(
                "nirspec_spectrum_exposure", scope, path.name, scheme=KeyScheme.CANONICAL),
            "stage": "cal",
        })
    return records


# Columns deploy owns on a re-register (everything else is web-owned triage).
_IDENTITY_COLS = ("observation", "exposure_root", "nod", "detector", "source_id", "filename", "stage")
_RENDER_COLS = ("exp_group", "grating", "image_width", "image_height", "storage_key")


def partition_spectrum_records(
    records: list[dict], existing_keys: set[tuple], now: str,
) -> tuple[list[dict], list[dict]]:
    """Split into (new_rows, update_rows) with split ownership.

    New rows seed ``review_status='pending'``/``masking='none'``. Update rows carry
    ONLY identity + non-null render columns (+ ``updated_at``) — never the web-owned
    triage columns — so a re-deploy leaves reviewer decisions intact.
    """
    new_records, update_records = [], []
    for r in records:
        key = (r["observation"], r["exposure_root"], r["nod"], r["detector"], r["source_id"])
        if key in existing_keys:
            update = {c: r[c] for c in _IDENTITY_COLS}
            update["updated_at"] = now
            for c in _RENDER_COLS:
                if r.get(c) is not None:
                    update[c] = r[c]
            update_records.append(update)
        else:
            new_records.append({
                **r,
                "review_status": "pending",
                "masking": "none",
                "created_at": now,
                "updated_at": now,
            })
    return new_records, update_records


def upsert_spectrum_exposures(client, records: list[dict], batch_size: int = 500) -> int:
    """Upsert grid rows, preserving web triage on existing rows. Returns the row count.

    Mirrors ``rate_exposures.upsert_rate_exposures``: batched SELECT to find existing
    conflict tuples, then a plain ``.insert()`` for new rows and an
    ``.upsert(on_conflict=…)`` for existing rows with the identity/render-only dict.
    """
    if not records:
        return 0

    filenames = [r["filename"] for r in records]
    existing: set[tuple] = set()
    for i in range(0, len(filenames), batch_size):
        batch = filenames[i:i + batch_size]
        resp = (client.table("spectrum_exposures")
                .select("observation, exposure_root, nod, detector, source_id, filename")
                .in_("filename", batch)
                .execute())
        for row in resp.data:
            existing.add((row["observation"], row["exposure_root"], row["nod"],
                          row["detector"], row["source_id"]))

    now = datetime.now(timezone.utc).isoformat()
    new_records, update_records = partition_spectrum_records(records, existing, now)

    for i in range(0, len(new_records), batch_size):
        client.table("spectrum_exposures").insert(new_records[i:i + batch_size]).execute()
    for i in range(0, len(update_records), batch_size):
        client.table("spectrum_exposures").upsert(
            update_records[i:i + batch_size],
            on_conflict="observation,exposure_root,nod,detector,source_id",
        ).execute()

    return len(records)
