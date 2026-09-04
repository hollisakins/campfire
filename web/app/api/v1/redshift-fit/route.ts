import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { streamSidecar } from '@/lib/server/sidecar-stream';
import { deriveSibling } from '@/lib/layout';

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
 * GET /api/v1/redshift-fit?target_id=X&grating=Y
 * GET /api/v1/redshift-fit?path=<fits_path>
 *
 * Fetches the redshift fitting results for a spectrum.
 * Requires API key authentication.
 *
 * Query parameters:
 * - target_id: Target ID to fetch fit for
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
    const supabase = createServiceClient();

    // Get accessible programs for this user
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json(
        { error: 'No accessible programs' },
        { status: 403 }
      );
    }

    // Service-role read bypasses RLS, so gate unpublished spectra here: a
    // non-admin must never receive zfit JSON for an draft/revoked spectrum,
    // whether resolved by (target_id, grating) or by fits_path. No-op in B1.
    const isAdmin = await isAdminUser(userId);

    // Parse query parameters
    const searchParams = request.nextUrl.searchParams;
    const targetId = searchParams.get('target_id');
    const grating = searchParams.get('grating');
    let fitsPath = searchParams.get('path');

    // If target_id and grating provided, look up the fits_path
    if (targetId && grating && !fitsPath) {
      // First verify the target exists and user has access
      const { data: targetData, error: targetError } = await supabase
        .from('targets')
        .select('program_slug')
        .eq('target_id', targetId)
        .single();

      if (targetError || !targetData) {
        return NextResponse.json(
          { error: 'Object not found' },
          { status: 404 }
        );
      }

      if (!accessibleProgramSlugs.includes(targetData.program_slug)) {
        return NextResponse.json(
          { error: 'Access denied to this object' },
          { status: 403 }
        );
      }

      // Look up the spectrum
      let spectrumLookup = supabase
        .from('spectra')
        .select('fits_path')
        .eq('target_id', targetId)
        .eq('grating', grating);
      if (!isAdmin) {
        spectrumLookup = spectrumLookup.eq('deploy_status', 'published');
      }
      const { data: spectrumData, error: spectrumError } = await spectrumLookup.single();

      if (spectrumError || !spectrumData) {
        return NextResponse.json(
          { error: `No ${grating} spectrum found for ${targetId}` },
          { status: 404 }
        );
      }

      fitsPath = spectrumData.fits_path;
    }

    if (!fitsPath) {
      return NextResponse.json(
        { error: 'Missing required parameters: either (target_id, grating) or path' },
        { status: 400 }
      );
    }

    // Verify user has access to this file via the spectra table
    let spectrumQuery = supabase
      .from('spectra')
      .select('id, target_id')
      .eq('fits_path', fitsPath);
    if (!isAdmin) {
      spectrumQuery = spectrumQuery.eq('deploy_status', 'published');
    }
    const { data: spectrum, error: spectrumError } = await spectrumQuery.single();

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

    // Derive the zfit-JSON sibling key via the shared layout contract
    const zfitJsonPath = deriveSibling(fitsPath, 'zfit');

    // Stream the sidecar through untouched (perf T2-D2, #508).
    const sidecar = await streamSidecar(zfitJsonPath, 'private, max-age=86400, stale-while-revalidate=3600');
    if (sidecar.status === 'ok') return sidecar.response;
    if (sidecar.status === 'missing') {
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
    console.error('Error in API /v1/redshift-fit:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
