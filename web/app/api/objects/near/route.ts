import { NextRequest } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';

export interface NearbyObject {
  id: number;
  object_id: string;
  field: string;
  ra: number;
  dec: number;
  redshift: number | null;
  redshift_quality: number;
  gratings: string[];
  n_spectra: number;
  /** Angular separation from the query point, degrees. */
  distance: number;
}

export interface NearbyObjectsResponse {
  objects: NearbyObject[];
}

const MAX_RADIUS_ARCSEC = 600;
const MAX_LIMIT = 50;

/**
 * GET /api/objects/near?ra=<deg>&dec=<deg>&radius=<arcsec>[&limit=<n>][&exclude=<object_id>]
 *
 * The nearest visible objects to a point, closest first, for the object
 * page's "Nearby objects" card and the inspection overlay's nearby list.
 * Backed by get_objects_near (perf T2-C, #506): a box on the coordinate
 * index plus the Haversine cut, instead of the 33-parameter list RPC these
 * cards used to run through a server action. RLS on objects gates the rows;
 * the caller's accessible program set is passed as a parameter so the
 * planner sees an array rather than a per-row function call.
 *
 * Not browser-cached: redshift and quality change as objects are inspected.
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const sp = request.nextUrl.searchParams;
  const ra = Number(sp.get('ra'));
  const dec = Number(sp.get('dec'));
  const radiusArcsec = Number(sp.get('radius'));
  const limit = Math.min(MAX_LIMIT, Math.max(1, parseInt(sp.get('limit') ?? '10', 10) || 10));
  const exclude = sp.get('exclude');
  if (
    !Number.isFinite(ra) || ra < 0 || ra > 360 ||
    !Number.isFinite(dec) || dec < -90 || dec > 90 ||
    !Number.isFinite(radiusArcsec) || radiusArcsec <= 0 || radiusArcsec > MAX_RADIUS_ARCSEC
  ) {
    return Response.json({ error: 'Invalid parameters' }, { status: 400 });
  }

  const headers = { 'Cache-Control': 'private, no-store' };
  const slugs = principal.access.accessibleSlugs;
  if (slugs.length === 0) {
    return Response.json({ objects: [] } satisfies NearbyObjectsResponse, { headers });
  }

  try {
    const { data, error } = await principal.supabase.rpc('get_objects_near', {
      p_ra: ra,
      p_dec: dec,
      p_radius_degrees: radiusArcsec / 3600,
      p_program_slugs: slugs,
      p_limit: limit,
      p_exclude_object_id: exclude || null,
    });
    if (error) {
      console.error('objects near error:', error);
      return Response.json({ error: error.message }, { status: 500 });
    }
    const body: NearbyObjectsResponse = { objects: (data ?? []) as NearbyObject[] };
    return Response.json(body, { headers });
  } catch (err) {
    console.error('objects near error:', err);
    return Response.json({ error: 'Failed to load nearby objects' }, { status: 500 });
  }
}
