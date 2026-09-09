import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { rejectLegacyOffset } from '@/lib/api-sync-pagination';

/**
 * GET /api/v1/sync/lines
 *
 * Bulk fetch for the Python client's emission-line catalog sync
 * (docs/design-emission-line-fitting.md). Returns spectrum_line_fits rows —
 * one per spectrum, with the per-line measurements as JSONB — for programs
 * accessible to the authenticated user, keyset-paginated, with optional
 * incremental filtering via updated_since. Mirrors /sync/photometry.
 *
 * Query parameters:
 * - updated_since: ISO 8601 timestamp (only rows updated after this)
 * - limit: page size (default 1000)
 * - after: keyset cursor — spectrum_id of the previous page's last row.
 *          Keyset-only: a non-zero `offset` is refused with 400.
 * - include_counts: 'false' to skip total_count (default true)
 * - include_unpublished: 'true' (admins only) to include fits of unpublished spectra
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
      return NextResponse.json({
        data: [],
        pagination: { total: 0, limit: 0, offset: 0 },
      });
    }

    const searchParams = request.nextUrl.searchParams;
    const legacy = rejectLegacyOffset(searchParams);
    if (legacy) return legacy;
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const afterRaw = searchParams.get('after');
    const afterId = afterRaw ? parseInt(afterRaw, 10) : null;
    const updatedSince = searchParams.get('updated_since') || null;
    const includeCounts = searchParams.get('include_counts') !== 'false';

    const supabase = createServiceClient();

    // Fits of unpublished spectra are admin-only behind explicit opt-in;
    // fail-closed otherwise (the RPC bypasses RLS).
    const includeUnpublished =
      searchParams.get('include_unpublished') === 'true' && (await isAdminUser(userId));

    const { data, error } = await supabase.rpc('get_line_fits_for_sync', {
      p_program_slugs: accessibleProgramSlugs,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_include_unpublished: includeUnpublished,
      p_include_counts: includeCounts,
      p_after_id: afterId,
    });

    if (error) {
      console.error('Error in sync lines:', error);
      return NextResponse.json(
        { error: 'Failed to fetch line fits', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { line_fit_records: [], total_count: 0 };

    return NextResponse.json({
      data: result.line_fit_records || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        after: afterId,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/sync/lines:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
