import { NextRequest, NextResponse } from 'next/server';
import { createDeviceAuthorization } from '@/lib/auth/device-flow';

/**
 * POST /api/v1/auth/device
 *
 * Initiates the OAuth 2.0 Device Authorization Grant flow.
 * Returns a device_code and user_code for the client to use.
 *
 * Response:
 * {
 *   device_code: string,      // Secret for polling (don't show to user)
 *   user_code: string,        // Show to user (e.g., "WDJB-MJPQ")
 *   verification_uri: string, // URL for user to visit
 *   verification_uri_complete: string, // URL with code pre-filled
 *   expires_in: number,       // Seconds until expiration (900)
 *   interval: number          // Minimum polling interval in seconds (5)
 * }
 */
export async function POST(request: NextRequest) {
  try {
    // Get client info for audit logging
    const clientIp = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim()
      || request.headers.get('x-real-ip')
      || 'unknown';
    const userAgent = request.headers.get('user-agent') || undefined;

    // Determine the base URL for verification
    const host = request.headers.get('host') || 'campfire.hollisakins.com';
    const protocol = host.includes('localhost') ? 'http' : 'https';
    const verificationUri = `${protocol}://${host}/cli-auth`;

    const result = await createDeviceAuthorization(
      verificationUri,
      clientIp,
      userAgent
    );

    return NextResponse.json({
      device_code: result.deviceCode,
      user_code: result.userCode,
      verification_uri: result.verificationUri,
      verification_uri_complete: result.verificationUriComplete,
      expires_in: result.expiresIn,
      interval: result.interval,
    });
  } catch (error) {
    console.error('Error in POST /api/v1/auth/device:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Failed to create device authorization' },
      { status: 500 }
    );
  }
}
