// POST /api/v1/identify — what the archive knows about files by their bytes
// (`campfire identify`, P5-2). Pins: bearer auth first, the body contract
// (≤ 200 files, sha256:<hex> hashes, anything else nulled rather than
// refused), the RPC call under the caller's explicit scope, no caching, and
// the live-only contract (a `dr` parameter is a 400).
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
    access: { isAdmin: admin, isLinkAccount: false, linkScope: null, accessibleSlugs: ['pa', 'pb'] },
  };
});
vi.mock('@/lib/api-auth', () => ({ authenticateApiRequest: (req: NextRequest) => authenticate(req) }));

const rpc = vi.fn<(name: string, args: Record<string, unknown>) => Promise<{ data: unknown; error: unknown }>>();
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({ rpc }) }));

import { POST } from './route';

const SHA = 'sha256:' + 'a'.repeat(64);
const SCI = 'sha256:' + 'b'.repeat(64);

function request(body: unknown, query: Record<string, string> = {}, token: string | null = 'sk_test') {
  const url = new URL('http://localhost/api/v1/identify');
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
  return new NextRequest(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

beforeEach(() => {
  authenticate.mockClear();
  rpc.mockReset();
  rpc.mockResolvedValue({ data: [{ sha256: SHA, live: [], releases: [], collected: [] }], error: null });
  admin = false;
});

describe('POST /api/v1/identify', () => {
  it('401 without a bearer credential, before reading the body', async () => {
    const res = await POST(request({ files: [{ sha256: SHA }] }, {}, null));
    expect(res.status).toBe(401);
    expect(rpc).not.toHaveBeenCalled();
  });

  it('400 on a dr parameter: releases are part of the answer, not a mode', async () => {
    const res = await POST(request({ files: [{ sha256: SHA }] }, { dr: 'DR2026.1' }));
    expect(res.status).toBe(400);
    expect(rpc).not.toHaveBeenCalled();
  });

  it('400 on a body without files, too many files, or no usable hash', async () => {
    expect((await POST(request('not json'))).status).toBe(400);
    expect((await POST(request({ files: [] }))).status).toBe(400);
    expect((await POST(request({ files: Array.from({ length: 201 }, () => ({ sha256: SHA })) }))).status).toBe(400);
    expect((await POST(request({ files: [{ sha256: 'md5:zzz' }] }))).status).toBe(400);
    expect(rpc).not.toHaveBeenCalled();
  });

  it('calls identify_files under the caller scope with malformed hashes nulled', async () => {
    const res = await POST(request({ files: [{ sha256: SHA, science_hash: SCI }, { sha256: 'nope', science_hash: SCI }] }));
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('private, no-store');
    expect(rpc).toHaveBeenCalledWith('identify_files', {
      p_files: [{ sha256: SHA, science_hash: SCI }, { sha256: null, science_hash: SCI }],
      p_program_slugs: ['pa', 'pb'],
      p_include_unpublished: false,
    });
    expect((await res.json()).results).toEqual([{ sha256: SHA, live: [], releases: [], collected: [] }]);
  });

  it('passes the admin flag as p_include_unpublished', async () => {
    admin = true;
    await POST(request({ files: [{ sha256: SHA }] }));
    expect(rpc.mock.calls[0][1].p_include_unpublished).toBe(true);
  });

  it('500 when the RPC fails', async () => {
    rpc.mockResolvedValue({ data: null, error: { message: 'boom' } });
    const res = await POST(request({ files: [{ sha256: SHA }] }));
    expect(res.status).toBe(500);
  });
});
