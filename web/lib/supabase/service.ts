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

export interface ServiceClientOptions {
  /**
   * Browse this data release (P6c): every call carries `x-campfire-dr`. The
   * caller owns the authorization — the hook trusts the service role with any
   * frozen tag — so a bearer route must gate a caller-supplied tag against
   * the caller's own visibility before passing it here (plan §8 P6c rules).
   */
  dr?: string | null;
}

/**
 * Create a Supabase client with the service role key for privileged
 * server-side operations. Bypasses RLS: callers own the authorization.
 */
export const createServiceClient = (options: ServiceClientOptions = {}): SupabaseClient => {
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
    ...(options.dr ? { global: { headers: { [DR_HEADER]: options.dr } } } : {}),
  });
};
