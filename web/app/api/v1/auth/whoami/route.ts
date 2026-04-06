import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';

/**
 * GET /api/v1/auth/whoami
 *
 * Returns information about the currently authenticated user.
 * Accepts both API keys and JWT access tokens.
 *
 * Response:
 * {
 *   user_id: string,
 *   email: string,
 *   full_name?: string,
 *   created_at: string
 * }
 */
export async function GET(request: NextRequest) {
  // Validate authentication (API key or JWT)
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'unauthorized', error_description: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }

  try {
    // Create Supabase client with service role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Get user info from auth.users
    const { data: userData, error: userError } = await supabase.auth.admin.getUserById(userId);

    if (userError || !userData.user) {
      return NextResponse.json(
        { error: 'not_found', error_description: 'User not found' },
        { status: 404 }
      );
    }

    // Get user profile for additional info
    const { data: profile } = await supabase
      .from('user_profiles')
      .select('full_name, is_admin, created_at')
      .eq('user_id', userId)
      .single();

    return NextResponse.json({
      user_id: userId,
      email: userData.user.email,
      full_name: profile?.full_name || null,
      is_admin: profile?.is_admin || false,
      created_at: profile?.created_at || userData.user.created_at,
    });
  } catch (error) {
    console.error('Error in GET /api/v1/auth/whoami:', error);
    return NextResponse.json(
      { error: 'server_error', error_description: 'Internal server error' },
      { status: 500 }
    );
  }
}
