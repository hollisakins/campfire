import { getRequestIdentity } from '@/lib/auth/identity';
import { getAvailableLists } from '@/lib/actions/lists';
import type { ObjectList } from '@/lib/types';

export interface ListsAvailableResponse {
  lists: ObjectList[];
}

/**
 * GET /api/lists/available
 *
 * Every list the viewer can see (system, own, public, shared), for the Tags
 * chip of the filter bar and the Advanced Filters modal. A GET route rather
 * than a server action (decision D-C, #506): the bar reads it on mount, so
 * it must run alongside the page's other reads, be cacheable and abortable,
 * and never queue the table's own request behind it.
 */
export async function GET() {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  try {
    const result = await getAvailableLists();
    if (result.error) return Response.json({ error: result.error }, { status: 500 });
    const body: ListsAvailableResponse = { lists: result.lists };
    return Response.json(body, { headers: { 'Cache-Control': 'private, no-store' } });
  } catch (err) {
    console.error('lists available error:', err);
    return Response.json({ error: 'Failed to load lists' }, { status: 500 });
  }
}
