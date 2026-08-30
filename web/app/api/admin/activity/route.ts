import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/admin/activity
 *
 * Fetch recent user activity (comments + inspection changes) for admin review.
 * Backed by the get_activity_feed / get_activity_users RPCs, which filter,
 * sort, and paginate server-side (single scan + window count) instead of the
 * previous fetch-every-row-then-sort-in-JS approach.
 *
 * Query params:
 * - page: Page number (default 1)
 * - page_size: Items per page (default 50, max 100)
 * - type: Filter by activity type ('comment', 'inspection', or comma-separated)
 * - user_id: Filter by user ID (comma-separated for multiple; special value
 *   'system' selects NULL-user system-generated audit rows)
 * - field_name: Filter inspection activities by field name (comma-separated)
 */

interface FeedRow {
  id: string;
  type: 'comment' | 'inspection';
  target_db_id: number;
  target_display_id: string;
  user_id: string | null;
  ts: string;
  content: string | null;
  edited_at: string | null;
  field_name: string | null;
  old_value: number | null;
  new_value: number | null;
  user_full_name: string | null;
  user_is_group_account: boolean;
  subject_kind: 'target' | 'object' | 'spectrum' | null;
  total_count: number;
}

interface ActivityUserRow {
  user_id: string | null;
  full_name: string | null;
}

export async function GET(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication. Authorization (is_admin) is enforced inside the
  // RPCs themselves, which run under the caller's JWT; the profile check here
  // just gives a clean 403 instead of a 500.
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

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

  // Parse query params
  const searchParams = request.nextUrl.searchParams;
  const page = parseInt(searchParams.get('page') || '1');
  const pageSize = Math.min(parseInt(searchParams.get('page_size') || '50'), 100);

  const typeParam = searchParams.get('type');
  const userIdParam = searchParams.get('user_id');
  const fieldNameParam = searchParams.get('field_name');

  const typeFilters = typeParam ? typeParam.split(',').filter(t => t) : [];
  const includeComments = typeFilters.length === 0 || typeFilters.includes('comment');
  const includeInspections = typeFilters.length === 0 || typeFilters.includes('inspection');

  // Special "system" value means NULL user_id (system-generated audit rows)
  const userIdFiltersRaw = userIdParam ? userIdParam.split(',').filter(id => id) : [];
  const includeSystemUser = userIdFiltersRaw.includes('system');
  const userIdFilters = userIdFiltersRaw.filter(id => id !== 'system');

  const fieldNameFilters = fieldNameParam ? fieldNameParam.split(',').filter(f => f) : [];

  try {
    const [feedRes, usersRes] = await Promise.all([
      supabase.rpc('get_activity_feed', {
        p_include_comments: includeComments,
        p_include_inspections: includeInspections,
        p_user_ids: userIdFilters.length > 0 ? userIdFilters : null,
        p_include_system: includeSystemUser,
        p_field_names: fieldNameFilters.length > 0 ? fieldNameFilters : null,
        p_page: page,
        p_page_size: pageSize,
      }),
      supabase.rpc('get_activity_users'),
    ]);

    if (feedRes.error) throw feedRes.error;
    if (usersRes.error) throw usersRes.error;

    const feedRows = (feedRes.data ?? []) as FeedRow[];
    const totalCount = feedRows[0]?.total_count ?? 0;

    const activities = feedRows.map((row) => {
      const base = {
        id: row.id,
        target_db_id: row.target_db_id,
        target_display_id: row.target_display_id,
        subject_kind: row.subject_kind,
        user_id: row.user_id,
        timestamp: row.ts,
        user_profile: row.user_id
          ? {
              user_id: row.user_id,
              full_name: row.user_full_name || 'Unknown User',
              is_group_account: row.user_is_group_account,
            }
          : { user_id: null, full_name: 'System', is_group_account: false },
      };
      if (row.type === 'comment') {
        return {
          ...base,
          type: 'comment' as const,
          content: row.content ?? '',
          edited_at: row.edited_at,
        };
      }
      return {
        ...base,
        type: 'inspection' as const,
        field_name: row.field_name ?? '',
        old_value: row.old_value,
        new_value: row.new_value,
      };
    });

    // Available users for the filter dropdown. A NULL user_id row from the RPC
    // signals system activity; surface it as the synthetic "System" entry.
    const userRows = (usersRes.data ?? []) as ActivityUserRow[];
    const availableUsers = userRows
      .filter(u => u.user_id !== null)
      .map(u => ({
        user_id: u.user_id as string,
        full_name: u.full_name || 'Unknown User',
      }));
    if (userRows.some(u => u.user_id === null)) {
      availableUsers.unshift({ user_id: 'system', full_name: 'System' });
    }

    return NextResponse.json({
      activities,
      total_count: totalCount,
      page,
      page_size: pageSize,
      has_next_page: page * pageSize < totalCount,
      available_users: availableUsers,
    });
  } catch (error) {
    console.error('Error fetching activity:', error);
    return NextResponse.json(
      { error: 'Failed to fetch activity' },
      { status: 500 }
    );
  }
}
