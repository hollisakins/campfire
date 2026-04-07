import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';
import { USERNAME_REGEX } from '@/lib/utils/username';

/**
 * POST /api/invites/accept
 *
 * Completes the invite acceptance process:
 * - Sets user password
 * - Creates user profile
 * - Grants program access
 * - Marks invite as accepted
 *
 * Body: { fullName: string, password: string, username: string }
 */
export async function POST(request: NextRequest) {
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

    // Parse request body
    const body = await request.json();
    const { fullName, password, username } = body;

    // Validate input
    if (!fullName || typeof fullName !== 'string' || !fullName.trim()) {
      return NextResponse.json(
        { error: 'Full name is required' },
        { status: 400 }
      );
    }

    if (!password || typeof password !== 'string' || password.length < 6) {
      return NextResponse.json(
        { error: 'Password must be at least 6 characters' },
        { status: 400 }
      );
    }

    if (!username || typeof username !== 'string' || !USERNAME_REGEX.test(username)) {
      return NextResponse.json(
        { error: 'Username must be 2–40 characters and contain only lowercase letters, numbers, dots, hyphens, and underscores (starting and ending with a letter or number).' },
        { status: 400 }
      );
    }

    // Use service client to bypass RLS
    const serviceClient = createServiceClient();

    // Look up pending invite
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

    // Set user password
    const { error: passwordError } = await supabase.auth.updateUser({
      password: password,
    });

    if (passwordError) {
      console.error('Error setting password:', passwordError);
      return NextResponse.json(
        { error: `Failed to set password: ${passwordError.message}` },
        { status: 500 }
      );
    }

    // Check username uniqueness
    const { data: existingUser } = await serviceClient
      .from('user_profiles')
      .select('user_id')
      .eq('username', username)
      .maybeSingle();

    if (existingUser) {
      return NextResponse.json(
        { error: 'That username is already taken. Please choose a different one.' },
        { status: 409 }
      );
    }

    // Create user profile (using service client to ensure it succeeds)
    const { error: profileError } = await serviceClient
      .from('user_profiles')
      .insert({
        user_id: user.id,
        username,
        full_name: fullName.trim(),
        is_group_account: false,
        can_comment: invite.can_comment,
        is_admin: invite.is_admin,
      });

    if (profileError) {
      console.error('Error creating profile:', profileError);
      return NextResponse.json(
        { error: `Failed to create profile: ${profileError.message}` },
        { status: 500 }
      );
    }

    // Grant program access
    if (invite.program_slugs && invite.program_slugs.length > 0) {
      const accessRecords = invite.program_slugs.map((programSlug: string) => ({
        user_id: user.id,
        program_slug: programSlug,
        granted_by: invite.invited_by,
      }));

      const { error: accessError } = await serviceClient
        .from('user_program_access')
        .upsert(accessRecords, {
          onConflict: 'user_id,program_slug',
          ignoreDuplicates: true,
        });

      if (accessError) {
        console.error('Error granting program access:', accessError);
        // Don't fail the whole request for this - user can be granted access later
      }
    }

    // Mark invite as accepted
    const { error: updateError } = await serviceClient
      .from('pending_invites')
      .update({ accepted_at: new Date().toISOString() })
      .eq('id', invite.id);

    if (updateError) {
      console.error('Error marking invite as accepted:', updateError);
      // Don't fail - profile is created, this is just bookkeeping
    }

    return NextResponse.json({
      success: true,
      message: 'Account setup complete',
    });

  } catch (error) {
    console.error('Error accepting invite:', error);
    return NextResponse.json(
      { error: 'Failed to complete account setup' },
      { status: 500 }
    );
  }
}
