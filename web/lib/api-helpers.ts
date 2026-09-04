import type { SupabaseClient } from '@supabase/supabase-js';
import { createServiceClient } from '@/lib/supabase/service';
import { getAccessContext, type LinkScope } from '@/lib/auth/access-context';

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
 * Whether the user is a CAMPFIRE admin.
 *
 * Admin-ness is derived ONLY from user_profiles.is_admin — NEVER inferred
 * from program slugs. Used to gate visibility of unpublished spectra
 * (deploy_status != 'published') and their parent objects/targets
 * (has_published_spectrum = false). Fail-closed: any lookup failure → false.
 *
 * Memoized per user (lib/auth/access-context.ts): after validateAuth() /
 * getRequestPrincipal() this is a Map hit, not a query.
 */
export async function isAdminUser(userId: string): Promise<boolean> {
  return (await getAccessContext(userId)).isAdmin;
}

export type { LinkScope } from '@/lib/auth/access-context';

/**
 * The share-link scope for a link account, or null for an ordinary user.
 *
 * The API layer's mirror of the SQL helpers (is_link_account() + link_*()):
 * routes that authorize through service-role queries instead of RLS must
 * consult this, or a link session/bearer JWT walks straight past the RLS
 * narrowing (docs/design-public-mirror.md §5). An inactive (revoked or
 * expired) link resolves to active: false, which callers must treat as
 * "sees nothing" — never as "ordinary user". Fail-closed: a profile lookup
 * failure counts as a link account with no scope. Memoized (see isAdminUser).
 */
export async function getLinkScope(userId: string): Promise<LinkScope | null> {
  return (await getAccessContext(userId)).linkScope;
}

/**
 * All program slugs the user may read, per the SQL authority
 * accessible_program_slugs(): grants ∪ public for ordinary users, every
 * program for admins, only the scoped observation's program for link
 * accounts. Memoized (see isAdminUser).
 */
export async function getAccessiblePrograms(userId: string): Promise<string[]> {
  return (await getAccessContext(userId)).accessibleSlugs;
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
  // Deliberately NOT memoized: the client calls this right after redeeming
  // an access code to decide whether to keep prompting, and that answer must
  // be fresh on whichever instance answers. Two reads, one hop.
  const supabase = createServiceClient();
  const [{ data: accessData }, { data: publicPrograms }] = await Promise.all([
    supabase.from('user_program_access').select('program_slug').eq('user_id', userId),
    supabase.from('programs').select('slug').eq('is_public', true),
  ]);

  const grantedPrograms = (accessData || []).map((a: { program_slug: string }) => a.program_slug);

  const publicProgramSlugs = (publicPrograms || []).map((p: { slug: string }) => p.slug);

  return {
    hasProprietaryAccess: grantedPrograms.length > 0,
    grantedPrograms,
    publicPrograms: publicProgramSlugs,
  };
}
