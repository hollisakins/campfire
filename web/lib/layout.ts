// campfire_layout — TypeScript mirror of the CAMPFIRE directory/key contract.
//
// This is the web side of the single layout authority. The python core lives in
// `layout/campfire_layout/`; both are kept in lockstep by the shared golden
// fixture (`layout/conformance/layout_golden.json`), checked by this package's
// `layout.test.ts` AND `pipeline/tests/test_layout.py` — so python↔TS drift
// fails CI.
//
// Web never touches the filesystem, so this mirror omits the local-path helpers
// (roots/dir_for/local_path/cache_path/…) and keeps only what the portal needs:
// storage keys, the key→sibling derivation, key parsing, and the presign/proxy
// allowlist. Scope field names are snake_case to match the contract + fixture
// exactly (fidelity over JS idiom).
//
// The product table itself is NOT hand-mirrored: `PRODUCTS` and the two
// suffix-dispatch tables are built from `./layout-products.json`, which
// `scripts/sync_layout_products.py` generates from the python registry
// (`products.py` + the `bijection.py` dispatch tables); CI runs it with
// `--check`, so a registry edit without a regenerated JSON fails. What stays
// hand-written below is *code* that mirrors python code, not data: the
// directory-walking parsers (`parseRelpath` / `parseLegacyKey`), the tile
// regex, the unsafe-key guard, and the in-directory prefix/fallback rules
// (`mosaic*` / `expmap*` / bare `.fits`) — see the comments at each site.

import layoutProducts from './layout-products.json';

export type Bucket = 'data' | 'tiles';
export type KeyScheme = 'legacy' | 'canonical';

export class LayoutError extends Error {}

export interface Scope {
  obs?: string;
  field?: string;
  filt?: string;
  pid?: string;
  data_subdir?: string;
  source_id?: string;
  detector?: string;
  exposure?: string;
  object_id?: string;
  catalog_id?: string;
  tile?: string;
  pixel_scale?: string;
  zoom?: number;
  x?: number;
  y?: number;
}

export type Instrument = 'nirspec' | 'nircam';
export type LifecycleClass =
  | 'cloud-backed-product'
  | 'user-state'
  | 'shared-calibration'
  | 'external-MAST'
  | 'regenerable'
  | 'cli-local';

type Builder = (s: Scope) => string;

interface ProductSpec {
  name: string;
  instrument: Instrument | null;
  tree: string; // products | reference | raw | cache | tiles | meta | cutouts
  lifecycle: LifecycleClass;
  bucket: Bucket | null;
  scopeKeys: (keyof Scope)[];
  subdir: Builder;
  suffix: string | null;
  legacyPrefix: Builder | null;
  filename: Builder | null;
  mirrored: boolean;
  schemeInvariant: boolean;
  compressedSuffixes: string[]; // filenames ending in these are stored gzipped ('.gz' key); local file stays plain
}

// One row of layout-products.json: the python ProductSpec with its Scope -> str
// builders flattened to '{field}' templates (the generator proves each builder
// is a pure substitution of Scope fields before it writes the template).
interface GeneratedProduct {
  name: string;
  instrument: string | null;
  tree: string;
  lifecycle: string;
  bucket: string | null;
  scope_keys: string[];
  subdir: string;
  filename: string | null;
  suffix: string | null;
  legacy_prefix: string | null;
  mirrored: boolean;
  scheme_invariant: boolean;
  compressed_suffixes: string[];
}

const PLACEHOLDER = /\{(\w+)\}/g;

// Render a '{field}' template against a scope. Mirrors the python f-string
// builders: a field is interpolated verbatim (ints such as zoom/x/y stringify),
// and a missing field is caught by `validate`, exactly as before.
function builder(template: string): Builder {
  return (s) => template.replace(PLACEHOLDER, (_m, k: string) => String(s[k as keyof Scope]));
}

