import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';
import { resolveReleaseMembers } from '@/lib/server/dr-files';

/**
 * The key a streaming route serves for one sidecar under a release (P6c-2):
 * the archived copy's key when the release holds the member archived, else
 * null (the route answers 404). Never the live key.
 */
export async function releaseSidecarKey(
  supabase: SupabaseClient,
  tag: string,
  liveKey: string,
): Promise<string | null> {
  const members = await resolveReleaseMembers(supabase, tag, [liveKey]);
  return members.get(liveKey)?.archiveKey ?? null;
}
