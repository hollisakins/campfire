import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { authorizeDeviceCode, denyDeviceCode } from '@/lib/auth/device-flow';

/**
 * POST /api/v1/auth/device/authorize
 *
 * Authorizes or denies a device code.
 * Called by the web UI when user clicks authorize/deny.
 * Requires authenticated session (cookie-based).
 *
 * Request body:
 * {
 *   user_code: string,
 *   action: "authorize" | "deny"
 * }
 *
 * Response (success):
 * { success: true }
 *
 * Response (errors):
 * { error: string, error_description: string }
 */
export async function POST(request: NextRequest) {
  try {
    // Get authenticated user from session
    const supabase = await createClient();
    const { data: { user }, error: authError } = await supabase.auth.getUser();

    if (authError || !user) {
      return NextResponse.json(
        { error: 'unauthorized', error_description: 'You must be logged in' },
        { status: 401 }
      );
    }

    const body = await request.json();
    const { user_code, action } = body;

    if (!user_code) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'user_code is required' },
        { status: 400 }
      );
    }

    if (action !== 'authorize' && action !== 'deny') {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'action must be "authorize" or "deny"' },
        { status: 400 }
      );
    }

    let success: boolean;

    if (action === 'authorize') {
      success = await authorizeDeviceCode(user_code, user.id);
    } else {
      success = await denyDeviceCode(user_code);
    }

    if (!success) {
      return NextResponse.json(
        { error: 'failed', error_description: 'Failed to process authorization. Code may be expired or invalid.' },
        { status: 400 }
      );
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Error in POST /api/v1/auth/device/authorize:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Internal server error' },
      { status: 500 }
    );
  }
}
