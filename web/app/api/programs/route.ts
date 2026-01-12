import { NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/programs
 *
 * Fetch programs.
 * - For admins: Returns all programs with access stats
 * - For non-admins (or unauthenticated): Returns basic program list (program_id, program_name)
 */
export async function GET() {
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  // If not authenticated, return basic program list
  if (!user) {
    try {
      const { data: programs, error } = await supabase
        .from('programs')
        .select('program_id, program_name')
        .order('program_name', { ascending: true });

      if (error) {
        console.error('Error fetching programs:', error);
        return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
      }

      return NextResponse.json({ programs: programs || [] });
    } catch (error) {
      console.error('Error:', error);
      return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
    }
  }

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  // If not admin, return basic program list
  if (!profile?.is_admin) {
    try {
      const { data: programs, error } = await supabase
        .from('programs')
        .select('program_id, program_name')
        .order('program_name', { ascending: true });

      if (error) {
        console.error('Error fetching programs:', error);
        return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
      }

      return NextResponse.json({ programs: programs || [] });
    } catch (error) {
      console.error('Error:', error);
      return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
    }
  }

  try {
    // Fetch all programs and stats in parallel using efficient RPC
    const [programsResult, statsResult] = await Promise.all([
      supabase
        .from('programs')
        .select('*')
        .order('program_name'),
      supabase.rpc('get_program_stats')
    ]);

    if (programsResult.error) {
      console.error('Error fetching programs:', programsResult.error);
      return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
    }

    // Build lookup maps from aggregated stats
    const statsMap = new Map<number, { object_count: number; user_access_count: number }>();
    for (const stat of statsResult.data || []) {
      statsMap.set(stat.program_id, {
        object_count: Number(stat.object_count) || 0,
        user_access_count: Number(stat.user_access_count) || 0,
      });
    }

    // Combine data
    const programsWithStats = (programsResult.data || []).map(p => ({
      ...p,
      object_count: statsMap.get(p.program_id)?.object_count || 0,
      user_access_count: statsMap.get(p.program_id)?.user_access_count || 0,
    }));

    return NextResponse.json({ programs: programsWithStats });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
  }
}
