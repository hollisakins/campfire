import { getDbStatus, getStatusMessage, type DbState } from '@/lib/server/db-status';

/**
 * GET /api/status — what the site-wide banner shows (components/layout/StatusBanner.tsx):
 * the shared database probe's state and the operator's message, if any.
 * Never touches the database beyond that one probe, and middleware skips its
 * session refresh here, so it still answers while GoTrue and PostgREST hang.
 * Anonymous, the same for every caller, no-store.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export interface SiteStatus {
  state: DbState;
  message: string | null;
}

export async function GET() {
  const [db, message] = await Promise.all([getDbStatus(), getStatusMessage()]);
  const body: SiteStatus = { state: db.state, message };
  return Response.json(body, { headers: { 'Cache-Control': 'no-store' } });
}
