import { getRequestIdentity } from '@/lib/auth/identity';
import { getObservationsOverview } from '@/lib/server/programs';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/observations — the observations tab of the metadata
 * page (get_observations_overview, scoped to the viewer's programs). See
 * ../programs/route.ts for why this is a route (perf T2-C, #506).
 */
export async function GET() {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const result = await getObservationsOverview();
  return Response.json(result, { status: result.error ? 500 : 200, headers: NO_STORE });
}
