// TS arm of the layout conformance test. Reads the SAME golden fixture as the
// python arm (pipeline/tests/test_layout.py); any divergence fails both.

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';
import {
  storageKey,
  bucketFor,
  parseKey,
  parseRelpath,
  deriveSibling,
  isKnownKey,
  isReadableKey,
  isWritableKey,
  isCompressedKey,
  toCanonicalKey,
  toLegacyKey,
  keyForParsed,
  archiveKey,
  isDrTag,
  observationFromKey,
  PRODUCTS,
  type Scope,
  archiveHex,
  isArchivable,
} from './layout';
import layoutProducts from './layout-products.json';

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURE = resolve(here, '../../layout/conformance/layout_golden.json');
const golden = JSON.parse(readFileSync(FIXTURE, 'utf-8'));

interface Case {
  product_type: string;
  scope: Scope;
  filename: string | null;
  relpath: string | null;
  key_legacy: string | null;
  key_canonical: string | null;
  bucket: string | null;
  tree_class: string;
}

describe('layout golden conformance', () => {
  for (const c of golden.cases as Case[]) {
    const fn = c.filename ?? undefined;

    it(`${c.product_type}: storage keys`, () => {
      if (c.key_legacy !== null) {
        expect(storageKey(c.product_type, c.scope, fn, 'legacy')).toBe(c.key_legacy);
      }
      if (c.key_canonical !== null) {
        expect(storageKey(c.product_type, c.scope, fn, 'canonical')).toBe(c.key_canonical);
      }
    });

    it(`${c.product_type}: bucket`, () => {
      if (c.bucket !== null) {
        expect(bucketFor(c.product_type)).toBe(c.bucket);
      } else {
        expect(() => bucketFor(c.product_type)).toThrow();
      }
    });

    it(`${c.product_type}: parse round-trips`, () => {
      if (c.key_legacy !== null) {
        const pk = parseKey(c.key_legacy);
        expect(pk.productType).toBe(c.product_type);
        expect(storageKey(pk.productType, pk.scope, pk.filename, 'legacy')).toBe(c.key_legacy);
      }
      if (c.key_canonical !== null) {
        const pk = parseKey(c.key_canonical);
        expect(pk.productType).toBe(c.product_type);
        expect(storageKey(pk.productType, pk.scope, pk.filename, 'canonical')).toBe(c.key_canonical);
      }
    });
  }
});

describe('siblings', () => {
  for (const s of golden.siblings as { from: string; to: string; expect: string }[]) {
    it(`${s.from} -> ${s.to}`, () => {
      expect(deriveSibling(s.from, s.to)).toBe(s.expect);
    });
  }
});

describe('presign allowlist', () => {
  it('accepts known keys', () => {
    for (const key of golden.known_keys as string[]) {
      expect(isKnownKey(key)).toBe(true);
    }
  });
  it('rejects unknown / unsafe keys', () => {
    for (const key of golden.unknown_keys as string[]) {
      expect(isKnownKey(key)).toBe(false);
    }
    expect(isKnownKey('spectra/ember/../../secret.fits')).toBe(false);
    expect(isKnownKey('data/../../../etc/passwd')).toBe(false);
  });
});

describe('compressed keys', () => {
  const gz = 'data/products/nircam/cosmos/f444w/mosaic_cosmos_f444w_30mas_sci.fits.gz';
  it('flags a gzipped mosaic FITS key', () => {
    expect(isCompressedKey(gz)).toBe(true);
  });
  it('is false for the plain .fits key and non-FITS mosaic siblings', () => {
    expect(isCompressedKey(gz.slice(0, -'.gz'.length))).toBe(false);
    expect(isCompressedKey('data/products/nircam/cosmos/f444w/mosaic_cosmos_f444w_30mas_thumb.png')).toBe(false);
    expect(isCompressedKey('data/products/nircam/cosmos/f444w/mosaic_cosmos_f444w_30mas_manifest.json')).toBe(false);
  });
  it('is false for unknown/unsafe keys', () => {
    expect(isCompressedKey('totally/made/up/key.gz')).toBe(false);
  });
});

