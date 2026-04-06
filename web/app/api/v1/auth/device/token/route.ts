import { NextRequest, NextResponse } from 'next/server';
import { checkDeviceCodeStatus, consumeDeviceCode } from '@/lib/auth/device-flow';
import { issueTokens } from '@/lib/auth/tokens';

// Track polling attempts per device code to enforce rate limiting
const pollingAttempts = new Map<string, { lastAttempt: number; interval: number }>();

// Clean up old entries periodically (every 5 minutes)
setInterval(() => {
  const now = Date.now();
  for (const [key, value] of pollingAttempts.entries()) {
    // Remove entries older than 20 minutes
    if (now - value.lastAttempt > 20 * 60 * 1000) {
      pollingAttempts.delete(key);
    }
  }
}, 5 * 60 * 1000);

/**
 * POST /api/v1/auth/device/token
 *
 * Polls for token after device authorization.
 * Called repeatedly by the client until authorization completes.
 *
 * Request body:
 * {
 *   grant_type: "urn:ietf:params:oauth:grant-type:device_code",
 *   device_code: string,
 *   client_id?: string
 * }
 *
 * Response (success):
 * {
 *   access_token: string,
 *   token_type: "Bearer",
 *   expires_in: number,
 *   refresh_token: string
 * }
 *
 * Response (pending):
 * { error: "authorization_pending" }
 *
 * Response (errors):
 * { error: "slow_down" | "expired_token" | "access_denied" | "invalid_grant" }
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { grant_type, device_code, device_name } = body;

    // Validate grant type
    if (grant_type !== 'urn:ietf:params:oauth:grant-type:device_code') {
      return NextResponse.json(
        { error: 'unsupported_grant_type', error_description: 'Invalid grant_type' },
        { status: 400 }
      );
    }

    if (!device_code) {
      return NextResponse.json(
        { error: 'invalid_request', error_description: 'device_code is required' },
        { status: 400 }
      );
    }

    // Check rate limiting
    const now = Date.now();
    const pollInfo = pollingAttempts.get(device_code);
    const minInterval = 5000; // 5 seconds in milliseconds

    if (pollInfo) {
      const timeSinceLastAttempt = now - pollInfo.lastAttempt;
      const requiredInterval = pollInfo.interval;

      if (timeSinceLastAttempt < requiredInterval) {
        // Client is polling too fast, increase required interval
        const newInterval = Math.min(pollInfo.interval + 5000, 30000); // Max 30 seconds
        pollingAttempts.set(device_code, { lastAttempt: now, interval: newInterval });

        return NextResponse.json(
          {
            error: 'slow_down',
            error_description: 'Polling too fast. Please slow down.',
            interval: Math.ceil(newInterval / 1000),
          },
          { status: 400 }
        );
      }
    }

    // Update polling tracker
    pollingAttempts.set(device_code, {
      lastAttempt: now,
      interval: pollInfo?.interval || minInterval,
    });

    // Check device code status
    const status = await checkDeviceCodeStatus(device_code);

    switch (status.status) {
      case 'not_found':
        return NextResponse.json(
          { error: 'invalid_grant', error_description: 'Invalid device code' },
          { status: 400 }
        );

      case 'expired':
        // Clean up polling tracker
        pollingAttempts.delete(device_code);
        return NextResponse.json(
          { error: 'expired_token', error_description: 'Device code has expired' },
          { status: 400 }
        );

      case 'denied':
        // Clean up polling tracker
        pollingAttempts.delete(device_code);
        return NextResponse.json(
          { error: 'access_denied', error_description: 'User denied authorization' },
          { status: 400 }
        );

      case 'pending':
        return NextResponse.json(
          { error: 'authorization_pending', error_description: 'User has not yet authorized' },
          { status: 400 }
        );

      case 'authorized':
        // User has authorized! Consume the device code and issue tokens
        const userId = await consumeDeviceCode(device_code);

        if (!userId) {
          return NextResponse.json(
            { error: 'server_error', error_description: 'Failed to consume device code' },
            { status: 500 }
          );
        }

        // Clean up polling tracker
        pollingAttempts.delete(device_code);

        // Get client info for token storage
        const clientIp = request.headers.get('x-forwarded-for')?.split(',')[0]?.trim()
          || request.headers.get('x-real-ip')
          || 'unknown';
        const userAgent = request.headers.get('user-agent') || undefined;

        // Issue tokens
        const tokens = await issueTokens(
          userId,
          device_name || 'Python Client',
          clientIp,
          userAgent
        );

        return NextResponse.json({
          access_token: tokens.accessToken,
          token_type: tokens.tokenType,
          expires_in: tokens.expiresIn,
          refresh_token: tokens.refreshToken,
          supabase_token: tokens.supabaseToken,
          supabase_url: process.env.NEXT_PUBLIC_SUPABASE_URL,
          supabase_anon_key: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
        });

      default:
        return NextResponse.json(
          { error: 'server_error', error_description: 'Unknown device code status' },
          { status: 500 }
        );
    }
  } catch (error) {
    console.error('Error in POST /api/v1/auth/device/token:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Internal server error' },
      { status: 500 }
    );
  }
}
