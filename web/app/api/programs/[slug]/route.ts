import { NextRequest, NextResponse } from 'next/server';
import { invalidateAccessContext } from '@/lib/auth/access-context';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';

/**
 * PATCH /api/programs/[slug]
 *
 * Update a program (toggle public, update fields).
 * Admin only.
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ slug: string }> }
) {
  const { slug } = await params;

  if (!slug) {
    return NextResponse.json({ error: 'Invalid program slug' }, { status: 400 });
  }

  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  if (!(await isAdminUser(user.id))) {
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
      .eq('slug', slug)
      .select()
      .single();

    if (error) {
      console.error('Error updating program:', error);
      return NextResponse.json({ error: 'Failed to update program' }, { status: 500 });
    }

    // A public/private flip changes every non-admin's accessible set, so drop
    // the whole memo on this instance (#505); others converge within the TTL.
    if ('is_public' in updates) invalidateAccessContext();

    return NextResponse.json({ program: updatedProgram });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update program' }, { status: 500 });
  }
}
