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

/** Roots a parsed key or relpath can live under (mirror of bijection.py):
 * 'live' is today's mutable tree / key space; 'archive' is the content-addressed
 * cloud store `archive/<sha256>/<relpath>` (write-once, server-side only);
 * 'dr' is the local release tree `dr/<tag>/products/<relpath>` (never a cloud key). */
export type KeyRoot = 'live' | 'archive' | 'dr';
export const ARCHIVE_ROOT = 'archive';
export const DR_ROOT = 'dr';
const SHA256_HEX = /^[0-9a-f]{64}$/;

/** Mirror of products.is_archivable: a mirrored, cloud-backed product on the
 * data bucket (NIRSpec finals and sidecars, NIRCam mosaics). Tiles live on the
 * tiles bucket and are never archived per object. */
export function isArchivable(productType: string): boolean {
  const s = PRODUCTS[productType];
  return !!s && s.bucket === 'data' && s.mirrored && s.lifecycle === 'cloud-backed-product';
}

/** Mirror of keys.archive_hex: `sha256:<hex>` or bare hex → 64 lowercase hex
 * chars. Any other scheme (a provisional `etag:` digest) is rejected, never
 * stripped: it must not mint an archive address. */
export function archiveHex(contentHash: string): string {
  const i = contentHash.indexOf(':');
  let hex: string;
  if (i >= 0) {
    if (contentHash.slice(0, i).toLowerCase() !== 'sha256') throw new LayoutError(`not a sha256 content hash: '${contentHash}'`);
    hex = contentHash.slice(i + 1).toLowerCase();
  } else {
    hex = contentHash.toLowerCase();
  }
  if (!SHA256_HEX.test(hex)) throw new LayoutError(`not a sha256 content hash: '${contentHash}'`);
  return hex;
}
const DR_TAG = /^DR\d{4}\.\d+$/;

export interface ParsedKey {
  productType: string;
  scope: Scope;
  filename: string;
  /** Which root the key was parsed from. Every rebuild MUST honour it
   * (`keyForParsed`); dropping the root would map an archive copy onto the
   * live key, the failure the data-release design warns about. */
  root: KeyRoot;
  /** Archive content hash (64 hex) or the DR tag; null for live. */
  rootId: string | null;
}

export function isDrTag(tag: string): boolean {
  return DR_TAG.test(tag);
}

function live(pk: Omit<ParsedKey, 'root' | 'rootId'>): ParsedKey {
  return { ...pk, root: 'live', rootId: null };
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

  // Release tree: dr/<tag>/products/<relpath> holds a live product's bytes under
  // a release; meta/ and anything else under dr/ is not a product.
  if (tree === DR_ROOT) {
    if (seg.length >= 4 && isDrTag(seg[1]) && seg[2] === 'products') {
      const inner = parseRelpath(seg.slice(2).join('/'));
      if (!isArchivable(inner.productType)) {
        throw new LayoutError(`unrecognized relpath '${relpath}' ('${inner.productType}' is not a release product)`);
      }
      return { ...inner, root: 'dr', rootId: seg[1] };
    }
    throw new LayoutError(`unrecognized relpath '${relpath}' (not a release product path)`);
  }

  if (tree === 'products' && seg.length >= 4 && seg[1] === 'nirspec') {
    const obs = seg[2];
    if (seg.length === 5 && seg[3] === 'manual_masks') return live({ productType: 'nirspec_manual_mask', scope: { obs }, filename: seg[4] });
    if (seg.length === 4) return live({ productType: nirspecObsProduct(seg[3]), scope: { obs }, filename: seg[3] });
  }
  if (tree === 'products' && seg.length >= 4 && seg[1] === 'nircam') {
    const field = seg[2];
    if (seg.length === 4 && seg[3].endsWith('_rgb.png')) return live({ productType: 'nircam_rgb', scope: { field }, filename: seg[3] });
    if (seg.length === 4 && seg[3].endsWith('_layout.png')) return live({ productType: 'nircam_layout', scope: { field }, filename: seg[3] });
    if (seg.length === 5) {
      const filt = seg[3];
      const fname = seg[4];
      const pt = dispatch(fname, NIRCAM_FILTER_SUFFIXES);
      if (pt) return live({ productType: pt, scope: { field, filt }, filename: fname });
      if (fname.startsWith('mosaic')) return live({ productType: 'nircam_mosaic', scope: { field, filt }, filename: plainFilename('nircam_mosaic', fname) });
      // '.png' is the dark web plot; '.fits' is the coverage map.
      if (fname.startsWith('expmap')) return live({ productType: fname.endsWith('.png') ? 'nircam_expmap_plot' : 'nircam_expmap', scope: { field, filt }, filename: fname });
      if (fname.endsWith('.fits')) return live({ productType: 'nircam_exposure', scope: { field, filt }, filename: fname });
    }
  }
  if (tree === 'reference' && seg.length >= 4 && seg[1] === 'nirspec') {
    const obs = seg[2];
    const fname = seg[seg.length - 1];
    if (fname === 'stuck_closed_shutters.toml') return live({ productType: 'nirspec_stuck_shutters', scope: { obs }, filename: fname });
    if (fname === 'nodded_background_overrides.toml') return live({ productType: 'nirspec_bkg_override', scope: { obs }, filename: fname });
    if (fname === 'redshifts.toml') return live({ productType: 'nirspec_redshifts', scope: { obs }, filename: fname });
  }
  if (tree === 'reference' && seg.length >= 4 && seg[1] === 'nircam') {
    const fname = seg[seg.length - 1];
    if (seg[2] === 'shared' && seg.length === 5) {
      if (seg[3] === 'flats') return live({ productType: 'nircam_flat', scope: {}, filename: fname });
      if (seg[3] === 'wisps') return live({ productType: 'nircam_wisp', scope: {}, filename: fname });
    } else {
      const field = seg[2];
      const kind = seg[3];
      if (kind === 'masks') return live({ productType: 'nircam_mask', scope: { field }, filename: fname });
      if (kind === 'astrom_cats') return live({ productType: 'nircam_astrom_cat', scope: { field }, filename: fname });
      if (kind === 'bad_pixels') return live({ productType: 'nircam_bad_pixel', scope: { field }, filename: fname });
    }
  }
  if (tree === 'tiles' && seg.length === 6 && seg[5].endsWith('.png')) {
    return live({ productType: 'tile', scope: { field: seg[1], filt: seg[2], zoom: +seg[3], x: +seg[4], y: +seg[5].slice(0, -4) }, filename: seg[5] });
  }
  if (tree === 'raw' && seg.length >= 4 && seg[1] === 'nirspec') return live({ productType: 'raw_nirspec', scope: { data_subdir: seg[2] }, filename: seg[seg.length - 1] });
  if (tree === 'raw' && seg.length >= 5 && seg[1] === 'nircam') return live({ productType: 'raw_nircam', scope: { pid: seg[2], filt: seg[3] }, filename: seg[seg.length - 1] });

  throw new LayoutError(`unrecognized relpath '${relpath}'`);
}

