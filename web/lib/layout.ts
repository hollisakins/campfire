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

type Builder = (s: Scope) => string;

interface ProductSpec {
  name: string;
  tree: string; // products | reference | raw | cache | tiles | meta | cutouts
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

function spec(p: Partial<ProductSpec> & Pick<ProductSpec, 'name' | 'tree' | 'scopeKeys' | 'subdir'>): ProductSpec {
  return {
    bucket: null,
    suffix: null,
    legacyPrefix: null,
    filename: null,
    mirrored: true,
    schemeInvariant: false,
    compressedSuffixes: [],
    ...p,
  };
}

const nirspecObs: Builder = (s) => `nirspec/${s.obs}`;
const nircamFieldFilter: Builder = (s) => `nircam/${s.field}/${s.filt}`;

export const PRODUCTS: Record<string, ProductSpec> = {};
function reg(p: ProductSpec) {
  PRODUCTS[p.name] = p;
}

// --- NIRSpec products (spectrum family shares products/nirspec/<obs>/) ---
reg(spec({ name: 'nirspec_spec', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_spec.fits', legacyPrefix: (s) => `spectra/${s.obs}` }));
reg(spec({ name: 'spectrum_json', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_spec.json', legacyPrefix: (s) => `spectra/${s.obs}` }));
// 1-D-only sibling of spectrum_json (perf T2-D2, #508): no 2-D S/N array.
reg(spec({ name: 'spectrum_1d_json', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_spec_1d.json', legacyPrefix: (s) => `spectra/${s.obs}` }));
reg(spec({ name: 'zfit', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_zfit.json', legacyPrefix: (s) => `spectra/${s.obs}` }));
reg(spec({ name: 'nirspec_spectrum_exposure', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '.fits' }));
reg(spec({ name: 'nirspec_rate', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_rate.fits' }));
reg(spec({ name: 'rgb', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_rgb.png', legacyPrefix: (s) => `rgb/${s.obs}` }));
reg(spec({ name: 'sed', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: '_sed.pdf', legacyPrefix: (s) => `sed/${s.obs}` }));

// --- NIRCam products ---
reg(spec({ name: 'nircam_exposure', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: '.fits' }));
reg(spec({ name: 'nircam_exposure_preview', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: '_preview.png', legacyPrefix: (s) => `nircam/exposures/${s.field}/${s.filt}` }));
reg(spec({ name: 'nircam_exposure_full', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: '_full.png', legacyPrefix: (s) => `nircam/exposures/${s.field}/${s.filt}` }));
reg(spec({ name: 'nircam_mosaic', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: null, compressedSuffixes: ['.fits'] }));
reg(spec({ name: 'nircam_rgb', tree: 'products', bucket: 'data', scopeKeys: ['field'], subdir: (s) => `nircam/${s.field}`, suffix: '_rgb.png' }));
reg(spec({ name: 'nircam_expmap', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: null }));
reg(spec({ name: 'nircam_expmap_plot', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: null }));
reg(spec({ name: 'nircam_mosaic_thumbnail', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: '_thumb.png' }));
reg(spec({ name: 'nircam_mosaic_quicklook', tree: 'products', bucket: 'data', scopeKeys: ['field', 'filt'], subdir: nircamFieldFilter, suffix: '_quicklook.png' }));
reg(spec({ name: 'nircam_layout', tree: 'products', bucket: 'data', scopeKeys: ['field'], subdir: (s) => `nircam/${s.field}`, suffix: '_layout.png' }));

// --- Map tiles (separate bucket, scheme-invariant) ---
reg(spec({
  name: 'tile', tree: 'tiles', bucket: 'tiles', scopeKeys: ['field', 'filt', 'zoom', 'x', 'y'],
  subdir: (s) => `${s.field}/${s.filt}/${s.zoom}/${s.x}`, filename: (s) => `${s.y}.png`,
  suffix: '.png', legacyPrefix: (s) => `${s.field}/${s.filt}/${s.zoom}/${s.x}`, schemeInvariant: true,
}));

// --- Photometry (key-only) ---
reg(spec({
  name: 'photometry_pz', tree: 'products', bucket: 'data', scopeKeys: ['field', 'object_id'],
  subdir: (s) => `photometry/${s.field}`, filename: (s) => `${s.object_id}_pz.json`,
  suffix: '_pz.json', legacyPrefix: (s) => `photometry/${s.field}`, mirrored: false,
}));

// --- Metadata (Postgres-resident; no storage key) ---
for (const [name, suffix] of [['summary', '_summary.ecsv'], ['pointings', '_pointings.ecsv'], ['shutters', '_shutters.ecsv'], ['nirspec_config', '_config.toml']] as const) {
  reg(spec({ name, tree: 'products', bucket: null, scopeKeys: ['obs'], subdir: nirspecObs, suffix, filename: (s) => `${s.obs}${suffix}` }));
}

// --- Reducer-decision reference state (user-state) ---
reg(spec({ name: 'nirspec_manual_mask', tree: 'products', bucket: 'data', scopeKeys: ['obs'], subdir: (s) => `nirspec/${s.obs}/manual_masks`, suffix: '.reg' }));
reg(spec({ name: 'nirspec_stuck_shutters', tree: 'reference', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: 'stuck_closed_shutters.toml', filename: () => 'stuck_closed_shutters.toml' }));
reg(spec({ name: 'nirspec_bkg_override', tree: 'reference', bucket: 'data', scopeKeys: ['obs'], subdir: nirspecObs, suffix: 'nodded_background_overrides.toml', filename: () => 'nodded_background_overrides.toml' }));
reg(spec({ name: 'nircam_mask', tree: 'reference', bucket: 'data', scopeKeys: ['field'], subdir: (s) => `nircam/${s.field}/masks` }));
reg(spec({ name: 'nircam_astrom_cat', tree: 'reference', bucket: 'data', scopeKeys: ['field'], subdir: (s) => `nircam/${s.field}/astrom_cats` }));
reg(spec({ name: 'nircam_bad_pixel', tree: 'reference', bucket: 'data', scopeKeys: ['field'], subdir: (s) => `nircam/${s.field}/bad_pixels` }));

// --- Shared calibration references ---
reg(spec({ name: 'nircam_flat', tree: 'reference', bucket: 'data', scopeKeys: [], subdir: () => 'nircam/shared/flats' }));
reg(spec({ name: 'nircam_wisp', tree: 'reference', bucket: 'data', scopeKeys: [], subdir: () => 'nircam/shared/wisps' }));

// --- Raw (external/MAST; not cloud-backed) ---
reg(spec({ name: 'raw_nirspec', tree: 'raw', bucket: null, scopeKeys: ['data_subdir'], subdir: (s) => `nirspec/${s.data_subdir}` }));
reg(spec({ name: 'raw_nircam', tree: 'raw', bucket: null, scopeKeys: ['pid', 'filt'], subdir: (s) => `nircam/${s.pid}/${s.filt}` }));

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

const NIRSPEC_OBS_SUFFIXES: [string, string][] = [
  ['_spec.fits', 'nirspec_spec'],
  ['_rate.fits', 'nirspec_rate'], // must precede the bare-'.fits' fallback below
  ['_spec_1d.json', 'spectrum_1d_json'],
  ['_spec.json', 'spectrum_json'],
  ['_zfit.json', 'zfit'],
  ['_rgb.png', 'rgb'],
  ['_sed.pdf', 'sed'],
  ['_summary.ecsv', 'summary'],
  ['_pointings.ecsv', 'pointings'],
  ['_shutters.ecsv', 'shutters'],
  ['_config.toml', 'nirspec_config'],
];
const NIRCAM_FILTER_SUFFIXES: [string, string][] = [
  ['_preview.png', 'nircam_exposure_preview'],
  ['_full.png', 'nircam_exposure_full'],
  ['_thumb.png', 'nircam_mosaic_thumbnail'], // before the 'mosaic' prefix check
  ['_quicklook.png', 'nircam_mosaic_quicklook'],
];
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
