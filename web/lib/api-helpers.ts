import { createClient } from '@supabase/supabase-js';

/**
 * Get all program IDs accessible to a user (public + explicit access)
 */
export async function getAccessiblePrograms(userId: string): Promise<number[]> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  // Get programs with explicit access
  const { data: accessData } = await supabase
    .from('user_program_access')
    .select('program_id')
    .eq('user_id', userId);

  const explicitAccessIds = (accessData || []).map((a: { program_id: number }) => a.program_id);

  // Get public programs
  const { data: publicPrograms } = await supabase
    .from('programs')
    .select('program_id')
    .eq('is_public', true);

  const publicProgramIds = (publicPrograms || []).map((p: { program_id: number }) => p.program_id);

  // Combine and deduplicate
  return [...new Set([...publicProgramIds, ...explicitAccessIds])];
}

/**
 * Check if user has any proprietary program access (granted programs, not public)
 * Used to determine if user needs to be prompted for an access code
 */
export async function checkUserProgramAccess(userId: string): Promise<{
  hasProprietaryAccess: boolean;
  grantedPrograms: number[];
  publicPrograms: number[];
}> {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  // Get programs with explicit access (proprietary)
  const { data: accessData } = await supabase
    .from('user_program_access')
    .select('program_id')
    .eq('user_id', userId);

  const grantedPrograms = (accessData || []).map((a: { program_id: number }) => a.program_id);

  // Get public programs
  const { data: publicPrograms } = await supabase
    .from('programs')
    .select('program_id')
    .eq('is_public', true);

  const publicProgramIds = (publicPrograms || []).map((p: { program_id: number }) => p.program_id);

  return {
    hasProprietaryAccess: grantedPrograms.length > 0,
    grantedPrograms,
    publicPrograms: publicProgramIds,
  };
}
