import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';

/**
 * GET /api/v1/observations
 *
 * List available observations with aggregate stats.
 * Requires API key or JWT authentication.
 *
 * Returns observation name, program info, object/spectrum counts, and total file size.
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
      return NextResponse.json({ observations: [] });
    }

    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    const { data, error } = await supabase.rpc('get_observation_stats', {
      p_program_slugs: accessibleProgramSlugs,
    });

    if (error) {
      console.error('Error fetching observation stats:', error);
      return NextResponse.json(
        { error: 'Failed to fetch observations' },
        { status: 500 }
      );
    }

    return NextResponse.json({ observations: data || [] });
  } catch (error) {
    console.error('Error in GET /api/v1/observations:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
