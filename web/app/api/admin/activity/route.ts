import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';
import { paginateQuery } from '@/lib/supabase/paginate';
import type { Activity, CommentActivity, InspectionActivity } from '@/lib/types';

/**
 * GET /api/admin/activity
 *
 * Fetch recent user activity (comments + inspection changes) for admin review.
 * Admin-only endpoint that combines data from comments and flag_audit_log tables.
 *
 * Query params:
 * - page: Page number (default 1)
 * - page_size: Items per page (default 50, max 100)
 * - type: Filter by activity type ('comment', 'inspection', or comma-separated)
 * - user_id: Filter by user ID (comma-separated for multiple)
 * - field_name: Filter inspection activities by field name (comma-separated)
 */
export async function GET(request: NextRequest) {
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

  // Parse query params
  const searchParams = request.nextUrl.searchParams;
  const page = parseInt(searchParams.get('page') || '1');
  const pageSize = Math.min(parseInt(searchParams.get('page_size') || '50'), 100);

  // Parse filter params
  const typeParam = searchParams.get('type');
  const userIdParam = searchParams.get('user_id');
  const fieldNameParam = searchParams.get('field_name');

  // Determine which types to fetch
  const typeFilters = typeParam ? typeParam.split(',').filter(t => t) : [];
  const includeComments = typeFilters.length === 0 || typeFilters.includes('comment');
  const includeInspections = typeFilters.length === 0 || typeFilters.includes('inspection');

  // Parse user IDs (special "system" value means null user_id)
  const userIdFiltersRaw = userIdParam ? userIdParam.split(',').filter(id => id) : [];
  const includeSystemUser = userIdFiltersRaw.includes('system');
  const userIdFilters = userIdFiltersRaw.filter(id => id !== 'system');

  // Parse field names
  const fieldNameFilters = fieldNameParam ? fieldNameParam.split(',').filter(f => f) : [];

  try {
    let commentActivities: CommentActivity[] = [];
    let inspectionActivities: InspectionActivity[] = [];

    // Fetch comments if included (paginate to avoid PostgREST max-rows truncation)
    if (includeComments) {
      const { data: comments, error: commentsError } = await paginateQuery(
        () => {
          let q = serviceClient
            .from('comments')
            .select(`
              id,
              target_id,
              user_id,
              content,
              created_at,
              edited_at,
              targets!inner(target_id)
            `)
            .eq('is_deleted', false)
            .order('created_at', { ascending: false })
            .order('id', { ascending: false });

          if (userIdFilters.length > 0) {
            q = q.in('user_id', userIdFilters);
          }
          // If only "system" selected, comments have no null user_ids — skip
          if (includeSystemUser && userIdFilters.length === 0) {
            q = q.eq('user_id', 'no-match');
          }
          return q;
        },
      );
      if (commentsError) throw commentsError;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      commentActivities = comments.map((c: any) => {
        const targetData = Array.isArray(c.targets) ? c.targets[0] : c.targets;
        return {
          id: `comment-${c.id}`,
          type: 'comment' as const,
          target_db_id: c.target_id,
          target_display_id: targetData?.target_id || '',
          user_id: c.user_id,
          timestamp: c.created_at,
          content: c.content,
          edited_at: c.edited_at,
        };
      });
    }

    // Fetch audit logs if included (paginate to avoid PostgREST max-rows truncation)
    if (includeInspections) {
      const { data: auditLogs, error: auditError } = await paginateQuery(
        () => {
          let q = serviceClient
            .from('flag_audit_log')
            .select(`
              id,
              target_id,
              user_id,
              field_name,
              old_value,
              new_value,
              changed_at,
              targets!inner(target_id)
            `)
            .order('changed_at', { ascending: false })
            .order('id', { ascending: false });

          if (userIdFilters.length > 0 && !includeSystemUser) {
            q = q.in('user_id', userIdFilters);
          } else if (userIdFilters.length === 0 && includeSystemUser) {
            q = q.is('user_id', null);
          }
          if (fieldNameFilters.length > 0) {
            q = q.in('field_name', fieldNameFilters);
          }
          return q;
        },
      );
      if (auditError) throw auditError;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      inspectionActivities = auditLogs.map((a: any) => {
        const targetData = Array.isArray(a.targets) ? a.targets[0] : a.targets;
        return {
          id: `audit-${a.id}`,
          type: 'inspection' as const,
          target_db_id: a.target_id,
          target_display_id: targetData?.target_id || '',
          user_id: a.user_id,
          timestamp: a.changed_at,
          field_name: a.field_name,
          old_value: a.old_value,
          new_value: a.new_value,
        };
      });
    }

    // Merge and sort
    let allActivities: Activity[] = [
      ...commentActivities,
      ...inspectionActivities,
    ].sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

    // In-memory filter for combined system + real user selections
    if (includeSystemUser && userIdFilters.length > 0) {
      allActivities = allActivities.filter(a =>
        a.user_id === null || userIdFilters.includes(a.user_id)
      );
    }

    // Apply pagination
    const totalCount = allActivities.length;
    const startIndex = (page - 1) * pageSize;
    const endIndex = startIndex + pageSize;
    const paginatedActivities = allActivities.slice(startIndex, endIndex);

    // Batch fetch user profiles for current page (filter nulls — system-generated entries)
    const userIdsOnPage = [...new Set(paginatedActivities.map(a => a.user_id).filter(Boolean))];
    const userProfiles: Record<string, { user_id: string; full_name: string; is_group_account: boolean }> = {};

    if (userIdsOnPage.length > 0) {
      const { data: profiles } = await serviceClient
        .from('user_profiles')
        .select('*')
        .in('user_id', userIdsOnPage);

      (profiles || []).forEach(p => {
        userProfiles[p.user_id] = p;
      });
    }

    // Join user profiles (null user_id = system propagation)
    const activitiesWithUsers = paginatedActivities.map(activity => ({
      ...activity,
      user_profile: activity.user_id
        ? (userProfiles[activity.user_id] || null)
        : { user_id: null, full_name: 'System', is_group_account: false },
    }));

    // Fetch available users for filter dropdown (users who have activity)
    // Paginate to avoid PostgREST max-rows truncation
    const [commentsUsers, auditUsers] = await Promise.all([
      paginateQuery<{ user_id: string }>(
        () => serviceClient
          .from('comments')
          .select('user_id')
          .eq('is_deleted', false)
          .order('user_id'),
      ),
      paginateQuery<{ user_id: string }>(
        () => serviceClient
          .from('flag_audit_log')
          .select('user_id')
          .order('user_id'),
      ),
    ]);

    const allActiveUserIdsRaw = [
      ...commentsUsers.data.map(c => c.user_id),
      ...auditUsers.data.map(a => a.user_id),
    ];
    const hasSystemActivity = allActiveUserIdsRaw.some(id => id === null);
    const allActiveUserIds = [...new Set(allActiveUserIdsRaw.filter(Boolean))];

    // Fetch profiles for all active users
    let availableUsers: { user_id: string; full_name: string }[] = [];
    if (allActiveUserIds.length > 0) {
      const { data: activeProfiles } = await serviceClient
        .from('user_profiles')
        .select('user_id, full_name')
        .in('user_id', allActiveUserIds)
        .order('full_name');

      availableUsers = (activeProfiles || []).map(p => ({
        user_id: p.user_id,
        full_name: p.full_name || 'Unknown User',
      }));
    }

    // Add "System" entry for null user_id activities (system propagation)
    if (hasSystemActivity) {
      availableUsers.unshift({ user_id: 'system', full_name: 'System' });
    }

    return NextResponse.json({
      activities: activitiesWithUsers,
      total_count: totalCount,
      page,
      page_size: pageSize,
      has_next_page: endIndex < totalCount,
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
