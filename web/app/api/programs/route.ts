import { NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/programs
 *
 * Fetch all programs with their access stats.
 * Admin only.
 */
export async function GET() {
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    // Fetch all programs
    const { data: programs, error: programsError } = await supabase
      .from('programs')
      .select('*')
      .order('program_name');

    if (programsError) {
      console.error('Error fetching programs:', programsError);
      return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
    }

    // Fetch object counts per program
    const { data: objectCounts } = await supabase
      .from('objects')
      .select('program_id');

    // Count objects per program
    const countsByProgram: Record<number, number> = {};
    for (const obj of objectCounts || []) {
      countsByProgram[obj.program_id] = (countsByProgram[obj.program_id] || 0) + 1;
    }

    // Fetch user access counts per program
    const { data: accessCounts } = await supabase
      .from('user_program_access')
      .select('program_id');

    const accessByProgram: Record<number, number> = {};
    for (const access of accessCounts || []) {
      accessByProgram[access.program_id] = (accessByProgram[access.program_id] || 0) + 1;
    }

    // Combine data
    const programsWithStats = (programs || []).map(p => ({
      ...p,
      object_count: countsByProgram[p.program_id] || 0,
      user_access_count: accessByProgram[p.program_id] || 0,
    }));

    return NextResponse.json({ programs: programsWithStats });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch programs' }, { status: 500 });
  }
}
