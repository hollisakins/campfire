import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { hashRefreshToken } from '@/lib/auth/tokens';

/**
 * POST /api/v1/auth/revoke
 *
 * Revokes a refresh token (used by CLI logout).
 *
 * Request body:
 * {
 *   token: string  // The refresh token to revoke
 * }
 *
 * Response: 200 (always, per RFC 7009 — even if token is invalid)
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { token } = body;

    if (!token || !token.startsWith('rt_')) {
      // Per RFC 7009, always return 200 even for invalid tokens
      return NextResponse.json({ revoked: true });
    }

    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    const tokenHash = hashRefreshToken(token);

    // Revoke the token directly by hash
    await supabase
      .from('refresh_tokens')
      .update({ is_revoked: true, revoked_at: new Date().toISOString() })
      .eq('token_hash', tokenHash);

    return NextResponse.json({ revoked: true });
  } catch (error) {
    console.error('Error in POST /api/v1/auth/revoke:', error);
    // Per RFC 7009, return 200 even on errors
    return NextResponse.json({ revoked: true });
  }
}
