import { NextRequest, NextResponse } from 'next/server';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser, parseCSV, parseIntCSV, resolveListIds } from '@/lib/api-helpers';
import { createServiceClient } from '@/lib/supabase/server';
import { convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';
import {
  cursorFingerprint,
  cursorRpcParams,
  decodeCursor,
  deprecationHeaders,
  encodeNextCursor,
  parseIncludeCount,
  type CursorPayload,
  type ListPagination,
} from '@/lib/api-cursor';

/**
 * GET /api/v1/objects
 *
 * Query objects (cross-program grouped sky positions) with filters for the
 * Python API. Objects carry inspection state and embed their member spectra.
 *
 * Query parameters mirror /api/v1/targets's vocabulary where it still makes
 * sense. `spectral_features` is gone (Phase E); `dq_flags` resolves to
 * per-spectrum filtering via the underlying RPC.
 *
 * Pagination (perf T2-F, #511, decision D-F): keyset. Each response carries
 * `pagination.next_cursor` (null on the last page); pass it back as
 * `cursor=` with the same filters and sort to get the next page at a cost
 * that does not grow with depth. `count=false` skips the total (it is skipped
 * by default on cursor pages; `total` is then -1). `offset=` is still
 * honoured for one client release and answered with a `Deprecation` header.
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
        pagination: { total: 0, limit: 0, offset: 0, has_more: false, next_cursor: null },
      });
    }

    const searchParams = request.nextUrl.searchParams;
    const supabase = createServiceClient();

    // Objects with no published spectrum are admin-only, gated behind an
    // explicit opt-in (include_unpublished=true). Fail-closed otherwise
    // (no-op in B1 since every object has a published spectrum).
    const includeUnpublished =
      searchParams.get('include_unpublished') === 'true' && (await isAdminUser(userId));

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

    // Pagination (limit 1..10000 so limit=0 can't produce NaN pages and a
    // single request can't ask for an unbounded page). `cursor` and a
    // non-zero `offset` are mutually exclusive.
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const rawOffset = searchParams.get('offset');
    const offset = rawOffset === null ? 0 : parseInt(rawOffset, 10);
    if (!Number.isFinite(limit) || limit < 1 || limit > 10000 || !Number.isFinite(offset) || offset < 0) {
      return NextResponse.json(
        { error: 'Invalid pagination: limit must be 1-10000 and offset must be >= 0' },
        { status: 400 }
      );
    }
    const rawCursor = searchParams.get('cursor');
    if (rawCursor && offset > 0) {
      return NextResponse.json(
        { error: 'Invalid pagination: pass either cursor or offset, not both' },
        { status: 400 }
      );
    }
    const fingerprint = cursorFingerprint(searchParams);
    let cursor: CursorPayload | null = null;
    if (rawCursor) {
      const decoded = decodeCursor(rawCursor, fingerprint);
      if (!decoded.ok) {
        return NextResponse.json({ error: decoded.error }, { status: 400 });
      }
      cursor = decoded.cursor;
    }
    const includeCount = parseIncludeCount(searchParams, cursor !== null);
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
      p_needs_review: needsReview,
      p_list_ids: listIds,
      p_list_ids_mode: searchParams.get('list_ids_mode') || 'any',
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
      p_include_unpublished: includeUnpublished,
      p_include_count: includeCount,
      ...cursorRpcParams(cursor),
    };

    const { data, error } = await supabase.rpc('get_filtered_objects_paginated', rpcParams);

    if (error) {
      console.error('Error fetching objects:', error);
      return NextResponse.json(
        { error: 'Failed to fetch objects', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { targets: [], total_count: 0, has_more: false };

    // total is -1 when the count was skipped (cursor pages by default).
    const rawTotal = Number(result.total_count);
    const pagination: ListPagination = {
      total: includeCount && Number.isFinite(rawTotal) ? rawTotal : -1,
      limit,
      has_more: Boolean(result.has_more),
      next_cursor: encodeNextCursor(result, fingerprint),
    };
    const headers: Record<string, string> = {};
    if (!cursor) {
      // Legacy response shape for offset callers (offset: 0 on a plain first
      // page). The deprecation notice goes only to callers that actually
      // sent offset=.
      pagination.offset = offset;
      if (rawOffset !== null) {
        Object.assign(headers, deprecationHeaders(new URL('/docs/api/rest#pagination', request.nextUrl.origin).toString()));
      }
    }

    return NextResponse.json({ data: result.targets || [], pagination }, { headers });
  } catch (error) {
    console.error('Error in API /v1/objects:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
