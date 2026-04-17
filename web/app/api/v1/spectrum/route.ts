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
 * GET /api/v1/spectrum?spectrum_id=X
 * GET /api/v1/spectrum?path=<fits_path>
 *
 * Fetches the JSON spectrum data for plotting.
 * Requires API key authentication.
 *
 * Query parameters:
 * - spectrum_id: Stable per-spectrum identifier (from spectra.spectrum_id)
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
    const spectrumId = searchParams.get('spectrum_id');
    let fitsPath = searchParams.get('path');

    // If spectrum_id provided, look up the fits_path
    if (spectrumId && !fitsPath) {
      const { data: spectrumRow, error: spectrumRowError } = await supabase
        .from('spectra')
        .select('fits_path, target_id, targets!inner(program_slug)')
        .eq('spectrum_id', spectrumId)
        .single();

      if (spectrumRowError || !spectrumRow) {
        return NextResponse.json(
          { error: `No spectrum found for ${spectrumId}` },
          { status: 404 }
        );
      }

      // PostgREST embeds the joined row as an object (or array depending on
      // cardinality); handle both shapes defensively.
      const joined = (spectrumRow as { targets?: { program_slug: string } | { program_slug: string }[] }).targets;
      const programSlug = Array.isArray(joined) ? joined[0]?.program_slug : joined?.program_slug;

      if (!programSlug || !accessibleProgramSlugs.includes(programSlug)) {
        return NextResponse.json(
          { error: 'Access denied to this spectrum' },
          { status: 403 }
        );
      }

      fitsPath = spectrumRow.fits_path;
    }

    if (!fitsPath) {
      return NextResponse.json(
        { error: 'Missing required parameters: either spectrum_id or path' },
        { status: 400 }
      );
    }

    // Verify user has access to this file via the spectra table
    const { data: spectrum, error: spectrumError } = await supabase
      .from('spectra')
      .select('id, target_id')
      .eq('fits_path', fitsPath)
      .single();

    if (spectrumError || !spectrum) {
      return NextResponse.json(
        { error: 'Spectrum not found' },
        { status: 404 }
      );
    }

    // Verify access to the target's program
    const { data: targetData } = await supabase
      .from('targets')
      .select('program_slug')
      .eq('target_id', spectrum.target_id)
      .single();

    if (!targetData || !accessibleProgramSlugs.includes(targetData.program_slug)) {
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

    const resp = NextResponse.json(data);
    resp.headers.set('Cache-Control', 'public, max-age=86400, stale-while-revalidate=3600');
    return resp;
  } catch (error) {
    console.error('Error in API /v1/spectrum:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
