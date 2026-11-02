import { NextRequest } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';

/** One exposure x detector of a program's observations (get_program_exposures). */
export interface ProgramExposure {
  observation: string;
  exposure_root: string;
  detector: string;
  grating: string | null;
  filter: string | null;
  date_obs: string | null;
  exposure_time: number | null;
  readout_pattern: string | null;
  n_groups: number | null;
  n_integrations: number | null;
  dither_position: number | null;
  n_dither_positions: number | null;
  mbkg_source: 'combined' | 'exposure' | null;
}

export interface ProgramExposuresResponse {
  exposures: ProgramExposure[];
  error?: string;
}

const NO_STORE = { 'Cache-Control': 'private, no-store' };

/**
 * GET /api/metadata/programs/[slug]/exposures — every exposure of the program's
 * observations: what was observed and which master background it uses
 * (metadata program page). The RPC is scoped to the caller's programs (and a
 * share link's observation), so an inaccessible program reads as no rows.
 */
export async function GET(_request: NextRequest, context: { params: Promise<{ slug: string }> }) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return Response.json({ exposures: [], error: 'Unauthorized' }, { status: 401 });
  const { slug } = await context.params;
  const { data, error } = await supabase.rpc('get_program_exposures', { p_program_slug: slug });
  if (error) {
    console.error('get_program_exposures failed:', error);
    return Response.json({ exposures: [], error: error.message } satisfies ProgramExposuresResponse, { headers: NO_STORE });
  }
  return Response.json({ exposures: (data ?? []) as ProgramExposure[] } satisfies ProgramExposuresResponse, { headers: NO_STORE });
}
