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

  // Parse user IDs
  const userIdFilters = userIdParam ? userIdParam.split(',').filter(id => id) : [];

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
              object_id,
              user_id,
              content,
              created_at,
              edited_at,
              objects!inner(object_id)
            `)
            .eq('is_deleted', false)
            .order('created_at', { ascending: false });

          if (userIdFilters.length > 0) {
            q = q.in('user_id', userIdFilters);
          }
          return q;
        },
      );
      if (commentsError) throw commentsError;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      commentActivities = comments.map((c: any) => {
        const objectData = Array.isArray(c.objects) ? c.objects[0] : c.objects;
        return {
          id: `comment-${c.id}`,
          type: 'comment' as const,
          object_db_id: c.object_id,
          object_display_id: objectData?.object_id || '',
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
              object_id,
              user_id,
              field_name,
              old_value,
              new_value,
              changed_at,
              objects!inner(object_id)
            `)
            .order('changed_at', { ascending: false });

          if (userIdFilters.length > 0) {
            q = q.in('user_id', userIdFilters);
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
        const objectData = Array.isArray(a.objects) ? a.objects[0] : a.objects;
        return {
          id: `audit-${a.id}`,
          type: 'inspection' as const,
          object_db_id: a.object_id,
          object_display_id: objectData?.object_id || '',
          user_id: a.user_id,
          timestamp: a.changed_at,
          field_name: a.field_name,
          old_value: a.old_value,
          new_value: a.new_value,
        };
      });
    }

    // Merge and sort
    const allActivities: Activity[] = [
      ...commentActivities,
      ...inspectionActivities,
    ].sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

    // Apply pagination
    const totalCount = allActivities.length;
    const startIndex = (page - 1) * pageSize;
    const endIndex = startIndex + pageSize;
    const paginatedActivities = allActivities.slice(startIndex, endIndex);

    // Batch fetch user profiles for current page
    const userIdsOnPage = [...new Set(paginatedActivities.map(a => a.user_id))];
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

    // Join user profiles
    const activitiesWithUsers = paginatedActivities.map(activity => ({
      ...activity,
      user_profile: userProfiles[activity.user_id] || null,
    }));

    // Fetch available users for filter dropdown (users who have activity)
    // Paginate to avoid PostgREST max-rows truncation
    const [commentsUsers, auditUsers] = await Promise.all([
      paginateQuery<{ user_id: string }>(
        () => serviceClient
          .from('comments')
          .select('user_id')
          .eq('is_deleted', false),
      ),
      paginateQuery<{ user_id: string }>(
        () => serviceClient
          .from('flag_audit_log')
          .select('user_id'),
      ),
    ]);

    const allActiveUserIds = [...new Set([
      ...commentsUsers.data.map(c => c.user_id),
      ...auditUsers.data.map(a => a.user_id),
    ])];

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
