// Client-side Supabase setup

import { createBrowserClient } from '@supabase/ssr';
import type { SupabaseClient } from '@supabase/supabase-js';
import { browserUrl } from '@/lib/host-aliases';
import { publicSupabaseCookieName } from './url';

let browserClient: SupabaseClient | null = null;

/**
 * The browser Supabase client. One instance per tab: `createBrowserClient`
 * already dedups internally, but a module-level singleton keeps call sites
 * from constructing (and re-reading env) on every render and gives hooks a
 * referentially stable client to depend on (#499).
 *
 * On an alias host (lib/host-aliases.ts) the client talks to the API's alias
 * too, but keeps the primary url's cookie name: the server clients look the
 * session up under that one name whichever host the request came in on.
 */
export const createClient = (): SupabaseClient => {
  if (browserClient) return browserClient;
  const supabaseUrl = browserUrl(process.env.NEXT_PUBLIC_SUPABASE_URL!);
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
  browserClient = createBrowserClient(supabaseUrl, supabaseAnonKey, {
    cookieOptions: { name: publicSupabaseCookieName() },
  });
  return browserClient;
};
