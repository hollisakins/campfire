// NIRSpec shutter footprints for the cutout figure overlay (epic #337, Phase 5).
// The figure routes authorize through service-role queries, so the shutters
// table's RLS is not in play here and its gate is mirrored explicitly (the same
// arrangement as /api/v1/shutters): non-admins see only shutters whose target
// has a published spectrum, and share-link accounts — whose policy narrows to
// one observation the figure routes refuse anyway — get no overlay at all.

import type { SupabaseClient } from '@supabase/supabase-js';

/** Widest field of view (arcsec) the shutter overlay is drawn for. Beyond
 *  this a 0.2″ shutter is well under a pixel on a default panel, and the
 *  search box would approach the whole-field scans the shutter RPCs cap
 *  (perf T1-6 / #502: ~65k rows in COSMOS). Matches /api/shutters' extent. */
export const SHUTTER_OVERLAY_MAX_FOV_ARCSEC = 120;
/** Row cap for one overlay query — a deterministic backstop (the query is
 *  ordered) far above what a 120″ box holds in the densest MSA fields. */
export const SHUTTER_OVERLAY_MAX_ROWS = 4000;

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
 * get_nearby_shutters RPC uses). `halfArcsec` is clamped to the overlay's
 * maximum extent and the rows are capped, so one request can never scan a
 * field; ordered so the overlay is stable between renders.
 */
export async function fetchShuttersInBox(
  supabase: SupabaseClient,
  args: { field: string; ra: number; dec: number; halfArcsec: number; includeUnpublished: boolean },
): Promise<FigureShutter[]> {
  const { field, ra, dec, includeUnpublished } = args;
  const halfArcsec = Math.min(args.halfArcsec, SHUTTER_OVERLAY_MAX_FOV_ARCSEC * 0.75);
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
    .order('shutter_idx')
    .limit(SHUTTER_OVERLAY_MAX_ROWS);
  if (!includeUnpublished) query = query.eq('has_published_spectrum', true);
  const { data, error } = await query;
  if (error) throw new Error(`shutters query failed for field ${field}: ${error.message}`);
  return (data ?? []) as unknown as FigureShutter[];
}
