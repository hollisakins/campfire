import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateApiKey } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
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
 * GET /api/v1/redshift-fit?object_id=X&grating=Y
 * GET /api/v1/redshift-fit?path=<fits_path>
 *
 * Fetches the redshift fitting results for a spectrum.
 * Requires API key authentication.
 *
 * Query parameters:
 * - object_id: Object ID to fetch fit for
 * - grating: Grating type (e.g., PRISM, G395M)
 * OR
 * - path: Direct FITS path (from query results)
 */
export async function GET(request: NextRequest) {
  // Validate API key
  const userId = await validateApiKey(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing API key' },
      { status: 401 }
    );
  }

  try {
    // Create Supabase client with service role
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Get accessible programs for this user
    const accessibleProgramIds = await getAccessiblePrograms(userId);

    if (accessibleProgramIds.length === 0) {
      return NextResponse.json(
        { error: 'No accessible programs' },
        { status: 403 }
      );
    }

    // Parse query parameters
    const searchParams = request.nextUrl.searchParams;
    const objectId = searchParams.get('object_id');
    const grating = searchParams.get('grating');
    let fitsPath = searchParams.get('path');

    // If object_id and grating provided, look up the fits_path
    if (objectId && grating && !fitsPath) {
      // First verify the object exists and user has access
      const { data: objectData, error: objectError } = await supabase
        .from('objects')
        .select('program_id')
        .eq('object_id', objectId)
        .single();

      if (objectError || !objectData) {
        return NextResponse.json(
          { error: 'Object not found' },
          { status: 404 }
        );
      }

      if (!accessibleProgramIds.includes(objectData.program_id)) {
        return NextResponse.json(
          { error: 'Access denied to this object' },
          { status: 403 }
        );
      }

      // Look up the spectrum
      const { data: spectrumData, error: spectrumError } = await supabase
        .from('spectra')
        .select('fits_path')
        .eq('object_id', objectId)
        .eq('grating', grating)
        .single();

      if (spectrumError || !spectrumData) {
        return NextResponse.json(
          { error: `No ${grating} spectrum found for ${objectId}` },
          { status: 404 }
        );
      }

      fitsPath = spectrumData.fits_path;
    }

    if (!fitsPath) {
      return NextResponse.json(
        { error: 'Missing required parameters: either (object_id, grating) or path' },
        { status: 400 }
      );
    }

    // Verify user has access to this file via the spectra table
    const { data: spectrum, error: spectrumError } = await supabase
      .from('spectra')
      .select('id, object_id')
      .eq('fits_path', fitsPath)
      .single();

    if (spectrumError || !spectrum) {
      return NextResponse.json(
        { error: 'Spectrum not found' },
        { status: 404 }
      );
    }

    // Verify access to the object's program
    const { data: objectData } = await supabase
      .from('objects')
      .select('program_id')
      .eq('object_id', spectrum.object_id)
      .single();

    if (!objectData || !accessibleProgramIds.includes(objectData.program_id)) {
      return NextResponse.json(
        { error: 'Access denied' },
        { status: 403 }
      );
    }

    // Convert FITS path to zfit JSON path
    // spectra/{obs_name}/{obs_name}_{grating}_{filter}_{source_id}_spec.fits
    // -> spectra/{obs_name}/{obs_name}_{grating}_{filter}_{source_id}_zfit.json
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

    return NextResponse.json(data);
  } catch (error) {
    console.error('Error in API /v1/redshift-fit:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
