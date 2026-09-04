import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { deriveSibling } from '@/lib/layout';
import { cdnFrontBase, frontUrlsFor } from '@/lib/server/cdn-front';

/**
 * Where a spectrum's sidecars are served from. `front: true` => every url is
 * on the delivery front (CORS-readable, content-addressed; null = the object
 * is not deployed, e.g. no redshift fit). `front: false` => the front is not
 * configured and the client fetches /api/spectrum and /api/redshift-fit
 * instead, which stream the bytes.
 */
export interface SpectrumSidecarUrls {
  front: boolean;
  spectrum: string | null;
  spectrum_1d: string | null;
  zfit: string | null;
}

const NO_FRONT: SpectrumSidecarUrls = { front: false, spectrum: null, spectrum_1d: null, zfit: null };

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
    if (!cdnFrontBase()) {
      return NextResponse.json(NO_FRONT, { headers });
    }

    const jsonKey = deriveSibling(fitsPath, 'spectrum_json');
    const json1dKey = deriveSibling(fitsPath, 'spectrum_1d_json');
    const zfitKey = deriveSibling(fitsPath, 'zfit');
    const urls = await frontUrlsFor([jsonKey, json1dKey, zfitKey]);
    const body: SpectrumSidecarUrls = {
      front: true,
      spectrum: urls.get(jsonKey) ?? null,
      spectrum_1d: urls.get(json1dKey) ?? null,
      zfit: urls.get(zfitKey) ?? null,
    };
    return NextResponse.json(body, { headers });
  } catch (err) {
    console.error('Error resolving spectrum sidecars:', err);
    return NextResponse.json({ error: 'Failed to resolve spectrum sidecars' }, { status: 500 });
  }
}