function fromGenerated(p: GeneratedProduct): ProductSpec {
  return {
    name: p.name,
    instrument: p.instrument as Instrument | null,
    tree: p.tree,
    lifecycle: p.lifecycle as LifecycleClass,
    bucket: p.bucket as Bucket | null,
    scopeKeys: p.scope_keys as (keyof Scope)[],
    subdir: builder(p.subdir),
    suffix: p.suffix,
    legacyPrefix: p.legacy_prefix === null ? null : builder(p.legacy_prefix),
    filename: p.filename === null ? null : builder(p.filename),
    mirrored: p.mirrored,
    schemeInvariant: p.scheme_invariant,
    compressedSuffixes: p.compressed_suffixes,
  };
}

/** The product registry, in the python registration order. */
export const PRODUCTS: Record<string, ProductSpec> = {};
for (const p of layoutProducts.products as GeneratedProduct[]) {
  PRODUCTS[p.name] = fromGenerated(p);
}

function get(productType: string): ProductSpec {
  const s = PRODUCTS[productType];
  if (!s) throw new LayoutError(`unknown product_type '${productType}'`);
  return s;
}

function validate(s: ProductSpec, scope: Scope): void {
  const missing = s.scopeKeys.filter((k) => scope[k] === undefined || scope[k] === null);
  if (missing.length) throw new LayoutError(`scope missing required field(s) ${missing.join(', ')} for '${s.name}'`);
}

function relDir(s: ProductSpec, scope: Scope): string {
  const sub = s.subdir(scope);
  return sub ? `${s.tree}/${sub}` : s.tree;
}

function resolveFilename(s: ProductSpec, scope: Scope, filename?: string): string {
  if (filename !== undefined) return filename;
  if (s.filename) return s.filename(scope);
  throw new LayoutError(`product '${s.name}' requires an explicit filename`);
}

// ---------------------------------------------------------------------------
// Storage keys
// ---------------------------------------------------------------------------

export function bucketFor(productType: string): Bucket {
  const s = get(productType);
  if (!s.bucket) throw new LayoutError(`product '${productType}' is not cloud-backed`);
  return s.bucket;
}

export function keyPrefix(productType: string, scope: Scope, scheme: KeyScheme = 'legacy'): string {
  const s = get(productType);
  if (!s.bucket) throw new LayoutError(`product '${productType}' is not cloud-backed`);
  validate(s, scope);
  if (s.schemeInvariant) return s.legacyPrefix!(scope);
  if (scheme === 'legacy' && s.legacyPrefix) return s.legacyPrefix(scope);
  if (s.mirrored) return `data/${relDir(s, scope)}`;
  if (s.legacyPrefix) return `data/${s.legacyPrefix(scope)}`;
  throw new LayoutError(`product '${productType}' has no resolvable storage key`);
}

export function storageKey(productType: string, scope: Scope, filename?: string, scheme: KeyScheme = 'legacy'): string {
  const s = get(productType);
  // Compressed products (nircam_mosaic FITS) are stored gzipped: the key gains
  // '.gz'; the plain local relpath does not. parseRelpath strips it back.
  let fname = resolveFilename(s, scope, filename);
  if (s.compressedSuffixes.some((sfx) => fname.endsWith(sfx))) fname = `${fname}.gz`;
  return `${keyPrefix(productType, scope, scheme)}/${fname}`;
}

// ---------------------------------------------------------------------------
// Parsing (key → product/scope/filename)
// ---------------------------------------------------------------------------

export interface ParsedKey {
  productType: string;
  scope: Scope;
  filename: string;
}

// Ordered suffix tables (most specific first) from layout-products.json —
// the same tables bijection.py dispatches on, in the same order (e.g.
// '_rate.fits' / '_lines.fits' precede the bare-'.fits' spectrum-exposure
// fallback in `nirspecObsProduct`; '_thumb.png' precedes the 'mosaic' prefix
// check in `parseRelpath`).
const NIRSPEC_OBS_SUFFIXES = layoutProducts.dispatch.nirspec_obs_suffixes as [string, string][];
const NIRCAM_FILTER_SUFFIXES = layoutProducts.dispatch.nircam_filter_suffixes as [string, string][];
// Hand-written (no python data to generate from): the tile key shape is a
// regex in bijection.py (_TILE_RE), not a registry field.
const TILE_RE = /^([^/]+)\/([^/]+)\/(\d+)\/(\d+)\/(\d+)\.png$/;

