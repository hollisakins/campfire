import { getRequestIdentity } from '@/lib/auth/identity';
import { getProgramsOverview } from '@/lib/server/programs';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/programs — programs overview for the metadata page and
 * /docs. A GET route, not a server action (perf T2-C, #506): the metadata
 * page issues this alongside the observations and scope reads, and actions
 * would serialize them. Access-scoped per viewer, so never shared-cached.
 */
export async function GET() {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const result = await getProgramsOverview();
  return Response.json(result, { status: result.error ? 500 : 200, headers: NO_STORE });
}
