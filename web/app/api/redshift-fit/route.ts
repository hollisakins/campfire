import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { deriveSibling } from '@/lib/layout';
import { streamSidecar } from '@/lib/server/sidecar-stream';

// Non-finite values in the deploy-side FITS arrays are serialized as JSON
// null (see python/campfire/deploy/generate.py); scalars are null when the
// chi2 grid has no finite minimum. Consumers must guard before arithmetic.
export interface RedshiftFitData {
  redshift: number | null;
  chi2_min: number | null;
  confidence: number | null;
  z_grid: number[];
  chi2_grid: (number | null)[];
  model_wave: (number | null)[];
  model_fnu: (number | null)[];
}

/**
 * GET /api/redshift-fit?path=<fits_path>
 *
 * Streams the redshift fitting results (zfit JSON sidecar) for a spectrum
 * FITS file. The fallback byte path when the delivery front is not
 * configured; the client asks /api/spectrum/sidecars first.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  // Get the fits_path from query parameters
  const searchParams = request.nextUrl.searchParams;
  const fitsPath = searchParams.get('path');

  if (!fitsPath) {
    return NextResponse.json(
      { error: 'Missing path parameter' },
      { status: 400 }
    );
  }

  try {
    // Verify user has access to this file
    const { data: spectrum, error: spectrumError } = await supabase
      .from('spectra')
      .select('id')
      .eq('fits_path', fitsPath)
      .single();

    if (spectrumError || !spectrum) {
      return NextResponse.json(
        { error: 'File not found or access denied' },
        { status: 404 }
      );
    }

    // Derive the zfit-JSON sibling key via the shared layout contract and
    // stream it through untouched (perf T2-D2, #508) — the fallback path when
    // the delivery front is not configured; see /api/spectrum/sidecars.
    const zfitJsonPath = deriveSibling(fitsPath, 'zfit');
    // Program-scoped, cookie-authenticated: browser-cacheable for a day, never
    // shared-cacheable — Vercel's edge serves `public` responses to any caller
    // of the same URL, cookie or not (#497).
    const sidecar = await streamSidecar(zfitJsonPath, 'private, max-age=86400, stale-while-revalidate=3600');
    if (sidecar.status === 'ok') return sidecar.response;
    if (sidecar.status === 'missing') {
      // Zfit file might not exist (fitting not run for this spectrum)
      return NextResponse.json(
        { error: 'Redshift fit data not available for this spectrum' },
        { status: 404 }
      );
    }
    console.error('Failed to fetch zfit JSON:', sidecar.upstreamStatus);
    return NextResponse.json(
      { error: 'Failed to fetch redshift fit data' },
      { status: 502 }
    );
  } catch (error) {
    console.error('Error fetching redshift fit data:', error);
    return NextResponse.json(
      { error: 'Failed to fetch redshift fit data' },
      { status: 500 }
    );
  }
}
