// Server-side Supabase setup

import { cache } from 'react';
import { createServerClient } from '@supabase/ssr';
import type { SupabaseClient } from '@supabase/supabase-js';
import { cookies } from 'next/headers';
import { notFound } from 'next/navigation';
import { checkBrowseTag } from '@/lib/dr/confirm';
import { requestDrTag } from '@/lib/dr/server';
import { DR_HEADER } from '@/lib/dr/tag';
import { serverSupabaseUrl } from '@/lib/supabase/url';

// The service-role client lives in ./service (no next/headers import) and is
// re-exported here for the many existing call sites.
export { createServiceClient } from './service';

/**
 * Confirm the database is browsing the release this request asked for
 * (P6c-1 rule: a release request never falls back to live silently). The
 * check itself lives in lib/dr/confirm (shared with the bearer routes'
 * service client); here its outcome is the page's: a refused tag or an
 * anonymous caller is the 404, anything else is a 500.
 */
export async function confirmBrowseTag(supabase: SupabaseClient, tag: string): Promise<void> {
  const check = await checkBrowseTag(supabase, tag);
  if (check.ok) return;
  if (check.kind === 'not-found') notFound();
  throw new Error(check.message);
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
  const supabaseUrl = serverSupabaseUrl();
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
