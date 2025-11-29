import { NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/users
 *
 * Fetch all users with their profiles and program access.
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
    // Fetch all user profiles
    const { data: users, error: usersError } = await supabase
      .from('user_profiles')
      .select('*')
      .order('created_at', { ascending: false });

    if (usersError) {
      console.error('Error fetching users:', usersError);
      return NextResponse.json({ error: 'Failed to fetch users' }, { status: 500 });
    }

    // Fetch all program access
    const { data: access, error: accessError } = await supabase
      .from('user_program_access')
      .select('*');

    if (accessError) {
      console.error('Error fetching access:', accessError);
      return NextResponse.json({ error: 'Failed to fetch access' }, { status: 500 });
    }

    // Fetch all programs for reference
    const { data: programs, error: programsError } = await supabase
      .from('programs')
      .select('program_id, program_name');

    if (programsError) {
      console.error('Error fetching programs:', programsError);
    }

    // Group access by user_id
    const accessByUser: Record<string, number[]> = {};
    for (const a of access || []) {
      if (!accessByUser[a.user_id]) {
        accessByUser[a.user_id] = [];
      }
      accessByUser[a.user_id].push(a.program_id);
    }

    // Combine data
    const usersWithAccess = (users || []).map(u => ({
      ...u,
      program_access: accessByUser[u.user_id] || [],
    }));

    return NextResponse.json({
      users: usersWithAccess,
      programs: programs || [],
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch users' }, { status: 500 });
  }
}
