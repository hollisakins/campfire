import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/codes
 *
 * List all access codes (admin only)
 */
export async function GET() {
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  // Check if user is admin
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    // Fetch all codes with redemption counts
    const { data: codes, error } = await supabase
      .from('access_codes')
      .select(`
        *,
        code_redemptions (
          id,
          user_id,
          redeemed_at
        )
      `)
      .order('created_at', { ascending: false });

    if (error) {
      console.error('Error fetching codes:', error);
      return NextResponse.json({ error: 'Failed to fetch codes' }, { status: 500 });
    }

    return NextResponse.json({ codes });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch codes' }, { status: 500 });
  }
}

/**
 * POST /api/codes
 *
 * Create a new access code (admin only)
 * Body: { code, description, grants_all_programs, program_ids, expires_at, max_uses }
 */
export async function POST(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  // Check if user is admin
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    const body = await request.json();
    const {
      code,
      description,
      grants_all_programs = false,
      program_ids = null,
      expires_at = null,
      max_uses = null,
    } = body;

    if (!code || typeof code !== 'string') {
      return NextResponse.json({ error: 'Code is required' }, { status: 400 });
    }

    // Normalize code
    const normalizedCode = code.trim().toUpperCase();

    if (normalizedCode.length < 4) {
      return NextResponse.json({ error: 'Code must be at least 4 characters' }, { status: 400 });
    }

    // Create the code
    const { data: newCode, error } = await supabase
      .from('access_codes')
      .insert({
        code: normalizedCode,
        description: description || null,
        grants_all_programs,
        program_ids: grants_all_programs ? null : program_ids,
        expires_at: expires_at || null,
        max_uses: max_uses || null,
        created_by: user.id,
        is_active: true,
        use_count: 0,
      })
      .select()
      .single();

    if (error) {
      if (error.code === '23505') {
        return NextResponse.json({ error: 'A code with this name already exists' }, { status: 409 });
      }
      console.error('Error creating code:', error);
      return NextResponse.json({ error: 'Failed to create code' }, { status: 500 });
    }

    return NextResponse.json({ code: newCode }, { status: 201 });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to create code' }, { status: 500 });
  }
}
