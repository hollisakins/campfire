import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

interface RouteParams {
  params: Promise<{ id: string }>;
}

/**
 * PATCH /api/admin/account-requests/[id]
 *
 * Approve or reject an account request (admin only)
 * Body: {
 *   action: 'approve' | 'reject',
 *   // Only for approve:
 *   is_admin?: boolean,
 *   can_comment?: boolean,
 *   program_ids?: number[],
 *   // Only for reject:
 *   rejection_reason?: string
 * }
 */
export async function PATCH(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;
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
    const body = await request.json();
    const { action, is_admin = false, can_comment = true, program_ids = [], rejection_reason } = body;

    // Validate action
    if (!action || !['approve', 'reject'].includes(action)) {
      return NextResponse.json(
        { error: 'Invalid action. Must be "approve" or "reject".' },
        { status: 400 }
      );
    }

    // Fetch the request
    const { data: accountRequest, error: fetchError } = await serviceClient
      .from('account_requests')
      .select('*')
      .eq('id', id)
      .single();

    if (fetchError || !accountRequest) {
      return NextResponse.json(
        { error: 'Account request not found' },
        { status: 404 }
      );
    }

    // Check if already processed
    if (accountRequest.status !== 'pending') {
      return NextResponse.json(
        { error: `This request has already been ${accountRequest.status}` },
        { status: 400 }
      );
    }

    if (action === 'approve') {
      // Check if user already exists
      const { data: existingUsers } = await serviceClient.auth.admin.listUsers();
      const existingUser = existingUsers?.users?.find(
        u => u.email?.toLowerCase() === accountRequest.email.toLowerCase()
      );

      if (existingUser) {
        return NextResponse.json(
          { error: 'A user with this email already exists' },
          { status: 409 }
        );
      }

      // Create pending_invites record (reusing existing invite flow)
      // Include full_name so it's pre-populated on the welcome page
      const { error: inviteError } = await serviceClient
        .from('pending_invites')
        .insert({
          email: accountRequest.email,
          full_name: accountRequest.full_name,
          program_ids,
          is_admin,
          can_comment,
          invited_by: user.id,
        });

      if (inviteError) {
        console.error('Error creating invite record:', inviteError);
        return NextResponse.json(
          { error: 'Failed to create invite record' },
          { status: 500 }
        );
      }

      // Send invite email via Supabase Admin API
      const { error: authError } = await serviceClient.auth.admin.inviteUserByEmail(
        accountRequest.email,
        {
          redirectTo: `${process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000'}/login`,
        }
      );

      if (authError) {
        console.error('Error sending invite email:', authError);
        // Clean up the pending invite record
        await serviceClient
          .from('pending_invites')
          .delete()
          .eq('email', accountRequest.email);

        return NextResponse.json(
          { error: `Failed to send invite email: ${authError.message}` },
          { status: 500 }
        );
      }

      // Update the account request status
      const { error: updateError } = await serviceClient
        .from('account_requests')
        .update({
          status: 'approved',
          is_admin,
          can_comment,
          program_ids,
          reviewed_at: new Date().toISOString(),
          reviewed_by: user.id,
        })
        .eq('id', id);

      if (updateError) {
        console.error('Error updating request status:', updateError);
        return NextResponse.json(
          { error: 'Failed to update request status' },
          { status: 500 }
        );
      }

      return NextResponse.json({
        success: true,
        message: `Request approved. An invite email has been sent to ${accountRequest.email}.`,
      });

    } else {
      // Reject the request
      const { error: updateError } = await serviceClient
        .from('account_requests')
        .update({
          status: 'rejected',
          rejection_reason: rejection_reason || null,
          reviewed_at: new Date().toISOString(),
          reviewed_by: user.id,
        })
        .eq('id', id);

      if (updateError) {
        console.error('Error updating request status:', updateError);
        return NextResponse.json(
          { error: 'Failed to update request status' },
          { status: 500 }
        );
      }

      return NextResponse.json({
        success: true,
        message: 'Request rejected.',
      });
    }

  } catch (error) {
    console.error('Error processing request:', error);
    return NextResponse.json(
      { error: 'An unexpected error occurred' },
      { status: 500 }
    );
  }
}

/**
 * DELETE /api/admin/account-requests/[id]
 *
 * Delete an account request (admin only)
 */
export async function DELETE(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;
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
    const { error } = await serviceClient
      .from('account_requests')
      .delete()
      .eq('id', id);

    if (error) {
      console.error('Error deleting request:', error);
      return NextResponse.json(
        { error: 'Failed to delete request' },
        { status: 500 }
      );
    }

    return NextResponse.json({
      success: true,
      message: 'Request deleted.',
    });

  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json(
      { error: 'Failed to delete request' },
      { status: 500 }
    );
  }
}
