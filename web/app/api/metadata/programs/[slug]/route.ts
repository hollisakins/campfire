import { NextRequest } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { getProgramDetail } from '@/lib/server/programs';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/programs/[slug] — one program's overview plus its
 * per-observation stats (metadata program page, /docs program page). See
 * ../route.ts for why this is a route and not an action (perf T2-C, #506).
 */
export async function GET(_request: NextRequest, context: { params: Promise<{ slug: string }> }) {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const { slug } = await context.params;
  const result = await getProgramDetail(decodeURIComponent(slug));
  const status = result.error === 'Program not found' || result.error === 'Access denied'
    ? 404
    : result.error ? 500 : 200;
  return Response.json(result, { status, headers: NO_STORE });
}
