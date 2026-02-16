import { NextRequest } from 'next/server';

/**
 * GET /api/auth/email-change
 *
 * Redirect proxy for Supabase email change confirmation emails.
 *
 * This allows email change confirmation emails to use campfire.hollisakins.com URLs
 * instead of the raw Supabase URL, improving email deliverability and brand trust.
 *
 * Flow:
 * 1. Email contains: https://campfire.hollisakins.com/api/auth/email-change?token={{ .TokenHash }}&type=email_change
 * 2. This route redirects to: https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify?token=...
 * 3. Supabase validates and redirects back to: https://campfire.hollisakins.com/profile/email-changed?code=...
 * 4. Confirmation page shows success message
 */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;

  const token = searchParams.get('token');
  const type = searchParams.get('type') || 'email_change';

  // Validate token exists
  if (!token) {
    return new Response('Invalid email change link: missing token', { status: 400 });
  }

  // Validate type is email_change
  if (type !== 'email_change') {
    return new Response('Invalid email change link: wrong token type', { status: 400 });
  }

  // Build the redirect_to URL (where Supabase should send the user after verification)
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'https://campfire.hollisakins.com';
  const redirectTo = `${appUrl}/profile/email-changed`;

  // Build Supabase verification URL
  // Email templates use {{ .TokenHash }}, so pass as token_hash (not token, which is for raw OTPs)
  const supabaseUrl = new URL('https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify');
  supabaseUrl.searchParams.set('token_hash', token);
  supabaseUrl.searchParams.set('type', type);
  supabaseUrl.searchParams.set('redirect_to', redirectTo);

  // Redirect to Supabase for token verification
  return Response.redirect(supabaseUrl.toString(), 302);
}
