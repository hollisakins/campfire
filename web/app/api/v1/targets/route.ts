import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import { buildFilterParams } from '@/lib/actions/filter-params';
import type { FilterOptions } from '@/lib/actions/filter-params';

/**
 * Parse comma-separated string into array, or null if empty/absent.
 */
function parseCSV(value: string | null): string[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => s.trim()).filter(s => s.length > 0);
  return items.length > 0 ? items : null;
}

/**
 * Parse comma-separated integers, or null if empty/absent.
 */
function parseIntCSV(value: string | null): number[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
  return items.length > 0 ? items : null;
}

/**
 * Parse flag query parameters from URL search params.
 * Supports both new multi-mode params and legacy single param.
 *
 * New params: prefix_include_any, prefix_include_all, prefix_exclude
 * Legacy: prefix (treated as include_any for backward compatibility)
 *
 * Returns values as single-element arrays for compatibility with buildFilterParams,
 * which expects arrays of flag values (it OR-reduces them internally).
 */
function parseFlagArrays(params: URLSearchParams, prefix: string): {
  values: number[];
  mode: 'any' | 'all' | 'none';
} {
  const includeAny = params.get(`${prefix}_include_any`);
  const includeAll = params.get(`${prefix}_include_all`);
  const exclude = params.get(`${prefix}_exclude`);
  const legacy = params.get(prefix); // Backward compatibility

  // Determine mode and mask value
  if (includeAll) {
    return { values: [parseInt(includeAll, 10)], mode: 'all' };
  }
  if (exclude) {
    return { values: [parseInt(exclude, 10)], mode: 'none' };
  }
  // include_any or legacy (treated as include_any)
  const anyValue = includeAny || legacy;
  if (anyValue) {
    return { values: [parseInt(anyValue, 10)], mode: 'any' };
  }
  return { values: [], mode: 'any' };
}

/**
 * Parse URL search params into FilterOptions for use with buildFilterParams.
 * This ensures the API route uses the exact same filter logic as the web frontend.
 */
function parseUrlToFilters(
  searchParams: URLSearchParams,
  accessibleProgramSlugs: string[]
): Partial<FilterOptions> {
  // Program filter (intersect with accessible programs)
  const programsParam = searchParams.get('programs');
  let programs: string[] = [];
  if (programsParam) {
    programs = programsParam
      .split(',')
      .map(p => p.trim())
      .filter(p => p.length > 0 && accessibleProgramSlugs.includes(p));
  }

  // Coordinate search
  const ra = searchParams.get('ra');
  const dec = searchParams.get('dec');
  const radius = searchParams.get('radius'); // in arcsec
  const coordinateSearch = (ra && dec && radius)
    ? { ra: parseFloat(ra), dec: parseFloat(dec), radius: parseFloat(radius), radius_unit: 'arcsec' as const }
    : null;

  // Bitmask filters (legacy + multi-mode support)
  const sf = parseFlagArrays(searchParams, 'spectral_features');
  const dq = parseFlagArrays(searchParams, 'dq_flags');

  // Inspected only
  const inspectedOnlyParam = searchParams.get('inspected_only');
  const inspectedOnly = inspectedOnlyParam
    ? inspectedOnlyParam.toLowerCase() === 'true'
    : null;

  return {
    programs,
    fields: parseCSV(searchParams.get('fields')) || [],
    gratings: parseCSV(searchParams.get('gratings')) || [],
    gratings_mode: (searchParams.get('gratings_mode') as 'any' | 'all' | 'none') || 'any',
    observations: parseCSV(searchParams.get('observations')) || [],
    redshift_quality: parseIntCSV(searchParams.get('redshift_quality')) || [],
    redshift_min: searchParams.get('redshift_min') ? parseFloat(searchParams.get('redshift_min')!) : null,
    redshift_max: searchParams.get('redshift_max') ? parseFloat(searchParams.get('redshift_max')!) : null,
    max_snr_min: searchParams.get('max_snr_min') ? parseFloat(searchParams.get('max_snr_min')!) : null,
    max_snr_max: searchParams.get('max_snr_max') ? parseFloat(searchParams.get('max_snr_max')!) : null,
    max_exposure_time_min: searchParams.get('max_exposure_time_min') ? parseFloat(searchParams.get('max_exposure_time_min')!) : null,
    max_exposure_time_max: searchParams.get('max_exposure_time_max') ? parseFloat(searchParams.get('max_exposure_time_max')!) : null,
    spectral_features: sf.values,
    spectral_features_mode: sf.mode,
    dq_flags: dq.values,
    dq_flags_mode: dq.mode,
    inspected_only: inspectedOnly,
    search: searchParams.get('search') || '',
    search_scope: (searchParams.get('search_scope') as 'target_id' | 'my_comments' | 'all_comments') || 'target_id',
    coordinate_search: coordinateSearch,
  };
}

