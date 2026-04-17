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
 * The DB trigger `track_spectrum_dq_changes` writes an audit row to
 * flag_audit_log (with spectrum_id set, target_id NULL).
 *
 * RLS check: `admin_spectra_update` only allows admins. To keep the UX
 * symmetric with object inspection (which any can_comment user can edit),
 * we bypass via the service-role client after verifying access at the API
 * layer.
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

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment')
    .eq('user_id', user.id)
    .single();

  if (!profile?.can_comment) {
    return NextResponse.json({ error: 'Permission denied' }, { status: 403 });
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

  // Fetch the spectrum to verify access via the parent target's program_slug.
  // RLS on spectra (select_spectra_by_access) enforces this for the SELECT.
  const { data: spectrum, error: fetchError } = await supabase
    .from('spectra')
    .select('id, target_id, dq_flags, targets!inner(program_slug)')
    .eq('id', spectrumId)
    .single();

  if (fetchError || !spectrum) {
    return NextResponse.json({ error: 'Spectrum not found' }, { status: 404 });
  }

  if (spectrum.dq_flags === newFlags) {
    return NextResponse.json({ spectrum, message: 'No change' });
  }

  // RLS on spectra is admin-only for UPDATE. Use a service-role client after
  // the access check above. The trigger logs the change with spectrum_id
  // (subject = spectrum) and the audit row's RLS policy ensures only users
  // who can read the parent target can read it back.
  const { createServiceClient } = await import('@/lib/supabase/server');
  const service = createServiceClient();

  const { data: updated, error: updateError } = await service
    .from('spectra')
    .update({ dq_flags: newFlags })
    .eq('id', spectrumId)
    .select()
    .single();

  if (updateError) {
    console.error('Spectrum dq_flags update failed:', updateError);
    return NextResponse.json(
      { error: 'Failed to update spectrum', details: updateError.message },
      { status: 500 }
    );
  }

  // The audit trigger runs as SECURITY DEFINER but uses auth.uid() — when
  // called via the service-role client, auth.uid() is NULL, so the audit
  // row's user_id would be NULL ("System"). Patch it post-hoc so the
  // activity feed attributes the change correctly.
  await service
    .from('flag_audit_log')
    .update({ user_id: user.id })
    .eq('spectrum_id', spectrumId)
    .eq('field_name', 'dq_flags')
    .is('user_id', null)
    .order('id', { ascending: false })
    .limit(1);

  return NextResponse.json({ spectrum: updated });
}
