// The object page's batched sidecar resolve (perf T2-E, #510) must answer
// exactly what /api/spectrum/sidecars answered per path before it, and never
// throw — the page renders whether or not the registry is reachable.
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('server-only', () => ({}));

type Resolved = { backend: 'r2' | 'osn'; key: string; contentHash: string | null; registeredAt: number | null };
let resolvedByKey: Record<string, Resolved> = {};
let resolveCalls: string[][] = [];
let resolveThrows = false;
vi.mock('@/lib/r2', () => ({
  resolveObjectBackends: async (keys: string[]) => {
    resolveCalls.push(keys);
    if (resolveThrows) throw new Error('registry down');
    return keys.map((k) => resolvedByKey[k] ?? { backend: 'r2', key: k, contentHash: null, registeredAt: null });
  },
  presignResolvedStable: async (o: { key: string }) => ({
    url: `https://uaz1.osn.mghpcc.org/campfire-jwst/${o.key}?X-Amz-Signature=abc`,
    exp: 1_757_000_000,
  }),
}));

import { resolveSpectrumSidecars, dehydrateSidecarUrls } from './spectrum-sidecars';
import { spectrum1dSources, spectrumSidecarsKey } from '@/lib/spectrum-sidecars';

const FITS_A = 'data/products/nirspec/obs_a/obj1_prism-clear_spec.fits';
const FITS_B = 'data/products/nirspec/obs_a/obj2_g140m-f070lp_spec.fits';
const sib = (fits: string, suffix: string) => fits.replace(/_spec\.fits$/, suffix);
const registered = (key: string): Resolved => ({ backend: 'osn', key, contentHash: 'h_' + key.length, registeredAt: 1_756_000_000 });

beforeEach(() => {
  vi.unstubAllEnvs();
  // The never-throws paths log; keep the run quiet.
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.stubEnv('CDN_FRONT_URL', 'https://front.example');
  vi.stubEnv('WORKER_JWT_SECRET', 'secret');
  resolvedByKey = {};
  resolveCalls = [];
  resolveThrows = false;
});

describe('resolveSpectrumSidecars', () => {
  it('resolves every sidecar of every path in one registry call', async () => {
    for (const f of [FITS_A, FITS_B]) {
      for (const suffix of ['_spec.json', '_spec_1d.json', '_zfit.json']) {
        const k = sib(f, suffix);
        resolvedByKey[k] = registered(k);
      }
    }
    const out = await resolveSpectrumSidecars([FITS_A, FITS_B, FITS_A]);
    expect(resolveCalls).toHaveLength(1);
    expect(resolveCalls[0]).toHaveLength(6);
    expect(out.size).toBe(2);
    for (const f of [FITS_A, FITS_B]) {
      const u = out.get(f)!;
      expect(u.front).toBe(true);
      expect(u.spectrum).toContain(`/o/${sib(f, '_spec.json')}?`);
      expect(u.spectrum_1d).toContain(`/o/${sib(f, '_spec_1d.json')}?`);
      expect(u.zfit).toContain(`/o/${sib(f, '_zfit.json')}?`);
      expect(u.has_1d).toBe(true);
      expect(u.has_zfit).toBe(true);
    }
  });

  it('reports a missing sidecar as definitively absent only when the full JSON resolved', async () => {
    resolvedByKey[sib(FITS_A, '_spec.json')] = registered(sib(FITS_A, '_spec.json'));
    // FITS_B: nothing registered at all (fail-open answer for every key).
    const out = await resolveSpectrumSidecars([FITS_A, FITS_B]);
    const a = out.get(FITS_A)!;
    expect(a.spectrum).not.toBeNull();
    expect(a.spectrum_1d).toBeNull();
    expect(a.has_1d).toBe(false);
    expect(a.has_zfit).toBe(false);
    const b = out.get(FITS_B)!;
    expect(b.spectrum).toBeNull();
    expect(b.has_1d).toBeNull();
    expect(b.has_zfit).toBeNull();
  });

  it('answers "front off" with no urls when the front is not configured', async () => {
    vi.stubEnv('CDN_FRONT_URL', '');
    resolvedByKey[sib(FITS_A, '_spec.json')] = registered(sib(FITS_A, '_spec.json'));
    const out = await resolveSpectrumSidecars([FITS_A]);
    const a = out.get(FITS_A)!;
    expect(a.front).toBe(false);
    expect(a.spectrum).toBeNull();
    // Presence flags still come from the registry.
    expect(a.has_1d).toBe(false);
  });

  it('never throws: a registry failure answers unknown for every path', async () => {
    resolveThrows = true;
    const out = await resolveSpectrumSidecars([FITS_A]);
    expect(out.get(FITS_A)).toEqual({ front: true, spectrum: null, spectrum_1d: null, zfit: null, has_1d: null, has_zfit: null });
  });

  it('never throws: a path the layout cannot parse answers unknown and does not sink the batch', async () => {
    resolvedByKey[sib(FITS_A, '_spec.json')] = registered(sib(FITS_A, '_spec.json'));
    const out = await resolveSpectrumSidecars(['not/a/layout/key.txt', FITS_A]);
    expect(out.get('not/a/layout/key.txt')!.has_1d).toBeNull();
    expect(out.get(FITS_A)!.has_1d).toBe(false);
    expect(resolveCalls[0]).toHaveLength(3);
  });

  it('answers an empty batch without touching the registry', async () => {
    const out = await resolveSpectrumSidecars([]);
    expect(out.size).toBe(0);
    expect(resolveCalls).toHaveLength(0);
  });
});

describe('spectrum1dSources', () => {
  it('prefers the 1-D front url, then the full JSON, and always names the streaming route', () => {
    const base = { front: true, spectrum: 'https://f/o/full', spectrum_1d: 'https://f/o/1d', zfit: null, has_1d: true, has_zfit: null };
    expect(spectrum1dSources(base, FITS_A).front).toBe('https://f/o/1d');
    expect(spectrum1dSources({ ...base, spectrum_1d: null }, FITS_A).front).toBe('https://f/o/full');
    expect(spectrum1dSources({ ...base, front: false }, FITS_A).front).toBeNull();
    expect(spectrum1dSources(undefined, FITS_A)).toEqual({
      front: null,
      route: `/api/spectrum?path=${encodeURIComponent(FITS_A)}&include=1d`,
    });
  });
});

describe('dehydrateSidecarUrls', () => {
  it('produces one settled query per path under the sidecar key', () => {
    const urls = { front: true, spectrum: 'https://f/o/full', spectrum_1d: null, zfit: null, has_1d: false, has_zfit: null };
    const state = dehydrateSidecarUrls(new Map([[FITS_A, urls]]));
    expect(state.queries).toHaveLength(1);
    expect(state.queries[0].queryKey).toEqual(spectrumSidecarsKey(FITS_A));
    expect(state.queries[0].state.data).toEqual(urls);
    expect(state.queries[0].state.status).toBe('success');
  });
});
