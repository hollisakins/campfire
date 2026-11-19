import { NextResponse } from 'next/server';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/service';
import { listAllAuthUsers, paginateQuery } from '@/lib/supabase/paginate';
import type { UserProfile } from '@/lib/types';

/** An auth login with no user_profiles row: invisible everywhere else. */
export interface OrphanAuthUser {
  user_id: string;
  email: string | null;
  created_at: string;
  invited_at: string | null;
  email_confirmed_at: string | null;
  last_sign_in_at: string | null;
  self_signup: boolean;
}

export interface AdminUserRow extends UserProfile {
  program_access: string[];
  email: string | null;
  last_sign_in_at: string | null;
}

export interface AdminUsersResponse {
  users: AdminUserRow[];
  orphans: OrphanAuthUser[];
  /** The auth listing failed: no emails, and orphans could not be computed. */
  auth_unavailable: boolean;
  programs: { slug: string; program_name: string | null; is_public: boolean }[];
}

/**
 * GET /api/users
 *
 * Every user profile with its program access and auth email, plus the auth
 * logins that have no profile. Admin only. Every read pages, so nothing is
 * truncated at PostgREST's max_rows or GoTrue's listUsers page size.
 */
export async function GET() {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  if (!(await isAdminUser(user.id))) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    const [profilesResult, accessResult, programsResult, authResult] = await Promise.all([
      paginateQuery<UserProfile>(
        () => supabase.from('user_profiles').select('*')
          .order('created_at', { ascending: false })
          .order('user_id'),
      ),
      paginateQuery<{ user_id: string; program_slug: string }>(
        () => supabase.from('user_program_access').select('user_id, program_slug')
          .order('user_id')
          .order('program_slug'),
      ),
      supabase.from('programs').select('slug, program_name, is_public'),
      // Emails live in auth.users, which only the service role can list.
      listAllAuthUsers(createServiceClient()),
    ]);

    if (profilesResult.error) {
      console.error('Error fetching users:', profilesResult.error);
      return NextResponse.json({ error: 'Failed to fetch users' }, { status: 500 });
    }
    if (accessResult.error) {
      console.error('Error fetching access:', accessResult.error);
      return NextResponse.json({ error: 'Failed to fetch access' }, { status: 500 });
    }
    // Emails and orphans are an addition to the page, not a precondition:
    // without the auth listing the profiles still render.
    const authUnavailable = authResult.error !== null;
    if (authResult.error) {
      console.error('Error listing auth users:', authResult.error);
    }
    if (programsResult.error) {
      console.error('Error fetching programs:', programsResult.error);
    }

    const accessByUser = new Map<string, string[]>();
    for (const a of accessResult.data) {
      const list = accessByUser.get(a.user_id);
      if (list) list.push(a.program_slug);
      else accessByUser.set(a.user_id, [a.program_slug]);
    }

    const authById = new Map(authResult.users.map(u => [u.id, u]));
    const profileIds = new Set(profilesResult.data.map(p => p.user_id));

    const users: AdminUserRow[] = profilesResult.data.map(p => {
      const auth = authById.get(p.user_id);
      return {
        ...p,
        program_access: accessByUser.get(p.user_id) ?? [],
        email: auth?.email ?? null,
        last_sign_in_at: auth?.last_sign_in_at ?? null,
      };
    });

    const orphans: OrphanAuthUser[] = authUnavailable ? [] : authResult.users
      .filter(u => !profileIds.has(u.id))
      .map(u => ({
        user_id: u.id,
        email: u.email ?? null,
        created_at: u.created_at,
        invited_at: u.invited_at ?? null,
        email_confirmed_at: u.email_confirmed_at ?? null,
        last_sign_in_at: u.last_sign_in_at ?? null,
        self_signup: u.user_metadata?.self_signup === 'true' || u.user_metadata?.self_signup === true,
      }))
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));

    const body: AdminUsersResponse = {
      users,
      orphans,
      auth_unavailable: authUnavailable,
      programs: programsResult.data ?? [],
    };
    return NextResponse.json(body, { headers: { 'Cache-Control': 'private, no-store' } });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch users' }, { status: 500 });
  }
}
