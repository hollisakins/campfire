import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import { generateDownloadUrl } from '@/lib/r2';

export interface SpectrumData {
  wave: number[];
  fnu: (number | null)[];
  fnu_err: (number | null)[];
  snr_2d: number[][];
  n_spatial: number;
  n_wave: number;
  profile: number[];
  profile_fit: number[];
  profile_pix: number[];
}

/**
 * GET /api/v1/spectrum?object_id=X&grating=Y
 * GET /api/v1/spectrum?path=<fits_path>
 *
 * Fetches the JSON spectrum data for plotting.
 * Requires API key authentication.
 *
 * Query parameters:
 * - object_id: Object ID to fetch spectrum for
 * - grating: Grating type (e.g., PRISM, G395M)
 * OR
 * - path: Direct FITS path (from query results)
 */
export async function GET(request: NextRequest) {
  // Validate API key
  const userId = await validateAuth(request);

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
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
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
        .select('program_slug')
        .eq('object_id', objectId)
        .single();

      if (objectError || !objectData) {
        return NextResponse.json(
          { error: 'Object not found' },
          { status: 404 }
        );
      }

      if (!accessibleProgramSlugs.includes(objectData.program_slug)) {
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
      .select('program_slug')
      .eq('object_id', spectrum.object_id)
      .single();

    if (!objectData || !accessibleProgramSlugs.includes(objectData.program_slug)) {
      return NextResponse.json(
        { error: 'Access denied' },
        { status: 403 }
      );
    }

    // Convert FITS path to JSON path
    const jsonPath = fitsPath.replace('.fits', '.json');

    // Generate signed URL for the JSON file
    const signedUrl = await generateDownloadUrl(jsonPath, 3600);

    // Check if R2 is configured
    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured' },
        { status: 503 }
      );
    }

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

    return NextResponse.json(data);
  } catch (error) {
    console.error('Error in API /v1/spectrum:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
