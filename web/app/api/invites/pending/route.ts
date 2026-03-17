import { NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * GET /api/invites/pending
 *
 * Returns the pending invite for the authenticated user.
 * Uses service role client to bypass RLS restrictions.
 */
export async function GET() {
  try {
    // Get authenticated user from session
    const supabase = await createClient();
    const { data: { user }, error: authError } = await supabase.auth.getUser();

    if (authError || !user || !user.email) {
      return NextResponse.json(
        { error: 'Authentication required' },
        { status: 401 }
      );
    }

    // Use service client to bypass RLS
    const serviceClient = createServiceClient();

    // Look up pending invite by email
    const { data: invite, error: inviteError } = await serviceClient
      .from('pending_invites')
      .select('*')
      .eq('email', user.email.toLowerCase())
      .is('accepted_at', null)
      .single();

    if (inviteError || !invite) {
      return NextResponse.json(
        { error: 'No pending invitation found for your account' },
        { status: 404 }
      );
    }

    // Fetch program names for the invited programs
    let programs: { slug: string; program_name: string | null }[] = [];
    if (invite.program_slugs && invite.program_slugs.length > 0) {
      const { data: programData } = await serviceClient
        .from('programs')
        .select('slug, program_name')
        .in('slug', invite.program_slugs);

      programs = programData || [];
    }

    return NextResponse.json({
      invite: {
        id: invite.id,
        email: invite.email,
        full_name: invite.full_name,
        program_slugs: invite.program_slugs,
        is_admin: invite.is_admin,
        can_comment: invite.can_comment,
        invited_by: invite.invited_by,
      },
      programs,
    });

  } catch (error) {
    console.error('Error fetching pending invite:', error);
    return NextResponse.json(
      { error: 'Failed to fetch invitation' },
      { status: 500 }
    );
  }
}
