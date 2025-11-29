import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * POST /api/codes/redeem
 *
 * Redeems an access code for the current user.
 * Body: { code: string }
 */
export async function POST(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  try {
    const body = await request.json();
    const { code } = body;

    if (!code || typeof code !== 'string') {
      return NextResponse.json(
        { error: 'Access code is required' },
        { status: 400 }
      );
    }

    // Normalize code (uppercase, trim)
    const normalizedCode = code.trim().toUpperCase();

    // Find the access code
    const { data: accessCode, error: codeError } = await supabase
      .from('access_codes')
      .select('*')
      .eq('code', normalizedCode)
      .eq('is_active', true)
      .single();

    if (codeError || !accessCode) {
      return NextResponse.json(
        { error: 'Invalid access code' },
        { status: 404 }
      );
    }

    // Check if expired
    if (accessCode.expires_at && new Date(accessCode.expires_at) < new Date()) {
      return NextResponse.json(
        { error: 'This access code has expired' },
        { status: 410 }
      );
    }

    // Check if max uses reached
    if (accessCode.max_uses && accessCode.use_count >= accessCode.max_uses) {
      return NextResponse.json(
        { error: 'This access code has reached its maximum uses' },
        { status: 410 }
      );
    }

    // Check if user already redeemed this code
    const { data: existingRedemption } = await supabase
      .from('code_redemptions')
      .select('id')
      .eq('code_id', accessCode.id)
      .eq('user_id', user.id)
      .single();

    if (existingRedemption) {
      return NextResponse.json(
        { error: 'You have already redeemed this code' },
        { status: 409 }
      );
    }

    // Determine which programs to grant
    let programIds: number[] = [];

    if (accessCode.grants_all_programs) {
      // Get all program IDs
      const { data: programs } = await supabase
        .from('programs')
        .select('program_id');

      programIds = (programs || []).map(p => p.program_id);
    } else if (accessCode.program_ids && accessCode.program_ids.length > 0) {
      programIds = accessCode.program_ids;
    }

    // Grant program access
    if (programIds.length > 0) {
      const accessRecords = programIds.map(programId => ({
        user_id: user.id,
        program_id: programId,
        granted_by: accessCode.created_by,
      }));

      // Use upsert to avoid duplicates
      const { error: accessError } = await supabase
        .from('user_program_access')
        .upsert(accessRecords, {
          onConflict: 'user_id,program_id',
          ignoreDuplicates: true
        });

      if (accessError) {
        console.error('Error granting program access:', accessError);
        return NextResponse.json(
          { error: 'Failed to grant program access' },
          { status: 500 }
        );
      }
    }

    // Record the redemption
    const { error: redemptionError } = await supabase
      .from('code_redemptions')
      .insert({
        code_id: accessCode.id,
        user_id: user.id,
      });

    if (redemptionError) {
      console.error('Error recording redemption:', redemptionError);
      // Don't fail - access was already granted
    }

    // Increment use count
    await supabase
      .from('access_codes')
      .update({ use_count: accessCode.use_count + 1 })
      .eq('id', accessCode.id);

    return NextResponse.json({
      success: true,
      message: accessCode.grants_all_programs
        ? 'Access granted to all programs'
        : `Access granted to ${programIds.length} program(s)`,
      programs_granted: programIds.length,
    });

  } catch (error) {
    console.error('Error redeeming code:', error);
    return NextResponse.json(
      { error: 'Failed to redeem access code' },
      { status: 500 }
    );
  }
}
