// GET /api/v1/storage/download — the per-file primitive behind the NIRCam
// bulk-download script. Pins the trust model (bearer auth, layout allowlist,
// filter_accessible_storage_keys under the caller's scope) and the answer
// shape (302 to a fresh presigned url, never cacheable; JSON on request).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

// The route's one authenticator (api-auth.ts): API key, access token, or —
// only for this route — a download token. Its own acceptance rules are tested
// in lib/auth/download-token.test.ts; here it is a bearer lookup.
let admin = false;
const authenticate = vi.fn(async (req: NextRequest) => {
  const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');
  const method = bearer === 'sk_test' ? 'api_key' : bearer === 'dl_test' ? 'download_token' : null;
  if (!method) return null;
  return {
    userId: 'user-1',
    method,
    access: { isAdmin: admin, isLinkAccount: false, linkScope: null, accessibleSlugs: ['public-program'] },
  };
});
vi.mock('@/lib/api-auth', () => ({
  authenticateStorageDownloadRequest: (req: NextRequest) => authenticate(req),
}));

const rpc = vi.fn<(name: string, args: Record<string, unknown>) => Promise<{ data: unknown; error: unknown }>>();
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({ rpc }) }));

const generateDownloadUrl = vi.fn<(key: string, ttl: number) => Promise<string>>();
vi.mock('@/lib/r2', () => ({
  generateDownloadUrl: (key: string, ttl: number) => generateDownloadUrl(key, ttl),
}));

import { GET } from './route';

const KEY = 'data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_tile1_sci.fits.gz';
const PRESIGNED = 'https://osn.example.org/bucket/' + KEY + '?X-Amz-Signature=abc';

function request(query: Record<string, string>, token: string | null = 'sk_test') {
  const url = new URL('http://localhost/api/v1/storage/download');
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
  return new NextRequest(url, {
    headers: token ? { authorization: `Bearer ${token}` } : {},
  });
}

beforeEach(() => {
  authenticate.mockClear();
  rpc.mockReset();
  rpc.mockResolvedValue({ data: [{ storage_key: KEY }], error: null });
  generateDownloadUrl.mockReset();
  generateDownloadUrl.mockResolvedValue(PRESIGNED);
  admin = false;
});

describe('GET /api/v1/storage/download', () => {
  it('401 without a valid bearer credential, before touching the key', async () => {
    const res = await GET(request({ key: KEY }, null));
    expect(res.status).toBe(401);
    expect(rpc).not.toHaveBeenCalled();
    expect(generateDownloadUrl).not.toHaveBeenCalled();
  });

  it('400 for a missing or non-layout key (never presigns arbitrary paths)', async () => {
    // No key with an accepted credential is 400 — the script's up-front
    // credential check relies on 401-vs-400 here.
    expect((await GET(request({}))).status).toBe(400);
    expect((await GET(request({}, 'dl_test'))).status).toBe(400);
    expect((await GET(request({}, 'nope'))).status).toBe(401);
    expect((await GET(request({ key: '../../etc/passwd' }))).status).toBe(400);
    expect((await GET(request({ key: 'not/a/product' }))).status).toBe(400);
    expect(rpc).not.toHaveBeenCalled();
  });

  it('authorizes the key under the caller scope and 302s to a fresh presigned url, uncacheable', async () => {
    const res = await GET(request({ key: KEY }));
    expect(res.status).toBe(302);
    expect(res.headers.get('location')).toBe(PRESIGNED);
    expect(res.headers.get('cache-control')).toBe('no-store');

    expect(rpc).toHaveBeenCalledWith('filter_accessible_storage_keys', {
      p_keys: [KEY],
      p_program_slugs: ['public-program'],
      p_include_unpublished: false,
    });
    // Long enough for one multi-GB file on a slow link; the script asks again
    // for the next file, so the bulk download never depends on this window.
    expect(generateDownloadUrl).toHaveBeenCalledWith(KEY, 21600);
  });

  it('a download token authorizes exactly like an API key', async () => {
    const res = await GET(request({ key: KEY }, 'dl_test'));
    expect(res.status).toBe(302);
    expect(res.headers.get('location')).toBe(PRESIGNED);
    expect(rpc.mock.calls[0][1]).toMatchObject({ p_program_slugs: ['public-program'], p_include_unpublished: false });
  });

  it('admins authorize with unpublished rows included', async () => {
    admin = true;
    await GET(request({ key: KEY }));
    expect(rpc.mock.calls[0][1]).toMatchObject({ p_include_unpublished: true });
  });

  it('404 when the key is outside the caller scope or does not exist, without presigning', async () => {
    rpc.mockResolvedValue({ data: [], error: null });
    const res = await GET(request({ key: KEY }));
    expect(res.status).toBe(404);
    expect(generateDownloadUrl).not.toHaveBeenCalled();
  });

  it('500 when authorization itself fails (never fail-open)', async () => {
    rpc.mockResolvedValue({ data: null, error: { message: 'boom' } });
    const res = await GET(request({ key: KEY }));
    expect(res.status).toBe(500);
    expect(generateDownloadUrl).not.toHaveBeenCalled();
  });

  it('redirect=false answers the url as JSON', async () => {
    const res = await GET(request({ key: KEY, redirect: 'false' }));
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(await res.json()).toEqual({ url: PRESIGNED, expires_in: 21600 });
  });
});
