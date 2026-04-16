import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/targets/[id]
 *
 * Fetch a single target with inspection details.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const targetId = parseInt(id, 10);

  if (isNaN(targetId)) {
    return NextResponse.json({ error: 'Invalid target ID' }, { status: 400 });
  }

  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  try {
    // Fetch the target
    const { data: target, error } = await supabase
      .from('targets')
      .select('*')
      .eq('id', targetId)
      .single();

    if (error) {
      console.error('Error fetching target:', error);
      return NextResponse.json({ error: 'Target not found' }, { status: 404 });
    }

    // Fetch last inspector's name if exists
    let lastInspectorName = null;
    if (target.last_inspected_by) {
      const { data: profile } = await supabase
        .from('user_profiles')
        .select('full_name')
        .eq('user_id', target.last_inspected_by)
        .single();
      lastInspectorName = profile?.full_name || null;
    }

    return NextResponse.json({
      object: {
        ...target,
        last_inspector_name: lastInspectorName,
      },
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to fetch target' }, { status: 500 });
  }
}

/**
 * PATCH /api/targets/[id]
 *
 * Phase D: removed. Target-level inspection state was lifted to objects;
 * write to PATCH /api/objects/[id]/inspect (with expected_version) instead,
 * or PATCH /api/spectra/[id]/dq for per-spectrum DQ flags.
 */
export async function PATCH() {
  return NextResponse.json(
    {
      error: 'gone',
      message:
        'Target inspection has moved to PATCH /api/objects/[id]/inspect. ' +
        'Per-spectrum DQ flags go to PATCH /api/spectra/[id]/dq.',
    },
    { status: 410 }
  );
}
