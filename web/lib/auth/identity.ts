// Request identity, resolved once (perf T2-B, #505; decision D-B).
//
// Every server action, route handler and server component that needs to
// know who is calling goes through getRequestIdentity(). It reads the
// session from the cookie jar (a local parse — supabase-js only talks to
// GoTrue from here when the token is within 90 s of expiry and needs a
// refresh, which middleware.ts normally handles first) and verifies the
// access token's signature locally (lib/auth/supabase-jwt.ts). The result is
// request-scoped via React cache(), so any number of callers within one RSC
// render or action invocation share a single resolution.
//
// Network fallback: when the token cannot be verified locally (secret not
// configured, unexpected algorithm, or a signature that does not match — a
// misconfigured secret looks exactly like forgery from here) the identity is
// resolved with auth.getUser() exactly as before this change, so a bad
// deployment degrades to the pre-T2-B cost, never to a broken login. A
// definitively expired token is anonymous without any network.
//
// Authorization is NOT done here: RLS (through the returned cookie-scoped
// client) and getAccessContext() (memoized admin / link / program set) do
// that. Never derive privileges from anything on RequestUser except `id`.

import { cache } from 'react';
import type { SupabaseClient, User } from '@supabase/supabase-js';
import { createClient } from '@/lib/supabase/server';
import { verifySupabaseJwt, type SupabaseClaims, type VerifyFailure } from './supabase-jwt';
import { getAccessContext, type AccessContext } from './access-context';

export interface RequestUser {
  id: string;
  email: string | null;
  /** GoTrue session id from the token; null on the network-fallback path. */
  sessionId: string | null;
  isAnonymous: boolean;
  appMetadata: Record<string, unknown>;
  userMetadata: Record<string, unknown>;
}

export interface RequestIdentity {
  /** null when the request carries no valid session. */
  user: RequestUser | null;
  /** Cookie-scoped client (RLS applies). Same instance for every caller in the request. */
  supabase: SupabaseClient;
}

function userFromClaims(claims: SupabaseClaims): RequestUser {
  return {
    id: claims.sub,
    email: typeof claims.email === 'string' && claims.email.length > 0 ? claims.email : null,
    sessionId: typeof claims.session_id === 'string' ? claims.session_id : null,
    isAnonymous: claims.is_anonymous === true,
    appMetadata: claims.app_metadata ?? {},
    userMetadata: claims.user_metadata ?? {},
  };
}

function userFromGoTrue(user: User): RequestUser {
  return {
    id: user.id,
    email: user.email && user.email.length > 0 ? user.email : null,
    sessionId: null,
    isAnonymous: user.is_anonymous === true,
    appMetadata: user.app_metadata ?? {},
    userMetadata: user.user_metadata ?? {},
  };
}

const warned = new Set<VerifyFailure>();
function warnFallback(reason: VerifyFailure): void {
  if (warned.has(reason)) return;
  warned.add(reason);
  const why =
    reason === 'no-secret'
      ? 'SUPABASE_JWT_SECRET is not set'
      : reason === 'unsupported-alg'
        ? 'the session token is not HS256 (asymmetric signing keys?)'
        : 'the session token signature did not verify against SUPABASE_JWT_SECRET';
  console.warn(
    `[identity] Falling back to auth.getUser() because ${why}. ` +
      'Identity still works but costs a GoTrue round trip per request (#505).'
  );
}

/**
 * Resolve the caller's identity for the current request. Request-scoped;
 * cheap to call repeatedly.
 */
export const getRequestIdentity = cache(async (): Promise<RequestIdentity> => {
  const supabase = await createClient();

  // Local cookie parse. Refreshes through GoTrue only when the token is
  // inside the expiry margin (middleware.ts has usually done this already);
  // the `user` object on this session is intentionally never read — it is
  // unverified until the signature check below.
  const { data: { session } } = await supabase.auth.getSession();
  const accessToken = session?.access_token;
  if (!accessToken) return { user: null, supabase };

  const verified = await verifySupabaseJwt(accessToken);
  if (verified.ok) return { user: userFromClaims(verified.claims), supabase };
  if (verified.reason === 'expired') return { user: null, supabase };

  warnFallback(verified.reason);
  const { data: { user } } = await supabase.auth.getUser();
  return { user: user ? userFromGoTrue(user) : null, supabase };
});

/** Identity plus the memoized access context, for callers that need both. */
export interface RequestPrincipal extends RequestIdentity {
  user: RequestUser;
  access: AccessContext;
}

/**
 * Identity or throw. For server actions whose contract is to throw on an
 * anonymous caller (the message matches the pre-existing sites).
 */
export async function requireUser(): Promise<RequestIdentity & { user: RequestUser }> {
  const identity = await getRequestIdentity();
  if (!identity.user) throw new Error('Not authenticated');
  return { ...identity, user: identity.user };
}

/**
 * Identity + access context, or null when anonymous. One call replaces the
 * getUser() + user_profiles pair most route handlers used to open with.
 */
export async function getRequestPrincipal(): Promise<RequestPrincipal | null> {
  const identity = await getRequestIdentity();
  if (!identity.user) return null;
  const access = await getAccessContext(identity.user.id);
  return { ...identity, user: identity.user, access };
}

/**
 * Admin identity or throw ('Not authenticated' / 'Admin access required',
 * matching the messages the per-file requireAdmin() helpers used).
 */
export async function requireAdmin(): Promise<RequestPrincipal> {
  const principal = await getRequestPrincipal();
  if (!principal) throw new Error('Not authenticated');
  if (!principal.access.isAdmin) throw new Error('Admin access required');
  return principal;
}
