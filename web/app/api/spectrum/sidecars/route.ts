import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { resolveSpectrumSidecars } from '@/lib/server/spectrum-sidecars';

// The response shape lives in lib/spectrum-sidecars (shared with the object
// page render, which resolves the same urls server-side — perf T2-E, #510).
export type { SpectrumSidecarUrls } from '@/lib/spectrum-sidecars';

/**
 * GET /api/spectrum/sidecars?path=<fits_path>
 *
 * ONE access check per spectrum per page (perf T2-D2, #508): resolves the
 * spectrum JSON, its 1-D sidecar and the zfit JSON to delivery-front urls in
 * one go, so the client fetches all three from the Worker (edge-cached per
 * content hash) with no further round trip through the app. A null url
 * with `front: true` is not proof of absence (registry row not active yet,
 * presign failed): the client falls back to the streaming route for that
 * one sidecar, and only that route's 404 means "no such product". Replaces the
 * per-route `spectra WHERE fits_path` lookups /api/spectrum and
 * /api/redshift-fit each ran (prod's #2 query shape).
 *
 * The object page does not call this for its own spectra any more: it
 * resolves them during the server render and seeds the client cache
 * (lib/server/spectrum-sidecars.ts). This route serves everything else —
 * inspection mode, deep links, cache misses.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const fitsPath = request.nextUrl.searchParams.get('path');
  if (!fitsPath) {
    return NextResponse.json({ error: 'Missing path parameter' }, { status: 400 });
  }

  try {
    const { data: spectrum, error } = await supabase
      .from('spectra')
      .select('id')
      .eq('fits_path', fitsPath)
      .single();
    if (error || !spectrum) {
      return NextResponse.json({ error: 'File not found or access denied' }, { status: 404 });
    }

    // Front urls are stable for at least one 6 h presign window; the answer
    // may sit in the browser cache for an hour — with Vary: Cookie, since
    // sign-out does not clear the HTTP cache (D-C).
    const headers = { 'Cache-Control': 'private, max-age=3600', Vary: 'Cookie' };
    const sidecars = await resolveSpectrumSidecars([fitsPath]);
    return NextResponse.json(sidecars.get(fitsPath), { headers });
  } catch (err) {
    console.error('Error resolving spectrum sidecars:', err);
    return NextResponse.json({ error: 'Failed to resolve spectrum sidecars' }, { status: 500 });
  }
}
