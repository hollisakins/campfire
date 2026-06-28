"""The key ↔ local-relpath bijection (total + reversible per scheme).

The forward builders (:mod:`paths`, :mod:`keys`) and these reverse parsers must
agree exactly — the golden conformance test pins the round-trip. The legacy
asymmetry the parsers encode: ``spectra/``, ``rgb/``, ``sed/`` keys all map to the
*same* disk dir ``products/nirspec/<obs>/`` and are told apart only by the
filename suffix, so ``key_to_relpath`` dispatches on key *prefix* while
``relpath_to_key`` dispatches on basename *suffix*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import keys as _keys
from . import paths as _paths
from . import products
from .scope import KeyScheme, LayoutError, Scope


@dataclass(frozen=True)
class ParsedKey:
    product_type: str
    scope: Scope
    filename: str


# Ordered suffix tables (most specific first). Within one directory context, a
# filename is matched against these to recover its product_type.
_NIRSPEC_OBS_SUFFIXES = (
    ("_spec.fits", "nirspec_spec"),
    ("_spec.json", "spectrum_json"),
    ("_zfit.json", "zfit"),
    ("_rgb.png", "rgb"),
    ("_sed.pdf", "sed"),
    ("_summary.ecsv", "summary"),
    ("_pointings.ecsv", "pointings"),
    ("_shutters.ecsv", "shutters"),
    ("_config.toml", "nirspec_config"),
)
_NIRCAM_FILTER_SUFFIXES = (
    ("_preview.png", "nircam_exposure_preview"),
    ("_full.png", "nircam_exposure_full"),
)
_EXPOSURE_RE = re.compile(r"_nrs[12]_\d+\.fits$")
_TILE_RE = re.compile(r"^(?P<field>[^/]+)/(?P<filt>[^/]+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png$")


def _dispatch(fname: str, table, fallback: Optional[str] = None) -> Optional[str]:
    for suffix, ptype in table:
        if fname.endswith(suffix):
            return ptype
    return fallback


def _nirspec_obs_product(fname: str) -> str:
    ptype = _dispatch(fname, _NIRSPEC_OBS_SUFFIXES)
    if ptype:
        return ptype
    if fname.endswith(".fits"):
        return "nirspec_spectrum_exposure"
    raise LayoutError(f"unrecognized NIRSpec product filename '{fname}'")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _reject_unsafe(key: str) -> None:
    if not key or key.startswith("/") or "\\" in key:
        raise LayoutError(f"unsafe key '{key}'")
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise LayoutError(f"unsafe key '{key}' (empty or traversal segment)")


# ---------------------------------------------------------------------------
# Relpath parsing (for canonical keys + download placement)
# ---------------------------------------------------------------------------

def parse_relpath(relpath: str) -> ParsedKey:
    """Recover (product_type, scope, filename) from a local relpath."""
    _reject_unsafe(relpath)
    seg = relpath.split("/")
    tree = seg[0]

    if tree == "products" and len(seg) >= 4 and seg[1] == "nirspec":
        obs = seg[2]
        if len(seg) == 5 and seg[3] == "manual_masks":
            return ParsedKey("nirspec_manual_mask", Scope(obs=obs), seg[4])
        if len(seg) == 4:
            fname = seg[3]
            return ParsedKey(_nirspec_obs_product(fname), Scope(obs=obs), fname)

    if tree == "products" and len(seg) >= 4 and seg[1] == "nircam":
        field = seg[2]
        if len(seg) == 4 and seg[3].endswith("_rgb.png"):
            return ParsedKey("nircam_rgb", Scope(field=field), seg[3])
        if len(seg) == 5 and seg[3] == "expmaps":
            return ParsedKey("nircam_expmap", Scope(field=field), seg[4])
        if len(seg) == 5:
            filt, fname = seg[3], seg[4]
            scope = Scope(field=field, filt=filt)
            ptype = _dispatch(fname, _NIRCAM_FILTER_SUFFIXES)
            if ptype:
                return ParsedKey(ptype, scope, fname)
            if fname.startswith("mosaic"):
                return ParsedKey("nircam_mosaic", scope, fname)
            if fname.endswith(".fits"):
                return ParsedKey("nircam_exposure", scope, fname)

    if tree == "reference" and len(seg) >= 4 and seg[1] == "nirspec":
        obs = seg[2]
        fname = seg[-1]
        if fname == "stuck_closed_shutters.toml":
            return ParsedKey("nirspec_stuck_shutters", Scope(obs=obs), fname)
        if fname == "nodded_background_overrides.toml":
            return ParsedKey("nirspec_bkg_override", Scope(obs=obs), fname)

    if tree == "reference" and len(seg) >= 4 and seg[1] == "nircam":
        if seg[2] == "shared" and len(seg) == 5:
            fname = seg[4]
            if seg[3] == "flats":
                return ParsedKey("nircam_flat", Scope(), fname)
            if seg[3] == "wisps":
                return ParsedKey("nircam_wisp", Scope(), fname)
        else:
            field = seg[2]
            kind = seg[3] if len(seg) >= 4 else None
            fname = seg[-1]
            if kind == "masks":
                return ParsedKey("nircam_mask", Scope(field=field), fname)
            if kind == "astrom_cats":
                return ParsedKey("nircam_astrom_cat", Scope(field=field), fname)
            if kind == "bad_pixels":
                return ParsedKey("nircam_bad_pixel", Scope(field=field), fname)

    if tree == "tiles" and len(seg) == 6 and seg[5].endswith(".png"):
        return ParsedKey(
            "tile",
            Scope(field=seg[1], filt=seg[2], zoom=int(seg[3]), x=int(seg[4]),
                  y=int(seg[5].removesuffix(".png"))),
            seg[5],
        )

    if tree == "raw" and len(seg) >= 4 and seg[1] == "nirspec":
        return ParsedKey("raw_nirspec", Scope(data_subdir=seg[2]), seg[-1])
    if tree == "raw" and len(seg) >= 5 and seg[1] == "nircam":
        return ParsedKey("raw_nircam", Scope(pid=seg[2], filt=seg[3]), seg[-1])

    raise LayoutError(f"unrecognized relpath '{relpath}'")


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------

def parse_key(key: str, *, bucket: Optional[str] = None) -> ParsedKey:
    """Recover (product_type, scope, filename) from a storage key.

    Handles canonical (``data/…``) keys, today's legacy keys, and tile keys.
    """
    _reject_unsafe(key)

    # Canonical data-bucket key: 'data/' + relpath (mirrored products), or
    # 'data/' + legacy-form prefix (key-only products like photometry, which have
    # no local relpath to mirror).
    if key.startswith("data/"):
        rest = key[len("data/"):]
        try:
            return parse_relpath(rest)
        except LayoutError:
            return _parse_legacy_key(rest, bucket=bucket)

    return _parse_legacy_key(key, bucket=bucket)


def _parse_legacy_key(key: str, *, bucket: Optional[str] = None) -> ParsedKey:
    """Dispatch a today's-scheme (legacy) key on its prefix."""
    seg = key.split("/")

    # Legacy data-bucket keys (prefix dispatch).
    if seg[0] == "spectra" and len(seg) == 3:
        return ParsedKey(_nirspec_obs_product(seg[2]), Scope(obs=seg[1]), seg[2])
    if seg[0] == "rgb" and len(seg) == 3:
        return ParsedKey("rgb", Scope(obs=seg[1]), seg[2])
    if seg[0] == "sed" and len(seg) == 3:
        return ParsedKey("sed", Scope(obs=seg[1]), seg[2])
    if seg[0] == "nircam" and len(seg) == 5 and seg[1] == "exposures":
        ptype = _dispatch(seg[4], _NIRCAM_FILTER_SUFFIXES)
        if ptype:
            return ParsedKey(ptype, Scope(field=seg[2], filt=seg[3]), seg[4])
    if seg[0] == "photometry" and len(seg) == 3:
        return ParsedKey("photometry_pz", Scope(field=seg[1], object_id=seg[2].removesuffix("_pz.json")), seg[2])

    # Tile keys (separate bucket, no prefix).
    if bucket == "tiles" or _TILE_RE.match(key):
        m = _TILE_RE.match(key)
        if m:
            return ParsedKey(
                "tile",
                Scope(field=m["field"], filt=m["filt"], zoom=int(m["z"]), x=int(m["x"]), y=int(m["y"])),
                f'{m["y"]}.png',
            )

    raise LayoutError(f"unrecognized storage key '{key}'")