function dispatch(fname: string, table: [string, string][]): string | null {
  for (const [suffix, pt] of table) if (fname.endsWith(suffix)) return pt;
  return null;
}

// Strip a trailing '.gz' for a compressed product's compressed file. The
// bijection carries the plain (local) filename; storageKey reapplies '.gz'.
// No-op for local relpaths (never gzipped) and non-compressed filenames.
function plainFilename(productType: string, fname: string): string {
  if (fname.endsWith('.gz')) {
    const base = fname.slice(0, -'.gz'.length);
    const s = PRODUCTS[productType];
    if (s && s.compressedSuffixes.some((sfx) => base.endsWith(sfx))) return base;
  }
  return fname;
}

// Hand-written: the bare-'.fits' fallback to the spectrum-exposure is code in
// bijection._nirspec_obs_product, not a dispatch-table row.
function nirspecObsProduct(fname: string): string {
  const pt = dispatch(fname, NIRSPEC_OBS_SUFFIXES);
  if (pt) return pt;
  if (fname.endsWith('.fits')) return 'nirspec_spectrum_exposure';
  throw new LayoutError(`unrecognized NIRSpec product filename '${fname}'`);
}

function rejectUnsafe(key: string): void {
  if (!key || key.startsWith('/') || key.includes('\\')) throw new LayoutError(`unsafe key '${key}'`);
  if (key.split('/').some((p) => p === '' || p === '.' || p === '..')) throw new LayoutError(`unsafe key '${key}'`);
}

