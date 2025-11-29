import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/profile
 *
 * Fetch the current user's profile and program access.
 */
export async function GET() {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  try {
    // Fetch user profile
    const { data: profile, error: profileError } = await supabase
      .from('user_profiles')
      .select('*')
      .eq('user_id', user.id)
      .single();

    if (profileError) {
      console.error('Error fetching profile:', profileError);
      return NextResponse.json({ error: 'Failed to fetch profile' }, { status: 500 });
    }

    // Fetch user's explicit program access
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_id, granted_at')
      .eq('user_id', user.id);

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    // Fetch all programs
    const { data: allPrograms } = await supabase
      .from('programs')
      .select('*')
      .order('program_name');

    // Annotate programs with access info
    const programsWithAccess = (allPrograms || []).map(program => ({
      ...program,
      has_access: program.is_public || explicitAccessIds.includes(program.program_id),
      access_type: program.is_public
        ? 'public'
        : explicitAccessIds.includes(program.program_id)
          ? 'granted'
          : 'none',
    }));

    // Fetch code redemptions
    const { data: redemptions } = await supabase
      .from('code_redemptions')
      .select(`
        id,
        redeemed_at,
        access_codes (code, description)
      `)
      .eq('user_id', user.id)
      .order('redeemed_at', { ascending: false });

    return NextResponse.json({
      profile,
      email: user.email,
      programs: programsWithAccess,
      redemptions: redemptions || [],
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch profile' }, { status: 500 });
  }
}

/**
 * PATCH /api/profile
 *
 * Update the current user's profile.
 */
export async function PATCH(request: NextRequest) {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  try {
    const body = await request.json();
    const { full_name } = body;

    const updates: Record<string, unknown> = {};
    if (typeof full_name === 'string' && full_name.trim()) {
      updates.full_name = full_name.trim();
    }

    if (Object.keys(updates).length === 0) {
      return NextResponse.json({ error: 'No updates provided' }, { status: 400 });
    }

    const { error } = await supabase
      .from('user_profiles')
      .update(updates)
      .eq('user_id', user.id);

    if (error) {
      console.error('Error updating profile:', error);
      return NextResponse.json({ error: 'Failed to update profile' }, { status: 500 });
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update profile' }, { status: 500 });
  }
}
