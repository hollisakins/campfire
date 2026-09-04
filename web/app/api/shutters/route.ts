import { NextRequest } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import type { Shutter } from '@/lib/actions/map';

export interface NearbyShuttersResponse {
  shutters: Shutter[];
}

// MSA shutter geometry is immutable between deployments, and the URL carries
// the asset version token (`v`, lib/asset-version.ts) that changes when one
// lands, so the browser may keep a response for a day. `private`: the rows
// are RLS-scoped (publication state, share-link scope) and must never enter
// the shared edge cache (#497).
const PRIVATE_DAY = 'private, max-age=86400, stale-while-revalidate=86400';

/**
 * GET /api/shutters?ra=<deg>&dec=<deg>&field=<field>&fov=<arcsec>[&v=<asset version>]
 *
 * Shutters within `fov` arcsec of a point, for the object page's cutout
 * overlay. A GET route rather than a server action on purpose (perf T2-C,
 * #506): as an action this read queued behind every other action on the
 * object page and was uncacheable for geometry that never changes. Reads run
 * under the caller's RLS session via get_nearby_shutters (SECURITY INVOKER).
 * `v` is an opaque cache-key token and is not read.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sp = request.nextUrl.searchParams;
  const ra = Number(sp.get('ra'));
  const dec = Number(sp.get('dec'));
  const field = sp.get('field');
  const fov = Number(sp.get('fov') ?? '5');
  if (
    !Number.isFinite(ra) || ra < 0 || ra > 360 ||
    !Number.isFinite(dec) || dec < -90 || dec > 90 ||
    !field ||
    !Number.isFinite(fov) || fov <= 0 || fov > 60
  ) {
    return Response.json({ error: 'Invalid parameters' }, { status: 400 });
  }

  try {
    const { data, error } = await supabase.rpc('get_nearby_shutters', {
      p_ra: ra,
      p_dec: dec,
      p_radius_arcsec: fov,
      p_field: field,
    });
    if (error) {
      console.error('nearby shutters error:', error);
      return Response.json({ error: error.message }, { status: 500 });
    }
    const body: NearbyShuttersResponse = { shutters: (data ?? []) as Shutter[] };
    return Response.json(body, { headers: { 'Cache-Control': PRIVATE_DAY } });
  } catch (err) {
    console.error('nearby shutters error:', err);
    return Response.json({ error: 'Failed to load shutters' }, { status: 500 });
  }
}