describe('foreign roots are rejected', () => {
  // data/archive/…, data/dr/… and dr/… as CLOUD keys are not part of the
  // contract (the release tree is local); dr/<tag>/meta is not a product path.
  for (const key of golden.foreign_root_keys as string[]) {
    it(key, () => {
      expect(() => parseKey(key)).toThrow();
      expect(() => parseKey(key, { bucket: 'data' })).toThrow();
      expect(() => parseKey(key, { bucket: 'tiles' })).toThrow();
      if (!key.startsWith('dr/DR') || !key.includes('/products/')) expect(() => parseRelpath(key)).toThrow();
      expect(() => toCanonicalKey(key)).toThrow();
      expect(isKnownKey(key)).toBe(false);
      expect(isReadableKey(key)).toBe(false);
      expect(isWritableKey(key)).toBe(false);
      expect(isCompressedKey(key)).toBe(false);
      expect(observationFromKey(key)).toBeNull();
    });
  }
});

interface ArchiveCase {
  key: string; product_type: string; scope: Scope; filename: string; root_id: string;
  relpath: string; live_canonical: string; compressed: boolean; bucket: string;
}

describe('archive keys (P1): read-only, root-preserving', () => {
  for (const c of golden.archive_keys as ArchiveCase[]) {
    it(c.key.split('/').pop()!, () => {
      const pk = parseKey(c.key);
      expect(pk.productType).toBe(c.product_type);
      expect(pk.filename).toBe(c.filename);
      expect(pk.root).toBe('archive');
      expect(pk.rootId).toBe(c.root_id);
      for (const [k, v] of Object.entries(c.scope)) expect((pk.scope as Record<string, unknown>)[k]).toBe(v);
      // rebuilds to itself, never to the live key
      expect(keyForParsed(pk)).toBe(c.key);
      expect(toCanonicalKey(c.key)).toBe(c.key);
      expect(archiveKey(pk.productType, pk.scope, pk.filename, `sha256:${c.root_id}`)).toBe(c.key);
      const livePk = parseRelpath(c.relpath);
      expect(livePk.root).toBe('live');
      expect(keyForParsed(livePk)).toBe(c.live_canonical);
      expect(() => toLegacyKey(c.key)).toThrow();
      expect(isReadableKey(c.key)).toBe(true);
      expect(isKnownKey(c.key)).toBe(true);
      expect(isWritableKey(c.key)).toBe(false);
      expect(isReadableKey(c.key, { bucket: 'data' })).toBe(true);
      expect(isWritableKey(c.key, { bucket: 'data' })).toBe(false);
      expect(isCompressedKey(c.key)).toBe(c.compressed);
      expect(() => deriveSibling(c.key, 'zfit')).toThrow();
    });
  }
  for (const key of golden.archive_rejected_keys as string[]) {
    it(`rejects ${key}`, () => {
      expect(() => parseKey(key)).toThrow();
      expect(isReadableKey(key)).toBe(false);
      expect(isWritableKey(key)).toBe(false);
    });
  }
});

interface DrCase { relpath: string; product_type: string; scope: Scope; filename: string; root_id: string; tree_class: string }

describe('release tree relpaths (P1): local only', () => {
  for (const c of golden.dr_relpaths as DrCase[]) {
    it(c.relpath, () => {
      const pk = parseRelpath(c.relpath);
      expect(pk.productType).toBe(c.product_type);
      expect(pk.filename).toBe(c.filename);
      expect(pk.root).toBe('dr');
      expect(pk.rootId).toBe(c.root_id);
      expect(isDrTag(c.root_id)).toBe(true);
      expect(() => keyForParsed(pk)).toThrow();
      expect(() => parseKey(`data/${c.relpath}`)).toThrow();
      expect(isReadableKey(c.relpath)).toBe(false);
    });
  }
  for (const rp of golden.dr_rejected_relpaths as string[]) {
    it(`rejects ${rp}`, () => expect(() => parseRelpath(rp)).toThrow());
  }
  it('rejects non-sha256 hashes and non-archivable products', () => {
    const scope = { obs: 'ember_egs_p1' };
    for (const h of golden.archive_rejected_hashes as string[]) {
      expect(() => archiveHex(h)).toThrow();
      expect(() => archiveKey('nirspec_spec', scope, 'ember_egs_p1_prism_clear_15182_spec.fits', h)).toThrow();
    }
    expect(archiveHex('SHA256:' + 'A'.repeat(64))).toBe('a'.repeat(64));
    for (const p of golden.archive_rejected_products as string[]) expect(isArchivable(p)).toBe(false);
    expect(isArchivable('nirspec_spec')).toBe(true);
    expect(isArchivable('nircam_mosaic')).toBe(true);
  });
  it('live parses carry root live', () => {
    for (const c of golden.cases as Case[]) {
      if (!c.key_canonical || !c.bucket) continue;
      const pk = parseKey(c.key_canonical, { bucket: c.bucket as 'data' | 'tiles' });
      expect(pk.root).toBe('live');
      expect(pk.rootId).toBeNull();
      expect(keyForParsed(pk)).toBe(c.key_canonical);
    }
  });
});

