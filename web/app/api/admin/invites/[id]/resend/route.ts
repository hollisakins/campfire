import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

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

  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  // Use service client for admin operations
  const serviceClient = createServiceClient();

  // Check admin permission
  const { data: profile } = await serviceClient
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

    // Resend invite email via Supabase Admin API
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