// Hand-written mirror of bijection.parse_relpath: the directory walk, the
// 'mosaic*' / 'expmap*' prefix rules and the fixed reference-file names are
// imperative code on the python side too, so there is no registry data to
// generate them from. Keep the two in step by hand; the golden fixture pins
// every branch.
export function parseRelpath(relpath: string): ParsedKey {
  rejectUnsafe(relpath);
  const seg = relpath.split('/');
  const tree = seg[0];

  if (tree === 'products' && seg.length >= 4 && seg[1] === 'nirspec') {
    const obs = seg[2];
    if (seg.length === 5 && seg[3] === 'manual_masks') return { productType: 'nirspec_manual_mask', scope: { obs }, filename: seg[4] };
    if (seg.length === 4) return { productType: nirspecObsProduct(seg[3]), scope: { obs }, filename: seg[3] };
  }
  if (tree === 'products' && seg.length >= 4 && seg[1] === 'nircam') {
    const field = seg[2];
    if (seg.length === 4 && seg[3].endsWith('_rgb.png')) return { productType: 'nircam_rgb', scope: { field }, filename: seg[3] };
    if (seg.length === 4 && seg[3].endsWith('_layout.png')) return { productType: 'nircam_layout', scope: { field }, filename: seg[3] };
    if (seg.length === 5) {
      const filt = seg[3];
      const fname = seg[4];
      const pt = dispatch(fname, NIRCAM_FILTER_SUFFIXES);
      if (pt) return { productType: pt, scope: { field, filt }, filename: fname };
      if (fname.startsWith('mosaic')) return { productType: 'nircam_mosaic', scope: { field, filt }, filename: plainFilename('nircam_mosaic', fname) };
      // '.png' is the dark web plot; '.fits' is the coverage map.
      if (fname.startsWith('expmap')) return { productType: fname.endsWith('.png') ? 'nircam_expmap_plot' : 'nircam_expmap', scope: { field, filt }, filename: fname };
      if (fname.endsWith('.fits')) return { productType: 'nircam_exposure', scope: { field, filt }, filename: fname };
    }
  }
  if (tree === 'reference' && seg.length >= 4 && seg[1] === 'nirspec') {
    const obs = seg[2];
    const fname = seg[seg.length - 1];
    if (fname === 'stuck_closed_shutters.toml') return { productType: 'nirspec_stuck_shutters', scope: { obs }, filename: fname };
    if (fname === 'nodded_background_overrides.toml') return { productType: 'nirspec_bkg_override', scope: { obs }, filename: fname };
    if (fname === 'redshifts.toml') return { productType: 'nirspec_redshifts', scope: { obs }, filename: fname };
  }
  if (tree === 'reference' && seg.length >= 4 && seg[1] === 'nircam') {
    const fname = seg[seg.length - 1];
    if (seg[2] === 'shared' && seg.length === 5) {
      if (seg[3] === 'flats') return { productType: 'nircam_flat', scope: {}, filename: fname };
      if (seg[3] === 'wisps') return { productType: 'nircam_wisp', scope: {}, filename: fname };
    } else {
      const field = seg[2];
      const kind = seg[3];
      if (kind === 'masks') return { productType: 'nircam_mask', scope: { field }, filename: fname };
      if (kind === 'astrom_cats') return { productType: 'nircam_astrom_cat', scope: { field }, filename: fname };
      if (kind === 'bad_pixels') return { productType: 'nircam_bad_pixel', scope: { field }, filename: fname };
    }
  }
  if (tree === 'tiles' && seg.length === 6 && seg[5].endsWith('.png')) {
    return { productType: 'tile', scope: { field: seg[1], filt: seg[2], zoom: +seg[3], x: +seg[4], y: +seg[5].slice(0, -4) }, filename: seg[5] };
  }
  if (tree === 'raw' && seg.length >= 4 && seg[1] === 'nirspec') return { productType: 'raw_nirspec', scope: { data_subdir: seg[2] }, filename: seg[seg.length - 1] };
  if (tree === 'raw' && seg.length >= 5 && seg[1] === 'nircam') return { productType: 'raw_nircam', scope: { pid: seg[2], filt: seg[3] }, filename: seg[seg.length - 1] };

  throw new LayoutError(`unrecognized relpath '${relpath}'`);
}

export function parseKey(key: string, opts?: { bucket?: Bucket }): ParsedKey {
  rejectUnsafe(key);
  // Canonical 'data/' key: relpath (mirrored products) or 'data/' + legacy-form
  // prefix (key-only products like photometry, with no local relpath to mirror).
  if (key.startsWith('data/')) {
    const rest = key.slice('data/'.length);
    try {
      return parseRelpath(rest);
    } catch {
      return parseLegacyKey(rest, opts);
    }
  }
  return parseLegacyKey(key, opts);
}

// Hand-written mirror of bijection._parse_legacy_key (prefix dispatch on
// today's bare keys) — same reason as parseRelpath.
function parseLegacyKey(key: string, opts?: { bucket?: Bucket }): ParsedKey {
  const seg = key.split('/');
  if (seg[0] === 'spectra' && seg.length === 3) return { productType: nirspecObsProduct(seg[2]), scope: { obs: seg[1] }, filename: seg[2] };
  if (seg[0] === 'rgb' && seg.length === 3) return { productType: 'rgb', scope: { obs: seg[1] }, filename: seg[2] };
  if (seg[0] === 'sed' && seg.length === 3) return { productType: 'sed', scope: { obs: seg[1] }, filename: seg[2] };
  if (seg[0] === 'nircam' && seg.length === 5 && seg[1] === 'exposures') {
    const pt = dispatch(seg[4], NIRCAM_FILTER_SUFFIXES);
    if (pt) return { productType: pt, scope: { field: seg[2], filt: seg[3] }, filename: seg[4] };
  }
  if (seg[0] === 'photometry' && seg.length === 3) {
    return { productType: 'photometry_pz', scope: { field: seg[1], object_id: seg[2].replace(/_pz\.json$/, '') }, filename: seg[2] };
  }

  if (opts?.bucket === 'tiles' || TILE_RE.test(key)) {
    const m = TILE_RE.exec(key);
    if (m) return { productType: 'tile', scope: { field: m[1], filt: m[2], zoom: +m[3], x: +m[4], y: +m[5] }, filename: `${m[5]}.png` };
  }

  throw new LayoutError(`unrecognized storage key '${key}'`);
}

