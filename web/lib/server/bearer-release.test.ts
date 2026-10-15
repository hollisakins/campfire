// The bearer routes' `dr=` gate (P6c-2): a release is forwarded to the
// service client only after the caller's own visibility admitted it.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

let releases: Record<string, { state: string }> = {};
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => ({}) }));
vi.mock('@/lib/server/dr-exports', async () => {
  const actual = await vi.importActual<typeof import('@/lib/server/dr-exports')>('@/lib/server/dr-exports');
  return {
    ...actual,
    fetchRelease: async (_sb: unknown, tag: string) => (releases[tag] ? { tag, ...releases[tag] } : null),
  };
});

import { assertBearerReleaseAccess } from './bearer-release';

const req = (qs: string) => new NextRequest(`http://x/api/v1/objects${qs}`);
const principal = (over: Partial<{ isAdmin: boolean; isLinkAccount: boolean }> = {}) =>
  ({ access: { isAdmin: false, isLinkAccount: false, accessibleSlugs: ['pub'], ...over } }) as never;

beforeEach(() => {
  releases = { 'DR-pub': { state: 'published' }, 'DR-frozen': { state: 'frozen' } };
});

describe('assertBearerReleaseAccess', () => {
  it('is the live catalog without the parameter', async () => {
    expect(await assertBearerReleaseAccess(req(''), principal())).toEqual({ tag: null });
  });
  it('admits a published release for a reader and forwards its tag', async () => {
    expect(await assertBearerReleaseAccess(req('?dr=DR-pub'), principal())).toEqual({ tag: 'DR-pub' });
  });
  it('refuses an unpublished release for a reader with the same 404 as an unknown one', async () => {
    const unpublished = await assertBearerReleaseAccess(req('?dr=DR-frozen'), principal());
    const unknown = await assertBearerReleaseAccess(req('?dr=DR-nope'), principal());
    expect(unpublished.response?.status).toBe(404);
    expect(unknown.response?.status).toBe(404);
    expect(await unpublished.response!.json()).toEqual(await unknown.response!.json());
  });
  it('admits any post-freeze state for an admin', async () => {
    expect(await assertBearerReleaseAccess(req('?dr=DR-frozen'), principal({ isAdmin: true }))).toEqual({ tag: 'DR-frozen' });
  });
  it('refuses link accounts, repeated and malformed values', async () => {
    expect((await assertBearerReleaseAccess(req('?dr=DR-pub'), principal({ isLinkAccount: true }))).response?.status).toBe(404);
    expect((await assertBearerReleaseAccess(req('?dr=DR-pub&dr=DR-pub'), principal())).response?.status).toBe(404);
    expect((await assertBearerReleaseAccess(req('?dr=..%2Fx'), principal())).response?.status).toBe(404);
  });
});
