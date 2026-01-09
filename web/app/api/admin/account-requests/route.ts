import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * GET /api/admin/account-requests
 *
 * List all account requests (admin only)
 * Query params:
 *   - status: optional filter ('pending' | 'approved' | 'rejected')
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

  // Use service client for all data queries (bypasses RLS)
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

  try {
    const searchParams = request.nextUrl.searchParams;
    const statusFilter = searchParams.get('status');

    // Build query
    let query = serviceClient
      .from('account_requests')
      .select('*')
      .order('created_at', { ascending: false });

    // Apply status filter if provided
    if (statusFilter && ['pending', 'approved', 'rejected'].includes(statusFilter)) {
      query = query.eq('status', statusFilter);
    }

    const { data: requests, error } = await query;

    if (error) {
      console.error('Error fetching account requests:', error);
      return NextResponse.json(
        { error: 'Failed to fetch account requests' },
        { status: 500 }
      );
    }

    // Get reviewer names for requests that have been reviewed
    const reviewerIds = [...new Set(requests?.map(r => r.reviewed_by).filter(Boolean))];
    let reviewerProfiles: Record<string, string> = {};

    if (reviewerIds.length > 0) {
      const { data: profiles } = await serviceClient
        .from('user_profiles')
        .select('user_id, full_name')
        .in('user_id', reviewerIds);

      reviewerProfiles = (profiles || []).reduce((acc, p) => {
        acc[p.user_id] = p.full_name;
        return acc;
      }, {} as Record<string, string>);
    }

    // Add reviewer names to requests
    const requestsWithNames = (requests || []).map(req => ({
      ...req,
      reviewed_by_name: req.reviewed_by ? reviewerProfiles[req.reviewed_by] || 'Unknown' : null,
    }));

    // Get counts by status
    const counts = {
      total: requests?.length || 0,
      pending: requests?.filter(r => r.status === 'pending').length || 0,
      approved: requests?.filter(r => r.status === 'approved').length || 0,
      rejected: requests?.filter(r => r.status === 'rejected').length || 0,
    };

    return NextResponse.json({
      requests: requestsWithNames,
      counts,
    });

  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json(
      { error: 'Failed to fetch account requests' },
      { status: 500 }
    );
  }
}
