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
from .scope import KeyScheme, LayoutError, LifecycleClass, Scope


#: Roots a parsed key or relpath can live under. ``live`` is today's mutable
#: tree / key space; ``archive`` is the content-addressed cloud store
#: ``archive/<sha256>/<relpath>`` (write-once, server-side only); ``dr`` is the
#: local release tree ``dr/<tag>/products/<relpath>`` (never a cloud key).
ROOT_LIVE = "live"
ROOT_ARCHIVE = "archive"
ROOT_DR = "dr"


@dataclass(frozen=True)
class ParsedKey:
    product_type: str
    scope: Scope
    filename: str
    #: Which root the key/relpath was parsed from. Builders that rebuild a key
    #: from a ParsedKey MUST honour it (:func:`key_for_parsed`); a parse that
    #: silently dropped the root would map an archive copy back onto the live
    #: key, which is exactly the failure the data-release design warns about.
    root: str = ROOT_LIVE
    #: The archive content hash (64 hex) or the DR tag; None for live.
    root_id: Optional[str] = None

    @property
    def is_live(self) -> bool:
        return self.root == ROOT_LIVE


# Ordered suffix tables (most specific first). Within one directory context, a
# filename is matched against these to recover its product_type.
_NIRSPEC_OBS_SUFFIXES = (
    ("_spec.fits", "nirspec_spec"),
    ("_rate.fits", "nirspec_rate"),  # must precede the bare-'.fits' fallback below
    ("_lines.fits", "nirspec_lines"),  # ditto
    ("_lines.json", "nirspec_lines_json"),
    ("_spec_1d.json", "spectrum_1d_json"),
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
    ("_thumb.png", "nircam_mosaic_thumbnail"),  # before the 'mosaic' prefix check
    ("_quicklook.png", "nircam_mosaic_quicklook"),
)
_EXPOSURE_RE = re.compile(r"_nrs[12]_\d+\.fits$")
_TILE_RE = re.compile(r"^(?P<field>[^/]+)/(?P<filt>[^/]+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png$")


def _dispatch(fname: str, table, fallback: Optional[str] = None) -> Optional[str]:
    for suffix, ptype in table:
        if fname.endswith(suffix):
            return ptype
    return fallback


