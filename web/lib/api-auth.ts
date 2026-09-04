import { NextRequest } from 'next/server';
import crypto from 'crypto';
import { validateAccessToken } from '@/lib/auth/tokens';
import { getAccessContext, type AccessContext } from '@/lib/auth/access-context';
import { createServiceClient } from '@/lib/supabase/service';

/**
 * Hash an API key using SHA-256
 * This is a one-way hash for secure storage
 */
export function hashApiKey(apiKey: string): string {
  return crypto.createHash('sha256').update(apiKey).digest('hex');
}

/** A bearer-authenticated /api/v1 caller. */
export interface ApiPrincipal {
  userId: string;
  method: 'api_key' | 'access_token';
  /** Memoized (lib/auth/access-context.ts): admin flag, program set, link scope. */
  access: AccessContext;
}

/**
 * Authenticate a bearer request (API key `sk_*` or campfire JWT) and resolve
 * its access context in the same step.
 *
 * Preamble cost (perf T2-B, #505): the credential check is the one
 * unavoidable hop for an API key (validate_api_key — kept uncached so key
 * revocation is immediate) and zero hops for a JWT (verified locally with
 * jose); the access context is a memo hit for every request after the first
 * in a 60 s window. Routes that then call isAdminUser() /
 * getAccessiblePrograms() / getLinkScope() are reading the same memo.
 */
export async function authenticateApiRequest(request: NextRequest): Promise<ApiPrincipal | null> {
  const authHeader = request.headers.get('authorization');
  if (!authHeader || !authHeader.startsWith('Bearer ')) return null;

  const token = authHeader.slice('Bearer '.length).trim();
  if (!token) return null;

  const method: ApiPrincipal['method'] = token.startsWith('sk_') ? 'api_key' : 'access_token';
  const userId =
    method === 'api_key'
      ? await validateApiKeyToken(token)
      : await validateAccessToken(token);
  if (!userId) return null;

  const access = await getAccessContext(userId);

  // Share links (docs/design-public-mirror.md §5.5): the programmatic API is
  // closed to link accounts, full stop. Every bearer-authorized /api/v1 route
  // authorizes at program grain (accessibleSlugs), which cannot express
  // "one observation" or the download opt-out — so a link visitor lifting the
  // JWT out of their own cookie jar (it is not httpOnly) or presenting an old
  // sk_ key must resolve to no credential at all, not to program-wide access.
  // The shared view itself never goes through here: browser pages use the
  // cookie session, and the cookie-capable cutout routes carry their own
  // link-scope checks. An unreadable profile also lands here (fail-closed).
  if (access.isLinkAccount) return null;

  return { userId, method, access };
}

/**
 * Validate authentication from request headers.
 * Supports both API keys (sk_*) and JWT access tokens.
 * Returns user_id if valid, null otherwise.
 *
 * Thin wrapper over authenticateApiRequest() for the routes that only need
 * the id; the access context it resolved stays memoized for the helpers in
 * lib/api-helpers.ts, so calling those afterwards costs no extra query.
 */
export async function validateAuth(request: NextRequest): Promise<string | null> {
  return (await authenticateApiRequest(request))?.userId ?? null;
}

// `last_used_at` is informational (shown on the API keys page). Writing it on
// every request put an UPDATE on the hot path of a sync fan-out that makes
// hundreds of calls with the same key; once per key per window per instance
// is plenty (#505).
const LAST_USED_TOUCH_INTERVAL_MS = 5 * 60_000;
const LAST_USED_MAX_TRACKED = 1000;
const lastUsedTouched = new Map<string, number>();

function shouldTouchLastUsed(keyHash: string, now: number): boolean {
  const last = lastUsedTouched.get(keyHash);
  if (last !== undefined && now - last < LAST_USED_TOUCH_INTERVAL_MS) return false;
  if (lastUsedTouched.size >= LAST_USED_MAX_TRACKED) {
    for (const [k, t] of lastUsedTouched) {
      if (now - t >= LAST_USED_TOUCH_INTERVAL_MS) lastUsedTouched.delete(k);
    }
  }
  lastUsedTouched.set(keyHash, now);
  return true;
}

/**
 * Validate an API key token string; returns the owning user_id or null.
 */
async function validateApiKeyToken(apiKey: string): Promise<string | null> {
  const supabase = createServiceClient();
  const keyHash = hashApiKey(apiKey);

  const { data, error } = await supabase.rpc('validate_api_key', {
    key_hash_input: keyHash,
  });

  if (error || !data || data.length === 0) {
    return null;
  }

  const result = data[0];

  if (!result.is_valid) {
    return null;
  }

  if (shouldTouchLastUsed(keyHash, Date.now())) {
    // Fire-and-forget; never on the response path.
    supabase.rpc('update_api_key_last_used', { key_hash_input: keyHash }).then(
      () => {},
      (err) => {
        console.error('Failed to update API key last_used_at:', err);
      }
    );
  }

  return result.user_id;
}

/**
 * Generate a new API key
 * Format: sk_live_<32 random hex chars>
 */
export function generateApiKey(): { key: string; prefix: string; hash: string } {
  const randomBytes = crypto.randomBytes(32);
  const randomHex = randomBytes.toString('hex');
  const key = `sk_live_${randomHex}`;
  const prefix = `sk_live_${randomHex.substring(0, 8)}...`;
  const hash = hashApiKey(key);

  return { key, prefix, hash };
}
