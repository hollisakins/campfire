import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { streamSidecar } from '@/lib/server/sidecar-stream';
import { deriveSibling } from '@/lib/layout';

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

/** PostgREST embeds a to-one join as an object, or an array depending on
 *  cardinality; read the program slug from either shape. */
function embeddedProgramSlug(row: { targets?: { program_slug: string } | { program_slug: string }[] | null }): string | undefined {
  const joined = row.targets;
  return Array.isArray(joined) ? joined[0]?.program_slug : joined?.program_slug ?? undefined;
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
    const supabase = createServiceClient();

    // Parse query parameters
    const searchParams = request.nextUrl.searchParams;
    const spectrumId = searchParams.get('spectrum_id');
    const pathParam = searchParams.get('path');

    if (!spectrumId && !pathParam) {
      return NextResponse.json(
        { error: 'Missing required parameters: either spectrum_id or path' },
        { status: 400 }
      );
    }

    // Get accessible programs for this user. Service-role reads bypass RLS,
    // so unpublished spectra are gated here: a non-admin must never receive
    // flux/wave/2D JSON for a draft/revoked spectrum, whether resolved by
    // spectrum_id or by fits_path. No-op in B1.
    const [accessibleProgramSlugs, isAdmin] = await Promise.all([
      getAccessiblePrograms(userId),
      isAdminUser(userId),
    ]);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json(
        { error: 'No accessible programs' },
        { status: 403 }
      );
    }

    // One lookup resolves the row, its fits_path and its program in either
    // branch — the embedded join replaces the two follow-up queries that
    // used to re-fetch what this row already carried (#497).
    let rowQuery = supabase
      .from('spectra')
      .select('fits_path, targets!inner(program_slug)');
    rowQuery = pathParam
      ? rowQuery.eq('fits_path', pathParam)
      : rowQuery.eq('spectrum_id', spectrumId!);
    if (!isAdmin) {
      rowQuery = rowQuery.eq('deploy_status', 'published');
    }
    const { data: spectrumRow, error: spectrumRowError } = await rowQuery.single();

    if (spectrumRowError || !spectrumRow) {
      return NextResponse.json(
        { error: pathParam ? 'Spectrum not found' : `No spectrum found for ${spectrumId}` },
        { status: 404 }
      );
    }

    const programSlug = embeddedProgramSlug(spectrumRow);
    if (!programSlug || !accessibleProgramSlugs.includes(programSlug)) {
      return NextResponse.json(
        { error: 'Access denied to this spectrum' },
        { status: 403 }
      );
    }

    const fitsPath = spectrumRow.fits_path;

    // Derive the spectrum-JSON sibling key via the shared layout contract
    const jsonPath = deriveSibling(fitsPath, 'spectrum_json');

    // Stream the sidecar through untouched (perf T2-D2, #508). Bearer
    // requests bypass Vercel's shared cache; `private` states the
    // (program-scoped) truth for the client's own cache (#497).
    const sidecar = await streamSidecar(jsonPath, 'private, max-age=86400, stale-while-revalidate=3600');
    if (sidecar.status === 'ok') return sidecar.response;
    console.error('Failed to fetch spectrum JSON:', sidecar.upstreamStatus);
    return NextResponse.json(
      { error: 'Failed to fetch spectrum data' },
      { status: sidecar.status === 'missing' ? 404 : 502 }
    );
  } catch (error) {
    console.error('Error in API /v1/spectrum:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
