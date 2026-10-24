import { createServiceClient } from '@/lib/supabase/service';

/**
 * GET /api/health
 *
 * Liveness + database reachability for the self-hosted deployment
 * (ops/README.md): the container HEALTHCHECK, ops/scripts/deploy.sh and the
 * external uptime monitor all read it. Anonymous on purpose, no-store, and
 * cheap: one single-row read through the service client, bounded by a short
 * timeout so a stalled database answers 503 instead of hanging the probe.
 *
 * `sha` is the deployed commit (GIT_SHA baked by web/Dockerfile; Vercel's
 * VERCEL_GIT_COMMIT_SHA while that deployment is live).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export interface HealthResult {
  ok: boolean;
  db: 'ok' | 'error';
  sha: string | null;
  uptime_s: number;
}

const NO_STORE = { 'Cache-Control': 'no-store' };
const DB_TIMEOUT_MS = 5000;

export async function GET() {
  let db: HealthResult['db'] = 'ok';
  try {
    const supabase = createServiceClient();
    const { error } = await supabase
      .from('programs')
      .select('slug')
      .limit(1)
      .abortSignal(AbortSignal.timeout(DB_TIMEOUT_MS));
    if (error) db = 'error';
  } catch {
    db = 'error';
  }

  const body: HealthResult = {
    ok: db === 'ok',
    db,
    sha: process.env.GIT_SHA || process.env.VERCEL_GIT_COMMIT_SHA || null,
    uptime_s: Math.round(process.uptime()),
  };
  return Response.json(body, { status: body.ok ? 200 : 503, headers: NO_STORE });
}
