// The bearer routes' `dr=` gate (P6c-2): a release is forwarded to the
// service client only after the caller's own visibility admitted it.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('server-only', () => ({}));

let releases: Record<string, { state: string }> = {};
// The service client with the header: the mode confirmation echoes the tag
// it was created with, unless the test says the hook is dead.
let hookActive = true;
let confirmCalls: string[] = [];
vi.mock('@/lib/supabase/service', () => ({
  createServiceClient: (opts: { dr?: string | null } = {}) => ({
    rpc: async (name: string) => {
      confirmCalls.push(`${name}:${opts.dr ?? ''}`);
      return hookActive ? { data: opts.dr ?? null, error: null } : { data: null, error: null };
    },
  }),
}));
vi.mock('@/lib/server/dr-exports', async () => {
  const actual = await vi.importActual<typeof import('@/lib/server/dr-exports')>('@/lib/server/dr-exports');
  return {
    ...actual,
    fetchRelease: async (_sb: unknown, tag: string) => (releases[tag] ? { tag, ...releases[tag] } : null),
  };
});

import { assertBearerReleaseAccess, bearerReleaseVisible, rejectReleaseParam } from './bearer-release';

const req = (qs: string) => new NextRequest(`http://x/api/v1/objects${qs}`);
const principal = (over: Partial<{ isAdmin: boolean; isLinkAccount: boolean }> = {}) =>
  ({ access: { isAdmin: false, isLinkAccount: false, accessibleSlugs: ['pub'], ...over } }) as never;

beforeEach(() => {
  releases = {
    'DR-pub': { state: 'published' }, 'DR-frozen': { state: 'frozen' }, 'DR-ready': { state: 'ready' },
    'DR-draft': { state: 'draft' }, 'DR-freezing': { state: 'freezing' }, 'DR-failed': { state: 'failed' },
    'DR-withdrawn': { state: 'withdrawn' },
  };
  hookActive = true;
  confirmCalls = [];
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
  it('admits an admin to every release past its freeze, and to nothing before it', async () => {
    for (const tag of ['DR-frozen', 'DR-ready', 'DR-withdrawn']) {
      expect(await assertBearerReleaseAccess(req(`?dr=${tag}`), principal({ isAdmin: true })), tag).toEqual({ tag });
    }
    for (const tag of ['DR-draft', 'DR-freezing', 'DR-failed']) {
      expect((await assertBearerReleaseAccess(req(`?dr=${tag}`), principal({ isAdmin: true }))).response?.status, tag).toBe(404);
    }
    expect(bearerReleaseVisible({ state: 'withdrawn' }, false)).toBe(false);
  });
  it('confirms the mode on the service client with the header, once, after the gate', async () => {
    await assertBearerReleaseAccess(req('?dr=DR-pub'), principal());
    expect(confirmCalls).toEqual(['dr_browse_tag:DR-pub']);
    confirmCalls = [];
    await assertBearerReleaseAccess(req('?dr=DR-nope'), principal());
    expect(confirmCalls).toEqual([]);
  });
  it('is a 500, never live rows, when the database does not browse the release', async () => {
    hookActive = false;
    const r = await assertBearerReleaseAccess(req('?dr=DR-pub'), principal());
    expect(r.response?.status).toBe(500);
    expect(await r.response!.json()).toEqual({ error: 'data release unavailable' });
  });
  it('rejectReleaseParam refuses dr on live-only routes with a 400', async () => {
    expect(rejectReleaseParam(new URLSearchParams(''))).toBeNull();
    const r = rejectReleaseParam(new URLSearchParams('dr=DR-pub'));
    expect(r?.status).toBe(400);
    expect((await r!.json()).error).toBe('dr not supported');
  });
  it('refuses link accounts, repeated and malformed values', async () => {
    expect((await assertBearerReleaseAccess(req('?dr=DR-pub'), principal({ isLinkAccount: true }))).response?.status).toBe(404);
    expect((await assertBearerReleaseAccess(req('?dr=DR-pub&dr=DR-pub'), principal())).response?.status).toBe(404);
    expect((await assertBearerReleaseAccess(req('?dr=..%2Fx'), principal())).response?.status).toBe(404);
  });
});