# ---------------------------------------------------------------------------
# Public bijection API
# ---------------------------------------------------------------------------

def key_to_relpath(key: str, *, bucket: str = "data") -> str:
    """Map a storage key to its local relpath (raises for key-only products)."""
    pk = parse_key(key, bucket=bucket)
    return _paths.local_relpath(pk.product_type, pk.scope, pk.filename)


def relpath_to_key(relpath: str, *, scheme: KeyScheme = KeyScheme.LEGACY) -> str:
    """Map a local relpath to its storage key under the given scheme."""
    pk = parse_relpath(relpath)
    return _keys.storage_key(pk.product_type, pk.scope, pk.filename, scheme=scheme)


def derive_sibling(key: str, target_product_type: str, *, bucket: Optional[str] = None) -> str:
    """Co-located sibling key, e.g. a spec key → its zfit/json key.

    Preserves the source key's prefix and scheme; only the filename suffix changes.
    """
    pk = parse_key(key, bucket=bucket)
    src = products.get(pk.product_type)
    tgt = products.get(target_product_type)
    if src.suffix is None or tgt.suffix is None:
        raise LayoutError(
            f"cannot derive sibling between '{pk.product_type}' and "
            f"'{target_product_type}' (a suffix discriminator is undefined)"
        )
    base = pk.filename[: -len(src.suffix)]
    new_fname = f"{base}{tgt.suffix}"
    prefix = key.rsplit("/", 1)[0]
    return f"{prefix}/{new_fname}"


def is_known_key(key: str, *, bucket: Optional[str] = None) -> bool:
    """True iff *key* parses to a cloud-backed product (presign/proxy allowlist).

    Rejects traversal/unsafe keys and keys that resolve to non-cloud products.
    """
    try:
        pk = parse_key(key, bucket=bucket)
    except LayoutError:
        return False
    spec = products.get(pk.product_type)
    if spec.bucket is None:
        return False
    if bucket is not None and spec.bucket != bucket:
        return False
    return True
