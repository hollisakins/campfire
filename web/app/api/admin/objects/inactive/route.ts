import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';

/**
 * /api/admin/objects/inactive
 *
 * Admin surface for soft-deleted objects (is_active = false).
 *
 * - GET:    list inactive objects (paginated). Also returns a count so the
 *           admin UI can badge "N inactive objects".
 * - PATCH:  reactivate a specific object: { id: number, action: 'reactivate' }
 *           or permanently delete: { id: number, action: 'delete' }.
 *
 * Admin-only; uses the service role client to bypass RLS.
 */

async function requireAdmin(request: NextRequest) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { error: NextResponse.json({ error: 'Authentication required' }, { status: 401 }) };
  }
  const service = createServiceClient();
  const { data: profile } = await service
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();
  if (!profile?.is_admin) {
    return { error: NextResponse.json({ error: 'Admin access required' }, { status: 403 }) };
  }
  return { service, userId: user.id };
}

export async function GET(request: NextRequest) {
  const guard = await requireAdmin(request);
  if ('error' in guard) return guard.error;
  const { service } = guard;

  const params = request.nextUrl.searchParams;
  const page = Math.max(1, parseInt(params.get('page') || '1', 10));
  const pageSize = Math.min(200, Math.max(1, parseInt(params.get('page_size') || '50', 10)));
  const offset = (page - 1) * pageSize;

  const [
    { count, error: countError },
    { data: rows, error: rowsError },
  ] = await Promise.all([
    service
      .from('objects')
      .select('id', { count: 'exact', head: true })
      .eq('is_active', false),
    service
      .from('objects')
      .select(
        'id, object_id, field, ra, dec, programs, gratings, ' +
        'redshift, redshift_quality, last_inspected_at, last_data_change_at, ' +
        'staleness_reason, updated_at'
      )
      .eq('is_active', false)
      .order('updated_at', { ascending: false })
      .range(offset, offset + pageSize - 1),
  ]);

  if (countError) {
    return NextResponse.json(
      { error: 'Failed to count inactive objects', details: countError.message },
      { status: 500 }
    );
  }

  if (rowsError) {
    return NextResponse.json(
      { error: 'Failed to fetch inactive objects', details: rowsError.message },
      { status: 500 }
    );
  }

  return NextResponse.json({
    objects: rows ?? [],
    total_count: count ?? 0,
    page,
    page_size: pageSize,
    has_next_page: offset + pageSize < (count ?? 0),
  });
}

export async function PATCH(request: NextRequest) {
  const guard = await requireAdmin(request);
  if ('error' in guard) return guard.error;
  const { service } = guard;

  let body: { id?: number; action?: 'reactivate' | 'delete' };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const objectId = typeof body.id === 'number' ? body.id : parseInt(String(body.id), 10);
  if (!objectId || Number.isNaN(objectId)) {
    return NextResponse.json({ error: 'id is required (number)' }, { status: 400 });
  }
  if (body.action !== 'reactivate' && body.action !== 'delete') {
    return NextResponse.json(
      { error: "action must be 'reactivate' or 'delete'" },
      { status: 400 }
    );
  }

  if (body.action === 'reactivate') {
    const { data: updated, error } = await service
      .from('objects')
      .update({ is_active: true, updated_at: new Date().toISOString() })
      .eq('id', objectId)
      .select()
      .single();
    if (error) {
      return NextResponse.json(
        { error: 'Failed to reactivate', details: error.message },
        { status: 500 }
      );
    }
    return NextResponse.json({ object: updated });
  }

  // action === 'delete': hard delete. Cascades via FKs — comments, list
  // memberships, and photometry are lost. Admin should double-check via
  // the activity feed before calling this.
  const { error } = await service.from('objects').delete().eq('id', objectId);
  if (error) {
    return NextResponse.json(
      { error: 'Failed to delete', details: error.message },
      { status: 500 }
    );
  }
  return NextResponse.json({ deleted: objectId });
}
