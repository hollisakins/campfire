import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';

/**
 * Flag query parameters for include/exclude filtering
 */
interface FlagQueryParams {
  include_any: number | null;
  include_all: number | null;
  exclude: number | null;
}

/**
 * Parse flag query parameters from URL search params.
 * Supports both new multi-mode params and legacy single param.
 *
 * New params: prefix_include_any, prefix_include_all, prefix_exclude
 * Legacy: prefix (treated as include_any for backward compatibility)
 */
function parseFlagQuery(params: URLSearchParams, prefix: string): FlagQueryParams {
  const includeAny = params.get(`${prefix}_include_any`);
  const includeAll = params.get(`${prefix}_include_all`);
  const exclude = params.get(`${prefix}_exclude`);
  const legacy = params.get(prefix); // Backward compatibility

  return {
    // Legacy single param is treated as include_any
    include_any: includeAny ? parseInt(includeAny, 10) : (legacy ? parseInt(legacy, 10) : null),
    include_all: includeAll ? parseInt(includeAll, 10) : null,
    exclude: exclude ? parseInt(exclude, 10) : null,
  };
}

/**
 * GET /api/v1/objects
 *
 * Query objects with filters for the Python API.
 * Requires API key authentication via Authorization header.
 *
 * Query parameters:
 * - programs: comma-separated list of program IDs (e.g., "1,2,3")
 * - fields: comma-separated list of field names (e.g., "COSMOS,UDS")
 * - gratings: comma-separated list of gratings (e.g., "PRISM,G395M")
 * - observations: comma-separated list of observation names
 * - redshift_min: minimum redshift (float)
 * - redshift_max: maximum redshift (float)
 * - max_snr_min: minimum max SNR (float)
 * - max_snr_max: maximum max SNR (float)
 * - redshift_quality: comma-separated list of quality codes (e.g., "1,2,3")
 *
 * Flag filters (each supports three modes):
 * - spectral_features: legacy single mask (match any)
 * - spectral_features_include_any: match any of these flags (OR)
 * - spectral_features_include_all: must have all of these flags (AND)
 * - spectral_features_exclude: must NOT have any of these flags (NOT)
 * (same pattern for object_flags and dq_flags)
 *
 * - inspected_only: "true" to filter to inspected objects only
 * - search: text search on object_id
 * - ra: right ascension for cone search (degrees)
 * - dec: declination for cone search (degrees)
 * - radius: search radius (arcsec)
 * - limit: maximum number of results (default: 1000)
 * - offset: pagination offset (default: 0)
 * - sort: sort column (object_id, ra, dec, redshift, redshift_quality)
 * - sort_dir: sort direction (asc, desc)
 */
