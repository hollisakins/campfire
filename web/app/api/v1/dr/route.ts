import { NextRequest, NextResponse } from 'next/server';
import { rejectReleaseParam } from '@/lib/server/bearer-release';
import { authenticateApiRequest } from '@/lib/api-auth';
import { createServiceClient } from '@/lib/supabase/service';
import { fetchVisibleReleases } from '@/lib/server/dr-exports';

/**
 * GET /api/v1/dr   (bearer; `campfire dr list`, P5-2)
 *
 * The data releases this caller may read, newest frozen first: published
 * ones, or every state past its freeze for admins (the shadow run), the same
 * rule as /api/v1/dr/<tag>/files. Link accounts are refused by
 * authenticateApiRequest. Live-only by contract: a `dr` parameter is a 400.
 *
 * Response: { releases: [{ tag, state, format_version, components, counts,
 *                          frozen_at, published_at, withdrawn_at, doi, notes,
 *                          known_issues }] }
 */
export async function GET(request: NextRequest) {
  const dr400 = rejectReleaseParam(request.nextUrl.searchParams);
  if (dr400) return dr400;
  const principal = await authenticateApiRequest(request);
  if (!principal) {
    return NextResponse.json({ error: 'Invalid or missing authentication' }, { status: 401 });
  }
  const noStore = { 'Cache-Control': 'private, no-store' };
  try {
    const releases = await fetchVisibleReleases(createServiceClient(), principal.access.isAdmin);
    return NextResponse.json(
      {
        releases: releases.map((r) => ({
          tag: r.tag,
          state: r.state,
          format_version: r.dr_format_version,
          components: r.components,
          counts: r.counts,
          frozen_at: r.frozen_at,
          published_at: r.published_at,
          withdrawn_at: r.withdrawn_at,
          doi: r.doi,
          notes: r.notes,
          known_issues: r.known_issues,
        })),
      },
      { headers: noStore },
    );
  } catch (error) {
    console.error('Error in GET /api/v1/dr:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
