import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * PATCH /api/objects/[id]/inspect
 *
 * Object-level inspection writes (redshift_inspected / redshift_quality).
 * Per-spectrum DQ flags go to /api/spectra/[id]/dq instead.
 *
 * Body:
 *   { redshift_inspected?: number | null,
 *     redshift_quality?: number,
 *     expected_version: number }
 *
 * Concurrency: optimistic locking via expected_version. The DB trigger
 * `bump_object_version` increments objects.version when these two fields
 * change. We only accept the write if the caller's expected_version matches
 * the current row; mismatch returns 409 Conflict and the UI prompts a
 * refresh (no merge UI per design doc).
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const objectDbId = parseInt(id, 10);

  if (isNaN(objectDbId)) {
    return NextResponse.json({ error: 'Invalid object ID' }, { status: 400 });
  }

  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment')
    .eq('user_id', user.id)
    .single();

  if (!profile?.can_comment) {
    return NextResponse.json({ error: 'Permission denied' }, { status: 403 });
  }

  let body: {
    redshift_inspected?: number | string | null;
    redshift_quality?: number;
    expected_version?: number;
  };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const { redshift_inspected, redshift_quality, expected_version } = body;

  if (typeof expected_version !== 'number') {
    return NextResponse.json(
      { error: 'expected_version is required (number)' },
      { status: 400 }
    );
  }

  // Fetch the current row to (a) verify access and (b) check the version.
  const { data: current, error: fetchError } = await supabase
    .from('objects')
    .select('id, version, redshift_inspected, redshift_quality, is_active')
    .eq('id', objectDbId)
    .single();

  if (fetchError || !current) {
    return NextResponse.json({ error: 'Object not found' }, { status: 404 });
  }

  if (!current.is_active) {
    return NextResponse.json(
      { error: 'Object is inactive — admin must reactivate before edits' },
      { status: 409 }
    );
  }

  if (current.version !== expected_version) {
    return NextResponse.json(
      {
        error: 'version_conflict',
        message: 'Inspection state has been changed, please refresh.',
        current_version: current.version,
      },
      { status: 409 }
    );
  }

  const updates: Record<string, unknown> = {
    last_inspected_at: new Date().toISOString(),
    last_inspected_by: user.id,
  };

  if (redshift_inspected !== undefined) {
    if (redshift_inspected === null || redshift_inspected === '') {
      updates.redshift_inspected = null;
    } else {
      const parsed =
        typeof redshift_inspected === 'number'
          ? redshift_inspected
          : parseFloat(String(redshift_inspected));
      if (Number.isNaN(parsed)) {
        return NextResponse.json(
          { error: 'redshift_inspected must be a number or null' },
          { status: 400 }
        );
      }
      updates.redshift_inspected = parsed;
    }
  }

  if (redshift_quality !== undefined) {
    const q = Number(redshift_quality);
    if (!Number.isInteger(q) || q < 0 || q > 4) {
      return NextResponse.json(
        { error: 'redshift_quality must be an integer in [0, 4]' },
        { status: 400 }
      );
    }
    updates.redshift_quality = q;
  }

  // The bump_object_version trigger only increments when one of the two
  // user-editable fields actually changes, so re-checking the version on
  // pure no-op writes is harmless.
  // RLS (`update_objects_by_access`) enforces program access + can_comment.
  const { data: updated, error: updateError } = await supabase
    .from('objects')
    .update(updates)
    .eq('id', objectDbId)
    .eq('version', expected_version)  // belt-and-suspenders against TOCTOU
    .select()
    .single();

  if (updateError) {
    console.error('Object inspection update failed:', updateError);
    return NextResponse.json(
      { error: 'Failed to update object', details: updateError.message },
      { status: 500 }
    );
  }

  if (!updated) {
    // The .eq('version', expected_version) clause didn't match → someone
    // raced us between the read above and this UPDATE.
    return NextResponse.json(
      {
        error: 'version_conflict',
        message: 'Inspection state has been changed, please refresh.',
      },
      { status: 409 }
    );
  }

  return NextResponse.json({ object: updated });
}
