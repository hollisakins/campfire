import { NextResponse } from 'next/server';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { paginateQuery } from '@/lib/supabase/paginate';

/**
 * GET /api/users
 *
 * Fetch all users with their profiles and program access.
 * Admin only.
 */
export async function GET() {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  if (!(await isAdminUser(user.id))) {
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

    // Fetch all program access (paginate to avoid PostgREST max-rows truncation)
    const { data: access, error: accessError } = await paginateQuery(
      () => supabase.from('user_program_access').select('*')
        .order('user_id')
        .order('program_slug'),
    );

    if (accessError) {
      console.error('Error fetching access:', accessError);
      return NextResponse.json({ error: 'Failed to fetch access' }, { status: 500 });
    }

    // Fetch all programs for reference
    const { data: programs, error: programsError } = await supabase
      .from('programs')
      .select('slug, program_name, is_public');

    if (programsError) {
      console.error('Error fetching programs:', programsError);
    }

    // Group access by user_id
    const accessByUser: Record<string, string[]> = {};
    for (const a of access || []) {
      if (!accessByUser[a.user_id]) {
        accessByUser[a.user_id] = [];
      }
      accessByUser[a.user_id].push(a.program_slug);
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
