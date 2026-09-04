import { getRequestIdentity } from '@/lib/auth/identity';
import { getDatabaseOverview } from '@/lib/server/programs';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/overview — the single-row scope summary in the metadata
 * page header (get_database_overview). See ../programs/route.ts for why this
 * is a route (perf T2-C, #506).
 */
export async function GET() {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const result = await getDatabaseOverview();
  return Response.json(result, { status: result.error ? 500 : 200, headers: NO_STORE });
}
