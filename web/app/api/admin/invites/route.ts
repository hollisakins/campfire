import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * GET /api/admin/invites
 *
 * List all pending invites (admin only)
 */
export async function GET() {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  // Use service client for all data queries (bypasses RLS)
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
    // Fetch pending invites (not yet accepted)
    const { data: invites, error } = await serviceClient
      .from('pending_invites')
      .select(`
        id,
        email,
        program_ids,
        is_admin,
        can_comment,
        invited_by,
        created_at,
        accepted_at
      `)
      .is('accepted_at', null)
      .order('created_at', { ascending: false });

    if (error) {
      console.error('Error fetching invites:', error);
      return NextResponse.json(
        { error: 'Failed to fetch invites' },
        { status: 500 }
      );
    }

    // Get inviter names
    const inviterIds = [...new Set(invites?.map(i => i.invited_by).filter(Boolean))];
    let inviterProfiles: Record<string, string> = {};

    if (inviterIds.length > 0) {
      const { data: profiles } = await serviceClient
        .from('user_profiles')
        .select('user_id, full_name')
        .in('user_id', inviterIds);

      inviterProfiles = (profiles || []).reduce((acc, p) => {
        acc[p.user_id] = p.full_name;
        return acc;
      }, {} as Record<string, string>);
    }

    // Add inviter names to invites
    const invitesWithNames = (invites || []).map(invite => ({
      ...invite,
      invited_by_name: invite.invited_by ? inviterProfiles[invite.invited_by] || 'Unknown' : null,
    }));

    return NextResponse.json({ invites: invitesWithNames });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json(
      { error: 'Failed to fetch invites' },
      { status: 500 }
    );
  }
}

/**
 * POST /api/admin/invites
 *
 * Send an invite to a new user (admin only)
 * Body: { email: string, program_ids: number[], is_admin?: boolean, can_comment?: boolean }
 */
export async function POST(request: NextRequest) {
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
    const body = await request.json();
    const { email, program_ids = [], is_admin = false, can_comment = true } = body;

    // Validate email
    if (!email || typeof email !== 'string' || !email.includes('@')) {
      return NextResponse.json(
        { error: 'Valid email is required' },
        { status: 400 }
      );
    }

    const normalizedEmail = email.trim().toLowerCase();

    // Check if user already exists
    const serviceClient = createServiceClient();
    const { data: existingUsers } = await serviceClient.auth.admin.listUsers();
    const existingUser = existingUsers?.users?.find(
      u => u.email?.toLowerCase() === normalizedEmail
    );

    if (existingUser) {
      return NextResponse.json(
        { error: 'A user with this email already exists' },
        { status: 409 }
      );
    }

    // Check if invite already exists
    const { data: existingInvite } = await supabase
      .from('pending_invites')
      .select('id')
      .eq('email', normalizedEmail)
      .is('accepted_at', null)
      .single();

    if (existingInvite) {
      return NextResponse.json(
        { error: 'An invite for this email is already pending' },
        { status: 409 }
      );
    }

    // Create pending invite record
    const { error: inviteError } = await supabase
      .from('pending_invites')
      .insert({
        email: normalizedEmail,
        program_ids,
        is_admin,
        can_comment,
        invited_by: user.id,
      });

    if (inviteError) {
      console.error('Error creating invite record:', inviteError);
      return NextResponse.json(
        { error: 'Failed to create invite' },
        { status: 500 }
      );
    }

    // Send invite email via Supabase Admin API
    // Redirect to /login which will process the hash tokens and redirect to /welcome
    const { error: authError } = await serviceClient.auth.admin.inviteUserByEmail(
      normalizedEmail,
      {
        redirectTo: `${process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000'}/login`,
      }
    );

    if (authError) {
      console.error('Error sending invite email:', authError);
      // Clean up the pending invite record
      await supabase
        .from('pending_invites')
        .delete()
        .eq('email', normalizedEmail);

      return NextResponse.json(
        { error: `Failed to send invite email: ${authError.message}` },
        { status: 500 }
      );
    }

    return NextResponse.json({
      success: true,
      message: `Invite sent to ${normalizedEmail}`,
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json(
      { error: 'Failed to send invite' },
      { status: 500 }
    );
  }
}