export function parseKey(key: string, opts?: { bucket?: Bucket }): ParsedKey {
  rejectUnsafe(key);
  // Content-addressed archive: archive/<sha256 hex>/<relpath> (P1). The relpath
  // is the product's live relpath; the root rides on the result and every
  // rebuild goes through keyForParsed.
  if (key.startsWith(`${ARCHIVE_ROOT}/`)) {
    const seg = key.split('/');
    if (seg.length < 3 || !SHA256_HEX.test(seg[1])) {
      throw new LayoutError(`unrecognized storage key '${key}' (archive/<sha256>/<relpath> expected)`);
    }
    const inner = parseRelpath(seg.slice(2).join('/'));
    if (inner.root !== 'live') throw new LayoutError(`unrecognized storage key '${key}' (nested roots)`);
    if (!isArchivable(inner.productType)) {
      throw new LayoutError(`unrecognized storage key '${key}' ('${inner.productType}' is not archivable)`);
    }
    return { ...inner, root: 'archive', rootId: seg[1] };
  }
  // Canonical 'data/' key: relpath (mirrored products) or 'data/' + legacy-form
  // prefix (key-only products like photometry, with no local relpath to mirror).
  if (key.startsWith('data/')) {
    const rest = key.slice('data/'.length);
    let pk: ParsedKey;
    try {
      pk = parseRelpath(rest);
    } catch {
      return parseLegacyKey(rest, opts);
    }
    if (pk.root !== 'live') throw new LayoutError(`unrecognized storage key '${key}' (release trees are local only)`);
    return pk;
  }
  return parseLegacyKey(key, opts);
}

