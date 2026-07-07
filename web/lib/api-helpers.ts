import { createClient, type SupabaseClient } from '@supabase/supabase-js';

/** Parse a comma-separated query-string value into a non-empty string list, or null. */
export function parseCSV(value: string | null): string[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => s.trim()).filter(s => s.length > 0);
  return items.length > 0 ? items : null;
}

/** Parse a comma-separated query-string value into a non-empty int list, or null. */
export function parseIntCSV(value: string | null): number[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
  return items.length > 0 ? items : null;
}

/**
 * Resolve a list of object-list slugs to DB IDs. Returns null if no slugs were
 * supplied (meaning: don't filter by list); returns [] if none match.
 */
export async function resolveListIds(
  supabase: SupabaseClient,
  slugs: string[] | null,
): Promise<number[] | null> {
  if (!slugs || slugs.length === 0) return null;
  const { data } = await supabase
    .from('object_lists')
    .select('id')
    .in('slug', slugs);
  return (data ?? []).map((r: { id: number }) => r.id);
}

/**
 * Check whether a user is a CAMPFIRE admin.
 *
 * Admin-ness is derived ONLY from user_profiles.is_admin via the service-role
 * client (mirrors the check in /api/v1/deploy/presign) — it is NEVER inferred
 * from program slugs. Used to gate visibility of unpublished spectra
 * (deploy_status != 'published') and their parent objects/targets
 * (has_published_spectrum = false). Fail-closed: any lookup failure → false.
 */
export async function isAdminUser(userId: string): Promise<boolean> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', userId)
    .single();

  return profile?.is_admin === true;
}

/**
 * Get all program slugs accessible to a user (public + explicit access)
 */
export async function getAccessiblePrograms(userId: string): Promise<string[]> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  // Get programs with explicit access
  const { data: accessData } = await supabase
    .from('user_program_access')
    .select('program_slug')
    .eq('user_id', userId);

  const explicitAccessSlugs = (accessData || []).map((a: { program_slug: string }) => a.program_slug);

  // Get public programs
  const { data: publicPrograms } = await supabase
    .from('programs')
    .select('slug')
    .eq('is_public', true);

  const publicProgramSlugs = (publicPrograms || []).map((p: { slug: string }) => p.slug);

  // Combine and deduplicate
  return [...new Set([...publicProgramSlugs, ...explicitAccessSlugs])];
}

// ---------------------------------------------------------------------------
// Sync auth preamble cache
// ---------------------------------------------------------------------------
// A full `campfire sync` fans out hundreds of /sync/* requests that each
// re-derive the same invariant for the same user: the accessible-program list
// (2 Supabase queries) and the admin flag (1 query). Recomputing it per page is
// pure fixed tax. Memoize per user for a short window so warm serverless
// instances collapse the repeats to a single lookup. Fail-open: a miss just
// falls back to the uncached query. The TTL bounds staleness if a user's access
// changes mid-sync (a rare event that a subsequent sync picks up anyway).
//
// Scoped to the sync routes on purpose -- other call sites keep the uncached
// helpers so their behavior is unchanged.
const SYNC_AUTH_TTL_MS = 60_000;
const SYNC_AUTH_CACHE_MAX = 1000;

type CacheEntry<T> = { value: T; expiresAt: number };

function cacheGet<T>(cache: Map<string, CacheEntry<T>>, key: string, now: number): T | undefined {
  const hit = cache.get(key);
  if (hit && hit.expiresAt > now) return hit.value;
  if (hit) cache.delete(key);
  return undefined;
}

function cacheSet<T>(cache: Map<string, CacheEntry<T>>, key: string, value: T, now: number): void {
  // Opportunistically drop expired entries before growing an unbounded map on a
  // long-lived warm instance.
  if (cache.size >= SYNC_AUTH_CACHE_MAX) {
    for (const [k, v] of cache) {
      if (v.expiresAt <= now) cache.delete(k);
    }
  }
  cache.set(key, { value, expiresAt: now + SYNC_AUTH_TTL_MS });
}

const accessibleProgramsCache = new Map<string, CacheEntry<string[]>>();
const isAdminCache = new Map<string, CacheEntry<boolean>>();

/**
 * getAccessiblePrograms with a short-TTL per-instance cache. Use ONLY on the
 * high-fanout /sync/* routes; see the cache note above.
 */
export async function getAccessibleProgramsCached(userId: string): Promise<string[]> {
  const now = Date.now();
  const cached = cacheGet(accessibleProgramsCache, userId, now);
  if (cached !== undefined) return cached;
  const value = await getAccessiblePrograms(userId);
  cacheSet(accessibleProgramsCache, userId, value, now);
  return value;
}

/**
 * isAdminUser with a short-TTL per-instance cache. Use ONLY on the /sync/*
 * routes; see the cache note above.
 */
export async function isAdminUserCached(userId: string): Promise<boolean> {
  const now = Date.now();
  const cached = cacheGet(isAdminCache, userId, now);
  if (cached !== undefined) return cached;
  const value = await isAdminUser(userId);
  cacheSet(isAdminCache, userId, value, now);
  return value;
}

/**
 * Check if user has any proprietary program access (granted programs, not public)
 * Used to determine if user needs to be prompted for an access code
 */
export async function checkUserProgramAccess(userId: string): Promise<{
  hasProprietaryAccess: boolean;
  grantedPrograms: string[];
  publicPrograms: string[];
}> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  // Get programs with explicit access (proprietary)
  const { data: accessData } = await supabase
    .from('user_program_access')
    .select('program_slug')
    .eq('user_id', userId);

  const grantedPrograms = (accessData || []).map((a: { program_slug: string }) => a.program_slug);

  // Get public programs
  const { data: publicPrograms } = await supabase
    .from('programs')
    .select('slug')
    .eq('is_public', true);

  const publicProgramSlugs = (publicPrograms || []).map((p: { slug: string }) => p.slug);

  return {
    hasProprietaryAccess: grantedPrograms.length > 0,
    grantedPrograms,
    publicPrograms: publicProgramSlugs,
  };
}
