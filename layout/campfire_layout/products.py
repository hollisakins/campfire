"""The product registry — the single declarative table the whole contract reads.

One :class:`ProductSpec` per ``product_type``. Everything else (paths, keys, the
bijection, lifecycle classification) is computed from this table, so adding or
changing a product is a one-line edit here plus a golden-fixture row.

Naming aligns to the planned ``storage_objects.product_type`` enum where one
exists, with clear new names where it does not (the enum is incomplete for real
products — see design §5.1). Products fall into three key tiers:

* **legacy-keyed** — uploaded today under a bare key (``spectra/``, ``rgb/``,
  ``sed/``, ``nircam/exposures/``, ``photometry/``, and tiles). ``legacy_prefix``
  is set; the LEGACY scheme reproduces today's key exactly.
* **reserved** — has a local home but is not in the bucket yet (NIRSpec/NIRCam
  exposure intermediates, all of ``reference/``). ``legacy_prefix`` is ``None``;
  ``storage_key`` returns the CANONICAL form in both schemes (no legacy key to
  preserve), so F1/B2 can adopt it later without a rename.
* **not cloud-backed** — ``bucket is None`` (raw → MAST, cache → regenerable,
  meta/cutouts → CLI-local). No storage key; ``storage_key`` raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .scope import Instrument, LayoutError, LifecycleClass, Scope

# A subdir/filename builder takes a (validated) Scope and returns a POSIX path
# fragment with no leading or trailing slash.
_Builder = Callable[[Scope], str]


@dataclass(frozen=True)
class ProductSpec:
    name: str
    tree: str                            # top-level: products|reference|raw|cache|tiles|meta|cutouts
    lifecycle: LifecycleClass
    scope_keys: tuple[str, ...]
    subdir: _Builder                     # dir under the tree, e.g. 'nirspec/<obs>'
    instrument: Optional[Instrument] = None
    bucket: Optional[str] = None         # 'data' | 'tiles' | None (not cloud-backed)
    suffix: Optional[str] = None         # filename discriminator for reverse dispatch
    legacy_prefix: Optional[_Builder] = None   # legacy key prefix, or None (reserved)
    filename: Optional[_Builder] = None  # builder for fully-scoped products (tile, photometry)
    mirrored: bool = True                # has a local relpath (False = key-only, e.g. photometry)
    scheme_invariant: bool = False       # key identical across schemes (tiles stay on R2)
    compressed_suffixes: tuple[str, ...] = ()  # filenames ending in these are stored gzipped
    #   (cloud key gains '.gz'); the local file stays uncompressed. See design
    #   note in keys.storage_key / bijection.parse_relpath.

    def validate(self, scope: Scope) -> None:
        scope.require(*self.scope_keys)

    def rel_dir(self, scope: Scope) -> str:
        """Local directory relative to ``$CAMPFIRE_ROOT`` (incl. tree segment)."""
        self.validate(scope)
        sub = self.subdir(scope)
        return f"{self.tree}/{sub}" if sub else self.tree

    def resolve_filename(self, scope: Scope, filename: Optional[str]) -> str:
        if filename is not None:
            return filename
        if self.filename is not None:
            return self.filename(scope)
        raise LayoutError(
            f"product '{self.name}' requires an explicit filename "
            f"(it has no scope-derived filename builder)"
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

NS = Instrument.NIRSPEC
NC = Instrument.NIRCAM
LC = LifecycleClass

PRODUCTS: dict[str, ProductSpec] = {}


def _register(spec: ProductSpec) -> ProductSpec:
    if spec.name in PRODUCTS:
        raise LayoutError(f"duplicate product_type '{spec.name}'")
    PRODUCTS[spec.name] = spec
    return spec


def _nirspec_obs_dir(s: Scope) -> str:
    return f"nirspec/{s.obs}"


def _nircam_field_filter_dir(s: Scope) -> str:
    return f"nircam/{s.field}/{s.filt}"


def _nircam_field_dir(s: Scope) -> str:
    return f"nircam/{s.field}"


# --- NIRSpec products/ (the spectrum family shares products/nirspec/<obs>/) -------

_register(ProductSpec(
    name="nirspec_spec", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="_spec.fits", legacy_prefix=lambda s: f"spectra/{s.obs}",
))
_register(ProductSpec(
    name="spectrum_json", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="_spec.json", legacy_prefix=lambda s: f"spectra/{s.obs}",
))
_register(ProductSpec(
    name="zfit", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="_zfit.json", legacy_prefix=lambda s: f"spectra/{s.obs}",
))
# The 4->1 canonical spectrum-exposure (#212). A reduction intermediate, not yet
# deployed: reserved key, real local home (bare '<root>_<nrsN>_<source>.fits').
_register(ProductSpec(
    name="nirspec_spectrum_exposure", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix=".fits", legacy_prefix=None,
))
# The stage-1 detector rate file ('<root>_<nrsN>_rate.fits'), one tier earlier
# than the spectrum-exposure. A source-independent intermediate deployed to OSN
# so rate-level masks can be reviewed on the web; reserved key, real local home.
# NB: its '_rate.fits' suffix must be dispatched BEFORE the bare-'.fits' fallback
# in bijection._nirspec_obs_product, or it is silently mis-parsed as a
# spectrum-exposure.
_register(ProductSpec(
    name="nirspec_rate", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="_rate.fits", legacy_prefix=None,
))
# Per-object visualizations, keyed by obs (legacy keys live in their own prefixes
# but mirror locally into the obs products dir).
_register(ProductSpec(
    name="rgb", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="_rgb.png", legacy_prefix=lambda s: f"rgb/{s.obs}",
))
_register(ProductSpec(
    name="sed", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="_sed.pdf", legacy_prefix=lambda s: f"sed/{s.obs}",
))

# --- NIRCam products/ -----------------------------------------------------------

# Canonical per-exposure FITS (mutated in place). Reserved key (PNGs are what
# ship today); real local home under products/nircam/<field>/<filter>/.
_register(ProductSpec(
    name="nircam_exposure", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix=".fits", legacy_prefix=None,
))
_register(ProductSpec(
    name="nircam_exposure_preview", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix="_preview.png",
    legacy_prefix=lambda s: f"nircam/exposures/{s.field}/{s.filt}",
))
_register(ProductSpec(
    name="nircam_exposure_full", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix="_full.png",
    legacy_prefix=lambda s: f"nircam/exposures/{s.field}/{s.filt}",
))
# Drizzled mosaic outputs + their JSON manifests (mosaic_*). Reserved key. The
# FITS extensions (_i2d/_sci/_err/_wht/_srcmask) are stored gzipped in the cloud
# ('.fits.gz' key) so high-NaN mosaics transfer smaller; the local tree keeps the
# plain '.fits' (pull decompresses). The '_manifest.json' sibling isn't a '.fits'
# so it stays uncompressed; the '_thumb.png' is a separate product.
_register(ProductSpec(
    name="nircam_mosaic", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix=None, legacy_prefix=None,
    compressed_suffixes=(".fits",),
))
# Field-tile RGB (distinct product from the per-object 'rgb' above).
_register(ProductSpec(
    name="nircam_rgb", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field",), subdir=_nircam_field_dir,
    suffix="_rgb.png", legacy_prefix=None,
))
# Per-(field, filter) exposure-coverage maps. Live in the canonical filter dir
# alongside the mosaics/exposures (products/nircam/<field>/<filter>/), keyed off
# an ``expmap_`` filename prefix; scoped by filt so the registry carries a real
# filter for them like every other per-filter NIRCam product.
_register(ProductSpec(
    name="nircam_expmap", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix=None, legacy_prefix=None,
))
# Web-ready dark PNG render of the expmap (``expmap_<field>_<filter>.png``),
# sibling of the expmap FITS in the filter dir. Prefix-dispatched like the FITS
# (``expmap_``); the ``.png`` extension tells the two apart in the bijection, so
# ``suffix`` stays None (there is no clean filename suffix to dispatch on).
_register(ProductSpec(
    name="nircam_expmap_plot", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix=None, legacy_prefix=None,
))
# Per-mosaic science thumbnail (``mosaic_..._thumb.png``), sibling of the mosaic
# FITS. Suffix-dispatched on ``_thumb.png`` (matched before the ``mosaic`` prefix).
_register(ProductSpec(
    name="nircam_mosaic_thumbnail", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix="_thumb.png", legacy_prefix=None,
))
# Mosaic quick-look (``<mosaic>_quicklook.png``): the larger rendition of the
# thumbnail pair (long side ~4k, for the web popup). A distinct suffix on
# purpose — ``_preview.png``/``_full.png`` in this directory already mean the
# per-exposure triage PNGs.
_register(ProductSpec(
    name="nircam_mosaic_quicklook", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt"),
    subdir=_nircam_field_filter_dir, suffix="_quicklook.png", legacy_prefix=None,
))
# Field layout plot (``<field>_layout.png``): stacked-filter coverage + tile
# outlines. Field-scoped, in the field root beside nircam_rgb; the landing-page
# preview. Suffix-dispatched on ``_layout.png``.
_register(ProductSpec(
    name="nircam_layout", instrument=NC, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field",), subdir=_nircam_field_dir,
    suffix="_layout.png", legacy_prefix=None,
))

# --- Map tiles (separate 'tiles' bucket, scheme-invariant, stays on R2) ---------

_register(ProductSpec(
    name="tile", instrument=NC, tree="tiles", bucket="tiles",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "filt", "zoom", "x", "y"),
    subdir=lambda s: f"{s.field}/{s.filt}/{s.zoom}/{s.x}",
    filename=lambda s: f"{s.y}.png", suffix=".png",
    legacy_prefix=lambda s: f"{s.field}/{s.filt}/{s.zoom}/{s.x}",
    scheme_invariant=True,
))

# --- Photometry (key-only: generated at deploy, no durable local home) ----------

_register(ProductSpec(
    name="photometry_pz", instrument=None, tree="products", bucket="data",
    lifecycle=LC.CLOUD_PRODUCT, scope_keys=("field", "object_id"),
    subdir=lambda s: f"photometry/{s.field}",
    filename=lambda s: f"{s.object_id}_pz.json", suffix="_pz.json",
    legacy_prefix=lambda s: f"photometry/{s.field}", mirrored=False,
))

# --- Metadata (Postgres-resident / CLI-local; no storage key) -------------------

for _name, _suffix in (
    ("summary", "_summary.ecsv"),
    ("pointings", "_pointings.ecsv"),
    ("shutters", "_shutters.ecsv"),
    ("nirspec_config", "_config.toml"),
):
    _register(ProductSpec(
        name=_name, instrument=NS, tree="products", bucket=None,
        lifecycle=LC.CLOUD_PRODUCT, scope_keys=("obs",), subdir=_nirspec_obs_dir,
        suffix=_suffix, filename=(lambda s, _sfx=_suffix: f"{s.obs}{_sfx}"),
    ))

# --- reducer-decision reference state (user-state; reserved cloud key) -----------

_register(ProductSpec(
    name="nirspec_manual_mask", instrument=NS, tree="products", bucket="data",
    lifecycle=LC.USER_STATE, scope_keys=("obs",),
    subdir=lambda s: f"nirspec/{s.obs}/manual_masks", suffix=".reg",
    legacy_prefix=None,
))
_register(ProductSpec(
    name="nirspec_stuck_shutters", instrument=NS, tree="reference", bucket="data",
    lifecycle=LC.USER_STATE, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="stuck_closed_shutters.toml",
    filename=lambda s: "stuck_closed_shutters.toml", legacy_prefix=None,
))
_register(ProductSpec(
    name="nirspec_bkg_override", instrument=NS, tree="reference", bucket="data",
    lifecycle=LC.USER_STATE, scope_keys=("obs",), subdir=_nirspec_obs_dir,
    suffix="nodded_background_overrides.toml",
    filename=lambda s: "nodded_background_overrides.toml", legacy_prefix=None,
))
_register(ProductSpec(
    name="nircam_mask", instrument=NC, tree="reference", bucket="data",
    lifecycle=LC.USER_STATE, scope_keys=("field",),
    subdir=lambda s: f"nircam/{s.field}/masks", legacy_prefix=None,
))
_register(ProductSpec(
    name="nircam_astrom_cat", instrument=NC, tree="reference", bucket="data",
    lifecycle=LC.USER_STATE, scope_keys=("field",),
    subdir=lambda s: f"nircam/{s.field}/astrom_cats", legacy_prefix=None,
))
_register(ProductSpec(
    name="nircam_bad_pixel", instrument=NC, tree="reference", bucket="data",
    lifecycle=LC.USER_STATE, scope_keys=("field",),
    subdir=lambda s: f"nircam/{s.field}/bad_pixels", legacy_prefix=None,
))

# --- shared calibration references (detector/filter-scoped, deduped by hash) -----

_register(ProductSpec(
    name="nircam_flat", instrument=NC, tree="reference", bucket="data",
    lifecycle=LC.SHARED_CALIBRATION, scope_keys=(),
    subdir=lambda s: "nircam/shared/flats", legacy_prefix=None,
))
_register(ProductSpec(
    name="nircam_wisp", instrument=NC, tree="reference", bucket="data",
    lifecycle=LC.SHARED_CALIBRATION, scope_keys=(),
    subdir=lambda s: "nircam/shared/wisps", legacy_prefix=None,
))

# --- raw (external/MAST; not cloud-backed) --------------------------------------

_register(ProductSpec(
    name="raw_nirspec", instrument=NS, tree="raw", bucket=None,
    lifecycle=LC.EXTERNAL_MAST, scope_keys=("data_subdir",),
    subdir=lambda s: f"nirspec/{s.data_subdir}",
))
_register(ProductSpec(
    name="raw_nircam", instrument=NC, tree="raw", bucket=None,
    lifecycle=LC.EXTERNAL_MAST, scope_keys=("pid", "filt"),
    subdir=lambda s: f"nircam/{s.pid}/{s.filt}",
))


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get(product_type: str) -> ProductSpec:
    try:
        return PRODUCTS[product_type]
    except KeyError:
        raise LayoutError(
            f"unknown product_type '{product_type}'. "
            f"Known: {sorted(PRODUCTS)}"
        ) from None
