import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { authenticateApiRequest } from '@/lib/api-auth';
import { assertBearerReleaseAccess } from '@/lib/server/bearer-release';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';

/**
 * GET /api/v1/observations
 *
 * List available observations with aggregate stats.
 * Requires API key or JWT authentication.
 *
 * Returns observation name, program info, object/spectrum counts, and total file size.
 */
export async function GET(request: NextRequest) {
  const principal = await authenticateApiRequest(request);
  const userId = principal?.userId ?? null;

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }

  try {
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json({ observations: [] });
    }

    // `dr=<tag>`: browse a data release (P6c-2), gated against the caller's
    // own visibility before the header is forwarded.
    const release = await assertBearerReleaseAccess(request, principal!);
    if (release.response) return release.response;
    const supabase = createServiceClient({ dr: release.tag });

    // Unpublished spectra only count toward observation stats for admins who
    // explicitly opt in. Fail-closed otherwise; no-op in B1.
    const includeUnpublished =
      request.nextUrl.searchParams.get('include_unpublished') === 'true' && (await isAdminUser(userId));

    const { data, error } = await supabase.rpc('get_observation_stats', {
      p_program_slugs: accessibleProgramSlugs,
      p_include_unpublished: includeUnpublished,
    });

    if (error) {
      console.error('Error fetching observation stats:', error);
      return NextResponse.json(
        { error: 'Failed to fetch observations' },
        { status: 500 }
      );
    }

    return NextResponse.json({ observations: data || [] });
  } catch (error) {
    console.error('Error in GET /api/v1/observations:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
