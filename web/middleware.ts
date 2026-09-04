// Session refresh middleware (perf T2-B, #505; decision D-B).
//
// The one job: keep the Supabase session cookie fresh so that everything
// downstream — server components (which cannot write cookies), server
// actions and route handlers — can resolve identity from the cookie with a
// local signature check (lib/auth/identity.ts) and never has to call GoTrue.
//
// Cost model: requests without an auth cookie return immediately. With one,
// supabase-js parses the cookie locally and only contacts GoTrue when the
// access token is within its 90 s expiry margin, writing the refreshed
// cookies onto both the forwarded request and the response. Identity itself
// is NOT attached to the request here (no trusted header to forge or to
// forget to strip); downstream re-verifies the token, which is ~0.1 ms.

import { NextResponse, type NextRequest } from 'next/server';
import { createServerClient } from '@supabase/ssr';

function hasSupabaseAuthCookie(request: NextRequest): boolean {
  return request.cookies
    .getAll()
    .some(c => c.name.startsWith('sb-') && c.name.includes('-auth-token'));
}

export async function middleware(request: NextRequest) {
  if (!hasSupabaseAuthCookie(request)) return NextResponse.next();

  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          // Refreshed (or cleared) session: forward to this request's
          // downstream readers AND to the browser.
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options)
          );
        },
      },
    }
  );

  // Local parse; network only for a near-expiry refresh (see header). The
  // session's user object is deliberately not read — it is unverified here.
  await supabase.auth.getSession();

  return response;
}

export const config = {
  matcher: [
    // Everything except Next internals and static assets.
    '/((?!_next/static|_next/image|favicon\\.ico|icon\\.svg|apple-icon\\.png|.*\\.(?:png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf|map)$).*)',
  ],
};
