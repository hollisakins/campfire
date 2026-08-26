import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

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
    const { is_admin, can_comment, can_inspect, program_access } = body;

    // Use service client for admin mutations on other users' data
    const serviceClient = createServiceClient();

    // Group accounts are never admins: a shared admin credential is
    // unattributable. The create endpoint refuses it; refuse promotion here
    // too so the shield toggle can't grant it after the fact.
    if (is_admin === true) {
      const { data: target } = await serviceClient
        .from('user_profiles')
        .select('is_group_account')
        .eq('user_id', userId)
        .single();

      if (target?.is_group_account) {
        return NextResponse.json(
          { error: 'Group accounts cannot be given admin privileges' },
          { status: 400 }
        );
      }
    }

    // Update profile fields
    const profileUpdates: Record<string, unknown> = {};
    if (typeof is_admin === 'boolean') profileUpdates.is_admin = is_admin;
    if (typeof can_comment === 'boolean') profileUpdates.can_comment = can_comment;
    if (typeof can_inspect === 'boolean') profileUpdates.can_inspect = can_inspect;

    if (Object.keys(profileUpdates).length > 0) {
      const { error: updateError } = await serviceClient
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
      const { error: deleteError } = await serviceClient
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

        const { error: insertError } = await serviceClient
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
 * Delete a user. For regular users this removes the profile and program
 * access but leaves the auth.users principal (historical behavior — invited
 * users own their auth identity). For group accounts the auth principal is
 * deleted too: the account exists only as a shared credential minted by an
 * admin, and leaving it behind would let everyone holding the password keep
 * signing in as a profileless (and therefore unrestricted-by-group-rules)
 * user. user_profiles and user_program_access both cascade from auth.users.
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
    // Use service client for admin mutations on other users' data
    const serviceClient = createServiceClient();

    const { data: target } = await serviceClient
      .from('user_profiles')
      .select('is_group_account')
      .eq('user_id', userId)
      .single();

    if (target?.is_group_account) {
      // Deleting the auth principal cascades away the profile and program
      // access, and stops the shared credentials from authenticating.
      const { error } = await serviceClient.auth.admin.deleteUser(userId);

      if (error) {
        console.error('Error deleting group account:', error);
        return NextResponse.json({ error: 'Failed to delete group account' }, { status: 500 });
      }

      return NextResponse.json({ success: true });
    }

    // Delete program access first (foreign key)
    await serviceClient
      .from('user_program_access')
      .delete()
      .eq('user_id', userId);

    // Delete user profile
    const { error } = await serviceClient
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
