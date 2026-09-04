import { NextRequest, NextResponse } from 'next/server';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/server';
import { findAuthUserByEmail } from '@/lib/supabase/paginate';

/**
 * POST /api/admin/invites/[id]/resend
 *
 * Resend an invite email for a pending invite (admin only)
 */
export async function POST(
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

  const { user } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  // Use service client for admin operations
  const serviceClient = createServiceClient();

  // Check admin permission
  if (!(await isAdminUser(user.id))) {
    return NextResponse.json(
      { error: 'Admin access required' },
      { status: 403 }
    );
  }

  try {
    // Check if invite exists and is not yet accepted
    const { data: invite, error: fetchError } = await serviceClient
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
        { error: 'This invite has already been accepted' },
        { status: 400 }
      );
    }

    // Check if user already exists in auth.users
    const { user: existingUser, error: lookupError } = await findAuthUserByEmail(
      serviceClient,
      invite.email
    );

    if (lookupError) {
      return NextResponse.json(
        { error: `Failed to check existing users: ${lookupError.message}` },
        { status: 500 }
      );
    }

    if (existingUser) {
      // Check if user has completed profile setup
      const { data: userProfile } = await serviceClient
        .from('user_profiles')
        .select('user_id')
        .eq('user_id', existingUser.id)
        .single();

      if (userProfile) {
        return NextResponse.json(
          { error: 'User has already completed registration. They can log in directly.' },
          { status: 400 }
        );
      }

      // User exists but hasn't completed profile - delete and resend
      const { error: deleteError } = await serviceClient.auth.admin.deleteUser(
        existingUser.id
      );

      if (deleteError) {
        console.error('Error deleting incomplete user:', deleteError);
        return NextResponse.json(
          { error: 'Failed to reset invite. Please try again.' },
          { status: 500 }
        );
      }
    }

    // Send (or resend) invite email via Supabase Admin API
    const { error: authError } = await serviceClient.auth.admin.inviteUserByEmail(
      invite.email,
      {
        redirectTo: `${process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000'}/login`,
      }
    );

    if (authError) {
      console.error('Error resending invite email:', authError);
      return NextResponse.json(
        { error: `Failed to resend invite: ${authError.message}` },
        { status: 500 }
      );
    }

    return NextResponse.json({
      success: true,
      message: `Invite resent to ${invite.email}`,
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json(
      { error: 'Failed to resend invite' },
      { status: 500 }
    );
  }
}
