// Local verification of Supabase-issued session JWTs (perf T2-B, #505).
//
// Pure module: jose + env only, no next/headers, so it runs in middleware
// (edge) and in Node route handlers / actions / RSC alike.
//
// The project signs session tokens with HS256 and the project JWT secret
// (`SUPABASE_JWT_SECRET`, the same secret lib/auth/tokens.ts already uses to
// mint deploy-CLI tokens). Verifying the signature here costs ~0.1 ms of CPU
// and replaces the `auth.getUser()` round trip to GoTrue (35–60 ms wall per
// call, measured) that every server action and route handler used to pay.
//
// What is lost relative to getUser(): server-side revocation inside the token
// lifetime (banned / deleted user, sign-out elsewhere) takes effect at the
// next refresh (jwt_expiry = 3600 s) instead of the next request. That is the
// trade-off decision D-B accepted.
//
// What is NOT lost: forgery resistance. The signature binds the token to the
// project secret; the `role` check rejects the anon and service_role keys
// (which are themselves HS256 JWTs signed with the same secret) and any other
// non-user token; `aud` is Supabase's user-token audience.

import { jwtVerify, decodeProtectedHeader, errors as joseErrors, type JWTPayload } from 'jose';

export interface SupabaseClaims extends JWTPayload {
  sub: string;
  role: string;
  email?: string;
  session_id?: string;
  is_anonymous?: boolean;
  app_metadata?: Record<string, unknown>;
  user_metadata?: Record<string, unknown>;
}

export type VerifyFailure =
  /** SUPABASE_JWT_SECRET is not configured — caller must verify over the network. */
  | 'no-secret'
  /** Token uses an algorithm this module can't check (asymmetric keys) — verify over the network. */
  | 'unsupported-alg'
  /** Signature/claims verified but the token is past `exp` (with tolerance). */
  | 'expired'
  /** Malformed, bad signature, wrong audience, or not a user token. */
  | 'invalid';

export type VerifyResult =
  | { ok: true; claims: SupabaseClaims }
  | { ok: false; reason: VerifyFailure };

export interface VerifyOptions {
  /** Override the secret (tests). Defaults to `process.env.SUPABASE_JWT_SECRET`. */
  secret?: string;
  /** Seconds of clock skew to tolerate on `exp` / `nbf`. */
  clockTolerance?: number;
}

const USER_AUDIENCE = 'authenticated';
const USER_ROLE = 'authenticated';

/**
 * Verify a Supabase session access token locally.
 *
 * Never throws. A result with `ok: false` and reason `no-secret` or
 * `unsupported-alg` means "could not check", not "bad token" — the caller
 * falls back to `auth.getUser()` for those. `expired` and `invalid` are
 * definitive.
 */
export async function verifySupabaseJwt(
  token: string,
  options: VerifyOptions = {}
): Promise<VerifyResult> {
  const secret = options.secret ?? process.env.SUPABASE_JWT_SECRET;
  if (!secret) return { ok: false, reason: 'no-secret' };

  let alg: string | undefined;
  try {
    alg = decodeProtectedHeader(token).alg;
  } catch {
    return { ok: false, reason: 'invalid' };
  }
  if (alg !== 'HS256') {
    // A project that has moved to asymmetric signing keys would land here;
    // the network fallback keeps it working until JWKS support is added.
    return { ok: false, reason: 'unsupported-alg' };
  }

  try {
    const { payload } = await jwtVerify(token, new TextEncoder().encode(secret), {
      algorithms: ['HS256'],
      audience: USER_AUDIENCE,
      clockTolerance: options.clockTolerance ?? 5,
    });
    if (typeof payload.sub !== 'string' || payload.sub.length === 0) {
      return { ok: false, reason: 'invalid' };
    }
    if (payload.role !== USER_ROLE) {
      return { ok: false, reason: 'invalid' };
    }
    return { ok: true, claims: payload as SupabaseClaims };
  } catch (err) {
    if (err instanceof joseErrors.JWTExpired) return { ok: false, reason: 'expired' };
    return { ok: false, reason: 'invalid' };
  }
}
