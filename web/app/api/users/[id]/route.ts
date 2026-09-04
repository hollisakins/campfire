import { NextRequest, NextResponse } from 'next/server';
import { invalidateAccessContext } from '@/lib/auth/access-context';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/server';

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
  const { user } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  if (!(await isAdminUser(user.id))) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    const body = await request.json();
    const { is_admin, can_comment, can_inspect, program_access } = body;

    // Use service client for admin mutations on other users' data
    const serviceClient = createServiceClient();

    // Group accounts are never admins: a shared admin credential is
    // unattributable. The create endpoint refuses it; refuse promotion here
    // too so the shield toggle can't grant it after the fact. Fail closed:
    // this check is the sole enforcement of the invariant (there is no DB
    // constraint), so a failed lookup must block the promotion, not allow it.
    if (is_admin === true) {
      const { data: target, error: targetError } = await serviceClient
        .from('user_profiles')
        .select('is_group_account')
        .eq('user_id', userId)
        .maybeSingle();

      if (targetError) {
        console.error('Error checking target profile:', targetError);
        return NextResponse.json(
          { error: 'Failed to verify the target account — admin status not changed' },
          { status: 500 }
        );
      }
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

    // Flags or grants changed: forget the memoized access set on this
    // instance (#505). Other instances converge within the memo TTL.
    invalidateAccessContext(userId);

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
 * users own their auth identity). For group accounts the shared credential
 * must actually stop working, so the principal is additionally banned. It is
 * deliberately NOT hard-deleted: download_log, code_redemptions, and
 * password_reset_log cascade from auth.users, so a hard delete would
 * silently destroy the audit trail of what the shared login accessed (and
 * for accounts with comments it would fail outright on the NO ACTION FKs).
 * The banned, profileless principal is the tombstone — same approach as
 * revokeShareLink in lib/actions/share-links.ts.
 * Admin only.
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: userId } = await params;
  const { user } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  // Prevent self-deletion
  if (user.id === userId) {
    return NextResponse.json({ error: 'Cannot delete your own account' }, { status: 400 });
  }

  if (!(await isAdminUser(user.id))) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    // Use service client for admin mutations on other users' data
    const serviceClient = createServiceClient();

    // Fail closed: if we cannot tell whether this is a group account, do not
    // delete anything — a fall-through here would remove the profile while
    // leaving a shared credential authenticating with no visible trace.
    const { data: target, error: targetError } = await serviceClient
      .from('user_profiles')
      .select('is_group_account')
      .eq('user_id', userId)
      .maybeSingle();

    if (targetError) {
      console.error('Error checking target profile:', targetError);
      return NextResponse.json(
        { error: 'Failed to verify the target account — nothing was deleted' },
        { status: 500 }
      );
    }

    if (target?.is_group_account) {
      // Ban the principal BEFORE the row deletes below, so the shared
      // credentials stop authenticating no matter what happens after. No
      // auth.admin.deleteUser here — see the handler doc comment.
      const { error: banError } = await serviceClient.auth.admin.updateUserById(userId, {
        // Effectively forever (100 years); GoTrue has no "permanent" literal.
        ban_duration: '876000h',
      });

      if (banError) {
        console.error('Error banning group account:', banError);
        return NextResponse.json({ error: 'Failed to disable group account' }, { status: 500 });
      }
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
