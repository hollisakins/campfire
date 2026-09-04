// Server-side Supabase setup

import { createServerClient } from '@supabase/ssr';
import { cookies } from 'next/headers';

// The service-role client lives in ./service (no next/headers import) and is
// re-exported here for the many existing call sites.
export { createServiceClient } from './service';

export const createClient = async () => {
  const cookieStore = await cookies();
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

  return createServerClient(supabaseUrl, supabaseAnonKey, {
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
};
