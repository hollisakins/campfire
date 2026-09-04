import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { sendInspectionRequestNotification } from '@/lib/email/resend';

/**
 * GET /api/inspection-requests
 *
 * Returns the current user's most recent inspection-access request (or null).
 * Used by the profile role/permissions card to reflect a pending request.
 */
export async function GET() {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const { data, error } = await supabase
    .from('inspection_access_requests')
    .select('id, status, message, created_at, reviewed_at')
    .eq('user_id', user.id)
    .order('created_at', { ascending: false })
    .limit(1)
    .maybeSingle();

  if (error) {
    console.error('Error fetching inspection request:', error);
    return NextResponse.json({ error: 'Failed to fetch request' }, { status: 500 });
  }

  return NextResponse.json({ request: data ?? null });
}

/**
 * POST /api/inspection-requests
 *
 * Creates a pending inspection-access request for the current user and notifies
 * admins by email. A partial unique index guarantees at most one open request
 * per user; a duplicate returns 409.
 *
 * Body: { message?: string }
 */
export async function POST(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  // Already an inspector — nothing to request.
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_inspect, full_name, username, is_group_account')
    .eq('user_id', user.id)
    .single();

  if (profile?.can_inspect) {
    return NextResponse.json({ error: 'You already have inspection access' }, { status: 400 });
  }
  if (profile?.is_group_account) {
    return NextResponse.json({ error: 'Group accounts cannot request inspection access' }, { status: 403 });
  }

  let message: string | null = null;
  try {
    const body = await request.json().catch(() => ({}));
    if (typeof body?.message === 'string' && body.message.trim()) {
      message = body.message.trim().slice(0, 1000);
    }
  } catch {
    // No/invalid body is fine — message stays null.
  }

  const { data: inserted, error } = await supabase
    .from('inspection_access_requests')
    .insert({ user_id: user.id, status: 'pending', message })
    .select('id, status, message, created_at')
    .single();

  if (error) {
    // Unique-violation on the pending partial index → already requested.
    if (error.code === '23505') {
      return NextResponse.json({ error: 'You already have a pending request' }, { status: 409 });
    }
    console.error('Error creating inspection request:', error);
    return NextResponse.json({ error: 'Failed to submit request' }, { status: 500 });
  }

  // Best-effort admin notification — never block the request on email.
  try {
    await sendInspectionRequestNotification({
      full_name: profile?.full_name || 'Unknown',
      username: profile?.username || 'unknown',
      email: user.email || 'unknown',
      message,
      created_at: inserted.created_at,
    });
  } catch (err) {
    console.error('Inspection request notification failed:', err);
  }

  return NextResponse.json({ request: inserted });
}
