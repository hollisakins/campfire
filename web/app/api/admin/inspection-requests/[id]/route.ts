import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * PATCH /api/admin/inspection-requests/[id]
 *
 * Review an inspection-access request (admin only).
 * Body: { action: 'approve' | 'reject' }
 *
 * Approving flips the requester's user_profiles.can_inspect to true.
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
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

  const body = await request.json().catch(() => ({}));
  const action = body?.action;
  if (action !== 'approve' && action !== 'reject') {
    return NextResponse.json({ error: 'Invalid action' }, { status: 400 });
  }

  // Load the request to find the requester.
  const { data: req, error: fetchError } = await serviceClient
    .from('inspection_access_requests')
    .select('id, user_id, status')
    .eq('id', id)
    .single();

  if (fetchError || !req) {
    return NextResponse.json({ error: 'Request not found' }, { status: 404 });
  }
  if (req.status !== 'pending') {
    return NextResponse.json({ error: 'Request has already been reviewed' }, { status: 409 });
  }

  // Grant inspection access on approval.
  if (action === 'approve') {
    const { error: grantError } = await serviceClient
      .from('user_profiles')
      .update({ can_inspect: true })
      .eq('user_id', req.user_id);

    if (grantError) {
      console.error('Error granting can_inspect:', grantError);
      return NextResponse.json({ error: 'Failed to grant access' }, { status: 500 });
    }
  }

  const { error: updateError } = await serviceClient
    .from('inspection_access_requests')
    .update({
      status: action === 'approve' ? 'approved' : 'rejected',
      reviewed_at: new Date().toISOString(),
      reviewed_by: user.id,
    })
    .eq('id', id);

  if (updateError) {
    console.error('Error updating inspection request:', updateError);
    return NextResponse.json({ error: 'Failed to update request' }, { status: 500 });
  }

  return NextResponse.json({ success: true });
}
