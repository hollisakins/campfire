import { NextRequest, NextResponse } from 'next/server';
import { getDeviceCodeByUserCode } from '@/lib/auth/device-flow';

/**
 * POST /api/v1/auth/device/validate
 *
 * Validates a user code from the verification page.
 * Called by the web UI before showing the authorize/deny buttons.
 *
 * Request body:
 * { user_code: string }
 *
 * Response (success):
 * { valid: true }
 *
 * Response (errors):
 * { error: "not_found" | "expired", error_description: string }
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { user_code } = body;

    if (!user_code) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'user_code is required' },
        { status: 400 }
      );
    }

    const result = await getDeviceCodeByUserCode(user_code);

    if (!result || !result.exists) {
      return NextResponse.json(
        { error: 'not_found', error_description: 'Invalid code' },
        { status: 404 }
      );
    }

    if (result.isExpired) {
      return NextResponse.json(
        { error: 'expired', error_description: 'Code has expired' },
        { status: 400 }
      );
    }

    if (!result.isPending) {
      return NextResponse.json(
        { error: 'already_used', error_description: 'Code has already been used' },
        { status: 400 }
      );
    }

    return NextResponse.json({ valid: true });
  } catch (error) {
    console.error('Error in POST /api/v1/auth/device/validate:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Internal server error' },
      { status: 500 }
    );
  }
}
