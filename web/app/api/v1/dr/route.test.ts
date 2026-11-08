// GET /api/v1/dr — the releases a bearer caller may read (`campfire dr list`,
// P5-2). Pins: bearer auth first, the published-only rule for readers and the
// every-state rule for admins (fetchVisibleReleases), the row shape, no
// caching, and the live-only contract (a `dr` parameter is a 400).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

let admin = false;
const authenticate = vi.fn(async (req: NextRequest) => {
  const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');
  if (bearer !== 'sk_test') return null;
  return {
    userId: 'user-1',
    method: 'api_key',
    access: { isAdmin: admin, isLinkAccount: false, linkScope: null, accessibleSlugs: ['pa'] },
  };
});
vi.mock('@/lib/api-auth', () => ({ authenticateApiRequest: (req: NextRequest) => authenticate(req) }));
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({}) }));

const fetchVisibleReleases = vi.fn<(sb: unknown, isAdmin: boolean) => Promise<unknown[]>>();
const fetchProgramRoster = vi.fn<(sb: unknown, tag: string, slugs: readonly string[]) => Promise<unknown>>();
vi.mock('@/lib/server/dr-exports', () => ({
  fetchVisibleReleases: (sb: unknown, isAdmin: boolean) => fetchVisibleReleases(sb, isAdmin),
  fetchProgramRoster: (sb: unknown, tag: string, slugs: readonly string[]) => fetchProgramRoster(sb, tag, slugs),
}));

import { GET } from './route';

const ROW = {
  tag: 'DR2026.1', state: 'published', components: ['nirspec'], dr_format_version: 1,
  counts: { spectra: 81420 }, frozen_at: '2026-10-01T00:00:00Z', published_at: '2026-10-05T00:00:00Z',
  withdrawn_at: null, doi: '10.1/x', notes: null, known_issues: null,
};

function request(query: Record<string, string> = {}, token: string | null = 'sk_test') {
  const url = new URL('http://localhost/api/v1/dr');
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
  return new NextRequest(url, { headers: token ? { authorization: `Bearer ${token}` } : {} });
}

beforeEach(() => {
  authenticate.mockClear();
  fetchVisibleReleases.mockReset();
  fetchVisibleReleases.mockResolvedValue([ROW]);
  fetchProgramRoster.mockReset();
  fetchProgramRoster.mockResolvedValue({ pa: { is_public: false, spectra: 3, cfpipe_versions: ['0.7.0'], crds_contexts: [] } });
  admin = false;
});

describe('GET /api/v1/dr', () => {
  it('401 without a bearer credential, before any lookup', async () => {
    const res = await GET(request({}, null));
    expect(res.status).toBe(401);
    expect(fetchVisibleReleases).not.toHaveBeenCalled();
  });

  it('400 on a dr parameter: the listing is live-only by contract', async () => {
    const res = await GET(request({ dr: 'DR2026.1' }));
    expect(res.status).toBe(400);
    expect(fetchVisibleReleases).not.toHaveBeenCalled();
  });

  it('lists the visible releases in the documented shape, uncached', async () => {
    const res = await GET(request());
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('private, no-store');
    expect(fetchVisibleReleases).toHaveBeenCalledWith(expect.anything(), false);
    const body = await res.json();
    expect(body.releases).toEqual([{
      tag: 'DR2026.1', state: 'published', format_version: 1, components: ['nirspec'],
      counts: { spectra: 81420 }, frozen_at: '2026-10-01T00:00:00Z', published_at: '2026-10-05T00:00:00Z',
      withdrawn_at: null, doi: '10.1/x', notes: null, known_issues: null,
      programs: { pa: { is_public: false, spectra: 3, cfpipe_versions: ['0.7.0'], crds_contexts: [] } },
    }]);
    // the roster is asked under the caller's own program set, per release
    expect(fetchProgramRoster).toHaveBeenCalledWith(expect.anything(), 'DR2026.1', ['pa']);
  });

  it('asks for every state when the caller is an admin', async () => {
    admin = true;
    await GET(request());
    expect(fetchVisibleReleases).toHaveBeenCalledWith(expect.anything(), true);
  });

  it('500 when the lookup fails', async () => {
    fetchVisibleReleases.mockRejectedValue(new Error('db down'));
    const res = await GET(request());
    expect(res.status).toBe(500);
  });
});
