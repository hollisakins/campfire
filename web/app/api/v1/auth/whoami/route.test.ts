// GET /api/v1/auth/whoami — the authenticated caller, plus (P5-3) the newest
// published data release and the access-set fingerprint the sync tail reads.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

let slugs = ['pa'];
let admin = false;
const authenticate = vi.fn(async (req: NextRequest) => {
  const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');
  if (bearer !== 'sk_test') return null;
  return {
    userId: 'user-1',
    method: 'api_key',
    access: { isAdmin: admin, isLinkAccount: false, linkScope: null, accessibleSlugs: slugs },
  };
});
vi.mock('@/lib/api-auth', () => ({ authenticateApiRequest: (req: NextRequest) => authenticate(req) }));

const getUserById = vi.fn();
const single = vi.fn();
vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: () => ({
    auth: { admin: { getUserById: (id: string) => getUserById(id) } },
    from: () => ({ select: () => ({ eq: () => ({ single: () => single() }) }) }),
  }),
}));

const fetchLatestPublishedRelease = vi.fn<() => Promise<unknown>>();
vi.mock('@/lib/server/dr-exports', () => ({
  fetchLatestPublishedRelease: () => fetchLatestPublishedRelease(),
}));

import { GET } from './route';

function request(token: string | null = 'sk_test') {
  return new NextRequest('http://localhost/api/v1/auth/whoami', {
    headers: token ? { authorization: `Bearer ${token}` } : {},
  });
}

beforeEach(() => {
  slugs = ['pa'];
  admin = false;
  getUserById.mockReset();
  getUserById.mockResolvedValue({ data: { user: { email: 'u@x.test', created_at: '2026-01-01T00:00:00Z' } }, error: null });
  single.mockReset();
  single.mockResolvedValue({ data: { full_name: 'U', is_admin: false, created_at: '2026-01-02T00:00:00Z' } });
  fetchLatestPublishedRelease.mockReset();
  fetchLatestPublishedRelease.mockResolvedValue({ tag: 'DR2026.1', doi: '10.1/x', published_at: '2026-10-05T00:00:00Z' });
});

describe('GET /api/v1/auth/whoami', () => {
  it('401 without a credential, before any lookup', async () => {
    const res = await GET(request(null));
    expect(res.status).toBe(401);
    expect(getUserById).not.toHaveBeenCalled();
    expect(fetchLatestPublishedRelease).not.toHaveBeenCalled();
  });

  it('answers the user, the newest published release and the fingerprint, uncached', async () => {
    const res = await GET(request());
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('private, no-store');
    const body = await res.json();
    expect(body).toMatchObject({
      user_id: 'user-1', email: 'u@x.test', full_name: 'U', is_admin: false, created_at: '2026-01-02T00:00:00Z',
      latest_dr: { tag: 'DR2026.1', doi: '10.1/x', published_at: '2026-10-05T00:00:00Z' },
    });
    expect(body.access_fingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
  });

  it('the fingerprint follows the program set', async () => {
    const a = (await (await GET(request())).json()).access_fingerprint;
    slugs = ['pa', 'pb'];
    const b = (await (await GET(request())).json()).access_fingerprint;
    expect(b).not.toBe(a);
    slugs = ['pb', 'pa'];
    expect((await (await GET(request())).json()).access_fingerprint).toBe(b);
  });

  it('latest_dr is null before the first publication', async () => {
    fetchLatestPublishedRelease.mockResolvedValue(null);
    const body = await (await GET(request())).json();
    expect(body.latest_dr).toBeNull();
  });

  it('404 when the auth user is gone; 500 when a lookup fails', async () => {
    getUserById.mockResolvedValue({ data: { user: null }, error: null });
    expect((await GET(request())).status).toBe(404);
    getUserById.mockResolvedValue({ data: { user: { email: 'u@x.test' } }, error: null });
    fetchLatestPublishedRelease.mockRejectedValue(new Error('db down'));
    expect((await GET(request())).status).toBe(500);
  });
});
