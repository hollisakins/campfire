import { NextResponse } from 'next/server';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';

/**
 * GET /api/programs
 *
 * Fetch programs.
 * - For admins: Returns all programs with access stats
 * - For non-admins (or unauthenticated): Returns basic program list (slug, program_name)
 */
export async function GET() {
  const { user, supabase } = await getRequestIdentity();

  // Signed out: there is nothing to return. The anon role has no privileges on
  // the catalog tables (P-1 hardening), so a query here would be a permission
  // error rather than the empty list RLS used to yield; answer directly.
  if (!user) {
    return NextResponse.json({ programs: [] });
  }

  // If not admin, return basic program list
  if (!(await isAdminUser(user.id))) {
    try {
      const { data: programs, error } = await supabase
        .from('programs')
        .select('slug, program_name')
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
    const statsMap = new Map<string, { target_count: number; user_access_count: number }>();
    for (const stat of statsResult.data || []) {
      statsMap.set(stat.slug, {
        target_count: Number(stat.target_count) || 0,
        user_access_count: Number(stat.user_access_count) || 0,
      });
    }

    // Combine data
    const programsWithStats = (programsResult.data || []).map(p => ({
      ...p,
      target_count: statsMap.get(p.slug)?.target_count || 0,
      user_access_count: statsMap.get(p.slug)?.user_access_count || 0,
    }));

    return NextResponse.json({ programs: programsWithStats });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
  }
}
