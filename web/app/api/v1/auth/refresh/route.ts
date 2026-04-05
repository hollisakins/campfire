import { NextRequest, NextResponse } from 'next/server';
import { rotateRefreshToken } from '@/lib/auth/tokens';

/**
 * POST /api/v1/auth/refresh
 *
 * Refreshes an access token using a refresh token.
 * Implements token rotation: the old refresh token is invalidated
 * and a new one is issued.
 *
 * Request body:
 * {
 *   grant_type: "refresh_token",
 *   refresh_token: string
 * }
 *
 * Response:
 * {
 *   access_token: string,
 *   token_type: "Bearer",
 *   expires_in: number,
 *   refresh_token: string  // NEW token (old one is now invalid)
 * }
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { grant_type, refresh_token } = body;

    // Validate grant type
    if (grant_type !== 'refresh_token') {
      return NextResponse.json(
        { error: 'unsupported_grant_type', error_description: 'Invalid grant_type' },
        { status: 400 }
      );
    }

    if (!refresh_token) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'refresh_token is required' },
        { status: 400 }
      );
    }

    // Validate refresh token format
    if (!refresh_token.startsWith('rt_')) {
      return NextResponse.json(
        { error: 'invalid_grant', error_description: 'Invalid refresh token format' },
        { status: 400 }
      );
    }

    // Get client info for audit logging
    const clientIp = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim()
      || request.headers.get('x-real-ip')
      || 'unknown';
    const userAgent = request.headers.get('user-agent') || undefined;

    // Rotate the refresh token
    const result = await rotateRefreshToken(refresh_token, clientIp, userAgent);

    if (!result) {
      return NextResponse.json(
        { error: 'invalid_grant', error_description: 'Invalid or expired refresh token' },
        { status: 400 }
      );
    }

    return NextResponse.json({
      access_token: result.accessToken,
      token_type: 'Bearer',
      expires_in: result.expiresIn,
      refresh_token: result.refreshToken,
      supabase_token: result.supabaseToken,
    });
  } catch (error) {
    console.error('Error in POST /api/v1/auth/refresh:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Internal server error' },
      { status: 500 }
    );
  }
}
