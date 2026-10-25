/**
 * The Supabase API url the SERVER talks to.
 *
 * `NEXT_PUBLIC_SUPABASE_URL` is inlined at build time into the client and the
 * server bundles alike, so an env file cannot change where server code
 * connects. `SUPABASE_URL` is read at runtime and lets the self-hosted stack
 * point the web container at Kong over the Docker network
 * (`http://kong:8000`) instead of back out through Cloudflare, whose 100 s
 * edge timeout is shorter than the 120 s the sync RPCs may run (ops/README.md,
 * Phase 2). Browser code keeps using the public url it was built with.
 *
 * No `server-only` import: middleware runs in the edge runtime, which still
 * reads runtime env but rejects that marker.
 */
export function serverSupabaseUrl(): string {
  return process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL!;
}

/**
 * The session cookie name, derived from the PUBLIC url the way supabase-js
 * does by default (`sb-<first hostname label>-auth-token`). The browser client
 * is built with the public url and writes that cookie; a server client created
 * with the internal url would otherwise look for `sb-kong-auth-token`, find
 * nothing, and treat every signed-in user as anonymous. Both server-side
 * cookie clients pass this explicitly so the transport url never leaks into
 * the cookie name.
 */
export function publicSupabaseCookieName(): string | undefined {
  // Unset only in unit tests that mock the client; supabase-js then applies
  // its own default (an undefined cookieOptions.name is ignored by @supabase/ssr).
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  return url ? supabaseCookieNameFor(url) : undefined;
}

export function supabaseCookieNameFor(url: string): string {
  return `sb-${new URL(url).hostname.split('.')[0]}-auth-token`;
}
