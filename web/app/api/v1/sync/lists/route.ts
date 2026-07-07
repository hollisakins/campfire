import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { isAdminUserCached } from '@/lib/api-helpers';

/**
 * GET /api/v1/sync/lists
 *
 * Returns all list metadata (system + public + user's own) for
 * Python client sync. Calls the get_lists_for_sync RPC which
 * returns a single JSONB array with member counts.
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
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // List member counts may reference unpublished-backed objects; admins can
    // opt in to include them, everyone else is fail-closed. No-op in B1.
    const includeUnpublished =
      request.nextUrl.searchParams.get('include_unpublished') === 'true' && (await isAdminUserCached(userId));

    const { data, error } = await supabase.rpc('get_lists_for_sync', {
      p_user_id: userId,
      p_include_unpublished: includeUnpublished,
    });

    if (error) {
      console.error('Error fetching lists for sync:', error);
      return NextResponse.json(
        { error: 'Failed to fetch lists', details: error.message },
        { status: 500 }
      );
    }

    return NextResponse.json({
      data: data || [],
    });
  } catch (error) {
    console.error('Error in /api/v1/sync/lists:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
