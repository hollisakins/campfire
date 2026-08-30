import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';
import { findAuthUserByEmail } from '@/lib/supabase/paginate';

/**
 * POST /api/admin/group-accounts
 *
 * Create a group account (admin only): a shared-credential principal for a
 * whole team. Unlike the invite flow there is no email round-trip — the admin
 * sets the password directly and distributes it. Re-using the email of a
 * previously deleted (tombstoned) group account revives it with the new
 * password, which doubles as the rotation path for a leaked shared password. The auth user is created
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
  // Truncate before applying the length floor: truncation can cut off the
  // trailing alphanumeric and the separator strip can then shorten the string
  // below 2 chars, so the floor must come last.
  base = base.slice(0, 38).replace(/[._-]+$/, '');
  if (base.length < 2) base = `group${base}`;
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

    // Check the email is not already taken
    const { user: existingUser, error: lookupError } = await findAuthUserByEmail(
      serviceClient,
      normalizedEmail
    );
    if (lookupError) {
      return NextResponse.json(
        { error: `Failed to check existing users: ${lookupError.message}` },
        { status: 500 }
      );
    }
    // The delete flow tombstones a group account (banned auth principal,
    // profile removed) rather than hard-deleting it, so its audit rows
    // survive. Such a tombstone may be REVIVED here — same email, new
    // password, fresh profile — which is also the recovery path for a leaked
    // shared password (delete, then re-create with the same email). Anything
    // else that matches the email is a genuine conflict: a live user, or a
    // profileless auth row from a pending invite (which the accept flow will
    // claim — never hijack it).
    let reviveUserId: string | null = null;
    if (existingUser) {
      const { data: existingProfile, error: profileLookupError } = await serviceClient
        .from('user_profiles')
        .select('user_id')
        .eq('user_id', existingUser.id)
        .maybeSingle();

      if (profileLookupError) {
        return NextResponse.json(
          { error: `Failed to check existing users: ${profileLookupError.message}` },
          { status: 500 }
        );
      }

      const isTombstonedGroup =
        !existingProfile && existingUser.user_metadata?.group_account === true;
      if (!isTombstonedGroup) {
        return NextResponse.json(
          { error: 'A user with this email already exists' },
          { status: 409 }
        );
      }
      reviveUserId = existingUser.id;
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
      // Belt and suspenders: derivation should always satisfy the DB CHECK,
      // but fail as a clean 400 rather than a constraint violation if not.
      if (!USERNAME_RE.test(resolvedUsername)) {
        return NextResponse.json(
          { error: 'Could not derive a valid username from the display name — provide one explicitly' },
          { status: 400 }
        );
      }
    }

    // Create the auth user (or revive the tombstoned one). No self_signup
    // flag, so handle_new_user() does not auto-provision a profile — we
    // insert it below with the group flag set.
    let groupUserId: string;
    if (reviveUserId) {
      const { error: reviveError } = await serviceClient.auth.admin.updateUserById(reviveUserId, {
        password,
        ban_duration: 'none',
        user_metadata: { group_account: true, full_name: full_name.trim() },
      });
      if (reviveError) {
        return NextResponse.json(
          { error: `Failed to reactivate account: ${reviveError.message}` },
          { status: 500 }
        );
      }
      groupUserId = reviveUserId;
    } else {
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
      groupUserId = created.user.id;
    }

    const cleanup = async (message: string) => {
      // A revived principal is re-banned, never deleted: the tombstone (and
      // the audit rows that cascade from auth.users) must survive a failed
      // revive exactly as they survived the original deletion.
      const { error: rollbackError } = reviveUserId
        ? (
            await serviceClient.auth.admin.updateUserById(groupUserId, {
              ban_duration: '876000h',
            })
          )
        : await serviceClient.auth.admin.deleteUser(groupUserId);
      if (rollbackError) {
        // A live, confirmed credential now exists with no profile: invisible
        // in the admin UI and blocking this email from re-registration.
        console.error(
          `Rollback failed — orphaned auth user ${groupUserId} (${normalizedEmail}):`,
          rollbackError
        );
        return NextResponse.json(
          {
            error:
              `${message}. Rollback also failed, leaving an orphaned login for ` +
              `${normalizedEmail} — it must be disabled in the Supabase dashboard.`,
          },
          { status: 500 }
        );
      }
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
