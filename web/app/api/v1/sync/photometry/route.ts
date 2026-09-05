import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';

/**
 * GET /api/v1/sync/photometry
 *
 * Bulk fetch for Python client photometry sync.
 * Returns object_photometry records (with JSONB band data) for objects
 * accessible by the authenticated user, paginated, with optional
 * incremental filtering via updated_since.
 *
 * Query parameters:
 * - updated_since: ISO 8601 timestamp (only return records updated after this)
 * - limit: page size (default 1000)
 * - after: keyset cursor — integer id of the previous page's last row (#103).
 *          O(log N + limit) per page. The only pagination since T2-F (#511):
 *          a non-zero `offset` is refused with 400 and an upgrade message.
 * - include_counts: 'false' to skip total_count (default true)
 */
import { rejectLegacyOffset } from '@/lib/api-sync-pagination';

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

    // Photometry for objects with no published spectrum is admin-only and
    // gated behind explicit opt-in. Fail-closed otherwise; no-op in B1.
    const includeUnpublished =
      searchParams.get('include_unpublished') === 'true' && (await isAdminUser(userId));

    const { data, error } = await supabase.rpc('get_photometry_for_sync', {
      p_program_slugs: accessibleProgramSlugs,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_include_unpublished: includeUnpublished,
      p_include_counts: includeCounts,
      p_after_id: afterId,
    });

    if (error) {
      console.error('Error in sync photometry:', error);
      return NextResponse.json(
        { error: 'Failed to fetch photometry', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { photometry_records: [], total_count: 0 };

    return NextResponse.json({
      data: result.photometry_records || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        after: afterId,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/sync/photometry:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
