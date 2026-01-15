import { NextRequest } from 'next/server';

/**
 * GET /api/auth/password-reset
 *
 * Redirect proxy for Supabase password reset emails.
 *
 * This allows password reset emails to use campfire.hollisakins.com URLs instead of
 * the raw Supabase URL, improving email deliverability and brand trust.
 *
 * Flow:
 * 1. Email contains: https://campfire.hollisakins.com/api/auth/password-reset?token={{ .TokenHash }}&type=recovery
 * 2. This route redirects to: https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify?token=...
 * 3. Supabase validates and redirects back to: https://campfire.hollisakins.com/reset-password#access_token=...
 * 4. ResetPasswordForm processes tokens and allows user to set new password
 */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;

  const token = searchParams.get('token');
  const type = searchParams.get('type') || 'recovery';

  // Validate token exists
  if (!token) {
    return new Response('Invalid reset link: missing token', { status: 400 });
  }

  // Validate type is recovery
  if (type !== 'recovery') {
    return new Response('Invalid reset link: wrong token type', { status: 400 });
  }

  // Build the redirect_to URL (where Supabase should send the user after verification)
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'https://campfire.hollisakins.com';
  const redirectTo = `${appUrl}/reset-password`;

  // Build Supabase verification URL
  const supabaseUrl = new URL('https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify');
  supabaseUrl.searchParams.set('token', token);
  supabaseUrl.searchParams.set('type', type);
  supabaseUrl.searchParams.set('redirect_to', redirectTo);

  // Redirect to Supabase for token verification
  return Response.redirect(supabaseUrl.toString(), 302);
}
