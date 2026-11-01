import { NextRequest } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';

/** One exposure x detector's master background (get_program_mbkg). */
export interface ExposureMbkg {
  observation: string;
  exposure_root: string;
  detector: string;
  grating: string | null;
  mbkg_source: 'combined' | 'exposure' | null;
  mbkg_rate_source: 'combined' | 'exposure' | null;
  mbkg_test: string | null;
  mbkg_exposures: string[] | null;
  mbkg_n_empty_shutters: number | null;
  mbkg_version: string | null;
  has_product: boolean;
}

export interface ProgramMbkgResponse {
  exposures: ExposureMbkg[];
  error?: string;
}

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/programs/[slug]/mbkg — the master background of every
 * exposure of the program's observations (metadata program page). The RPC is
 * scoped to the caller's programs (and a share link's observation), so an
 * inaccessible program reads as no rows.
 */
export async function GET(_request: NextRequest, context: { params: Promise<{ slug: string }> }) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return Response.json({ exposures: [], error: 'Unauthorized' }, { status: 401 });
  const { slug } = await context.params;
  const { data, error } = await supabase.rpc('get_program_mbkg', { p_program_slug: slug });
  if (error) {
    console.error('get_program_mbkg failed:', error);
    return Response.json({ exposures: [], error: error.message } satisfies ProgramMbkgResponse, { headers: NO_STORE });
  }
  return Response.json({ exposures: (data ?? []) as ExposureMbkg[] } satisfies ProgramMbkgResponse, { headers: NO_STORE });
}
