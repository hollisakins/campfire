// GET/HEAD /health: 200 while the Worker is configured to serve, 503 naming
// the missing setting otherwise, never cached, and no upstream fetch either way.
import { describe, it, expect, vi, afterEach } from 'vitest';
import worker from './index';

const ENV = {
  JWT_SECRET: 'test-shared-secret',
  ALLOWED_ORIGINS: 'https://campfire.hollisakins.com',
  ALLOWED_FETCH_HOSTS: 'uaz1.osn.mghpcc.org',
};

const health = (method = 'GET') => new Request('https://worker.example/health', { method });

describe('health probe', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('answers 200 ok without fetching anything', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    const res = await worker.fetch(health(), ENV);

    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('no-store');
    expect(await res.json()).toEqual({ ok: true });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('answers HEAD with the same status and no body', async () => {
    const res = await worker.fetch(health('HEAD'), ENV);

    expect(res.status).toBe(200);
    expect(await res.text()).toBe('');
  });

  it('answers 503 naming a missing secret', async () => {
    const res = await worker.fetch(health(), { ...ENV, JWT_SECRET: '' });

    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ ok: false, missing: ['JWT_SECRET'] });
  });

  it('answers 503 when the fetch allowlist is empty', async () => {
    const res = await worker.fetch(health(), { ...ENV, ALLOWED_FETCH_HOSTS: ' ' });

    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ ok: false, missing: ['ALLOWED_FETCH_HOSTS'] });
  });

  it('refuses other methods', async () => {
    const res = await worker.fetch(health('POST'), ENV);

    expect(res.status).toBe(405);
  });
});
