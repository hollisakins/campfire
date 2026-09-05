import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { deriveSibling } from '@/lib/layout';
import { streamSidecar } from '@/lib/server/sidecar-stream';

/** The 1-D sidecar (`<stem>_spec_1d.json`, perf T2-D2 #508): everything the
 * primary trace and the cross-dispersion profile need, without the 2-D S/N
 * array that is 80–95 % of the full payload. */
export interface SpectrumData1D {
  wave: number[];
  fnu: (number | null)[];
  fnu_err: (number | null)[];
  n_spatial: number;
  n_wave: number;
  // Cross-dispersion profile data
  profile: number[];       // Collapsed spatial profile (normalized)
  profile_fit: number[];   // Optimal extraction weight (normalized)
  profile_pix: number[];   // Pixel positions (centered on source)
}

/** The full spectrum JSON (`<stem>_spec.json`): the 1-D payload plus the
 * 2-D S/N heatmap. */
export interface SpectrumData extends SpectrumData1D {
  snr_2d: number[][];
}

// Program-scoped, cookie-authenticated: browser-cacheable for a day, never
// shared-cacheable — Vercel's edge serves `public` responses to any caller
// of the same URL, cookie or not (#497).
const CACHE_CONTROL = 'private, max-age=86400, stale-while-revalidate=3600';

/**
 * GET /api/spectrum?path=<fits_path>[&include=1d]
 *
 * The fallback byte path for a spectrum's JSON sidecar, used when the
 * delivery front is not configured (see /api/spectrum/sidecars, which is
 * what the client asks first). Streams the object through untouched — no
 * parse-and-re-serialize. `include=1d` serves the 1-D sidecar
 * (`_spec_1d.json`) and falls back to the full JSON for spectra deployed
 * before the sidecar existed.
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
  const oneD = searchParams.get('include') === '1d';

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

    if (oneD) {
      const sidecar = await streamSidecar(deriveSibling(fitsPath, 'spectrum_1d_json'), CACHE_CONTROL);
      if (sidecar.status === 'ok') return sidecar.response;
      if (sidecar.status === 'error') {
        console.error('Failed to fetch 1-D spectrum JSON:', sidecar.upstreamStatus);
      }
      // Missing (pre-#508 deploy): the full JSON is a superset.
    }

    const full = await streamSidecar(deriveSibling(fitsPath, 'spectrum_json'), CACHE_CONTROL);
    if (full.status === 'ok') return full.response;
    console.error('Failed to fetch spectrum JSON:', full.upstreamStatus);
    return NextResponse.json(
      { error: 'Failed to fetch spectrum data' },
      { status: full.status === 'missing' ? 404 : 502 }
    );
  } catch (error) {
    console.error('Error fetching spectrum data:', error);
    return NextResponse.json(
      { error: 'Failed to fetch spectrum data' },
      { status: 500 }
    );
  }
}
