import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * PATCH /api/spectra/[id]/dq
 *
 * Writes dq_flags to a single spectrum (flags are per-spectrum, so marking
 * one grating doesn't affect siblings).
 *
 * Body:
 *   { dq_flags: number }   // bitmask
 *
 * Auth: `update_spectra_dq_by_access` RLS policy allows can_comment users
 * to update spectra whose parent target is in an accessible program.
 * `enforce_spectra_dq_user_update_scope` trigger restricts non-admins to
 * the dq_flags column. `track_spectrum_dq_changes` trigger records the
 * change to flag_audit_log with auth.uid() attribution.
 */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const spectrumId = parseInt(id, 10);

  if (isNaN(spectrumId)) {
    return NextResponse.json({ error: 'Invalid spectrum ID' }, { status: 400 });
  }

  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  let body: { dq_flags?: number };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  if (typeof body.dq_flags !== 'number' || !Number.isInteger(body.dq_flags) || body.dq_flags < 0) {
    return NextResponse.json(
      { error: 'dq_flags must be a non-negative integer' },
      { status: 400 }
    );
  }
  const newFlags = body.dq_flags;

  const { data: updated, error: updateError } = await supabase
    .from('spectra')
    .update({ dq_flags: newFlags })
    .eq('id', spectrumId)
    .select()
    .single();

  if (updateError) {
    // RLS or column-scope trigger rejection → 403.
    if (updateError.code === '42501' || updateError.code === 'PGRST301') {
      return NextResponse.json({ error: 'Permission denied' }, { status: 403 });
    }
    // No row matched (either nonexistent or filtered by RLS).
    if (updateError.code === 'PGRST116') {
      return NextResponse.json({ error: 'Spectrum not found' }, { status: 404 });
    }
    console.error('Spectrum dq_flags update failed:', updateError);
    return NextResponse.json(
      { error: 'Failed to update spectrum', details: updateError.message },
      { status: 500 }
    );
  }

  return NextResponse.json({ spectrum: updated });
}
