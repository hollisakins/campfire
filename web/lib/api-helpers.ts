import { createClient } from '@supabase/supabase-js';

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
