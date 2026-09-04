// Per-user access context with a short-TTL memo (perf T2-B, #505).
//
// One object answers every "what may this user see" question the server
// asks — admin flag, share-link scope, accessible program slugs — computed
// once per user per instance per TTL instead of once per server action /
// route handler. It generalizes the memo that #412 added for the /sync/*
// fan-out to every server entry point.
//
// SEMANTICS mirror the SQL authority `accessible_program_slugs()`
// (supabase/schemas/functions.sql), which every program-gated RLS policy
// routes through, for every principal shape:
//   - ordinary user: grants (user_program_access) ∪ public programs
//   - admin: every program (the single point where operators inherit access)
//   - link account: ONLY its scoped observation's program; no is_public
//     union, no grants (docs/design-public-mirror.md §5.1); a field-scoped
//     link gets '{}' (NIRCam narrows on the field axis instead)
//   - revoked / expired link, or an unreadable profile: nothing (fail-closed)
// Keep the two in lock-step; lib/auth/access-context.test.ts pins them.
//
// STALENESS. A grant, revocation, promotion or link revocation can lag by up
// to ACCESS_CONTEXT_TTL_MS on a warm instance (the same window the sync memo
// already accepted). Where the caller queries through the user's own
// cookie session, RLS still enforces the real answer at the database — a
// stale, wider slug list cannot leak rows, a stale narrower one only hides
// newly granted data for the window. The bearer /api/v1 routes authorize with
// the service role and rely on this set, so the window is real there.
// Mutation paths call invalidateAccessContext() so the instance that handled
// the change answers freshly at once.

import type { SupabaseClient } from '@supabase/supabase-js';
import { createServiceClient } from '@/lib/supabase/service';

/**
 * The share-link scope for a link account, or null for an ordinary user.
 * An inactive (revoked or expired) link resolves to active: false, which
 * callers must treat as "sees nothing" — never as "ordinary user".
 */
export interface LinkScope {
  active: boolean;
  observation: string | null;
  field: string | null;
  allowDownload: boolean;
  includeDrafts: boolean;
}

export const DEAD_LINK_SCOPE: LinkScope = Object.freeze({
  active: false, observation: null, field: null, allowDownload: false, includeDrafts: false,
});

export interface AccessContext {
  userId: string;
  /** user_profiles.is_admin — NEVER inferred from program slugs. */
  isAdmin: boolean;
  canComment: boolean;
  canInspect: boolean;
  /** True when the account was minted by a share link (or its profile is unreadable). */
  isLinkAccount: boolean;
  /** null for ordinary users; DEAD_LINK_SCOPE for a revoked/expired link. */
  linkScope: LinkScope | null;
  /** Program slugs this user may read, per accessible_program_slugs(). */
  accessibleSlugs: string[];
  /** Explicit grants only (user_program_access), for the access-code prompt. */
  grantedSlugs: string[];
  /** Whether a user_profiles row exists (invited accounts may not have one yet). */
  hasProfile: boolean;
}

export const ACCESS_CONTEXT_TTL_MS = 60_000;
const ACCESS_CONTEXT_CACHE_MAX = 1000;

type Entry = { promise: Promise<AccessContext>; expiresAt: number };
const cache = new Map<string, Entry>();

/** Fail-closed context: sees nothing, may do nothing. Never cached. */
function deadContext(userId: string): AccessContext {
  return {
    userId,
    isAdmin: false,
    canComment: false,
    canInspect: false,
    isLinkAccount: true,
    linkScope: DEAD_LINK_SCOPE,
    accessibleSlugs: [],
    grantedSlugs: [],
    hasProfile: false,
  };
}

