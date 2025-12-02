import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';
import type { Activity, CommentActivity, InspectionActivity } from '@/lib/types';

/**
 * GET /api/admin/activity
 *
 * Fetch recent user activity (comments + inspection changes) for admin review.
 * Admin-only endpoint that combines data from comments and flag_audit_log tables.
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

  // Parse pagination params
  const searchParams = request.nextUrl.searchParams;
  const page = parseInt(searchParams.get('page') || '1');
  const pageSize = Math.min(parseInt(searchParams.get('page_size') || '50'), 100);

  try {
    // Fetch comments with object join
    const { data: comments, error: commentsError } = await serviceClient
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

    if (commentsError) throw commentsError;

    // Fetch audit logs with object join
    const { data: auditLogs, error: auditError } = await serviceClient
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

    if (auditError) throw auditError;

    // Transform to unified format
    const commentActivities: CommentActivity[] = (comments || []).map(c => {
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

    const inspectionActivities: InspectionActivity[] = (auditLogs || []).map(a => {
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

    // Batch fetch user profiles
    const userIds = [...new Set(paginatedActivities.map(a => a.user_id))];
    const userProfiles: Record<string, { user_id: string; full_name: string; is_group_account: boolean }> = {};

    if (userIds.length > 0) {
      const { data: profiles } = await serviceClient
        .from('user_profiles')
        .select('*')
        .in('user_id', userIds);

      (profiles || []).forEach(p => {
        userProfiles[p.user_id] = p;
      });
    }

    // Join user profiles
    const activitiesWithUsers = paginatedActivities.map(activity => ({
      ...activity,
      user_profile: userProfiles[activity.user_id] || null,
    }));

    return NextResponse.json({
      activities: activitiesWithUsers,
      total_count: totalCount,
      page,
      page_size: pageSize,
      has_next_page: endIndex < totalCount,
    });
  } catch (error) {
    console.error('Error fetching activity:', error);
    return NextResponse.json(
      { error: 'Failed to fetch activity' },
      { status: 500 }
    );
  }
}
