import { NextRequest } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';
import type { NirspecStuckShutter } from '@/lib/types';

export interface StuckShuttersResponse {
  rows: NirspecStuckShutter[];
}

const ROOT_RE = /^jw\d{11}_\d{5}$/;
const MAX_ROOTS = 100;

/**
 * GET /api/admin/nirspec/stuck-shutters?observation=<obs>&roots=<root>,<root>
 *
 * The nirspec_stuck_shutters rows (manual and web) of an observation's exposure
 * roots, for the nods review page. Admin-only (the table's RLS is the authority;
 * the check here only turns a non-admin into a 403 instead of an empty list).
 * Writes are server actions (lib/actions/nirspec-nods.ts).
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  if (!principal.access.isAdmin) return Response.json({ error: 'Admin access required' }, { status: 403 });

  const params = request.nextUrl.searchParams;
  const observation = params.get('observation') ?? '';
  if (!observation) return Response.json({ error: 'Missing observation' }, { status: 400 });
  const roots = (params.get('roots') ?? '').split(',').map((r) => r.trim()).filter(Boolean);
  if (roots.length > MAX_ROOTS || roots.some((r) => !ROOT_RE.test(r))) {
    return Response.json({ error: 'Invalid roots' }, { status: 400 });
  }

  let query = principal.supabase
    .from('nirspec_stuck_shutters')
    .select('*')
    .eq('observation', observation)
    .order('exposure_root')
    .order('shutter');
  if (roots.length) query = query.in('exposure_root', roots);
  const { data, error } = await query;
  if (error) return Response.json({ error: error.message }, { status: 500 });

  const body: StuckShuttersResponse = { rows: (data ?? []) as NirspecStuckShutter[] };
  return Response.json(body, { headers: { 'Cache-Control': 'private, no-store' } });
}
