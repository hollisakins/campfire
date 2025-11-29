import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * PATCH /api/programs/[id]
 *
 * Update a program (toggle public, update fields).
 * Admin only.
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const programId = parseInt(id, 10);

  if (isNaN(programId)) {
    return NextResponse.json({ error: 'Invalid program ID' }, { status: 400 });
  }

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
    const { is_public, program_name, description } = body;

    const updates: Record<string, unknown> = {};
    if (typeof is_public === 'boolean') updates.is_public = is_public;
    if (program_name !== undefined) updates.program_name = program_name;
    if (description !== undefined) updates.description = description;

    if (Object.keys(updates).length === 0) {
      return NextResponse.json({ error: 'No updates provided' }, { status: 400 });
    }

    const { data: updatedProgram, error } = await supabase
      .from('programs')
      .update(updates)
      .eq('program_id', programId)
      .select()
      .single();

    if (error) {
      console.error('Error updating program:', error);
      return NextResponse.json({ error: 'Failed to update program' }, { status: 500 });
    }

    return NextResponse.json({ program: updatedProgram });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update program' }, { status: 500 });
  }
}
