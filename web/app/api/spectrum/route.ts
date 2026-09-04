import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';
import { deriveSibling } from '@/lib/layout';

export interface SpectrumData {
  wave: number[];
  fnu: (number | null)[];
  fnu_err: (number | null)[];
  snr_2d: number[][];
  n_spatial: number;
  n_wave: number;
  // Cross-dispersion profile data
  profile: number[];       // Collapsed spatial profile (normalized)
  profile_fit: number[];   // Optimal extraction weight (normalized)
  profile_pix: number[];   // Pixel positions (centered on source)
}

/**
 * GET /api/spectrum?path=<fits_path>
 *
 * Fetches the JSON spectrum data for a FITS file.
 * The JSON file has the same path as the FITS file but with .json extension.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

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

    // Derive the spectrum-JSON sibling key via the shared layout contract
    const jsonPath = deriveSibling(fitsPath, 'spectrum_json');

    // Generate signed URL for the JSON file
    const signedUrl = await generateDownloadUrl(jsonPath, 3600);

    // Fetch the JSON data from R2
    const response = await fetch(signedUrl);

    if (!response.ok) {
      console.error('Failed to fetch spectrum JSON:', response.status);
      return NextResponse.json(
        { error: 'Failed to fetch spectrum data' },
        { status: 502 }
      );
    }

    const data: SpectrumData = await response.json();

    // Program-scoped, cookie-authenticated: browser-cacheable for a day, never
    // shared-cacheable — Vercel's edge serves `public` responses to any caller
    // of the same URL, cookie or not (#497).
    const response2 = NextResponse.json(data);
    response2.headers.set('Cache-Control', 'private, max-age=86400, stale-while-revalidate=3600');
    return response2;
  } catch (error) {
    console.error('Error fetching spectrum data:', error);
    return NextResponse.json(
      { error: 'Failed to fetch spectrum data' },
      { status: 500 }
    );
  }
}
