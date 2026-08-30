import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * The caller's accessible program slugs, from the SQL authority.
 *
 * accessible_program_slugs() (SECURITY DEFINER, supabase/schemas/functions.sql)
 * is what every program-gated RLS policy routes through, and it is the only
 * place that knows every principal shape: ordinary users (grants ∪ public),
 * admins (every program), link accounts (their scoped observation's program
 * only — docs/design-public-mirror.md §5.1), and revoked/expired links ('{}').
 *
 * Server actions used to hand-roll the ordinary-user union
 * (user_program_access ∪ is_public) client-side, which silently broke the
 * other principals: a link account has no grants and must not get the
 * is_public union, so the pre-filter came out empty and blanked the spectra
 * browser for exactly the rows RLS would have returned. Call this instead of
 * re-deriving the set.
 */
export async function getAccessibleProgramSlugs(supabase: SupabaseClient): Promise<string[]> {
  const { data, error } = await supabase.rpc('accessible_program_slugs');
  if (error) {
    console.error('Error fetching accessible programs:', error);
    return [];
  }
  return (data as string[] | null) ?? [];
}
