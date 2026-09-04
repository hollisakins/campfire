import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { SupabaseClient } from '@supabase/supabase-js';
import {
  getAccessContext,
  invalidateAccessContext,
  ACCESS_CONTEXT_TTL_MS,
  DEAD_LINK_SCOPE,
} from './access-context';

// ---------------------------------------------------------------------------
// A minimal fake of the PostgREST query builder: enough for the four reads
// computeAccessContext() performs. Each table returns canned rows filtered by
// the .eq() calls it saw; `errors` forces a failure for a table.
// ---------------------------------------------------------------------------
type Row = Record<string, unknown>;
interface Fixture {
  user_profiles?: Row[];
  user_program_access?: Row[];
  programs?: Row[];
  share_links?: Row[];
  observations?: Row[];
  errors?: Partial<Record<string, { message: string }>>;
}

function fakeDb(fx: Fixture, calls: string[] = []): SupabaseClient {
  const from = (table: string) => {
    calls.push(table);
    const filters: [string, unknown][] = [];
    const rows = () => {
      const all = (fx[table as keyof Fixture] as Row[] | undefined) ?? [];
      return all.filter(r => filters.every(([k, v]) => r[k] === v));
    };
    const result = (data: unknown) =>
      Promise.resolve(fx.errors?.[table] ? { data: null, error: fx.errors[table] } : { data, error: null });
    const builder = {
      select: () => builder,
      eq: (k: string, v: unknown) => {
        filters.push([k, v]);
        return builder;
      },
      maybeSingle: () => result(rows()[0] ?? null),
      single: () => result(rows()[0] ?? null),
      then: (onOk: (v: unknown) => unknown, onErr?: (e: unknown) => unknown) =>
        result(rows()).then(onOk, onErr),
    };
    return builder;
  };
  return { from } as unknown as SupabaseClient;
}

const U = '00000000-0000-0000-0000-00000000000a';
const programs = [
  { slug: 'pub1', is_public: true },
  { slug: 'pub2', is_public: true },
  { slug: 'priv1', is_public: false },
  { slug: 'priv2', is_public: false },
];

