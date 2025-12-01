import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * PATCH /api/codes/[id]
 *
 * Update an access code (toggle active, update fields)
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

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
    const { is_active, description, expires_at, max_uses } = body;

    const updates: Record<string, unknown> = {};
    if (typeof is_active === 'boolean') updates.is_active = is_active;
    if (description !== undefined) updates.description = description;
    if (expires_at !== undefined) updates.expires_at = expires_at;
    if (max_uses !== undefined) updates.max_uses = max_uses;

    if (Object.keys(updates).length === 0) {
      return NextResponse.json({ error: 'No updates provided' }, { status: 400 });
    }

    const { data: updatedCode, error } = await supabase
      .from('access_codes')
      .update(updates)
      .eq('id', id)
      .select()
      .single();

    if (error) {
      console.error('Error updating code:', error);
      return NextResponse.json({ error: 'Failed to update code' }, { status: 500 });
    }

    return NextResponse.json({ code: updatedCode });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update code' }, { status: 500 });
  }
}

/**
 * DELETE /api/codes/[id]
 *
 * Soft-delete an access code by setting is_active = false
 * Preserves redemption history
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const supabase = await createClient();

  // Check authentication and admin status
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  }

  try {
    // Soft-delete by setting is_active to false
    const { data: deletedCode, error } = await supabase
      .from('access_codes')
      .update({ is_active: false })
      .eq('id', id)
      .select()
      .single();

    if (error) {
      console.error('Error deactivating code:', error);
      return NextResponse.json({ error: 'Failed to deactivate code' }, { status: 500 });
    }

    return NextResponse.json({ success: true, code: deletedCode });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to deactivate code' }, { status: 500 });
  }
}
