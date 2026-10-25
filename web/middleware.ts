// Session refresh middleware (perf T2-B, #505; decision D-B), plus the
// data-release request header (P6c-1).
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
//
// Release browsing: `?dr=` is parsed HERE and nowhere else
// (lib/dr/tag.ts drRequestDecision). One well-formed tag on a release-capable
// path is copied onto the forwarded request as the `x-campfire-dr` header,
// which the server Supabase clients pass to PostgREST; a repeated or
// malformed `dr` is a 404; a live-only path (map, tags, inspection, profile,
// admin, auth, home, docs, cutouts) is redirected to the same URL without
// the parameter; an `/api/*` write under a release is a 409 before its
// handler; `/api/v1` is never stamped (the bearer gate owns it); with
// NEXT_PUBLIC_DR_PAGES off the parameter is ignored. Any incoming
// `x-campfire-dr` is dropped first: the header is derived from the URL,
// never trusted from the client.

import { NextResponse, type NextRequest } from 'next/server';
import { createServerClient } from '@supabase/ssr';
import { DR_HEADER, drRequestDecision } from '@/lib/dr/tag';
import { drPagesMode } from '@/lib/dr-pages';
import { publicSupabaseCookieName, serverSupabaseUrl } from '@/lib/supabase/url';

function hasSupabaseAuthCookie(request: NextRequest): boolean {
  return request.cookies
    .getAll()
    .some(c => c.name.startsWith('sb-') && c.name.includes('-auth-token'));
}

/** The release part of middleware, separable for tests. */
export function applyDrDecision(request: NextRequest): NextResponse | null {
  request.headers.delete(DR_HEADER);
  const decision = drRequestDecision(request.nextUrl, drPagesMode(), request.method);
  switch (decision.kind) {
    case 'release':
      request.headers.set(DR_HEADER, decision.tag);
      return null;
    case 'strip':
      return NextResponse.redirect(new URL(decision.location, request.nextUrl), 307);
    case 'not-found':
      return NextResponse.rewrite(new URL('/not-found', request.nextUrl), { status: 404 });
    case 'read-only':
      return NextResponse.json({ error: 'read-only under a data release' }, { status: 409 });
    default:
      return null;
  }
}

export async function middleware(request: NextRequest) {
  const early = applyDrDecision(request);
  if (early) return early;

  if (!hasSupabaseAuthCookie(request)) return NextResponse.next({ request });

  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    serverSupabaseUrl(),
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      // Named after the public url the browser wrote it with (see url.ts).
      cookieOptions: { name: publicSupabaseCookieName() },
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
