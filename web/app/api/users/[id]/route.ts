import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * PATCH /api/users/[id]
 *
 * Update a user profile (admin status, program access).
 * Admin only.
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: userId } = await params;
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
    const body = await request.json();
    const { is_admin, can_comment, program_access } = body;

    // Update profile fields
    const profileUpdates: Record<string, unknown> = {};
    if (typeof is_admin === 'boolean') profileUpdates.is_admin = is_admin;
    if (typeof can_comment === 'boolean') profileUpdates.can_comment = can_comment;

    if (Object.keys(profileUpdates).length > 0) {
      const { error: updateError } = await supabase
        .from('user_profiles')
        .update(profileUpdates)
        .eq('user_id', userId);

      if (updateError) {
        console.error('Error updating profile:', updateError);
        return NextResponse.json({ error: 'Failed to update profile' }, { status: 500 });
      }
    }

    // Update program access if provided
    if (Array.isArray(program_access)) {
      // Delete existing access
      const { error: deleteError } = await supabase
        .from('user_program_access')
        .delete()
        .eq('user_id', userId);

      if (deleteError) {
        console.error('Error deleting access:', deleteError);
        return NextResponse.json({ error: 'Failed to update program access' }, { status: 500 });
      }

      // Insert new access
      if (program_access.length > 0) {
        const accessRows = program_access.map((programSlug: string) => ({
          user_id: userId,
          program_slug: programSlug,
          granted_by: user.id,
        }));

        const { error: insertError } = await supabase
          .from('user_program_access')
          .insert(accessRows);

        if (insertError) {
          console.error('Error inserting access:', insertError);
          return NextResponse.json({ error: 'Failed to update program access' }, { status: 500 });
        }
      }
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update user' }, { status: 500 });
  }
}

/**
 * DELETE /api/users/[id]
 *
 * Delete a user (removes from user_profiles, not auth.users).
 * Admin only.
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: userId } = await params;
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  // Prevent self-deletion
  if (user.id === userId) {
    return NextResponse.json({ error: 'Cannot delete your own account' }, { status: 400 });
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
    // Delete program access first (foreign key)
    await supabase
      .from('user_program_access')
      .delete()
      .eq('user_id', userId);

    // Delete user profile
    const { error } = await supabase
      .from('user_profiles')
      .delete()
      .eq('user_id', userId);

    if (error) {
      console.error('Error deleting user:', error);
      return NextResponse.json({ error: 'Failed to delete user' }, { status: 500 });
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to delete user' }, { status: 500 });
  }
}
