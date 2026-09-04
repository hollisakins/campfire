// Per-field imaging versions (#497) as the cutout store's key (#509): the
// publish-state term must never roll on a transient lookup failure.
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('next/cache', () => ({ unstable_cache: (fn: () => unknown) => fn }));

const db = vi.hoisted(() => ({
  publicity: { data: true as boolean | null, error: null as null | { message: string } },
  layersError: null as null | { message: string },
}));

/** A thenable that also answers every builder method with itself. */
function chain<T>(result: T) {
  const c: Record<string, unknown> = {};
  for (const m of ['select', 'eq', 'order', 'limit', 'maybeSingle']) c[m] = () => c;
  c.then = (resolve: (v: T) => void) => Promise.resolve(result).then(resolve);
  return c;
}

vi.mock('@/lib/supabase/server', () => ({
  createServiceClient: () => ({
    from: (table: string) => {
      if (table === 'map_layers') {
        return chain({
          data: db.layersError ? null : [
            { field: 'egs', filter: 'f444w', tile_version: 3 },
            { field: 'cosmos', filter: 'f444w', tile_version: 7 },
          ],
          error: db.layersError,
        });
      }
      if (table === 'fitsgl_datasets') {
        return chain({
          data: [{
            field: 'egs', prefix: 'fitsgl/egs', deployed_at: '2026-09-04T00:00:00+00:00',
            source_hashes: { ne: { f444w: 'sha256:aa' } }, is_default: true,
            tiles: ['ne'], bands: ['f444w'], pixel_scale: '30mas',
          }],
        });
      }
      return chain({ data: { deployed_at: '2026-09-01T00:00:00+00:00' } });
    },
    rpc: async () => db.publicity,
  }),
}));

import { getAssetVersions } from './asset-version';

beforeEach(() => {
  db.layersError = null;
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('getAssetVersions — input queries', () => {
  it('yields no versions at all when an input query fails (a truncated input list is not a version)', async () => {
    db.publicity = { data: true, error: null };
    db.layersError = { message: 'db down' };
    const v = await getAssetVersions();
    expect(v.byField).toEqual({});
    expect(v.global).toBe('');
  });
});

describe('getAssetVersions — publish state', () => {
  it('a cold lookup failure leaves only that field without a version; the others are unaffected', async () => {
    db.publicity = { data: null, error: { message: 'rpc down' } };
    const v = await getAssetVersions();
    expect(v.byField.egs).toBeUndefined();
    expect(v.byField.cosmos).toMatch(/^[0-9a-f]{10}$/);
    expect(v.global).toMatch(/^[0-9a-f]{10}$/);
  });

  it('a later lookup failure reuses the last known publish state, so the version does not move', async () => {
    db.publicity = { data: true, error: null };
    const ok = await getAssetVersions();
    expect(ok.byField.egs).toMatch(/^[0-9a-f]{10}$/);

    db.publicity = { data: null, error: { message: 'rpc down' } };
    const during = await getAssetVersions();
    expect(during.byField.egs).toBe(ok.byField.egs);
    expect(during.byField.cosmos).toBe(ok.byField.cosmos);
    expect(during.global).toBe(ok.global);
  });

  it('a real publish-state change does move the version', async () => {
    db.publicity = { data: true, error: null };
    const published = await getAssetVersions();
    db.publicity = { data: false, error: null };
    const unpublished = await getAssetVersions();
    expect(unpublished.byField.egs).not.toBe(published.byField.egs);
  });
});
