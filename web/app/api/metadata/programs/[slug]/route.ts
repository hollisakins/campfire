import { NextRequest } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { getProgramDetail } from '@/lib/server/programs';

export type { ProgramDetailResult as ProgramDetailResponse } from '@/lib/server/programs';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/programs/[slug] — one program's overview plus its
 * per-observation stats (metadata program page, /docs program page). See
 * ../route.ts for why this is a route and not an action (perf T2-C, #506).
 */
// Domain-level failures (RPC error, unknown program, access denied) ride in
// the body with a 200, exactly as the server action this replaced resolved
// them: fetchJson() throws on non-2xx and drops the body, and the consumers
// read `data.error` / `data.program`. Only an anonymous caller is non-2xx.
export async function GET(_request: NextRequest, context: { params: Promise<{ slug: string }> }) {
  const { user } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  const { slug } = await context.params;
  // Next has already percent-decoded the segment; decoding again would throw on a bare '%'.
  const result = await getProgramDetail(slug);
  return Response.json(result, { headers: NO_STORE });
}
