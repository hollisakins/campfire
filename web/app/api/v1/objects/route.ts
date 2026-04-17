import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';

function parseCSV(value: string | null): string[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => s.trim()).filter(s => s.length > 0);
  return items.length > 0 ? items : null;
}

function parseIntCSV(value: string | null): number[] | null {
  if (!value) return null;
  const items = value.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
  return items.length > 0 ? items : null;
}

/**
 * GET /api/v1/objects
 *
 * Query objects (cross-program grouped sky positions) with filters for the
 * Python API. Objects carry inspection state and embed their member spectra.
 *
 * Query parameters mirror /api/v1/targets's vocabulary where it still makes
 * sense. `spectral_features` is gone (Phase E); `dq_flags` resolves to
 * per-spectrum filtering via the underlying RPC.
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

    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Programs filter (intersect with accessible)
    const programsParam = parseCSV(searchParams.get('programs'));
    const filterPrograms = programsParam
      ? programsParam.filter(p => accessibleProgramSlugs.includes(p))
      : null;

    // Coordinate search
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

    // List slugs → IDs
    const listSlugs = parseCSV(searchParams.get('lists'));
    let listIds: number[] | null = null;
    if (listSlugs && listSlugs.length > 0) {
      const { data: listRows } = await supabase
        .from('object_lists')
        .select('id')
        .in('slug', listSlugs);
      listIds = (listRows ?? []).map(r => r.id);
    }

    const inspectedOnlyParam = searchParams.get('inspected_only');
    const inspectedOnly = inspectedOnlyParam
      ? inspectedOnlyParam.toLowerCase() === 'true'
      : null;

    const hasPhotometryParam = searchParams.get('has_photometry');
    const hasPhotometry = hasPhotometryParam
      ? hasPhotometryParam.toLowerCase() === 'true'
      : null;

    // Pagination
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const page = Math.floor(offset / limit) + 1;

    // Sort
    const validSortColumns = [
      'object_id', 'ra', 'dec', 'redshift', 'redshift_quality', 'field',
      'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z', 'distance',
    ];
    const sortColumn = searchParams.get('sort') || 'object_id';
    const sortDirection = searchParams.get('sort_dir') || 'asc';
    const finalSortColumn = validSortColumns.includes(sortColumn) ? sortColumn : 'object_id';

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
      p_search: searchParams.get('search') || null,
      p_inspected_only: inspectedOnly,
      p_list_ids: listIds,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_has_photometry: hasPhotometry,
      p_photo_z_min: searchParams.get('photo_z_min') ? parseFloat(searchParams.get('photo_z_min')!) : null,
      p_photo_z_max: searchParams.get('photo_z_max') ? parseFloat(searchParams.get('photo_z_max')!) : null,
      p_sort_column: finalSortColumn,
      p_sort_direction: sortDirection,
      p_page: page,
      p_page_size: limit,
    };

    const { data, error } = await supabase.rpc('get_filtered_objects_paginated', rpcParams);

    if (error) {
      console.error('Error fetching objects:', error);
      return NextResponse.json(
        { error: 'Failed to fetch objects', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { targets: [], total_count: 0 };

    return NextResponse.json({
      data: result.targets || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        offset,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/objects:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
