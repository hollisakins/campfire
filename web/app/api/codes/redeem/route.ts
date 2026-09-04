import { NextRequest, NextResponse } from 'next/server';
import { invalidateAccessContext } from '@/lib/auth/access-context';
import { getRequestIdentity } from '@/lib/auth/identity';

/**
 * POST /api/codes/redeem
 *
 * Redeems an access code for the current user.
 * Body: { code: string }
 *
 * The entire redemption (validation, program grants, redemption record,
 * use_count increment) runs inside the redeem_access_code() SECURITY DEFINER
 * RPC, so access codes are never readable by non-admins and max_uses holds
 * under concurrent redemptions.
 */

const STATUS_RESPONSES: Record<string, { error: string; http: number }> = {
  unauthenticated: { error: 'Authentication required', http: 401 },
  group_account: { error: 'Group accounts cannot redeem access codes', http: 403 },
  invalid: { error: 'Invalid access code', http: 404 },
  expired: { error: 'This access code has expired', http: 410 },
  exhausted: { error: 'This access code has reached its maximum uses', http: 410 },
  already_redeemed: { error: 'You have already redeemed this code', http: 409 },
  no_programs: { error: 'This code does not grant access to any programs', http: 400 },
};

export async function POST(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  try {
    const body = await request.json();
    const { code } = body;

    if (!code || typeof code !== 'string') {
      return NextResponse.json(
        { error: 'Access code is required' },
        { status: 400 }
      );
    }

    // Normalize code (uppercase, trim)
    const normalizedCode = code.trim().toUpperCase();

    const { data: result, error: rpcError } = await supabase.rpc(
      'redeem_access_code',
      { p_code: normalizedCode }
    );

    if (rpcError || !result?.status) {
      console.error('Error redeeming code:', rpcError);
      return NextResponse.json(
        { error: 'Failed to redeem access code' },
        { status: 500 }
      );
    }

    if (result.status !== 'ok') {
      const mapped = STATUS_RESPONSES[result.status];
      if (!mapped) {
        console.error('Unexpected redemption status:', result.status);
        return NextResponse.json(
          { error: 'Failed to redeem access code' },
          { status: 500 }
        );
      }
      return NextResponse.json({ error: mapped.error }, { status: mapped.http });
    }

    // New grants: drop this instance's memoized access set so the next
    // request from this user sees them at once (#505).
    invalidateAccessContext(user.id);

    return NextResponse.json({
      success: true,
      message: result.grants_all_programs
        ? 'Access granted to all programs'
        : `Access granted to ${result.programs_granted} program(s)`,
      programs_granted: result.programs_granted,
    });

  } catch (error) {
    console.error('Error redeeming code:', error);
    return NextResponse.json(
      { error: 'Failed to redeem access code' },
      { status: 500 }
    );
  }
}
