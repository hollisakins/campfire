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
