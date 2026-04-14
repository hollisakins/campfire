import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';

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
 * - offset: pagination offset (default 0)
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
    const limit = parseInt(searchParams.get('limit') || '1000', 10);
    const offset = parseInt(searchParams.get('offset') || '0', 10);
    const updatedSince = searchParams.get('updated_since') || null;

    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    const { data, error } = await supabase.rpc('get_photometry_for_sync', {
      p_program_slugs: accessibleProgramSlugs,
      p_updated_since: updatedSince,
      p_limit: limit,
      p_offset: offset,
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
        offset,
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