/**
 * GET /api/v1/targets
 *
 * Query targets with filters for the Python API.
 * Requires API key authentication via Authorization header.
 *
 * Query parameters:
 * - programs: comma-separated list of program IDs (e.g., "1,2,3")
 * - fields: comma-separated list of field names (e.g., "COSMOS,UDS")
 * - gratings: comma-separated list of gratings (e.g., "PRISM,G395M")
 * - gratings_mode: filter mode for gratings (any, all, none; default: any)
 * - observations: comma-separated list of observation names
 * - redshift_min: minimum redshift (float)
 * - redshift_max: maximum redshift (float)
 * - max_snr_min: minimum max SNR (float)
 * - max_snr_max: maximum max SNR (float)
 * - max_exposure_time_min: minimum max exposure time (float)
 * - max_exposure_time_max: maximum max exposure time (float)
 * - redshift_quality: comma-separated list of quality codes (e.g., "1,2,3")
 *
 * Flag filters (each supports three modes):
 * - spectral_features: legacy single mask (match any)
 * - spectral_features_include_any: match any of these flags (OR)
 * - spectral_features_include_all: must have all of these flags (AND)
 * - spectral_features_exclude: must NOT have any of these flags (NOT)
 * (same pattern for dq_flags)
 *
 * - inspected_only: "true" to filter to inspected objects only
 * - search: text search on target_id
 * - search_scope: search scope (target_id, my_comments, all_comments; default: target_id)
 * - ra: right ascension for cone search (degrees)
 * - dec: declination for cone search (degrees)
 * - radius: search radius (arcsec)
 * - limit: maximum number of results (default: 1000)
 * - offset: pagination offset (default: 0)
 * - sort: sort column (target_id, ra, dec, redshift, redshift_quality, field, observation, max_snr, max_exposure_time, distance)
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
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json({
        data: [],
        pagination: {
          total: 0,
          limit: 0,
          offset: 0,
        },
      });
    }

    const searchParams = request.nextUrl.searchParams;

    // Parse URL params into canonical filter format, then build RPC params
    const filters = parseUrlToFilters(searchParams, accessibleProgramSlugs);
    const rpcParams = buildFilterParams(filters, accessibleProgramSlugs, userId);

    // Pagination
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const page = Math.floor(offset / limit) + 1;

    // Sorting (validate column)
    const sortColumn = searchParams.get('sort') || 'target_id';
    const sortDirection = searchParams.get('sort_dir') || 'asc';
    const validSortColumns = ['target_id', 'ra', 'dec', 'redshift', 'redshift_quality', 'field', 'observation', 'max_snr', 'max_exposure_time', 'distance'];
    const finalSortColumn = validSortColumns.includes(sortColumn) ? sortColumn : 'target_id';

    // Incremental sync filter (ISO 8601 timestamp)
    const updatedSince = searchParams.get('updated_since') || null;

    // Create Supabase client with service role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Call the RPC function
    const { data, error } = await supabase.rpc('get_filtered_targets_paginated', {
      ...rpcParams,
      p_sort_column: finalSortColumn,
      p_sort_direction: sortDirection,
      p_page: page,
      p_page_size: limit,
      p_updated_since: updatedSince,
    });

    if (error) {
      console.error('Error fetching targets:', error);
      return NextResponse.json(
        { error: 'Failed to fetch targets', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { targets: [], total_count: 0 };

    // Return API response
    return NextResponse.json({
      data: result.targets || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        offset,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/targets:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
