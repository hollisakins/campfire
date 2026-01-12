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

    // Fetch user stats using batched RPC (reduces 5 queries to 1)
    const { data: statsData } = await supabase
      .rpc('get_user_profile_stats', { p_user_id: user.id });

    const userStats = statsData as { objects_inspected: number; comments_posted: number; last_activity: string | null } | null;

    // Fetch recent comments with object info (for comment history)
    // Note: comments.object_id is FK to objects.id (many-to-one), so Supabase returns single object
    const { data: recentComments, count: totalComments } = await supabase
      .from('comments')
      .select(`
        id,
        content,
        created_at,
        edited_at,
        objects (
          id,
          object_id
        )
      `, { count: 'exact' })
      .eq('user_id', user.id)
      .eq('is_deleted', false)
      .order('created_at', { ascending: false })
      .limit(5);

    // Transform comments to match CommentHistoryItem interface
    // Supabase types infer objects as array, but runtime returns single object for many-to-one
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const commentHistoryItems = (recentComments || []).map((comment: any) => {
      // Handle both array (typed) and object (runtime) cases
      const obj = Array.isArray(comment.objects) ? comment.objects[0] : comment.objects;
      return {
        id: comment.id,
        content: comment.content,
        created_at: comment.created_at,
        edited_at: comment.edited_at,
        object_db_id: obj?.id,
        object_display_id: obj?.object_id,
      };
    });

    return NextResponse.json({
      profile,
      email: user.email,
      programs: programsWithAccess,
      redemptions: redemptions || [],
      stats: {
        objects_inspected: userStats?.objects_inspected || 0,
        comments_posted: userStats?.comments_posted || 0,
        last_activity: userStats?.last_activity || null,
      },
      recent_comments: {
        items: commentHistoryItems,
        total_count: totalComments || 0,
      },
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

  // Check if user is a group account
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_group_account')
    .eq('user_id', user.id)
    .single();

  if (profile?.is_group_account) {
    return NextResponse.json(
      { error: 'Group accounts cannot edit their profile' },
      { status: 403 }
    );
  }

  try {
    const body = await request.json();
    const { full_name, preferences } = body;

    const updates: Record<string, unknown> = {};
    if (typeof full_name === 'string' && full_name.trim()) {
      updates.full_name = full_name.trim();
    }

    // Handle preferences update (merge with existing)
    if (preferences !== undefined) {
      // First get current preferences
      const { data: currentProfile } = await supabase
        .from('user_profiles')
        .select('preferences')
        .eq('user_id', user.id)
        .single();

      const currentPrefs = currentProfile?.preferences || {};

      // Deep merge preferences
      updates.preferences = {
        ...currentPrefs,
        ...preferences,
        spectrum: preferences.spectrum
          ? { ...(currentPrefs.spectrum || {}), ...preferences.spectrum }
          : currentPrefs.spectrum,
      };
    }

    if (Object.keys(updates).length === 0) {
      return NextResponse.json({ error: 'No updates provided' }, { status: 400 });
    }

    const { data: updatedProfile, error } = await supabase
      .from('user_profiles')
      .update(updates)
      .eq('user_id', user.id)
      .select('preferences')
      .single();

    if (error) {
      console.error('Error updating profile:', error);
      return NextResponse.json({ error: 'Failed to update profile' }, { status: 500 });
    }

    return NextResponse.json({ success: true, preferences: updatedProfile?.preferences });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update profile' }, { status: 500 });
  }
}
