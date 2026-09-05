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
 */
export async function GET() {
  const response = NextResponse.json({
    latest: '0.5.0',
    minimum: '0.5.0',
  });

  // Cache for 1 hour — version changes are infrequent
  response.headers.set('Cache-Control', 'public, max-age=3600, stale-while-revalidate=600');

  return response;
}
