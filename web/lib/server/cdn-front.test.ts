// Mint side of the delivery front (perf T2-D1, #507). The Worker's
// object.test.ts is the verifying mirror: the token message here must match
// objectTokenMessage there byte for byte.
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('server-only', () => ({}));

let resolved: Array<{ backend: 'r2' | 'osn'; key: string; contentHash: string | null; registeredAt: number | null }> = [];
vi.mock('@/lib/r2', () => ({
  resolveObjectBackends: async () => resolved,
  presignResolvedStable: async (o: { key: string }) => ({
    url: `https://uaz1.osn.mghpcc.org/campfire-jwst/${o.key}?X-Amz-Date=20260904T120000Z&X-Amz-Signature=abc`,
    exp: 1_757_000_000,
  }),
}));

import { frontUrlsFor, frontUrlFor, objectTokenMessage, buildFrontUrl, cdnFrontBase } from './cdn-front';
import { hmacBase64Url } from './worker-token';

const KEY = 'data/products/nirspec/obs/x_spec.json';

beforeEach(() => {
  vi.unstubAllEnvs();
  resolved = [];
});

describe('cdnFrontBase', () => {
  it('is null unless both the Worker origin and the shared secret are set', () => {
    expect(cdnFrontBase()).toBeNull();
    vi.stubEnv('CDN_FRONT_URL', 'https://w.example/');
    expect(cdnFrontBase()).toBeNull();
    vi.stubEnv('WORKER_JWT_SECRET', 's');
    expect(cdnFrontBase()).toBe('https://w.example');
  });
});

describe('frontUrlsFor', () => {
  it('returns null for every key when the front is off (callers stream themselves)', async () => {
    resolved = [{ backend: 'osn', key: KEY, contentHash: 'sha256:aa', registeredAt: 1_756_990_000 }];
    const out = await frontUrlsFor([KEY]);
    expect(out.get(KEY)).toBeNull();
  });

  it('mints a url carrying the key path, hash, expiry, token and upstream', async () => {
    vi.stubEnv('CDN_FRONT_URL', 'https://w.example');
    vi.stubEnv('WORKER_JWT_SECRET', 'secret');
    resolved = [{ backend: 'osn', key: KEY, contentHash: 'sha256:aa', registeredAt: 1_756_990_000 }];

    const url = await frontUrlFor(KEY);
    expect(url).not.toBeNull();
    const u = new URL(url as string);
    expect(u.origin).toBe('https://w.example');
    expect(u.pathname).toBe(`/o/${KEY}`);
    expect(u.searchParams.get('h')).toBe('sha256:aa');
    expect(u.searchParams.get('e')).toBe('1757000000');
    expect(u.searchParams.get('r')).toBe('1756990000');
    const upstream = u.searchParams.get('u') as string;
    expect(upstream).toContain(`/campfire-jwst/${KEY}?`);
    const expected = await hmacBase64Url(
      objectTokenMessage(KEY, 'sha256:aa', 1_757_000_000, upstream, 1_756_990_000),
      'secret',
    );
    expect(u.searchParams.get('t')).toBe(expected);
  });

  it('is deterministic for the same inputs (the browser cache can hit)', async () => {
    vi.stubEnv('CDN_FRONT_URL', 'https://w.example');
    vi.stubEnv('WORKER_JWT_SECRET', 'secret');
    resolved = [{ backend: 'osn', key: KEY, contentHash: 'sha256:aa', registeredAt: 1_756_990_000 }];
    expect(await frontUrlFor(KEY)).toBe(await frontUrlFor(KEY));
  });

  it('skips keys without a registry content identity, keeps the others', async () => {
    vi.stubEnv('CDN_FRONT_URL', 'https://w.example');
    vi.stubEnv('WORKER_JWT_SECRET', 'secret');
    const OTHER = 'data/products/nirspec/obs/y_spec.json';
    resolved = [
      { backend: 'osn', key: KEY, contentHash: 'sha256:aa', registeredAt: 1_756_990_000 },
      { backend: 'r2', key: OTHER, contentHash: null, registeredAt: null },
    ];
    const out = await frontUrlsFor([KEY, OTHER]);
    expect(out.get(KEY)).toContain('/o/');
    expect(out.get(OTHER)).toBeNull();
  });

  it('percent-encodes key segments and the upstream', () => {
    const url = buildFrontUrl('https://w.example', 'data/a b/c.json', 'sha256:x', 1, 'https://h/b/data/a b/c.json?q=1&r=2', 'tok', 7);
    expect(url).toBe('https://w.example/o/data/a%20b/c.json?h=sha256%3Ax&e=1&r=7&t=tok&u=https%3A%2F%2Fh%2Fb%2Fdata%2Fa%20b%2Fc.json%3Fq%3D1%26r%3D2');
  });
});
