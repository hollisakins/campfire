import { getRequestIdentity } from '@/lib/auth/identity';
import { getObservationsOverview } from '@/lib/server/programs';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/observations — the observations tab of the metadata
 * page (get_observations_overview, scoped to the viewer's programs). See
 * ../programs/route.ts for why this is a route (perf T2-C, #506).
 */
// Domain-level failures (RPC error, unknown program, access denied) ride in
// the body with a 200, exactly as the server action this replaced resolved
// them: fetchJson() throws on non-2xx and drops the body, and the consumers
// read `data.error` / `data.program`. Only an anonymous caller is non-2xx.
export async function GET() {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const result = await getObservationsOverview();
  return Response.json(result, { headers: NO_STORE });
}
