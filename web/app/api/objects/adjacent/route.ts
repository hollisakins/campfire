import { NextRequest } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { buildFilterParams } from '@/lib/actions/filter-params';
import { parseFiltersFromURL, parseSortingFromURL } from '@/lib/utils/url-params';

export interface AdjacentObjectsResponse {
  prev: string | null;
  next: string | null;
  /** 1-based position in the filtered, sorted catalog; 0 when unknown. */
  currentIndex: number;
  total: number;
}

const EMPTY: AdjacentObjectsResponse = { prev: null, next: null, currentIndex: 0, total: 0 };
const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/objects/adjacent?id=<object_id>&<list filter + sort params>
 *
 * Prev/next object ids and position for the object page's navigation arrows.
 * The filter and sort parameters are the list page's own URL parameters
 * (lib/utils/url-params.ts) — the object page forwards them verbatim — so
 * the arrows walk exactly the sequence the table showed. The client consults
 * its sessionStorage navigation cache first and only calls this on a miss or
 * at a page boundary.
 *
 * A GET route rather than a server action (perf T2-C, #506): as an action
 * this queued behind the page's other actions and could not be aborted when
 * the user moved on. Backed by get_adjacent_objects, which runs under the
 * caller's RLS session with the same filter contract as the list RPC.
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sp = request.nextUrl.searchParams;
  const objectId = sp.get('id');
  if (!objectId) return Response.json({ error: 'Missing id' }, { status: 400 });

  const slugs = principal.access.accessibleSlugs;
  if (slugs.length === 0) return Response.json(EMPTY, { headers: NO_STORE });

  const filters = parseFiltersFromURL(sp);
  const { sortColumn, sortDirection } = parseSortingFromURL(sp, 'objects');
  const rpcParams = buildFilterParams(filters, slugs, principal.user.id);

  // Strip target-only params that the objects RPC doesn't accept
  /* eslint-disable @typescript-eslint/no-unused-vars */
  const {
    p_dq_flags_include_any: _dq1, p_dq_flags_include_all: _dq2, p_dq_flags_exclude: _dq3,
    ...objectsParams
  } = rpcParams;
  /* eslint-enable @typescript-eslint/no-unused-vars */

  try {
    const { data, error } = await principal.supabase.rpc('get_adjacent_objects', {
      p_current_object_id: objectId,
      ...objectsParams,
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
    });
    if (error) {
      console.error('adjacent objects error:', error);
      return Response.json({ error: error.message }, { status: 500 });
    }
    const row = data?.[0];
    const body: AdjacentObjectsResponse = row
      ? {
          prev: row.prev_object_id || null,
          next: row.next_object_id || null,
          currentIndex: Number(row.current_index) || 0,
          total: Number(row.total_count) || 0,
        }
      : EMPTY;
    return Response.json(body, { headers: NO_STORE });
  } catch (err) {
    console.error('adjacent objects error:', err);
    return Response.json({ error: 'Failed to load navigation' }, { status: 500 });
  }
}
