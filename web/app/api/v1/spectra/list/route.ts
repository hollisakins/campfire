import { NextRequest, NextResponse } from 'next/server';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, parseCSV, parseIntCSV, resolveListIds } from '@/lib/api-helpers';
import { createServiceClient } from '@/lib/supabase/server';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';

function parseFlagMode(
  params: URLSearchParams,
  prefix: string,
): { any: number | null; all: number | null; none: number | null } {
  const includeAny = params.get(`${prefix}_include_any`);
  const includeAll = params.get(`${prefix}_include_all`);
  const exclude = params.get(`${prefix}_exclude`);
  const legacy = params.get(prefix);
  if (includeAll) return { any: null, all: parseInt(includeAll, 10), none: null };
  if (exclude) return { any: null, all: null, none: parseInt(exclude, 10) };
  const anyVal = includeAny || legacy;
  if (anyVal) return { any: parseInt(anyVal, 10), all: null, none: null };
  return { any: null, all: null, none: null };
}

/**
 * GET /api/v1/spectra/list
 *
 * Flat list of spectra (one row per spectrum) with filters. Separate from
 * /api/v1/spectra (the signed-URL download endpoint) to avoid a collision
 * on the base path.
 */
export async function GET(request: NextRequest) {
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json({ error: 'Invalid or missing API key' }, { status: 401 });
  }

  try {
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json({
        data: [],
        pagination: { total: 0, limit: 0, offset: 0 },
      });
    }

    const searchParams = request.nextUrl.searchParams;
    const supabase = createServiceClient();

    const programsParam = parseCSV(searchParams.get('programs'));
    const filterPrograms = programsParam
      ? programsParam.filter(p => accessibleProgramSlugs.includes(p))
      : null;

    const ra = searchParams.get('ra');
    const dec = searchParams.get('dec');
    const radius = searchParams.get('radius');
    let coordRa: number | null = null;
    let coordDec: number | null = null;
    let radiusDegrees: number | null = null;
    if (ra && dec && radius) {
      coordRa = parseFloat(ra);
      coordDec = parseFloat(dec);
      radiusDegrees = convertRadiusToDegrees(parseFloat(radius), 'arcsec');
    }

    const listIds = await resolveListIds(supabase, parseCSV(searchParams.get('lists')));

    const inspectedOnlyParam = searchParams.get('inspected_only');
    const inspectedOnly = inspectedOnlyParam
      ? inspectedOnlyParam.toLowerCase() === 'true'
      : null;

    const needsReviewParam = searchParams.get('needs_review');
    const needsReview = needsReviewParam
      ? needsReviewParam.toLowerCase() === 'true'
      : null;

    const hasPhotometryParam = searchParams.get('has_photometry');
    const hasPhotometry = hasPhotometryParam
      ? hasPhotometryParam.toLowerCase() === 'true'
      : null;

    const dq = parseFlagMode(searchParams, 'dq_flags');

    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const page = Math.floor(offset / limit) + 1;

    const validSortColumns = [
      'target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'ra', 'dec',
      'redshift', 'redshift_quality', 'redshift_auto', 'signal_to_noise',
      'exposure_time', 'grating', 'distance',
    ];
    const sortColumn = searchParams.get('sort') || 'spectrum_id';
    const sortDirection = searchParams.get('sort_dir') || 'asc';
    const finalSortColumn = validSortColumns.includes(sortColumn) ? sortColumn : 'spectrum_id';

    const rpcParams = {
      p_program_slugs: accessibleProgramSlugs,
      p_filter_programs: filterPrograms && filterPrograms.length > 0 ? filterPrograms : null,
      p_fields: parseCSV(searchParams.get('fields')),
      p_gratings: parseCSV(searchParams.get('gratings')),
      p_gratings_mode: searchParams.get('gratings_mode') || 'any',
      p_observations: parseCSV(searchParams.get('observations')),
      p_redshift_quality: parseIntCSV(searchParams.get('redshift_quality')),
      p_redshift_min: searchParams.get('redshift_min') ? parseFloat(searchParams.get('redshift_min')!) : null,
      p_redshift_max: searchParams.get('redshift_max') ? parseFloat(searchParams.get('redshift_max')!) : null,
      p_max_snr_min: searchParams.get('max_snr_min') ? parseFloat(searchParams.get('max_snr_min')!) : null,
      p_max_snr_max: searchParams.get('max_snr_max') ? parseFloat(searchParams.get('max_snr_max')!) : null,
      p_max_exposure_time_min: searchParams.get('max_exposure_time_min') ? parseFloat(searchParams.get('max_exposure_time_min')!) : null,
      p_max_exposure_time_max: searchParams.get('max_exposure_time_max') ? parseFloat(searchParams.get('max_exposure_time_max')!) : null,
      p_dq_flags_include_any: dq.any,
      p_dq_flags_include_all: dq.all,
      p_dq_flags_exclude: dq.none,
      p_list_ids: listIds,
      p_search: searchParams.get('search') || null,
      p_inspected_only: inspectedOnly,
      p_needs_review: needsReview,
      p_has_photometry: hasPhotometry,
      p_comment_search: null,
      p_comment_search_scope: null,
      p_comment_user_id: null,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_sort_column: finalSortColumn,
      p_sort_direction: sortDirection,
      p_page: page,
      p_page_size: limit,
      p_include_thumbnails: false,
    };

    const { data, error } = await supabase.rpc('get_filtered_spectra_paginated', rpcParams);

    if (error) {
      console.error('Error fetching spectra:', error);
      return NextResponse.json(
        { error: 'Failed to fetch spectra', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { targets: [], total_count: 0 };

    // The RPC returns one row per spectrum wrapped in a target-shaped object;
    // the Python client wants a flat spectra list, so hoist the single
    // spectrum entry up to the top level.
    type RpcRow = Record<string, unknown> & { spectra?: unknown[] };
    const flat = (result.targets || []).map((row: RpcRow) => {
      const spectra = Array.isArray(row.spectra) ? row.spectra : [];
      const spec = (spectra[0] as Record<string, unknown> | undefined) ?? {};
      const { spectra: _drop, ...parent } = row;
      void _drop;
      return { ...parent, ...spec };
    });

    return NextResponse.json({
      data: flat,
      pagination: {
        total: result.total_count || 0,
        limit,
        offset,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/spectra/list:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
