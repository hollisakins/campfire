import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
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
  const userId = await validateAuth(request);

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

    const supabase = createServiceClient();

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