// Hand-written mirror of bijection._parse_legacy_key (prefix dispatch on
// today's bare keys) — same reason as parseRelpath.
function parseLegacyKey(key: string, opts?: { bucket?: Bucket }): ParsedKey {
  const seg = key.split('/');
  if (seg[0] === 'spectra' && seg.length === 3) return live({ productType: nirspecObsProduct(seg[2]), scope: { obs: seg[1] }, filename: seg[2] });
  if (seg[0] === 'rgb' && seg.length === 3) return live({ productType: 'rgb', scope: { obs: seg[1] }, filename: seg[2] });
  if (seg[0] === 'sed' && seg.length === 3) return live({ productType: 'sed', scope: { obs: seg[1] }, filename: seg[2] });
  if (seg[0] === 'nircam' && seg.length === 5 && seg[1] === 'exposures') {
    const pt = dispatch(seg[4], NIRCAM_FILTER_SUFFIXES);
    if (pt) return live({ productType: pt, scope: { field: seg[2], filt: seg[3] }, filename: seg[4] });
  }
  if (seg[0] === 'photometry' && seg.length === 3) {
    return live({ productType: 'photometry_pz', scope: { field: seg[1], object_id: seg[2].replace(/_pz\.json$/, '') }, filename: seg[2] });
  }

  if (opts?.bucket === 'tiles' || TILE_RE.test(key)) {
    const m = TILE_RE.exec(key);
    if (m) return live({ productType: 'tile', scope: { field: m[1], filt: m[2], zoom: +m[3], x: +m[4], y: +m[5] }, filename: `${m[5]}.png` });
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
  if (pk.root !== 'live') {
    // Each archived sidecar sits under its OWN content hash; string surgery on
    // the spec key would name an object that does not exist. Resolve siblings
    // of an archive object through the registry / dr_members.
    throw new LayoutError(`cannot derive a sibling of an archive key ('${key}'); look it up by hash`);
  }
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
  // An archive key is already its own canonical form; never collapse it onto
  // the live key (which may hold newer bytes).
  return keyForParsed(parseKey(key, opts), 'canonical');
}

/** Archive key for one object: `archive/<sha256 hex>/<relpath>` (mirror of
 * keys.archive_key). Only mirrored cloud products (NIRSpec finals and sidecars,
 * NIRCam mosaics) can be archived. */
export function archiveKey(productType: string, scope: Scope, filename: string | undefined, contentHash: string): string {
  const s = get(productType);
  if (!isArchivable(productType)) {
    throw new LayoutError(`product '${productType}' cannot be archived (not a mirrored data-bucket cloud product)`);
  }
  validate(s, scope);
  const hex = archiveHex(contentHash);
  let fname = resolveFilename(s, scope, filename);
  if (s.compressedSuffixes.some((sfx) => fname.endsWith(sfx))) fname = `${fname}.gz`;
  return `${ARCHIVE_ROOT}/${hex}/${relDir(s, scope)}/${fname}`;
}

/** Rebuild the storage key a ParsedKey came from, honouring its root: live →
 * `storageKey` under `scheme`; archive → `archiveKey` with the parsed hash; dr →
 * throws (a release tree is local; its objects live under `archive/`). */
export function keyForParsed(pk: ParsedKey, scheme: KeyScheme = 'canonical'): string {
  if (pk.root === 'live') return storageKey(pk.productType, pk.scope, pk.filename, scheme);
  if (pk.root === 'archive') {
    if (!pk.rootId) throw new LayoutError('archive ParsedKey without a content hash cannot be rebuilt');
    return archiveKey(pk.productType, pk.scope, pk.filename, pk.rootId);
  }
  throw new LayoutError(`a '${pk.root}' path has no storage key`);
}

/** Rewrite *key* into the LEGACY scheme (today's bare key on R2). The dual-read
 * R2 fallback signs against this. Idempotent on an already-legacy key. Throws
 * `LayoutError` for unknown/unsafe keys. */
export function toLegacyKey(key: string, opts?: { bucket?: Bucket }): string {
  const pk = parseKey(key, opts);
  if (pk.root !== 'live') throw new LayoutError(`an archive key has no legacy (R2) form: '${key}'`);
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

function parsedCloudKey(key: string, opts?: { bucket?: Bucket }): ParsedKey | null {
  let pk: ParsedKey;
  try {
    pk = parseKey(key, opts);
  } catch {
    return null;
  }
  const s = PRODUCTS[pk.productType];
  if (!s || !s.bucket) return null;
  if (opts?.bucket && s.bucket !== opts.bucket) return null;
  return pk;
}

/** True iff *key* names a cloud-backed object a client may READ through the
 * presign / download / proxy routes: a live key or an archive copy. Rejects
 * traversal/unsafe keys, non-cloud products and release-tree paths. */
export function isReadableKey(key: string, opts?: { bucket?: Bucket }): boolean {
  return parsedCloudKey(key, opts) !== null;
}

/** True iff a client may be handed a PUT for *key*: live keys only. Archive
 * objects are written once, server-side (retain-on-supersede, the freeze byte
 * pass) and never overwritten; the deploy presign route uses this, never
 * `isReadableKey`. */
export function isWritableKey(key: string, opts?: { bucket?: Bucket }): boolean {
  const pk = parsedCloudKey(key, opts);
  return pk !== null && pk.root === 'live';
}

/** Read allowlist (alias of `isReadableKey`, kept for callers). Before P1 this
 * one predicate gated both reads and deploy PUTs; PUTs go through
 * `isWritableKey` now. */
export function isKnownKey(key: string, opts?: { bucket?: Bucket }): boolean {
  return isReadableKey(key, opts);
}

/** Observation name for a NIRSpec object key (replaces extractObservationName). */
export function observationFromKey(key: string): string | null {
  try {
    return parseKey(key).scope.obs ?? null;
  } catch {
    return null;
  }
}
