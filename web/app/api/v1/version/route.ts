import { NextResponse } from 'next/server';

/**
 * GET /api/v1/version
 *
 * Returns the minimum recommended Python client version.
 * No authentication required. Heavily cached.
 *
 * Bump `latest` when a new client release is tagged.
 * Bump `minimum` when older clients will break (e.g., API changes).
 *
 * 0.5.0 (perf T2-F, #511): the /api/v1/sync/* endpoints refuse offset
 * pagination, so every client that predates the keyset sync walk (#103) is
 * below the floor. Kept in step with SYNC_CLIENT_FLOOR in
 * lib/api-sync-pagination.ts.
 *
 * 0.6.0 (latest only): a first-time sync bootstraps from the nightly
 * catalog snapshot (/api/v1/sync/snapshot) instead of walking every stream.
 * Older clients still walk live, so the floor stays.
 *
 * 0.6.1: /sync/lines pages at 250 rows (line-fit rows are ~11 KB of JSON; a
 * 5000-row page timed out at Cloudflare). A server-side cap cannot protect
 * older clients -- they stop paging at the first short page -- so upgrading
 * is the fix for a `sync --full` that fails on line fits.
 */
export async function GET() {
  const response = NextResponse.json({
    latest: '0.6.1',
    minimum: '0.5.0',
  });

  // Cache for 1 hour — version changes are infrequent
  response.headers.set('Cache-Control', 'public, max-age=3600, stale-while-revalidate=600');

  return response;
}
