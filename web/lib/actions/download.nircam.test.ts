// NIRCam bulk-download authorization (issue: the generated download script
// listed only the exposure maps).
//
// Root cause reproduced here: the mosaic authorization query sent every
// selected path in one PostgREST `.in()` list. That list rides in the request
// URL, so a whole-field COSMOS selection built a ~20-70 KB URL, the gateway
// refused it, and the action returned an empty url map — indistinguishable
// from "you may download none of these". The expmap query (19 keys, ~2 KB)
// was the only one that fit, so the script came out holding just the expmaps.
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('server-only', () => ({}));

// The module reads WORKER_JWT_SECRET at load time, so set it before the import.
vi.hoisted(() => {
  process.env.WORKER_JWT_SECRET = 'test-secret';
});

// A fake PostgREST builder that records what each query sent and refuses any
// `.in()` list whose serialized URL exceeds the gateway limit — the production
// failure, made deterministic.
const URL_LIMIT_BYTES = 8192;

let queries: Array<{ table: string; column: string; keys: string[] }> = [];
let rowsFor: (table: string, keys: string[]) => string[] = (_t, keys) => keys;

// The client itself must NOT be thenable (the action `await`s it); each
// `.from()` starts a fresh, awaitable query.
function fakeClient() {
  return {
    from: (table: string) => {
      const state = { column: '', keys: [] as string[] };
      const q: Record<string, unknown> = {
        select: () => q,
        eq: () => q,
        in: (column: string, keys: string[]) => {
          state.column = column;
          state.keys = keys;
          queries.push({ table, column, keys });
          return q;
        },
        then: (resolve: (v: { data: unknown; error: unknown }) => unknown) => {
          const urlBytes = state.keys.reduce(
            (n, k) => n + encodeURIComponent(`"${k}",`).length, 64);
          if (urlBytes > URL_LIMIT_BYTES) {
            return resolve({ data: null, error: { message: 'URI too long' } });
          }
          const allowed = rowsFor(table, state.keys);
          return resolve({
            data: allowed.map((k) => ({ [state.column]: k, target_id: 't' })),
            error: null,
          });
        },
      };
      return q;
    },
  };
}

vi.mock('@/lib/supabase/server', () => ({
  createClient: async () => fakeClient(),
  createServiceClient: () => fakeClient(),
}));

vi.mock('@/lib/r2', () => ({
  generateDownloadUrls: async (keys: string[]) => keys.map((k) => `signed://${k}`),
}));

vi.mock('@/lib/server/worker-token', () => ({
  hmacBase64Url: async () => 'sig',
}));

// Unused by these two actions, but imported by the module under test.
vi.mock('./spectra', () => ({ getSpectra: async () => ({}) }));
vi.mock('./download-tracking', () => ({ trackDownload: () => {} }));
vi.mock('@/lib/auth/identity', () => ({ getRequestIdentity: async () => ({ user: null, supabase: fakeClient() }) }));
vi.mock('@/lib/auth/access-context', () => ({ getAccessContext: async () => ({ accessibleSlugs: [] }) }));

import {
  generateNircamMosaicDownloadUrls,
  generateNircamExpmapDownloadUrls,
} from './download';

const mosaicKey = (filt: string, tile: string, ext: string) =>
  `data/products/nircam/cosmos/${filt}/mosaic_nircam_${filt}_cosmos_30mas_${tile}_${ext}.fits.gz`;

/** A COSMOS-scale mosaic selection: 8 filters x 20 tiles x 4 extensions. */
const cosmosMosaics = () => {
  const out: string[] = [];
  for (const f of ['f090w', 'f115w', 'f150w', 'f200w', 'f277w', 'f356w', 'f410m', 'f444w']) {
    for (let t = 1; t <= 20; t++) {
      for (const e of ['sci', 'err', 'wht', 'srcmask']) out.push(mosaicKey(f, `tile${t}`, e));
    }
  }
  return out;
};

const cosmosExpmaps = () =>
  Array.from({ length: 19 }, (_, i) => `data/products/nircam/cosmos/f${100 + i}w/expmap_cosmos_f${100 + i}w.fits`);

beforeEach(() => {
  queries = [];
  rowsFor = (_t, keys) => keys;
});

describe('generateNircamMosaicDownloadUrls', () => {
  it('authorizes a whole-field selection instead of overflowing the request URL', async () => {
    const paths = cosmosMosaics();
    const { urls, error } = await generateNircamMosaicDownloadUrls(paths);

    expect(error).toBeNull();
    // Every selected mosaic is presigned — the regression returned {} here.
    expect(Object.keys(urls)).toHaveLength(paths.length);
    expect(urls[paths[0]]).toContain('signed%3A%2F%2F');
    // ...and no single query got near the URL limit.
    expect(queries.length).toBeGreaterThan(1);
    expect(Math.max(...queries.map((q) => q.keys.length))).toBeLessThanOrEqual(50);
    expect(queries.every((q) => q.table === 'nircam_images' && q.column === 'file_path')).toBe(true);
  });

  it('presigns only the paths the caller may see', async () => {
    const paths = cosmosMosaics().slice(0, 60);
    const visible = new Set(paths.slice(0, 5));
    rowsFor = (_t, keys) => keys.filter((k) => visible.has(k));

    const { urls, error } = await generateNircamMosaicDownloadUrls(paths);
    expect(error).toBeNull();
    expect(Object.keys(urls).sort()).toEqual([...visible].sort());
  });

  it('reports a query failure instead of silently authorizing nothing', async () => {
    rowsFor = () => {
      throw new Error('unreachable');
    };
    // Force the failure path with a chunk the fake gateway rejects outright.
    const { urls, error } = await generateNircamMosaicDownloadUrls(
      Array.from({ length: 3 }, (_, i) => `${'x'.repeat(4000)}${i}`),
    );
    expect(urls).toEqual({});
    expect(error).toBe('Failed to authorize download');
  });

  it('rejects an absurd request outright', async () => {
    const { error } = await generateNircamMosaicDownloadUrls(
      Array.from({ length: 5001 }, (_, i) => `k${i}`),
    );
    expect(error).toMatch(/Too many files requested/);
    expect(queries).toHaveLength(0);
  });
});

describe('generateNircamExpmapDownloadUrls', () => {
  it('authorizes active expmap rows in chunks', async () => {
    const keys = cosmosExpmaps();
    const { urls, error } = await generateNircamExpmapDownloadUrls(keys);
    expect(error).toBeNull();
    expect(Object.keys(urls)).toHaveLength(keys.length);
    expect(queries.every((q) => q.table === 'storage_objects' && q.column === 'storage_key')).toBe(true);
  });

  it('chunks a large expmap selection too', async () => {
    const keys = Array.from({ length: 120 }, (_, i) => `data/products/nircam/cosmos/f${i}/expmap_cosmos_f${i}.fits`);
    const { urls, error } = await generateNircamExpmapDownloadUrls(keys);
    expect(error).toBeNull();
    expect(Object.keys(urls)).toHaveLength(120);
    expect(queries).toHaveLength(3);
  });
});
