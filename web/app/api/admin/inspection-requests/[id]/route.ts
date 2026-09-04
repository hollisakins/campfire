import { NextRequest, NextResponse } from 'next/server';
import { invalidateAccessContext } from '@/lib/auth/access-context';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { createServiceClient } from '@/lib/supabase/server';
import { sendInspectionDecisionNotification } from '@/lib/email/resend';

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
  const { user } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const serviceClient = createServiceClient();

  if (!(await isAdminUser(user.id))) {
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
    invalidateAccessContext(req.user_id);
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

  // Tell the requester the outcome (the profile page promises this email).
  // Best-effort: a failed email never fails the review itself.
  try {
    const [{ data: requesterProfile }, { data: requesterAuth }] = await Promise.all([
      serviceClient
        .from('user_profiles')
        .select('full_name')
        .eq('user_id', req.user_id)
        .single(),
      serviceClient.auth.admin.getUserById(req.user_id),
    ]);

    const requesterEmail = requesterAuth?.user?.email;
    if (requesterEmail) {
      await sendInspectionDecisionNotification({
        email: requesterEmail,
        full_name: requesterProfile?.full_name || 'there',
        approved: action === 'approve',
      });
    } else {
      console.warn('No email found for inspection requester', req.user_id);
    }
  } catch (err) {
    console.error('Error sending inspection decision notification:', err);
  }

  return NextResponse.json({ success: true });
}
