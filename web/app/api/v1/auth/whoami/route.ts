import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { authenticateApiRequest } from '@/lib/api-auth';
import { accessFingerprint } from '@/lib/server/access-fingerprint';
import { fetchLatestPublishedRelease } from '@/lib/server/dr-exports';

/**
 * GET /api/v1/auth/whoami
 *
 * Returns information about the currently authenticated user.
 * Accepts both API keys and JWT access tokens.
 *
 * Response:
 * {
 *   user_id: string,
 *   email: string,
 *   full_name?: string,
 *   is_admin: boolean,
 *   created_at: string,
 *   latest_dr: { tag, doi, published_at } | null,   // the newest published data release (P5-3)
 *   access_fingerprint: string                      // sha256 of the caller's program set
 * }
 *
 * The two P5-3 fields live here, on the authenticated endpoint, and not on
 * the public, hour-cached /api/v1/version: the fingerprint is per caller,
 * and `campfire sync` reads both at its tail (decision 7 of the DR plan).
 */
export async function GET(request: NextRequest) {
  // Validate authentication (API key or JWT) and resolve the access context
  const principal = await authenticateApiRequest(request);

  if (!principal) {
    return NextResponse.json(
      { error: 'unauthorized', error_description: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }
  const userId = principal.userId;

  try {
    // Create Supabase client with service role
    const supabase = createServiceClient();

    const [userRes, profileRes, latest] = await Promise.all([
      supabase.auth.admin.getUserById(userId),
      supabase
        .from('user_profiles')
        .select('full_name, is_admin, created_at')
        .eq('user_id', userId)
        .single(),
      fetchLatestPublishedRelease(supabase),
    ]);

    if (userRes.error || !userRes.data.user) {
      return NextResponse.json(
        { error: 'not_found', error_description: 'User not found' },
        { status: 404 }
      );
    }
    const profile = profileRes.data;

    return NextResponse.json(
      {
        user_id: userId,
        email: userRes.data.user.email,
        full_name: profile?.full_name || null,
        is_admin: profile?.is_admin || false,
        created_at: profile?.created_at || userRes.data.user.created_at,
        latest_dr: latest ? { tag: latest.tag, doi: latest.doi, published_at: latest.published_at } : null,
        access_fingerprint: accessFingerprint(principal.access),
      },
      { headers: { 'Cache-Control': 'private, no-store' } },
    );
  } catch (error) {
    console.error('Error in GET /api/v1/auth/whoami:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Internal server error' },
      { status: 500 }
    );
  }
}
