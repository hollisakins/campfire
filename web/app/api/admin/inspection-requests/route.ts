import { NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * GET /api/admin/inspection-requests
 *
 * Lists inspection-access requests with requester details (admin only).
 * Defaults to pending; pass ?status=all to include reviewed ones.
 */
export async function GET(request: Request) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const serviceClient = createServiceClient();

  const { data: profile } = await serviceClient
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  const url = new URL(request.url);
  const status = url.searchParams.get('status') || 'pending';

  let query = serviceClient
    .from('inspection_access_requests')
    .select('id, user_id, status, message, created_at, reviewed_at, reviewed_by')
    .order('created_at', { ascending: false });

  if (status !== 'all') {
    query = query.eq('status', status);
  }

  const { data: requests, error } = await query;

  if (error) {
    console.error('Error fetching inspection requests:', error);
    return NextResponse.json({ error: 'Failed to fetch requests' }, { status: 500 });
  }

  // Join requester profile + email for display.
  const userIds = [...new Set((requests || []).map((r) => r.user_id))];
  const profilesById: Record<string, { full_name: string; username: string; can_inspect: boolean }> = {};
  if (userIds.length > 0) {
    const { data: profiles } = await serviceClient
      .from('user_profiles')
      .select('user_id, full_name, username, can_inspect')
      .in('user_id', userIds);
    for (const p of profiles || []) {
      profilesById[p.user_id] = { full_name: p.full_name, username: p.username, can_inspect: p.can_inspect };
    }
  }

  const emailsById: Record<string, string> = {};
  await Promise.all(
    userIds.map(async (uid) => {
      const { data } = await serviceClient.auth.admin.getUserById(uid);
      if (data?.user?.email) emailsById[uid] = data.user.email;
    })
  );

  const enriched = (requests || []).map((r) => ({
    ...r,
    full_name: profilesById[r.user_id]?.full_name ?? 'Unknown',
    username: profilesById[r.user_id]?.username ?? 'unknown',
    can_inspect: profilesById[r.user_id]?.can_inspect ?? false,
    email: emailsById[r.user_id] ?? null,
  }));

  return NextResponse.json({ requests: enriched });
}