// ---------------------------------------------------------------------------
// Public bijection-lite API (web subset)
// ---------------------------------------------------------------------------

/** Co-located sibling key, e.g. a spec key → its zfit/json key. Preserves the
 * source key's prefix and scheme; only the filename suffix changes. */
export function deriveSibling(key: string, targetProductType: string, opts?: { bucket?: Bucket }): string {
  const pk = parseKey(key, opts);
  const src = get(pk.productType);
  const tgt = get(targetProductType);
  if (src.suffix === null || tgt.suffix === null) {
    throw new LayoutError(`cannot derive sibling between '${pk.productType}' and '${targetProductType}'`);
  }
  const base = pk.filename.slice(0, -src.suffix.length);
  const prefix = key.slice(0, key.lastIndexOf('/'));
  return `${prefix}/${base}${tgt.suffix}`;
}

/** Rewrite *key* into the CANONICAL scheme (`data/` + relpath), regardless of the
 * input scheme. Used by dual-read (#215) to look an object up in the registry,
 * whose rows hold canonical keys after the R2->OSN re-key. `parseKey` accepts
 * either scheme, so passing an already-canonical key is idempotent. Throws
 * `LayoutError` for unknown/unsafe keys. */
export function toCanonicalKey(key: string, opts?: { bucket?: Bucket }): string {
  const pk = parseKey(key, opts);
  return storageKey(pk.productType, pk.scope, pk.filename, 'canonical');
}

/** Rewrite *key* into the LEGACY scheme (today's bare key on R2). The dual-read
 * R2 fallback signs against this. Idempotent on an already-legacy key. Throws
 * `LayoutError` for unknown/unsafe keys. */
export function toLegacyKey(key: string, opts?: { bucket?: Bucket }): string {
  const pk = parseKey(key, opts);
  return storageKey(pk.productType, pk.scope, pk.filename, 'legacy');
}

/** True iff *key* addresses an object stored gzipped in the bucket (nircam_mosaic
 * FITS). Compression is a layout property (`compressedSuffixes`): the key ends in
 * `.gz` and its product declares the underlying suffix. The web download layer
 * serves such keys as-is (a `.fits.gz` attachment); the local file is always the
 * plain form. False for unknown/unsafe keys and uncompressed products. */
export function isCompressedKey(key: string, opts?: { bucket?: Bucket }): boolean {
  if (!key.endsWith('.gz')) return false;
  let pk: ParsedKey;
  try {
    pk = parseKey(key, opts);
  } catch {
    return false;
  }
  const s = PRODUCTS[pk.productType];
  return !!s && s.compressedSuffixes.some((sfx) => pk.filename.endsWith(sfx));
}

/** True iff *key* parses to a cloud-backed product — the presign/proxy allowlist.
 * Rejects traversal/unsafe keys and keys resolving to non-cloud products. */
export function isKnownKey(key: string, opts?: { bucket?: Bucket }): boolean {
  let pk: ParsedKey;
  try {
    pk = parseKey(key, opts);
  } catch {
    return false;
  }
  const s = PRODUCTS[pk.productType];
  if (!s || !s.bucket) return false;
  if (opts?.bucket && s.bucket !== opts.bucket) return false;
  return true;
}

/** Observation name for a NIRSpec object key (replaces extractObservationName). */
export function observationFromKey(key: string): string | null {
  try {
    return parseKey(key).scope.obs ?? null;
  } catch {
    return null;
  }
}
