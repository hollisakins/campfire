import { NextRequest } from 'next/server';

/**
 * GET /api/invite
 *
 * Redirect proxy for Supabase invite emails.
 *
 * This allows invite emails to use campfire.hollisakins.com URLs instead of
 * the raw Supabase URL, improving email deliverability by matching the sending domain.
 *
 * Flow:
 * 1. Email contains: https://campfire.hollisakins.com/api/invite?token=...
 * 2. This route redirects to: https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify?token=...
 * 3. Supabase validates and redirects back to: https://campfire.hollisakins.com/login#access_token=...
 * 4. LoginForm processes tokens and redirects to /welcome
 */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;

  const token = searchParams.get('token');
  const type = searchParams.get('type') || 'invite';

  // Validate token exists
  if (!token) {
    return new Response('Invalid invite link: missing token', { status: 400 });
  }

  // Build the redirect_to URL (where Supabase should send the user after verification)
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'https://campfire.hollisakins.com';
  const redirectTo = searchParams.get('redirect_to') || `${appUrl}/login`;

  // Build Supabase verification URL
  // Email templates use {{ .TokenHash }}, so pass as token_hash (not token, which is for raw OTPs)
  const supabaseUrl = new URL('https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify');
  supabaseUrl.searchParams.set('token_hash', token);
  supabaseUrl.searchParams.set('type', type);
  supabaseUrl.searchParams.set('redirect_to', redirectTo);

  // Redirect to Supabase for token verification
  return Response.redirect(supabaseUrl.toString(), 302);
}
