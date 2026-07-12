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
  deriveSibling,
  isKnownKey,
  isCompressedKey,
  PRODUCTS,
  type Scope,
} from './layout';

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

describe('registry parity', () => {
  it('every cloud-backed product has a golden case', () => {
    const covered = new Set((golden.cases as Case[]).map((c) => c.product_type));
    const missing = Object.keys(PRODUCTS).filter((p) => !covered.has(p));
    expect(missing).toEqual([]);
  });
});
