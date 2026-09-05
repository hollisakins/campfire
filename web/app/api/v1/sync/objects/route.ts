import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';

/**
 * GET /api/v1/sync/objects
 *
 * Lightweight endpoint for Python client objects catalog sync.
 * Returns objects (cross-program grouped sky positions) with member
 * target IDs, paginated, with optional incremental filtering via
 * updated_since.
 *
 * Query parameters:
 * - updated_since: ISO 8601 timestamp (only return objects updated after this)
 * - limit: page size (default 1000)
 * - after: keyset cursor — object_id of the previous page's last row (#103).
 *          O(log N + limit) per page. The only pagination since T2-F (#511):
 *          a non-zero `offset` is refused with 400 and an upgrade message
 *          (client floor 0.5.0, see /api/v1/version).
 * - include_counts: 'false' to skip total_count / total_accessible_count (default true)
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
    const afterObjectId = searchParams.get('after') || null;
    const updatedSince = searchParams.get('updated_since') || null;
    const includeCounts = searchParams.get('include_counts') !== 'false';

    const supabase = createServiceClient();

    // Admins can opt in to syncing objects with no published spectrum;
    // everyone else is fail-closed to published-backed objects. No-op in B1.
    const includeUnpublished =
      searchParams.get('include_unpublished') === 'true' && (await isAdminUser(userId));

    const { data, error } = await supabase.rpc('get_objects_for_sync', {
      p_program_slugs: accessibleProgramSlugs,
      p_user_id: userId,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_include_counts: includeCounts,
      p_include_unpublished: includeUnpublished,
      p_after_object_id: afterObjectId,
    });

    if (error) {
      console.error('Error in sync objects:', error);
      return NextResponse.json(
        { error: 'Failed to fetch objects', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { objects: [], total_count: 0, total_accessible_count: 0 };

    return NextResponse.json({
      data: result.objects || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        after: afterObjectId,
      },
      total_accessible_count: result.total_accessible_count || 0,
    });
  } catch (error) {
    console.error('Error in API /v1/sync/objects:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
