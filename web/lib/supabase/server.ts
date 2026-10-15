// Server-side Supabase setup

import { cache } from 'react';
import { createServerClient } from '@supabase/ssr';
import type { SupabaseClient } from '@supabase/supabase-js';
import { cookies } from 'next/headers';
import { notFound } from 'next/navigation';
import { requestDrTag } from '@/lib/dr/server';
import { DR_HEADER } from '@/lib/dr/tag';

// The service-role client lives in ./service (no next/headers import) and is
// re-exported here for the many existing call sites.
export { createServiceClient } from './service';

/**
 * Confirm the database is browsing the release this request asked for
 * (P6c-1 rule: a release request never falls back to live silently). One
 * round trip per request and tag (request-memoized): `dr_browse_tag()` must
 * echo the tag the pre-request hook set from the header. A `PT404` is the
 * hook refusing the tag (unknown, or not visible to this caller) and a
 * `42501` is an anonymous caller (no EXECUTE on the function; anonymous
 * never browses a release): both are the page's 404. Anything else — the
 * hook not registered, a stale PostgREST config, an error — is a 500,
 * never a live render.
 */
export async function confirmBrowseTag(supabase: SupabaseClient, tag: string): Promise<void> {
  const { data, error } = await supabase.rpc('dr_browse_tag');
  if (error) {
    if (error.code === 'PT404' || error.code === '42501') notFound();
    throw new Error(`data release ${tag}: ${error.message}`);
  }
  if (data !== tag) {
    throw new Error(`data release ${tag}: the database answered ${JSON.stringify(data)} (pre-request hook not active?)`);
  }
}

// Per request: the tags already confirmed (React cache() is request-scoped
// in server components and route handlers; elsewhere it is a plain Map).
const confirmedTags = cache(() => new Map<string, Promise<void>>());

async function confirmOnce(supabase: SupabaseClient, tag: string): Promise<void> {
  const pending = confirmedTags();
  let p = pending.get(tag);
  if (!p) {
    p = confirmBrowseTag(supabase, tag);
    pending.set(tag, p);
  }
  await p;
}

export const createClient = async () => {
  const cookieStore = await cookies();
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
  // Release browsing (P6c-1): the header middleware stamped from `?dr=`
  // rides on every PostgREST call of this client.
  const drTag = await requestDrTag();

  const client = createServerClient(supabaseUrl, supabaseAnonKey, {
    ...(drTag ? { global: { headers: { [DR_HEADER]: drTag } } } : {}),
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options)
          );
        } catch {
          // Called from a Server Component, which cannot write cookies.
          // Safe to ignore: middleware.ts refreshes the session cookie
          // before the render, so this only fires on a stale token that
          // middleware did not see (perf T2-B, #505).
        }
      },
    },
  });
  if (drTag) await confirmOnce(client, drTag);
  return client;
};
