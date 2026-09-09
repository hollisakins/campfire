// NIRSpec shutter footprints for the cutout figure overlay (epic #337, Phase 5).
// The figure routes authorize through service-role queries, so the shutters
// table's RLS is not in play here and its gate is mirrored explicitly (the same
// arrangement as /api/v1/shutters): non-admins see only shutters whose target
// has a published spectrum, and share-link accounts — whose policy narrows to
// one observation the figure routes refuse anyway — get no overlay at all.

import type { SupabaseClient } from '@supabase/supabase-js';

export interface FigureShutter {
  object_id: string;
  observation: string;
  center_ra: number;
  center_dec: number;
  position_angle: number;
  shutter_state: 'source' | 'open' | 'stuck_closed';
  aperture_width_arcsec: number;
  aperture_height_arcsec: number;
}

/**
 * Shutters whose centre falls within `halfArcsec` of `(ra, dec)` on each axis
 * (an RA/Dec box, widened by 1/cos(dec) in RA — the same box the
 * get_nearby_shutters RPC uses). Rows are ordered so the overlay is stable
 * between renders.
 */
export async function fetchShuttersInBox(
  supabase: SupabaseClient,
  args: { field: string; ra: number; dec: number; halfArcsec: number; includeUnpublished: boolean },
): Promise<FigureShutter[]> {
  const { field, ra, dec, halfArcsec, includeUnpublished } = args;
  const dDec = halfArcsec / 3600;
  const dRa = dDec / (Math.cos((dec * Math.PI) / 180) || 1e-8);
  let query = supabase
    .from('shutters')
    .select(
      'object_id, observation, center_ra, center_dec, position_angle, shutter_state, ' +
        'aperture_width_arcsec, aperture_height_arcsec',
    )
    .eq('field', field)
    .gte('center_ra', ra - dRa)
    .lte('center_ra', ra + dRa)
    .gte('center_dec', dec - dDec)
    .lte('center_dec', dec + dDec)
    .order('observation')
    .order('object_id')
    .order('shutter_idx');
  if (!includeUnpublished) query = query.eq('has_published_spectrum', true);
  const { data, error } = await query;
  if (error) throw new Error(`shutters query failed for field ${field}: ${error.message}`);
  return (data ?? []) as unknown as FigureShutter[];
}
