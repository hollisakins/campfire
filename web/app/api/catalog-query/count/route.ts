import { NextRequest } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { getSpectra } from '@/lib/actions/spectra';
import { defaultSortColumn } from '@/lib/actions/spectra-types';
import { parseFiltersFromURL, parseViewModeFromURL } from '@/lib/utils/url-params';

export interface CatalogQueryCountResponse {
  /** Rows the filter state selects in the given view, -1 when unknown. */
  total: number;
  view: 'objects' | 'spectra';
}

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/catalog-query/count?<the list page's own URL parameters>
 *
 * The live count of the Filters modal's draft: the same filter state the
 * list page carries (`q`, `search`, `scope`, `view`), counted by the same
 * action the table uses, so the number in the modal's footer is the number
 * the table will show. A GET route rather than a call to the action from
 * the client (decision D-C, #506): the client aborts it on the next
 * keystroke, and it never queues the table's own request behind stale
 * counts.
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sp = request.nextUrl.searchParams;
  const view = parseViewModeFromURL(sp);
  const filters = parseFiltersFromURL(sp);
  const result = await getSpectra(filters, 1, 1, defaultSortColumn(view), 'asc', view, { includeCount: true });
  if (result.error) return Response.json({ error: result.error }, { status: 400 });
  const body: CatalogQueryCountResponse = { total: result.total, view };
  return Response.json(body, { headers: NO_STORE });
}
