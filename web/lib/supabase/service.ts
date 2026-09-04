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

/**
 * Create a Supabase client with the service role key for privileged
 * server-side operations. Bypasses RLS: callers own the authorization.
 */
export const createServiceClient = (): SupabaseClient => {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;

  if (!serviceRoleKey) {
    throw new Error('SUPABASE_SERVICE_ROLE_KEY is not configured');
  }

  return createSupabaseClient(supabaseUrl, serviceRoleKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
};