def _plain_filename(product_type: str, fname: str) -> str:
    """Strip a trailing ``.gz`` for a compressed product's compressed file.

    The bijection always carries the *plain* (local) filename; the cloud key's
    ``.gz`` is (re)applied only by :func:`keys.storage_key`. A local relpath is
    never gzipped, so this is a no-op there; it fires only when a ``.gz`` cloud
    key routes through :func:`parse_relpath` (via ``parse_key``'s ``data/``
    delegation). Non-compressed filenames are returned untouched.
    """
    if fname.endswith(".gz"):
        base = fname[: -len(".gz")]
        spec = products.get(product_type)
        if any(base.endswith(sfx) for sfx in spec.compressed_suffixes):
            return base
    return fname


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

    # Release tree: dr/<tag>/products/<relpath> holds a live product's bytes
    # under a release; meta/ and anything else under dr/ is not a product.
    if tree == _paths.DR_ROOT:
        if len(seg) >= 4 and _paths.is_dr_tag(seg[1]) and seg[2] == "products":
            inner = parse_relpath("/".join(seg[2:]))
            if not products.is_archivable(inner.product_type):
                raise LayoutError(f"unrecognized relpath '{relpath}' ('{inner.product_type}' is not a release product)")
            return ParsedKey(inner.product_type, inner.scope, inner.filename,
                             root=ROOT_DR, root_id=seg[1])
        raise LayoutError(f"unrecognized relpath '{relpath}' (not a release product path)")

    if tree == "products" and len(seg) >= 4 and seg[1] == "nirspec":
        obs = seg[2]
        if len(seg) == 5 and seg[3] == "manual_masks":
            return ParsedKey("nirspec_manual_mask", Scope(obs=obs), seg[4])
        if len(seg) == 5 and seg[3] == "bkgslits" and seg[4].endswith("_bkgslits.fits"):
            return ParsedKey("nirspec_bkgslits", Scope(obs=obs), seg[4])
        if len(seg) == 4:
            fname = seg[3]
            return ParsedKey(_nirspec_obs_product(fname), Scope(obs=obs), fname)

    if tree == "products" and len(seg) >= 4 and seg[1] == "nircam":
        field = seg[2]
        if len(seg) == 4 and seg[3].endswith("_rgb.png"):
            return ParsedKey("nircam_rgb", Scope(field=field), seg[3])
        if len(seg) == 4 and seg[3].endswith("_layout.png"):
            return ParsedKey("nircam_layout", Scope(field=field), seg[3])
        if len(seg) == 5:
            filt, fname = seg[3], seg[4]
            scope = Scope(field=field, filt=filt)
            ptype = _dispatch(fname, _NIRCAM_FILTER_SUFFIXES)
            if ptype:
                return ParsedKey(ptype, scope, fname)
            if fname.startswith("mosaic"):
                return ParsedKey("nircam_mosaic", scope, _plain_filename("nircam_mosaic", fname))
            if fname.startswith("expmap"):
                # '.png' is the dark web plot; '.fits' is the coverage map.
                return ParsedKey(
                    "nircam_expmap_plot" if fname.endswith(".png")
                    else "nircam_expmap", scope, fname)
            if fname.endswith(".fits"):
                return ParsedKey("nircam_exposure", scope, fname)

    if tree == "reference" and len(seg) >= 4 and seg[1] == "nirspec":
        obs = seg[2]
        fname = seg[-1]
        if fname == "stuck_closed_shutters.toml":
            return ParsedKey("nirspec_stuck_shutters", Scope(obs=obs), fname)
        if fname == "nodded_background_overrides.toml":
            return ParsedKey("nirspec_bkg_override", Scope(obs=obs), fname)
        if fname == "redshifts.toml":
            return ParsedKey("nirspec_redshifts", Scope(obs=obs), fname)

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

    # Content-addressed archive: archive/<sha256 hex>/<relpath>. The relpath is
    # the product's live relpath, so parsing is delegated; the root is kept on
    # the result and every rebuild goes through key_for_parsed.
    if key.startswith(_keys.ARCHIVE_ROOT + "/"):
        seg = key.split("/")
        if len(seg) < 3 or not _keys._SHA256_HEX.match(seg[1]):
            raise LayoutError(f"unrecognized storage key '{key}' (archive/<sha256>/<relpath> expected)")
        inner = parse_relpath("/".join(seg[2:]))
        if inner.root != ROOT_LIVE:
            raise LayoutError(f"unrecognized storage key '{key}' (nested roots)")
        if not products.is_archivable(inner.product_type):
            raise LayoutError(f"unrecognized storage key '{key}' ('{inner.product_type}' is not archivable)")
        return ParsedKey(inner.product_type, inner.scope, inner.filename,
                         root=ROOT_ARCHIVE, root_id=seg[1])

    # Canonical data-bucket key: 'data/' + relpath (mirrored products), or
    # 'data/' + legacy-form prefix (key-only products like photometry, which have
    # no local relpath to mirror).
    if key.startswith("data/"):
        rest = key[len("data/"):]
        try:
            pk = parse_relpath(rest)
        except LayoutError:
            return _parse_legacy_key(rest, bucket=bucket)
        if pk.root != ROOT_LIVE:
            raise LayoutError(f"unrecognized storage key '{key}' (release trees are local only)")
        return pk

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
    """Map a local relpath to its storage key under the given scheme.

    A release relpath (``dr/<tag>/products/…``) has no key of its own: the bytes
    came from an archive object recorded in ``dr_manifest``. Refuse rather than
    hand back the live key, which may hold newer bytes.
    """
    pk = parse_relpath(relpath)
    if pk.root != ROOT_LIVE:
        raise LayoutError(f"'{relpath}' is a release path; resolve its archive key through dr_manifest")
    return _keys.storage_key(pk.product_type, pk.scope, pk.filename, scheme=scheme)


