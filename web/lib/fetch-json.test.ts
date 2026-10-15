import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchJson, withDrParam } from './fetch-json';

describe('withDrParam', () => {
  it('appends the release to a route url', () => {
    expect(withDrParam('/api/filter-options', 'DR1')).toBe('/api/filter-options?dr=DR1');
    expect(withDrParam('/api/objects/near?ra=1&dec=2', 'DR2026.1')).toBe('/api/objects/near?ra=1&dec=2&dr=DR2026.1');
    expect(withDrParam('/api/x', null)).toBe('/api/x');
  });
});

describe('fetchJson', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('forwards the release as ?dr= so middleware stamps the header', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await fetchJson('/api/objects/adjacent?id=J1', { dr: 'DR-seed' });
    expect(fetchMock).toHaveBeenCalledWith('/api/objects/adjacent?id=J1&dr=DR-seed', { signal: undefined });
  });

  it('leaves live calls alone', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await fetchJson('/api/filter-options');
    expect(fetchMock).toHaveBeenCalledWith('/api/filter-options', { signal: undefined });
  });
});
