// GET /api/v1/storage/download — the per-file primitive behind the NIRCam
// bulk-download script. Pins the trust model (bearer auth, layout allowlist,
// filter_accessible_storage_keys under the caller's scope) and the answer
// shape (302 to a fresh presigned url, never cacheable; JSON on request).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

const validateAuth = vi.fn<(req: NextRequest) => Promise<string | null>>();
vi.mock('@/lib/api-auth', () => ({ validateAuth: (req: NextRequest) => validateAuth(req) }));

let admin = false;
vi.mock('@/lib/api-helpers', () => ({
  getAccessiblePrograms: async () => ['public-program'],
  isAdminUser: async () => admin,
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
  validateAuth.mockReset();
  validateAuth.mockImplementation(async (req) =>
    req.headers.get('authorization') === 'Bearer sk_test' ? 'user-1' : null,
  );
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
    expect((await GET(request({}))).status).toBe(400);
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