async function computeAccessContext(userId: string, db: SupabaseClient): Promise<AccessContext> {
  // One wall-clock hop: the three independent reads in parallel.
  const [profileRes, grantsRes, programsRes] = await Promise.all([
    db
      .from('user_profiles')
      .select('is_admin, is_link_account, can_comment, can_inspect')
      .eq('user_id', userId)
      .maybeSingle(),
    db.from('user_program_access').select('program_slug').eq('user_id', userId),
    db.from('programs').select('slug, is_public'),
  ]);

  if (profileRes.error || grantsRes.error || programsRes.error) {
    throw profileRes.error ?? grantsRes.error ?? programsRes.error;
  }

  // maybeSingle: an authenticated user with NO profile row is a real,
  // pre-existing state (invited accounts before /welcome creates it) and must
  // resolve as an ordinary user with no grants — not as a dead link.
  const profile = profileRes.data as
    | { is_admin: boolean | null; is_link_account: boolean | null; can_comment: boolean | null; can_inspect: boolean | null }
    | null;
  const grantedSlugs = (grantsRes.data ?? []).map((r: { program_slug: string }) => r.program_slug);
  const programs = (programsRes.data ?? []) as { slug: string; is_public: boolean | null }[];

  const base = {
    userId,
    isAdmin: profile?.is_admin === true,
    canComment: profile?.can_comment === true,
    canInspect: profile?.can_inspect === true,
    hasProfile: profile !== null,
    grantedSlugs,
  };

  if (profile?.is_link_account === true) {
    const { data: link, error: linkErr } = await db
      .from('share_links')
      .select('observation, field, allow_download, include_drafts, revoked_at, expires_at')
      .eq('link_user_id', userId)
      .maybeSingle();
    if (linkErr) throw linkErr;

    const active =
      !!link &&
      link.revoked_at === null &&
      (link.expires_at === null || new Date(link.expires_at) > new Date());
    if (!active) {
      return { ...base, isAdmin: false, isLinkAccount: true, linkScope: DEAD_LINK_SCOPE, accessibleSlugs: [] };
    }

    const linkScope: LinkScope = {
      active: true,
      observation: link.observation ?? null,
      field: link.field ?? null,
      allowDownload: link.allow_download === true,
      includeDrafts: link.include_drafts === true,
    };

    let accessibleSlugs: string[] = [];
    if (linkScope.observation) {
      const { data: obs, error: obsErr } = await db
        .from('observations')
        .select('program_slug')
        .eq('name', linkScope.observation)
        .maybeSingle();
      if (obsErr) throw obsErr;
      if (obs?.program_slug) accessibleSlugs = [obs.program_slug];
    }
    // A link account is never an admin for authorization purposes, whatever
    // the profile row says — the SQL side has no admin union for links either.
    return { ...base, isAdmin: false, isLinkAccount: true, linkScope, accessibleSlugs };
  }

  const accessibleSlugs = base.isAdmin
    ? programs.map(p => p.slug)
    : [...new Set([...programs.filter(p => p.is_public === true).map(p => p.slug), ...grantedSlugs])];

  return { ...base, isLinkAccount: false, linkScope: null, accessibleSlugs };
}

function sweep(now: number): void {
  if (cache.size < ACCESS_CONTEXT_CACHE_MAX) return;
  for (const [k, v] of cache) {
    if (v.expiresAt <= now) cache.delete(k);
  }
}

/**
 * The user's access context, memoized per instance for ACCESS_CONTEXT_TTL_MS.
 * Concurrent callers for the same user share one in-flight computation. A
 * failed computation is never cached and resolves to the fail-closed dead
 * context, so a transient error costs one request, not a minute of lockout.
 *
 * `db` is injectable for tests; production callers omit it.
 */
export function getAccessContext(userId: string, db?: SupabaseClient): Promise<AccessContext> {
  const now = Date.now();
  const hit = cache.get(userId);
  if (hit && hit.expiresAt > now) return hit.promise;

  sweep(now);
  const promise = computeAccessContext(userId, db ?? createServiceClient()).catch(err => {
    console.error('Failed to resolve access context; failing closed:', err);
    cache.delete(userId);
    return deadContext(userId);
  });
  cache.set(userId, { promise, expiresAt: now + ACCESS_CONTEXT_TTL_MS });
  return promise;
}

/**
 * Drop the memoized context for one user (or everyone) on this instance.
 * Call after a grant / revocation / promotion / link revocation so the
 * instance that handled the mutation answers freshly on the next request.
 */
export function invalidateAccessContext(userId?: string): void {
  if (userId === undefined) cache.clear();
  else cache.delete(userId);
}
