import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateApiKey } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';

/**
 * GET /api/v1/metadata
 *
 * Returns available filter options for the authenticated user.
 * Includes programs, fields, gratings, and observations the user has access to.
 *
 * Response:
 * {
 *   programs: [{ program_id, program_name, pi_name, is_public }],
 *   fields: ["COSMOS", "UDS", ...],
 *   gratings: ["PRISM", "G395M", ...],
 *   observations: ["ember_uds_p4", ...]
 * }
 */
export async function GET(request: NextRequest) {
  // Validate API key
  const userId = await validateApiKey(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing API key' },
      { status: 401 }
    );
  }

  try {
    // Create Supabase client with service role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Get accessible programs for this user
    const accessibleProgramIds = await getAccessiblePrograms(userId);

    if (accessibleProgramIds.length === 0) {
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
      .select('program_id, program_name, pi_name, is_public')
      .in('program_id', accessibleProgramIds)
      .order('program_id');

    if (programsError) {
      console.error('Error fetching programs:', programsError);
      return NextResponse.json(
        { error: 'Failed to fetch programs' },
        { status: 500 }
      );
    }

    // Fetch fields and observations from materialized view
    const { data: filterData, error: filterError } = await supabase
      .from('mv_filter_options')
      .select('fields, observations')
      .single();

    if (filterError) {
      console.error('Error fetching filter options:', filterError);
      // Continue with empty arrays if materialized view fails
    }

    // Fetch distinct gratings from spectra table
    const { data: gratingData, error: gratingError } = await supabase
      .from('spectra')
      .select('grating')
      .order('grating');

    if (gratingError) {
      console.error('Error fetching gratings:', gratingError);
    }

    // Get unique gratings
    const uniqueGratings = gratingData
      ? [...new Set(gratingData.map(g => g.grating))].filter(Boolean).sort()
      : [];

    return NextResponse.json({
      programs: programs || [],
      fields: filterData?.fields || [],
      gratings: uniqueGratings,
      observations: filterData?.observations || [],
    });
  } catch (error) {
    console.error('Error in API /v1/metadata:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
