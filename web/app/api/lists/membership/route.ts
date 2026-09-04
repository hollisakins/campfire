import { NextRequest } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { listsWithMembership } from '@/lib/server/lists';
import type { ObjectListWithMembership } from '@/lib/types';

export interface ListsMembershipResponse {
  lists: ObjectListWithMembership[];
}

/**
 * GET /api/lists/membership?object=<objects.id>
 *
 * Every list the viewer can see, flagged with whether the object is a member
 * — the object page's tag section reads this on mount. A GET route rather
 * than a server action (perf T2-C, #506) so it runs alongside the page's
 * other reads instead of queueing behind them. Adds/removes stay actions.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const objectId = parseInt(request.nextUrl.searchParams.get('object') ?? '', 10);
  if (!Number.isInteger(objectId) || objectId <= 0) {
    return Response.json({ error: 'Invalid object' }, { status: 400 });
  }

  try {
    const result = await listsWithMembership(supabase, user.id, objectId);
    if (result.error) return Response.json({ error: result.error }, { status: 500 });
    const body: ListsMembershipResponse = { lists: result.lists };
    return Response.json(body, { headers: { 'Cache-Control': 'private, no-store' } });
  } catch (err) {
    console.error('lists membership error:', err);
    return Response.json({ error: 'Failed to load lists' }, { status: 500 });
  }
}
