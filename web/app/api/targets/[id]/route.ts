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
 * Update inspection fields on a target.
 * Requires can_comment permission.
 * Creates audit log entries for all changes.
 */
export async function PATCH(
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

  // Check can_comment permission
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('can_comment')
    .eq('user_id', user.id)
    .single();

  if (!profile?.can_comment) {
    return NextResponse.json({ error: 'Permission denied' }, { status: 403 });
  }

  try {
    const body = await request.json();
    const {
      redshift_inspected,
      redshift_quality,
      spectral_features,
      object_flags,
      dq_flags,
    } = body;

    // Fetch current target state for audit logging
    const { data: currentTarget, error: fetchError } = await supabase
      .from('targets')
      .select('redshift_inspected, redshift_quality, spectral_features, object_flags, dq_flags')
      .eq('id', targetId)
      .single();

    if (fetchError) {
      console.error('Error fetching current target:', fetchError);
      return NextResponse.json({ error: 'Target not found' }, { status: 404 });
    }

    // Build updates object
    const updates: Record<string, unknown> = {
      last_inspected_at: new Date().toISOString(),
      last_inspected_by: user.id,
    };

    // Track changes for audit log
    const auditEntries: Array<{
      target_id: number;
      user_id: string;
      field_name: string;
      old_value: number | null;
      new_value: number | null;
    }> = [];

    // Handle redshift_inspected (can be null to clear override)
    if (redshift_inspected !== undefined) {
      const newValue = redshift_inspected === null || redshift_inspected === ''
        ? null
        : parseFloat(redshift_inspected);

      if (newValue !== currentTarget.redshift_inspected) {
        updates.redshift_inspected = newValue;
        auditEntries.push({
          target_id: targetId,
          user_id: user.id,
          field_name: 'redshift_inspected',
          old_value: currentTarget.redshift_inspected,
          new_value: newValue,
        });
      }
    }

    // Handle redshift_quality
    if (redshift_quality !== undefined) {
      const newValue = parseInt(redshift_quality, 10);
      if (!isNaN(newValue) && newValue !== currentTarget.redshift_quality) {
        updates.redshift_quality = newValue;
        auditEntries.push({
          target_id: targetId,
          user_id: user.id,
          field_name: 'redshift_quality',
          old_value: currentTarget.redshift_quality,
          new_value: newValue,
        });
      }
    }

    // Handle spectral_features
    if (spectral_features !== undefined) {
      const newValue = parseInt(spectral_features, 10);
      if (!isNaN(newValue) && newValue !== currentTarget.spectral_features) {
        updates.spectral_features = newValue;
        auditEntries.push({
          target_id: targetId,
          user_id: user.id,
          field_name: 'spectral_features',
          old_value: currentTarget.spectral_features,
          new_value: newValue,
        });
      }
    }

    // Handle object_flags
    if (object_flags !== undefined) {
      const newValue = parseInt(object_flags, 10);
      if (!isNaN(newValue) && newValue !== currentTarget.object_flags) {
        updates.object_flags = newValue;
        auditEntries.push({
          target_id: targetId,
          user_id: user.id,
          field_name: 'object_flags',
          old_value: currentTarget.object_flags,
          new_value: newValue,
        });
      }
    }

    // Handle dq_flags
    if (dq_flags !== undefined) {
      const newValue = parseInt(dq_flags, 10);
      if (!isNaN(newValue) && newValue !== currentTarget.dq_flags) {
        updates.dq_flags = newValue;
        auditEntries.push({
          target_id: targetId,
          user_id: user.id,
          field_name: 'dq_flags',
          old_value: currentTarget.dq_flags,
          new_value: newValue,
        });
      }
    }

    // Only update if there are actual changes
    if (auditEntries.length === 0) {
      return NextResponse.json({ message: 'No changes detected' });
    }

    // Update the target — RLS enforces program access + can_comment
    const { data: updatedRows, error: updateError } = await supabase
      .from('targets')
      .update(updates)
      .eq('id', targetId)
      .select();

    if (updateError) {
      console.error('Error updating target:', updateError);
      return NextResponse.json({
        error: 'Failed to update target',
        details: updateError.message,
        code: updateError.code,
      }, { status: 500 });
    }

    if (!updatedRows || updatedRows.length === 0) {
      return NextResponse.json({
        error: 'Update blocked by access policy',
        details: 'You do not have permission to modify this target.',
      }, { status: 403 });
    }

    const updatedTarget = updatedRows[0];

    // Insert audit log entries — RLS enforces program access
    if (auditEntries.length > 0) {
      const { error: auditError } = await supabase
        .from('flag_audit_log')
        .insert(auditEntries);

      if (auditError) {
        console.error('Error creating audit log:', auditError);
        // Don't fail the request, just log the error
      }
    }

    // Propagate to cross-matched targets when quality is set to Secure
    let propagatedCount = 0;
    if (updates.redshift_quality === 4) {
      try {
        const { data: propResult } = await supabase.rpc('propagate_crossmatch_inspection', {
          p_target_id: targetId,
        });
        propagatedCount = propResult ?? 0;
      } catch (e) {
        console.error('Cross-match propagation error:', e);
        // Non-fatal: the primary save succeeded
      }
    }

    return NextResponse.json({
      object: updatedTarget,
      changes: auditEntries.length,
      propagated: propagatedCount,
    });
  } catch (error) {
    console.error('Error:', error);
    return NextResponse.json({ error: 'Failed to update target' }, { status: 500 });
  }
}
