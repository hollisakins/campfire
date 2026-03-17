import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';

/**
 * GET /api/v1/metadata
 *
 * Returns available filter options for the authenticated user.
 * Includes programs, fields, gratings, and observations the user has access to.
 *
 * Response:
 * {
 *   programs: [{ slug, program_name, pi_name, is_public }],
 *   fields: ["COSMOS", "UDS", ...],
 *   gratings: ["PRISM", "G395M", ...],
 *   observations: ["ember_uds_p4", ...]
 * }
 */
export async function GET(request: NextRequest) {
  // Validate authentication (API key or JWT)
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }

  try {
    // Create Supabase client with service role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Get accessible programs for this user
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json({
        programs: [],
        fields: [],
        gratings: [],
        observations: [],
      });
    }

    // Fetch programs with full metadata
    const { data: programs, error: programsError } = await supabase
      .from('programs')
      .select('slug, program_name, pi_name, is_public')
      .in('slug', accessibleProgramSlugs)
      .order('slug');

    if (programsError) {
      console.error('Error fetching programs:', programsError);
      return NextResponse.json(
        { error: 'Failed to fetch programs' },
        { status: 500 }
      );
    }

    // Fetch fields, observations, and gratings from materialized view
    // This is much more efficient than querying the spectra table directly
    const { data: filterData, error: filterError } = await supabase
      .from('mv_filter_options')
      .select('fields, observations, gratings')
      .single();

    if (filterError) {
      console.error('Error fetching filter options:', filterError);
      // Continue with empty arrays if materialized view fails
    }

    const response = NextResponse.json({
      programs: programs || [],
      fields: filterData?.fields || [],
      gratings: filterData?.gratings || [],
      observations: filterData?.observations || [],
    });

    // Cache for 5 minutes - filter options only change on data deployments
    response.headers.set('Cache-Control', 'public, max-age=300, stale-while-revalidate=60');

    return response;
  } catch (error) {
    console.error('Error in API /v1/metadata:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
