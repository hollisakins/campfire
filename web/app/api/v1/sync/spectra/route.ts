import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessibleProgramsCached, isAdminUserCached } from '@/lib/api-helpers';

/**
 * GET /api/v1/sync/spectra
 *
 * Lightweight endpoint for Python client spectra catalog sync. Returns flat
 * per-spectrum rows with download-relevant fields, paginated, with optional
 * incremental filtering via updated_since.
 *
 * Query parameters:
 * - updated_since: ISO 8601 timestamp (only return spectra updated after this)
 * - limit: page size (default 1000)
 * - after: keyset cursor — spectrum_id of the previous page's last row (#103).
 *          Preferred over offset; O(log N + limit) per page.
 * - offset: legacy pagination offset (default 0); kept for old clients.
 * - include_counts: 'false' to skip total_count / total_accessible_count (default true)
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
    const accessibleProgramSlugs = await getAccessibleProgramsCached(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json({
        data: [],
        pagination: { total: 0, limit: 0, offset: 0 },
      });
    }

    const searchParams = request.nextUrl.searchParams;
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const afterSpectrumId = searchParams.get('after') || null;
    const updatedSince = searchParams.get('updated_since') || null;
    const includeCounts = searchParams.get('include_counts') !== 'false';

    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Admins can opt in to syncing unpublished spectra; everyone else is
    // fail-closed to published rows only. No-op in B1.
    const includeUnpublished =
      searchParams.get('include_unpublished') === 'true' && (await isAdminUserCached(userId));

    const { data, error } = await supabase.rpc('get_spectra_for_sync', {
      p_program_slugs: accessibleProgramSlugs,
      p_user_id: userId,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_offset: offset,
      p_include_counts: includeCounts,
      p_include_unpublished: includeUnpublished,
      p_after_spectrum_id: afterSpectrumId,
    });

    if (error) {
      console.error('Error in sync spectra:', error);
      return NextResponse.json(
        { error: 'Failed to fetch spectra', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { spectra: [], total_count: 0, total_accessible_count: 0 };

    return NextResponse.json({
      data: result.spectra || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        offset,
      },
      total_accessible_count: result.total_accessible_count || 0,
    });
  } catch (error) {
    console.error('Error in API /v1/sync/spectra:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
