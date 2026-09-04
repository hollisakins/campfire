// Stream-through of JSON sidecars (perf T2-D2, #508): the fallback path
// must forward the upstream body untouched, with a length only when the
// upstream body is stored verbatim.
import { describe, it, expect, vi, afterEach } from 'vitest';

vi.mock('server-only', () => ({}));
vi.mock('@/lib/r2', () => ({ generateDownloadUrl: async (key: string) => `signed://bucket/${key}` }));

import { streamSidecar } from './sidecar-stream';

afterEach(() => vi.unstubAllGlobals());

describe('streamSidecar', () => {
  it('passes the body through as application/json with the upstream length', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response('{"wave":[1,2]}', { status: 200, headers: { 'Content-Type': 'binary/octet-stream', 'Content-Length': '14' } }));
    const out = await streamSidecar('data/products/nirspec/o/x_spec.json', 'private, max-age=1');
    expect(out.status).toBe('ok');
    const res = out.response as Response;
    expect(res.headers.get('Content-Type')).toBe('application/json');
    expect(res.headers.get('Content-Length')).toBe('14');
    expect(res.headers.get('Cache-Control')).toBe('private, max-age=1');
    expect(res.headers.get('Vary')).toBe('Cookie');
    expect(await res.text()).toBe('{"wave":[1,2]}');
  });

  it('drops the length when the upstream body is encoded', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response('x', { status: 200, headers: { 'Content-Length': '1', 'Content-Encoding': 'gzip' } }));
    const out = await streamSidecar('k', 'no-store');
    expect(out.response?.headers.get('Content-Length')).toBeNull();
  });

  it('reports a missing sidecar distinctly from an upstream failure', async () => {
    vi.stubGlobal('fetch', async () => new Response('nope', { status: 404 }));
    expect((await streamSidecar('k', 'no-store')).status).toBe('missing');
    vi.stubGlobal('fetch', async () => new Response('boom', { status: 503 }));
    expect(await streamSidecar('k', 'no-store')).toMatchObject({ status: 'error', upstreamStatus: 503 });
  });
});
