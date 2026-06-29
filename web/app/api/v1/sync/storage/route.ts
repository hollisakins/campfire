import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';

/**
 * GET /api/v1/sync/storage
 *
 * Catalog sync for the Python client's local `storage_objects` mirror — the
 * single download/availability layer (finals + intermediates + future NIRCam
 * share one engine). Mirrors /api/v1/sync/spectra: paginated, optional
 * incremental filtering via updated_since, counts on the first page only.
 *
 * Scope is program-based (epic #210): admins get a faithful full mirror;
 * non-admins get published, active rows in accessible programs. Enforced in the
 * get_storage_objects_for_sync RPC (this route runs under the service role).
 *
 * Query parameters:
 * - updated_since: ISO 8601 timestamp (only rows updated after this)
 * - limit: page size (default 1000)
 * - offset: pagination offset (default 0)
 * - include_counts: 'false' to skip total/accessible counts (default true)
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
    const admin = await isAdminUser(userId);

    // Non-admins with no program access have nothing to mirror. Admins fall
    // through (the RPC returns the full mirror regardless of program list).
    if (accessibleProgramSlugs.length === 0 && !admin) {
      return NextResponse.json({
        data: [],
        pagination: { total: 0, limit: 0, offset: 0 },
        total_accessible_count: 0,
      });
    }

    const searchParams = request.nextUrl.searchParams;
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const updatedSince = searchParams.get('updated_since') || null;
    const includeCounts = searchParams.get('include_counts') !== 'false';

    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    const { data, error } = await supabase.rpc('get_storage_objects_for_sync', {
      p_program_slugs: accessibleProgramSlugs,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_offset: offset,
      p_include_counts: includeCounts,
      // Admins mirror everything (drafts + field-only products); everyone else
      // is fail-closed to published, in-program rows.
      p_include_unpublished: admin,
    });

    if (error) {
      console.error('Error in sync storage:', error);
      return NextResponse.json(
        { error: 'Failed to fetch storage objects', details: error.message },
        { status: 500 }
      );
    }

    const result = data?.[0] || { objects: [], total_count: 0, total_accessible_count: 0 };

    return NextResponse.json({
      data: result.objects || [],
      pagination: {
        total: result.total_count || 0,
        limit,
        offset,
      },
      total_accessible_count: result.total_accessible_count || 0,
    });
  } catch (error) {
    console.error('Error in API /v1/sync/storage:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
