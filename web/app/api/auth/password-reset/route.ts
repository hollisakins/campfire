import { NextRequest } from 'next/server';

/**
 * GET /api/auth/password-reset
 *
 * Redirect proxy for Supabase password reset emails.
 *
 * This allows password reset emails to use campfire-jwst.org URLs instead of
 * the raw Supabase URL, improving email deliverability and brand trust.
 *
 * Flow:
 * 1. Email contains: https://campfire-jwst.org/api/auth/password-reset?token={{ .TokenHash }}&type=recovery
 * 2. This route redirects to: $NEXT_PUBLIC_SUPABASE_URL/auth/v1/verify?token=... (the browser follows it, so the PUBLIC api url, never the internal one)
 * 3. Supabase validates and redirects back to: https://campfire-jwst.org/reset-password?code=...
 *    (PKCE; the browser client exchanges the code), or #access_token=... for the implicit flow,
 *    or ?error=access_denied&error_code=otp_expired when the single-use token was already used
 * 4. ResetPasswordForm settles the link from the auth client's events (lib/auth/auth-link.ts)
 *    and lets the user set a new password
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
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'https://campfire-jwst.org';
  const redirectTo = `${appUrl}/reset-password`;

  // Build Supabase verification URL
  const supabaseUrl = new URL('/auth/v1/verify', process.env.NEXT_PUBLIC_SUPABASE_URL!);
  supabaseUrl.searchParams.set('token', token);
  supabaseUrl.searchParams.set('type', type);
  supabaseUrl.searchParams.set('redirect_to', redirectTo);

  // Redirect to Supabase for token verification
  return Response.redirect(supabaseUrl.toString(), 302);
}
