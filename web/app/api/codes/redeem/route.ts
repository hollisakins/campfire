import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

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

  // Check if user is a group account
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_group_account')
    .eq('user_id', user.id)
    .single();

  if (profile?.is_group_account) {
    return NextResponse.json(
      { error: 'Group accounts cannot redeem access codes' },
      { status: 403 }
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
    let programSlugs: string[] = [];

    if (accessCode.grants_all_programs) {
      // Get all program slugs
      const { data: programs } = await supabase
        .from('programs')
        .select('slug');

      programSlugs = (programs || []).map(p => p.slug);
    } else if (accessCode.program_slugs && accessCode.program_slugs.length > 0) {
      programSlugs = accessCode.program_slugs;
    }

    // Validate that code grants at least one program
    if (programSlugs.length === 0) {
      return NextResponse.json(
        { error: 'This code does not grant access to any programs' },
        { status: 400 }
      );
    }

    // Use service client for system operations (granting access, incrementing counters)
    const serviceClient = createServiceClient();

    // Grant program access
    if (programSlugs.length > 0) {
      const accessRecords = programSlugs.map(programSlug => ({
        user_id: user.id,
        program_slug: programSlug,
        granted_by: accessCode.created_by,
      }));

      // Use upsert to avoid duplicates
      const { error: accessError } = await serviceClient
        .from('user_program_access')
        .upsert(accessRecords, {
          onConflict: 'user_id,program_slug',
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
    const { error: incrementError } = await serviceClient
      .from('access_codes')
      .update({ use_count: accessCode.use_count + 1 })
      .eq('id', accessCode.id);

    if (incrementError) {
      console.error('Error incrementing use count:', incrementError);
      // Don't fail - access was already granted
    }

    return NextResponse.json({
      success: true,
      message: accessCode.grants_all_programs
        ? 'Access granted to all programs'
        : `Access granted to ${programSlugs.length} program(s)`,
      programs_granted: programSlugs.length,
    });

  } catch (error) {
    console.error('Error redeeming code:', error);
    return NextResponse.json(
      { error: 'Failed to redeem access code' },
      { status: 500 }
    );
  }
}
