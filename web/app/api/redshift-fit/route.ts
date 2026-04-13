import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';

export interface RedshiftFitData {
  redshift: number;
  chi2_min: number;
  confidence: number;
  z_grid: number[];
  chi2_grid: number[];
  model_wave: number[];
  model_fnu: number[];
}

/**
 * GET /api/redshift-fit?path=<fits_path>
 *
 * Fetches the redshift fitting results for a spectrum FITS file.
 * Converts the FITS path to a zfit JSON path and returns the fitting data.
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

    // Convert FITS path to zfit JSON path
    // spectra/{obs_name}/{obs_name}_{grating}_{filter}_{source_id}_spec.fits
    // → spectra/{obs_name}/{obs_name}_{grating}_{filter}_{source_id}_zfit.json
    const zfitJsonPath = fitsPath.replace('_spec.fits', '_zfit.json');

    // Generate signed URL for the zfit JSON file
    const signedUrl = await generateDownloadUrl(zfitJsonPath, 3600);

    // Check if R2 is configured
    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured' },
        { status: 503 }
      );
    }

    // Fetch the zfit JSON data from R2
    const response = await fetch(signedUrl);

    if (!response.ok) {
      // Zfit file might not exist (fitting not run for this spectrum)
      if (response.status === 404) {
        return NextResponse.json(
          { error: 'Redshift fit data not available for this spectrum' },
          { status: 404 }
        );
      }
      console.error('Failed to fetch zfit JSON:', response.status);
      return NextResponse.json(
        { error: 'Failed to fetch redshift fit data' },
        { status: 502 }
      );
    }

    const data: RedshiftFitData = await response.json();

    const resp = NextResponse.json(data);
    resp.headers.set('Cache-Control', 'public, max-age=86400, stale-while-revalidate=3600');
    return resp;
  } catch (error) {
    console.error('Error fetching redshift fit data:', error);
    return NextResponse.json(
      { error: 'Failed to fetch redshift fit data' },
      { status: 500 }
    );
  }
}
