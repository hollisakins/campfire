import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { isAdminUser } from '@/lib/api-helpers';

/**
 * GET /api/v1/storage/budget
 *
 * Admin-only wrapper over the get_storage_budget() RPC, so `campfire status`
 * can show the global cap line (bytes-at-rest vs cap, by product type/backend).
 * The cap is an operator concern, so this stays admin-gated even though the
 * registry rows themselves are program-scoped.
 */
export async function GET(request: NextRequest) {
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }

  if (!(await isAdminUser(userId))) {
    return NextResponse.json(
      { error: 'Admin privileges required' },
      { status: 403 }
    );
  }

  try {
    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    const { data, error } = await supabase.rpc('get_storage_budget');
    if (error) {
      console.error('Error fetching storage budget:', error);
      return NextResponse.json(
        { error: 'Failed to fetch storage budget', details: error.message },
        { status: 500 }
      );
    }

    return NextResponse.json(data);
  } catch (error) {
    console.error('Error in API /v1/storage/budget:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
