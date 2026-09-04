// Pure parts of the content-addressed cutout store (perf T2-D3, #509).
import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));
vi.mock('next/server', () => ({ after: () => { throw new Error('no request scope'); } }));
vi.mock('@/lib/storage', () => ({
  getS3Client: () => ({ send: async () => ({}) }),
  getBucketName: () => 'campfire-tiles',
  getPublicUrlBase: () => 'https://campfire-tiles.example/',
}));

import { cutoutStoreKey, storeSizeFor, cutoutStoreFor, CUTOUT_SIZE_LADDER } from './store';

describe('storeSizeFor', () => {
  it('rounds browser sizes up to the ladder and keeps larger sizes exact', () => {
    expect(storeSizeFor(20)).toBe(64);
    expect(storeSizeFor(40)).toBe(64);
    expect(storeSizeFor(48)).toBe(64);
    expect(storeSizeFor(64)).toBe(64);
    expect(storeSizeFor(96)).toBe(300);
    expect(storeSizeFor(300)).toBe(300);
    expect(storeSizeFor(560)).toBe(600);
    expect(storeSizeFor(600)).toBe(600);
    expect(storeSizeFor(2048)).toBe(2048);
    expect(CUTOUT_SIZE_LADDER).toEqual([64, 300, 600]);
  });
});

describe('cutoutStoreKey', () => {
  const base = { field: 'egs', version: 'ab12cd34ef', size: 64, fov: 5, ra: 214.8250001, dec: 52.825 };

  it('is deterministic and carries every render input', () => {
    expect(cutoutStoreKey(base)).toBe('cutouts/egs/vab12cd34ef/64/5/214.8250001_+52.8250000.png');
    expect(cutoutStoreKey({ ...base, fov: 3.2, dec: -2.3 })).toBe('cutouts/egs/vab12cd34ef/64/3.2/214.8250001_-2.3000000.png');
  });

  it('a new imaging version is a new prefix (re-deploy changes every url)', () => {
    expect(cutoutStoreKey({ ...base, version: 'ffff000000' })).not.toBe(cutoutStoreKey(base));
    expect(cutoutStoreKey({ ...base, version: 'ffff000000' }).startsWith('cutouts/egs/vffff000000/')).toBe(true);
  });

  it('sanitizes the field and version segments', () => {
    expect(cutoutStoreKey({ ...base, field: 'A 2744/x', version: 'v/..1' })).toBe(
      'cutouts/a_2744_x/vv1/64/5/214.8250001_+52.8250000.png');
  });
});

describe('cutoutStoreFor', () => {
  it('joins the public tiles base and the key', () => {
    const e = cutoutStoreFor({ field: 'egs', version: 'v1', size: 64, fov: 5, ra: 1, dec: 1 });
    expect(e?.url).toBe('https://campfire-tiles.example/cutouts/egs/vv1/64/5/1.0000000_+1.0000000.png');
  });
});
