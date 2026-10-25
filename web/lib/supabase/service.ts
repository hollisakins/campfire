// The service-role Supabase client (bypasses RLS).
//
// This is the ONLY module allowed to read SUPABASE_SERVICE_ROLE_KEY — an
// ESLint rule (eslint.config.mjs) rejects the env read anywhere else, so every
// privileged client is constructed here with the correct auth options
// (no session persistence, no auto-refresh) and is easy to audit.
//
// Deliberately free of `next/headers` so it can be imported from any server
// context, including modules that middleware may pull in.

import { createClient as createSupabaseClient, type SupabaseClient } from '@supabase/supabase-js';
import { DR_HEADER } from '@/lib/dr/tag';
import { serverSupabaseUrl } from '@/lib/supabase/url';

export interface ServiceClientOptions {
  /**
   * Browse this data release (P6c-2): every call carries `x-campfire-dr`.
   * The database's pre-request hook trusts the service role with any
   * post-freeze tag, so the caller owns the authorization: a bearer route
   * passes ONLY the tag `assertBearerReleaseAccess` (lib/server/bearer-release.ts)
   * returned for the caller.
   */
  dr?: string | null;
}

/**
 * Create a Supabase client with the service role key for privileged
 * server-side operations. Bypasses RLS: callers own the authorization.
 */
export const createServiceClient = (options: ServiceClientOptions = {}): SupabaseClient => {
  const supabaseUrl = serverSupabaseUrl();
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;

  if (!serviceRoleKey) {
    throw new Error('SUPABASE_SERVICE_ROLE_KEY is not configured');
  }

  return createSupabaseClient(supabaseUrl, serviceRoleKey, {
    ...(options.dr ? { global: { headers: { [DR_HEADER]: options.dr } } } : {}),
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
};