def key_for_parsed(pk: ParsedKey, *, scheme: KeyScheme = KeyScheme.CANONICAL) -> str:
    """Rebuild the storage key a ParsedKey came from, honouring its root.

    live → :func:`keys.storage_key` under ``scheme``; archive →
    :func:`keys.archive_key` with the parsed hash; dr → raises (a release tree is
    local; its objects live under ``archive/``).
    """
    if pk.root == ROOT_LIVE:
        return _keys.storage_key(pk.product_type, pk.scope, pk.filename, scheme=scheme)
    if pk.root == ROOT_ARCHIVE:
        if not pk.root_id:
            raise LayoutError("archive ParsedKey without a content hash cannot be rebuilt")
        return _keys.archive_key(pk.product_type, pk.scope, pk.filename, content_hash=pk.root_id)
    raise LayoutError(f"a '{pk.root}' path has no storage key")


def derive_sibling(key: str, target_product_type: str, *, bucket: Optional[str] = None) -> str:
    """Co-located sibling key, e.g. a spec key → its zfit/json key.

    Preserves the source key's prefix and scheme; only the filename suffix changes.
    """
    pk = parse_key(key, bucket=bucket)
    if pk.root != ROOT_LIVE:
        # Each archived sidecar sits under its OWN content hash; string surgery
        # on the spec key would name an object that does not exist. Siblings of
        # an archive object are resolved through the registry / dr_manifest.
        raise LayoutError(f"cannot derive a sibling of an archive key ('{key}'); look it up by hash")
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


def is_compressed_key(key: str, *, bucket: Optional[str] = None) -> bool:
    """True iff *key* addresses an object stored gzipped in the bucket.

    Compression is a layout property (``ProductSpec.compressed_suffixes``): the
    key ends in ``.gz`` and its product declares the underlying suffix as
    compressed. This is the single source of truth for gzip-on-upload /
    gunzip-on-pull — the local file is always the plain (decompressed) form, so
    the registry ``content_hash``/``size_bytes`` describe that plain form.
    False for unknown/unsafe keys and uncompressed products.
    """
    if not key.endswith(".gz"):
        return False
    try:
        pk = parse_key(key, bucket=bucket)
    except LayoutError:
        return False
    spec = products.get(pk.product_type)
    # pk.filename is the plain (stripped) name; verify it matches a declared
    # compressed suffix so a stray '.gz' key is not misclassified.
    return any(pk.filename.endswith(sfx) for sfx in spec.compressed_suffixes)


def _parsed_cloud_key(key: str, bucket: Optional[str]) -> Optional[ParsedKey]:
    try:
        pk = parse_key(key, bucket=bucket)
    except LayoutError:
        return None
    spec = products.get(pk.product_type)
    if spec.bucket is None:
        return None
    if bucket is not None and spec.bucket != bucket:
        return None
    return pk


def is_readable_key(key: str, *, bucket: Optional[str] = None) -> bool:
    """True iff *key* names a cloud-backed object a client may READ through the
    presign / download / proxy routes: a live key or an archive copy. Rejects
    traversal/unsafe keys, non-cloud products and release-tree paths."""
    return _parsed_cloud_key(key, bucket) is not None


def is_writable_key(key: str, *, bucket: Optional[str] = None) -> bool:
    """True iff a client may be handed a PUT for *key*: live keys only. Archive
    objects are written once, server-side (retain-on-supersede, the freeze
    byte pass), and are never overwritten; the deploy presign route must use
    this, never :func:`is_readable_key`."""
    pk = _parsed_cloud_key(key, bucket)
    return pk is not None and pk.root == ROOT_LIVE


def is_known_key(key: str, *, bucket: Optional[str] = None) -> bool:
    """Read allowlist (alias of :func:`is_readable_key`, kept for callers).

    Before P1 this one predicate gated both reads and deploy PUTs; PUTs must
    now go through :func:`is_writable_key`.
    """
    return is_readable_key(key, bucket=bucket)
