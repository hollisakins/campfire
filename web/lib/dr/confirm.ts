import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Confirm the database is browsing the release a request asked for (P6c-1
 * rule: a release request never falls back to live silently). One round
 * trip: `dr_browse_tag()` must echo the tag the pre-request hook set from
 * the `x-campfire-dr` header. Free of `next/*` imports so both the cookie
 * client (pages, route handlers) and the service client (the bearer routes)
 * run the same check; each maps the outcome to its own 404 / 500.
 */
export type BrowseTagCheck =
  | { ok: true }
  /** The hook refused the tag (unknown, or not visible to this caller), or
   *  the caller may not execute the function (anonymous). */
  | { ok: false; kind: 'not-found'; message: string }
  /** The database answered live, or something else: the hook is not
   *  registered, a stale PostgREST config, an error. Never a live render. */
  | { ok: false; kind: 'unavailable'; message: string };

export async function checkBrowseTag(supabase: SupabaseClient, tag: string): Promise<BrowseTagCheck> {
  const { data, error } = await supabase.rpc('dr_browse_tag');
  if (error) {
    if (error.code === 'PT404' || error.code === '42501') {
      return { ok: false, kind: 'not-found', message: `data release ${tag}: ${error.message}` };
    }
    return { ok: false, kind: 'unavailable', message: `data release ${tag}: ${error.message}` };
  }
  if (data !== tag) {
    return {
      ok: false,
      kind: 'unavailable',
      message: `data release ${tag}: the database answered ${JSON.stringify(data)} (pre-request hook not active?)`,
    };
  }
  return { ok: true };
}
