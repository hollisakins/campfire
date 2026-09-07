// Client-side Supabase setup

import { createBrowserClient } from '@supabase/ssr';
import type { SupabaseClient } from '@supabase/supabase-js';
import { installAuthChannelBfcacheGuard } from './bfcache-auth-channel';

let browserClient: SupabaseClient | null = null;

/**
 * The browser Supabase client. One instance per tab: `createBrowserClient`
 * already dedups internally, but a module-level singleton keeps call sites
 * from constructing (and re-reading env) on every render and gives hooks a
 * referentially stable client to depend on (#499).
 */
export const createClient = (): SupabaseClient => {
  if (browserClient) return browserClient;
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
  browserClient = createBrowserClient(supabaseUrl, supabaseAnonKey);
  // Pause the auth client's cross-tab channel while this document is parked
  // in bfcache, or every signed-in page is evicted by the next one's boot
  // events and the back button always reloads (#540).
  if (typeof window !== 'undefined') installAuthChannelBfcacheGuard(browserClient);
  return browserClient;
};