beforeEach(() => {
  invalidateAccessContext();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('getAccessContext — mirrors accessible_program_slugs()', () => {
  it('ordinary user: grants ∪ public, not admin, no link scope', async () => {
    const ctx = await getAccessContext(
      U,
      fakeDb({
        user_profiles: [{ user_id: U, is_admin: false, is_link_account: false, can_comment: true, can_inspect: false }],
        user_program_access: [{ user_id: U, program_slug: 'priv1' }, { user_id: 'someone-else', program_slug: 'priv2' }],
        programs,
      })
    );
    expect(ctx.isAdmin).toBe(false);
    expect(ctx.isLinkAccount).toBe(false);
    expect(ctx.linkScope).toBeNull();
    expect(ctx.canComment).toBe(true);
    expect(ctx.canInspect).toBe(false);
    expect(ctx.hasProfile).toBe(true);
    expect([...ctx.accessibleSlugs].sort()).toEqual(['priv1', 'pub1', 'pub2']);
    expect(ctx.grantedSlugs).toEqual(['priv1']);
  });

  it('user without a profile row is an ordinary user with public access only', async () => {
    const ctx = await getAccessContext(U, fakeDb({ user_profiles: [], user_program_access: [], programs }));
    expect(ctx.hasProfile).toBe(false);
    expect(ctx.isLinkAccount).toBe(false);
    expect([...ctx.accessibleSlugs].sort()).toEqual(['pub1', 'pub2']);
  });

  it('admin: every program', async () => {
    const ctx = await getAccessContext(
      U,
      fakeDb({
        user_profiles: [{ user_id: U, is_admin: true, is_link_account: false }],
        user_program_access: [],
        programs,
      })
    );
    expect(ctx.isAdmin).toBe(true);
    expect([...ctx.accessibleSlugs].sort()).toEqual(['priv1', 'priv2', 'pub1', 'pub2']);
  });

  it('active observation-scoped link: only that observation’s program, no public union', async () => {
    const ctx = await getAccessContext(
      U,
      fakeDb({
        user_profiles: [{ user_id: U, is_admin: true /* ignored for links */, is_link_account: true }],
        user_program_access: [{ user_id: U, program_slug: 'priv2' }], // ignored for links
        programs,
        share_links: [
          { link_user_id: U, observation: 'obs-1', field: null, allow_download: true, include_drafts: false, revoked_at: null, expires_at: null },
        ],
        observations: [{ name: 'obs-1', program_slug: 'priv1' }],
      })
    );
    expect(ctx.isAdmin).toBe(false);
    expect(ctx.isLinkAccount).toBe(true);
    expect(ctx.linkScope).toEqual({ active: true, observation: 'obs-1', field: null, allowDownload: true, includeDrafts: false });
    expect(ctx.accessibleSlugs).toEqual(['priv1']);
  });

  it('active field-scoped link: no program slugs at all', async () => {
    const ctx = await getAccessContext(
      U,
      fakeDb({
        user_profiles: [{ user_id: U, is_link_account: true }],
        programs,
        share_links: [{ link_user_id: U, observation: null, field: 'cosmos', allow_download: false, include_drafts: true, revoked_at: null, expires_at: null }],
      })
    );
    expect(ctx.linkScope?.active).toBe(true);
    expect(ctx.linkScope?.field).toBe('cosmos');
    expect(ctx.accessibleSlugs).toEqual([]);
  });

  it('revoked or expired link: dead scope, sees nothing', async () => {
    for (const link of [
      { revoked_at: '2026-01-01T00:00:00Z', expires_at: null },
      { revoked_at: null, expires_at: '2020-01-01T00:00:00Z' },
    ]) {
      invalidateAccessContext();
      const ctx = await getAccessContext(
        U,
        fakeDb({
          user_profiles: [{ user_id: U, is_link_account: true }],
          programs,
          share_links: [{ link_user_id: U, observation: 'obs-1', field: null, ...link }],
          observations: [{ name: 'obs-1', program_slug: 'priv1' }],
        })
      );
      expect(ctx.linkScope).toEqual(DEAD_LINK_SCOPE);
      expect(ctx.accessibleSlugs).toEqual([]);
    }
  });

  it('fails closed on a query error and does not cache the failure', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const calls: string[] = [];
    const broken = fakeDb({ programs, errors: { user_profiles: { message: 'boom' } } }, calls);
    const ctx = await getAccessContext(U, broken);
    expect(ctx.isAdmin).toBe(false);
    expect(ctx.isLinkAccount).toBe(true);
    expect(ctx.linkScope).toEqual(DEAD_LINK_SCOPE);
    expect(ctx.accessibleSlugs).toEqual([]);

    // Next call recomputes (the failure was not memoized).
    const calls2: string[] = [];
    const ok = await getAccessContext(U, fakeDb({ user_profiles: [], programs }, calls2));
    expect(calls2.length).toBeGreaterThan(0);
    expect([...ok.accessibleSlugs].sort()).toEqual(['pub1', 'pub2']);
  });
});

describe('getAccessContext — memo', () => {
  const fx: Fixture = { user_profiles: [{ user_id: U, is_admin: false, is_link_account: false }], user_program_access: [], programs };

  it('computes once per user within the TTL and shares in-flight work', async () => {
    const calls: string[] = [];
    const db = fakeDb(fx, calls);
    const [a, b] = await Promise.all([getAccessContext(U, db), getAccessContext(U, db)]);
    expect(a).toBe(b);
    const after = calls.length;
    await getAccessContext(U, db);
    expect(calls.length).toBe(after); // hit
    expect(after).toBe(3); // profile + grants + programs, one wave
  });

  it('recomputes after the TTL and after invalidation', async () => {
    vi.useFakeTimers();
    const calls: string[] = [];
    const db = fakeDb(fx, calls);
    await getAccessContext(U, db);
    const n = calls.length;
    vi.advanceTimersByTime(ACCESS_CONTEXT_TTL_MS + 1);
    await getAccessContext(U, db);
    expect(calls.length).toBe(2 * n);
    invalidateAccessContext(U);
    await getAccessContext(U, db);
    expect(calls.length).toBe(3 * n);
  });

  it('does not memoize link accounts beyond the in-flight computation', async () => {
    const calls: string[] = [];
    const db = fakeDb(
      {
        user_profiles: [{ user_id: U, is_admin: false, is_link_account: true, can_comment: false, can_inspect: false }],
        share_links: [{ link_user_id: U, observation: null, field: 'cosmos', allow_download: true, include_drafts: false, revoked_at: null, expires_at: null }],
        programs,
      },
      calls
    );
    const [a, b] = await Promise.all([getAccessContext(U, db), getAccessContext(U, db)]);
    expect(a).toBe(b);
    expect(a.isLinkAccount).toBe(true);
    const after = calls.length;
    // A revocation stamped elsewhere is visible on the very next call.
    const revokedDb = fakeDb({
      user_profiles: [{ user_id: U, is_admin: false, is_link_account: true, can_comment: false, can_inspect: false }],
      share_links: [{ link_user_id: U, observation: null, field: 'cosmos', allow_download: true, include_drafts: false, revoked_at: '2026-01-01T00:00:00Z', expires_at: null }],
      programs,
    });
    const c = await getAccessContext(U, revokedDb);
    expect(calls.length).toBe(after);
    expect(c.linkScope).toEqual(DEAD_LINK_SCOPE);
  });

  it('keys by user', async () => {
    const calls: string[] = [];
    const db = fakeDb(fx, calls);
    await getAccessContext(U, db);
    await getAccessContext('00000000-0000-0000-0000-00000000000b', db);
    expect(calls.length).toBe(6);
  });
});
