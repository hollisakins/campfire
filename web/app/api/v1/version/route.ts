import { NextResponse } from 'next/server';

/**
 * GET /api/v1/version
 *
 * Returns the minimum recommended Python client version.
 * No authentication required. Heavily cached.
 *
 * Bump `latest` when a new client release is tagged.
 * Bump `minimum` when older clients will break (e.g., API changes).
 */
export async function GET() {
  const response = NextResponse.json({
    latest: '0.4.0',
    minimum: '0.4.0',
  });

  // Cache for 1 hour — version changes are infrequent
  response.headers.set('Cache-Control', 'public, max-age=3600, stale-while-revalidate=600');

  return response;
}
