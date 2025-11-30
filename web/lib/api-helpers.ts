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

  const explicitAccessIds = (accessData || []).map((a: any) => a.program_id);

  // Get public programs
  const { data: publicPrograms } = await supabase
    .from('programs')
    .select('program_id')
    .eq('is_public', true);

  const publicProgramIds = (publicPrograms || []).map((p: any) => p.program_id);

  // Combine and deduplicate
  return [...new Set([...publicProgramIds, ...explicitAccessIds])];
}
