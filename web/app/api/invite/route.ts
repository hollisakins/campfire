import { NextRequest } from 'next/server';

/**
 * GET /api/invite
 *
 * Redirect proxy for Supabase invite emails.
 *
 * This allows invite emails to use campfire.hollisakins.com URLs instead of
 * the raw Supabase URL, improving email deliverability by matching the sending domain.
 *
 * Instead of an immediate 302 redirect, this serves an interstitial HTML page
 * with a button. This prevents university/corporate email security scanners
 * (SafeLinks, Proofpoint, etc.) from pre-fetching and consuming the token
 * before the user clicks the link.
 *
 * Flow:
 * 1. Email contains: https://campfire.hollisakins.com/api/invite?token=...
 * 2. This route serves an interstitial page with an "Accept Invitation" button
 * 3. User clicks button → https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify?token=...
 * 4. Supabase validates and redirects back to: https://campfire.hollisakins.com/login#access_token=...
 * 5. LoginForm processes tokens and redirects to /welcome
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
  const supabaseUrl = new URL('https://puyczxwyuzpnqvpachip.supabase.co/auth/v1/verify');
  supabaseUrl.searchParams.set('token', token);
  supabaseUrl.searchParams.set('type', type);
  supabaseUrl.searchParams.set('redirect_to', redirectTo);

  const verifyUrl = supabaseUrl.toString();

  // Serve an interstitial page instead of an immediate redirect.
  // Email security scanners (SafeLinks, Proofpoint) pre-fetch URLs via GET
  // but won't click buttons or execute JavaScript, so the token is preserved
  // until the real user interacts with the page.
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Accept Invitation - CAMPFIRE</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background-color: #f1f5f9;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 20px;
    }
    .card {
      background: #ffffff;
      border-radius: 12px;
      padding: 48px 40px;
      max-width: 440px;
      width: 100%;
      text-align: center;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    }
    .icon {
      margin-bottom: 24px;
    }
    h1 {
      color: #0f172a;
      font-size: 22px;
      font-weight: 600;
      margin-bottom: 12px;
    }
    p {
      color: #475569;
      font-size: 15px;
      line-height: 1.6;
      margin-bottom: 32px;
    }
    .btn {
      display: inline-block;
      background-color: #c127d3;
      color: #ffffff;
      padding: 14px 36px;
      text-decoration: none;
      border-radius: 12px;
      font-weight: 500;
      font-size: 16px;
      transition: background-color 0.15s;
    }
    .btn:hover { background-color: #a620b3; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">
      <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#c127d3" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>
      </svg>
    </div>
    <h1>Welcome to CAMPFIRE</h1>
    <p>Click the button below to accept your invitation and set up your account.</p>
    <a href="${verifyUrl}" class="btn">Accept Invitation</a>
  </div>
</body>
</html>`;

  return new Response(html, {
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}
