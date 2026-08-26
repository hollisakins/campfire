import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * POST /api/admin/group-accounts
 *
 * Create a group account (admin only): a shared-credential principal for a
 * whole team. Unlike the invite flow there is no email round-trip — the admin
 * sets the password directly and distributes it. The auth user is created
 * first, then the profile, then program access; if a later step fails the auth
 * user is deleted so a failed create never leaves an orphan principal
 * (mirrors mintShareLink in lib/actions/share-links.ts).
 *
 * Body: {
 *   email: string,
 *   password: string,
 *   full_name: string,
 *   username?: string,        // derived from full_name/email if omitted
 *   can_comment?: boolean,    // default true
 *   can_inspect?: boolean,    // default false
 *   program_slugs?: string[],
 * }
 *
 * Group accounts are never admins — a shared admin credential is
 * unattributable, so is_admin is not accepted here.
 */

// Mirrors user_profiles_username_check in supabase/schemas/tables.sql.
const USERNAME_RE = /^[a-z0-9][a-z0-9._-]{0,38}[a-z0-9]$/;

/** Same sanitization as handle_new_user() in supabase/schemas/triggers.sql. */
function sanitizeUsername(raw: string): string {
  let base = raw
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, '')
    .replace(/^[._-]+/, '')
    .replace(/[._-]+$/, '');
  if (base.length < 2) base = `group${base}`;
  base = base.slice(0, 38).replace(/[._-]+$/, '');
  return base;
}

export async function POST(request: NextRequest) {
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
    const {
      email,
      password,
      full_name,
      username,
      can_comment = true,
      can_inspect = false,
      program_slugs = [],
    } = body;

    if (!email || typeof email !== 'string' || !email.includes('@')) {
      return NextResponse.json({ error: 'Valid email is required' }, { status: 400 });
    }
    if (!password || typeof password !== 'string' || password.length < 8) {
      return NextResponse.json(
        { error: 'Password must be at least 8 characters' },
        { status: 400 }
      );
    }
    if (!full_name || typeof full_name !== 'string' || !full_name.trim()) {
      return NextResponse.json({ error: 'Display name is required' }, { status: 400 });
    }
    if (
      !Array.isArray(program_slugs) ||
      program_slugs.some((s: unknown) => typeof s !== 'string')
    ) {
      return NextResponse.json({ error: 'program_slugs must be a list of slugs' }, { status: 400 });
    }

    const normalizedEmail = email.trim().toLowerCase();
    const serviceClient = createServiceClient();

    // Check the email is not already taken (same pattern as the invite route)
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

    // Resolve a username: an explicit one must satisfy the DB check as given;
    // a derived one is sanitized and de-duplicated with a numeric suffix.
    let resolvedUsername: string;
    if (typeof username === 'string' && username.trim()) {
      resolvedUsername = username.trim().toLowerCase();
      if (!USERNAME_RE.test(resolvedUsername)) {
        return NextResponse.json(
          {
            error:
              'Username must be 2-40 characters of lowercase letters, digits, . _ or -, starting and ending with a letter or digit',
          },
          { status: 400 }
        );
      }
      const { data: taken } = await serviceClient
        .from('user_profiles')
        .select('user_id')
        .eq('username', resolvedUsername)
        .maybeSingle();
      if (taken) {
        return NextResponse.json({ error: 'Username is already taken' }, { status: 409 });
      }
    } else {
      const base = sanitizeUsername(full_name.trim() || normalizedEmail.split('@')[0]);
      resolvedUsername = base;
      let suffix = 0;
      // De-duplicate with a numeric suffix (same scheme as handle_new_user).
      for (;;) {
        const { data: taken } = await serviceClient
          .from('user_profiles')
          .select('user_id')
          .eq('username', resolvedUsername)
          .maybeSingle();
        if (!taken) break;
        suffix += 1;
        resolvedUsername = base.slice(0, 39 - String(suffix).length) + suffix;
      }
    }

    // Create the auth user. No self_signup flag, so handle_new_user() does not
    // auto-provision a profile — we insert it below with the group flag set.
    const { data: created, error: userError } = await serviceClient.auth.admin.createUser({
      email: normalizedEmail,
      password,
      email_confirm: true,
      user_metadata: { group_account: true, full_name: full_name.trim() },
    });

    if (userError || !created?.user) {
      return NextResponse.json(
        { error: `Failed to create account: ${userError?.message ?? 'unknown error'}` },
        { status: 500 }
      );
    }
    const groupUserId = created.user.id;

    const cleanup = async (message: string) => {
      await serviceClient.auth.admin.deleteUser(groupUserId);
      return NextResponse.json({ error: message }, { status: 500 });
    };

    const { error: profileError } = await serviceClient.from('user_profiles').insert({
      user_id: groupUserId,
      username: resolvedUsername,
      full_name: full_name.trim(),
      is_group_account: true,
      can_comment: !!can_comment,
      can_inspect: !!can_inspect,
      is_admin: false,
      is_link_account: false,
    });
    if (profileError) {
      return cleanup(`Failed to create profile: ${profileError.message}`);
    }

    if (program_slugs.length > 0) {
      const accessRows = program_slugs.map((programSlug: string) => ({
        user_id: groupUserId,
        program_slug: programSlug,
        granted_by: user.id,
      }));
      const { error: accessError } = await serviceClient
        .from('user_program_access')
        .insert(accessRows);
      if (accessError) {
        return cleanup(`Failed to grant program access: ${accessError.message}`);
      }
    }

    return NextResponse.json({
      success: true,
      user_id: groupUserId,
      username: resolvedUsername,
      message: `Group account "${full_name.trim()}" created`,
    });
  } catch (error) {
    console.error('Error creating group account:', error);
    return NextResponse.json({ error: 'Failed to create group account' }, { status: 500 });
  }
}