describe('sibling round-trips', () => {
  // A spectrum key and the four sidecars the web's resolver derives from it
  // (spectrum-sidecars.ts) form a clique under deriveSibling in both schemes.
  type RoundTrip = { scheme: string; spec: string; sidecars: Record<string, string> };
  for (const rt of golden.sibling_roundtrips as RoundTrip[]) {
    it(`${rt.scheme}: spec <-> all four sidecars`, () => {
      expect(Object.keys(rt.sidecars).sort()).toEqual(['nirspec_lines_json', 'spectrum_1d_json', 'spectrum_json', 'zfit']);
      expect(parseKey(rt.spec).productType).toBe('nirspec_spec');
      for (const [kind, key] of Object.entries(rt.sidecars)) {
        expect(deriveSibling(rt.spec, kind)).toBe(key);
        expect(deriveSibling(key, 'nirspec_spec')).toBe(rt.spec);
        expect(parseKey(key).productType).toBe(kind);
        for (const [other, otherKey] of Object.entries(rt.sidecars)) {
          expect(deriveSibling(key, other)).toBe(otherKey);
        }
      }
    });
  }
});

describe('compressed keys (golden)', () => {
  type Row = { key: string; product_type: string; scope: Scope; filename: string; relpath: string; compressed: boolean };
  for (const row of golden.compressed_keys as Row[]) {
    it(row.key, () => {
      const pk = parseKey(row.key);
      expect(pk.productType).toBe(row.product_type);
      expect(pk.scope).toEqual(row.scope);
      expect(pk.filename).toBe(row.filename); // plain local filename, '.gz' stripped
      expect(isCompressedKey(row.key)).toBe(row.compressed);
      expect(storageKey(pk.productType, pk.scope, pk.filename, 'canonical')).toBe(row.key);
      const fromRelpath = parseRelpath(row.relpath);
      expect(fromRelpath.productType).toBe(row.product_type);
      expect(fromRelpath.filename).toBe(row.filename);
      expect(toCanonicalKey(row.key)).toBe(row.key);
    });
  }
});

describe('registry parity', () => {
  it('every cloud-backed product has a golden case', () => {
    const covered = new Set((golden.cases as Case[]).map((c) => c.product_type));
    const missing = Object.keys(PRODUCTS).filter((p) => !covered.has(p));
    expect(missing).toEqual([]);
  });
  it('PRODUCTS is the generated table, in registration order', () => {
    expect(Object.keys(PRODUCTS)).toEqual(layoutProducts.products.map((p) => p.name));
  });
  it('the lifecycle carried by the generated table matches the golden tree_class', () => {
    // The python arm checks tree_class() against the fixture; TS has no
    // tree_class, so the per-product lifecycle in the JSON is pinned here.
    for (const c of golden.cases as Case[]) {
      expect(PRODUCTS[c.product_type].lifecycle).toBe(c.tree_class);
    }
  });
  it('suffix-dispatch tables name registered products with matching suffixes', () => {
    const tables = [layoutProducts.dispatch.nirspec_obs_suffixes, layoutProducts.dispatch.nircam_filter_suffixes];
    for (const table of tables) {
      for (const [suffix, productType] of table) {
        expect(PRODUCTS[productType]).toBeDefined();
        expect(PRODUCTS[productType].suffix).toBe(suffix);
      }
    }
  });
});
