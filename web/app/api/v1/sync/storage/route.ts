import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
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
 * - after: keyset cursor — integer id of the previous page's last row (#103).
 *          O(log N + limit) per page. The only pagination since T2-F (#511):
 *          a non-zero `offset` is refused with 400 and an upgrade message.
 * - include_counts: 'false' to skip total/accessible counts (default true)
 * - product_types: comma-separated product kinds to return (default: all).
 *          The Python client mirrors only the kinds it can download —
 *          finals by default — instead of paging the whole registry.
 * - observations / fields: comma-separated scope (union) for a per-scope
 *          refresh, e.g. the intermediates of one observation before
 *          `campfire pull --intermediate`.
 *
 * Response carries `deleted_ids` (tombstones) on the first incremental page:
 * see the RPC.
 */
import { rejectLegacyOffset } from '@/lib/api-sync-pagination';

/** Comma-separated query value → trimmed non-empty items, or null if absent. */
function parseList(raw: string | null): string[] | null {
  if (raw === null) return null;
  const items = raw.split(',').map((s) => s.trim()).filter(Boolean);
  return items.length > 0 ? items : null;
}

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
    const legacy = rejectLegacyOffset(searchParams);
    if (legacy) return legacy;
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const afterRaw = searchParams.get('after');
    const afterId = afterRaw ? parseInt(afterRaw, 10) : null;
    const updatedSince = searchParams.get('updated_since') || null;
    const includeCounts = searchParams.get('include_counts') !== 'false';
    const productTypes = parseList(searchParams.get('product_types'));
    const observations = parseList(searchParams.get('observations'));
    const fields = parseList(searchParams.get('fields'));

    const supabase = createServiceClient();

    const baseArgs = {
      p_program_slugs: accessibleProgramSlugs,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_include_counts: includeCounts,
      // Admins mirror everything (drafts + field-only products); everyone else
      // is fail-closed to published, in-program rows.
      p_include_unpublished: admin,
      p_after_id: afterId,
    };
    // The scope arguments are sent only when the client asked for a scope, so
    // an unscoped call still resolves against the pre-scope RPC signature.
    const scopeArgs: Record<string, string[]> = {};
    if (productTypes) scopeArgs.p_product_types = productTypes;
    if (observations) scopeArgs.p_observations = observations;
    if (fields) scopeArgs.p_fields = fields;
    const scoped = Object.keys(scopeArgs).length > 0;

    let { data, error } = await supabase.rpc('get_storage_objects_for_sync', {
      ...baseArgs,
      ...scopeArgs,
    });

    // Deploy window: the Vercel build and the Supabase migration land
    // independently on merge. If this build runs against an RPC that does not
    // know the scope parameters yet (PostgREST: no matching function), fall
    // back to the unscoped call — the client filters the rows it receives, so
    // the only cost is an unfiltered page in that window.
    if (error && scoped && error.code === 'PGRST202') {
      console.warn('sync storage: RPC without scope parameters; retrying unscoped');
      ({ data, error } = await supabase.rpc('get_storage_objects_for_sync', baseArgs));
    }

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
        after: afterId,
      },
      total_accessible_count: result.total_accessible_count || 0,
      // Tombstones: ids of registry rows that left the visible set since
      // `updated_since` (superseded / revoked, or their spectrum un-published);
      // first incremental page only, absent from an RPC predating the column.
      deleted_ids: result.deleted_ids ?? [],
    });
  } catch (error) {
    console.error('Error in API /v1/sync/storage:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
