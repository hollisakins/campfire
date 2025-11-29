import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * DELETE /api/admin/invites/[id]
 *
 * Cancel a pending invite (admin only)
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const inviteId = parseInt(id, 10);

  if (isNaN(inviteId)) {
    return NextResponse.json(
      { error: 'Invalid invite ID' },
      { status: 400 }
    );
  }

  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  // Check admin permission
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return NextResponse.json(
      { error: 'Admin access required' },
      { status: 403 }
    );
  }

  try {
    // Check if invite exists and is not yet accepted
    const { data: invite, error: fetchError } = await supabase
      .from('pending_invites')
      .select('id, email, accepted_at')
      .eq('id', inviteId)
      .single();

    if (fetchError || !invite) {
      return NextResponse.json(
        { error: 'Invite not found' },
        { status: 404 }
      );
    }

    if (invite.accepted_at) {
      return NextResponse.json(
        { error: 'Cannot cancel an already accepted invite' },
        { status: 400 }
      );
    }

    // Delete the invite
    const { error: deleteError } = await supabase
      .from('pending_invites')
      .delete()
      .eq('id', inviteId);

    if (deleteError) {
      console.error('Error deleting invite:', deleteError);
      return NextResponse.json(
        { error: 'Failed to cancel invite' },
        { status: 500 }
      );
    }

    return NextResponse.json({
      success: true,
      message: `Invite for ${invite.email} has been cancelled`,
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json(
      { error: 'Failed to cancel invite' },
      { status: 500 }
    );
  }
}