export async function GET(request: NextRequest) {
  // Validate API key
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing API key' },
      { status: 401 }
    );
  }

  try {
    // Get accessible programs for this user
    const accessibleProgramIds = await getAccessiblePrograms(userId);

    if (accessibleProgramIds.length === 0) {
      return NextResponse.json({
        data: [],
        pagination: {
          total: 0,
          limit: 0,
          offset: 0,
        },
      });
    }

    // Parse query parameters
    const searchParams = request.nextUrl.searchParams;

    // Program filter (intersect with accessible programs)
    let filterPrograms: number[] | null = null;
    const programsParam = searchParams.get('programs');
    if (programsParam) {
      const requestedPrograms = programsParam.split(',').map(p => parseInt(p.trim(), 10));
      filterPrograms = requestedPrograms.filter(p =>
        !isNaN(p) && accessibleProgramIds.includes(p)
      );
    }

    // Field filter
    let fields: string[] | null = null;
    const fieldsParam = searchParams.get('fields');
    if (fieldsParam) {
      fields = fieldsParam.split(',').map(f => f.trim()).filter(f => f.length > 0);
    }

    // Grating filter
    let gratings: string[] | null = null;
    const gratingsParam = searchParams.get('gratings');
    if (gratingsParam) {
      gratings = gratingsParam.split(',').map(g => g.trim()).filter(g => g.length > 0);
    }

    // Observation filter
    let observations: string[] | null = null;
    const observationsParam = searchParams.get('observations');
    if (observationsParam) {
      observations = observationsParam.split(',').map(o => o.trim()).filter(o => o.length > 0);
    }

    // Redshift quality filter
    let redshiftQuality: number[] | null = null;
    const redshiftQualityParam = searchParams.get('redshift_quality');
    if (redshiftQualityParam) {
      redshiftQuality = redshiftQualityParam
        .split(',')
        .map(q => parseInt(q.trim(), 10))
        .filter(q => !isNaN(q));
    }

    // Redshift range
    const redshiftMin = searchParams.get('redshift_min');
    const redshiftMax = searchParams.get('redshift_max');

    // SNR range
    const maxSnrMin = searchParams.get('max_snr_min');
    const maxSnrMax = searchParams.get('max_snr_max');

    // Bitmask filters (support both legacy single param and new multi-mode params)
    const spectralFeaturesQuery = parseFlagQuery(searchParams, 'spectral_features');
    const objectFlagsQuery = parseFlagQuery(searchParams, 'object_flags');
    const dqFlagsQuery = parseFlagQuery(searchParams, 'dq_flags');

    // Inspected only filter
    let inspectedOnly: boolean | null = null;
    const inspectedOnlyParam = searchParams.get('inspected_only');
    if (inspectedOnlyParam) {
      inspectedOnly = inspectedOnlyParam.toLowerCase() === 'true';
    }

    // Text search
    const search = searchParams.get('search');

    // Coordinate search
    const ra = searchParams.get('ra');
    const dec = searchParams.get('dec');
    const radius = searchParams.get('radius'); // in arcsec

    let coordRa: number | null = null;
    let coordDec: number | null = null;
    let radiusDegrees: number | null = null;

    if (ra && dec && radius) {
      coordRa = parseFloat(ra);
      coordDec = parseFloat(dec);
      radiusDegrees = parseFloat(radius) / 3600; // Convert arcsec to degrees
    }

    // Pagination
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const page = Math.floor(offset / limit) + 1;

    // Sorting
    const sortColumn = searchParams.get('sort') || 'object_id';
    const sortDirection = searchParams.get('sort_dir') || 'asc';

    // Validate sort column
    const validSortColumns = ['object_id', 'ra', 'dec', 'redshift', 'redshift_quality', 'field'];
    const finalSortColumn = validSortColumns.includes(sortColumn) ? sortColumn : 'object_id';

    // Create Supabase client with service role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Call the RPC function
    const { data, error } = await supabase.rpc('get_filtered_objects_paginated', {
      p_program_ids: accessibleProgramIds,
      p_filter_programs: filterPrograms,
      p_fields: fields,
      p_gratings: gratings,
      p_observations: observations,
      p_redshift_quality: redshiftQuality,
      p_redshift_min: redshiftMin ? parseFloat(redshiftMin) : null,
      p_redshift_max: redshiftMax ? parseFloat(redshiftMax) : null,
      p_max_snr_min: maxSnrMin ? parseFloat(maxSnrMin) : null,
      p_max_snr_max: maxSnrMax ? parseFloat(maxSnrMax) : null,
      p_spectral_features_include_any: spectralFeaturesQuery.include_any,
      p_spectral_features_include_all: spectralFeaturesQuery.include_all,
      p_spectral_features_exclude: spectralFeaturesQuery.exclude,
      p_object_flags_include_any: objectFlagsQuery.include_any,
      p_object_flags_include_all: objectFlagsQuery.include_all,
      p_object_flags_exclude: objectFlagsQuery.exclude,
      p_dq_flags_include_any: dqFlagsQuery.include_any,
      p_dq_flags_include_all: dqFlagsQuery.include_all,
      p_dq_flags_exclude: dqFlagsQuery.exclude,
      p_search: search?.trim() || null,
      p_inspected_only: inspectedOnly,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_sort_column: finalSortColumn,
      p_sort_direction: sortDirection,
      p_page: page,
      p_page_size: limit,
    });

    if (error) {
      console.error('Error fetching objects:', error);
      return NextResponse.json(
        { error: 'Failed to fetch objects', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { objects: [], total_count: 0 };

    // Return API response
    return NextResponse.json({
      data: result.objects || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        offset,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/objects:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
