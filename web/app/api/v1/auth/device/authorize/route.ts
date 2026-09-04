import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { authorizeDeviceCode, denyDeviceCode } from '@/lib/auth/device-flow';
import { getLinkScope } from '@/lib/api-helpers';

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
    const { user } = await getRequestIdentity();

    if (!user) {
      return NextResponse.json(
        { error: 'unauthorized', error_description: 'You must be logged in' },
        { status: 401 }
      );
    }

    // Share-link sessions must never mint durable API credentials — a device
    // token would outlive revocation and bypass the link's RLS scoping.
    // authorize_device_code() also refuses link accounts server-side; this is
    // the legible error for the UI.
    if (await getLinkScope(user.id)) {
      return NextResponse.json(
        { error: 'forbidden', error_description: 'Share-link sessions cannot authorize API access' },
        { status: 403 }
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
