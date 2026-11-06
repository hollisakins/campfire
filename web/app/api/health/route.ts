import { getDbStatus, type DbStatus } from '@/lib/server/db-status';

/**
 * GET /api/health
 *
 * Liveness + database health for the self-hosted deployment (ops/README.md):
 * the container HEALTHCHECK, ops/scripts/deploy.sh and the external site check
 * all read it. Anonymous on purpose, no-store, and cheap: the shared database
 * probe (lib/server/db-status.ts), which answers 503 both when the database
 * cannot be reached and when it is stuck (commits waiting on a stalled disk,
 * a lock pile-up) — the 2026-11-06 volume stall served reads from cache and
 * kept a plain read green for its whole 15 minutes.
 *
 * `sha` is the deployed commit (GIT_SHA baked by web/Dockerfile; Vercel's
 * VERCEL_GIT_COMMIT_SHA while that deployment is live).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export interface HealthResult {
  ok: boolean;
  db: 'ok' | 'degraded' | 'error';
  reason: DbStatus['reason'];
  sha: string | null;
  uptime_s: number;
}

const NO_STORE = { 'Cache-Control': 'no-store' };

export async function GET() {
  const status = await getDbStatus();
  const body: HealthResult = {
    ok: status.state === 'ok',
    db: status.state === 'down' ? 'error' : status.state,
    reason: status.reason,
    sha: process.env.GIT_SHA || process.env.VERCEL_GIT_COMMIT_SHA || null,
    uptime_s: Math.round(process.uptime()),
  };
  return Response.json(body, { status: body.ok ? 200 : 503, headers: NO_STORE });
}
