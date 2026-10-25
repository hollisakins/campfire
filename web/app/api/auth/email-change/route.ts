import { NextRequest } from 'next/server';

/**
 * GET /api/auth/email-change
 *
 * Redirect proxy for Supabase email change confirmation emails.
 *
 * This allows email change confirmation emails to use campfire-jwst.org URLs
 * instead of the raw Supabase URL, improving email deliverability and brand trust.
 *
 * Flow:
 * 1. Email contains: https://campfire-jwst.org/api/auth/email-change?token={{ .TokenHash }}&type=email_change
 * 2. This route redirects to: $NEXT_PUBLIC_SUPABASE_URL/auth/v1/verify?token=... (the browser follows it, so the PUBLIC api url, never the internal one)
 * 3. Supabase validates and redirects back to: https://campfire-jwst.org/profile/email-changed?code=...
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
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'https://campfire-jwst.org';
  const redirectTo = `${appUrl}/profile/email-changed`;

  // Build Supabase verification URL
  const supabaseUrl = new URL('/auth/v1/verify', process.env.NEXT_PUBLIC_SUPABASE_URL!);
  supabaseUrl.searchParams.set('token', token);
  supabaseUrl.searchParams.set('type', type);
  supabaseUrl.searchParams.set('redirect_to', redirectTo);

  // Redirect to Supabase for token verification
  return Response.redirect(supabaseUrl.toString(), 302);
}
