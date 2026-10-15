import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { authenticateApiRequest } from '@/lib/api-auth';
import { assertBearerReleaseAccess } from '@/lib/server/bearer-release';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { streamSidecar } from '@/lib/server/sidecar-stream';
import { deriveSibling } from '@/lib/layout';
import { resolveReleaseMembersAs } from '@/lib/server/dr-files';

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
  const principal = await authenticateApiRequest(request);
  const userId = principal?.userId ?? null;

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing API key' },
      { status: 401 }
    );
  }

  try {
    // Create Supabase client with service role
    // `dr=<tag>`: browse a data release (P6c-2), gated against the caller's
    // own visibility before the header is forwarded.
    const release = await assertBearerReleaseAccess(request, principal!);
    if (release.response) return release.response;
    const supabase = createServiceClient({ dr: release.tag });

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
    // cat_spectra: the live row, or the release's frozen row under `dr=`
    // (views carry no FK metadata; the row's own program_slug is the scope).
    let rowQuery = supabase
      .from('cat_spectra')
      .select('fits_path, program_slug');
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

    const programSlug = spectrumRow.program_slug as string | null;
    if (!programSlug || !accessibleProgramSlugs.includes(programSlug)) {
      return NextResponse.json(
        { error: 'Access denied to this spectrum' },
        { status: 403 }
      );
    }

    const fitsPath = spectrumRow.fits_path;

    // Derive the spectrum-JSON sibling key via the shared layout contract.
    // Under a release: the archived copy, or a 404 (P6c-2).
    let jsonPath: string | null = deriveSibling(fitsPath, 'spectrum_json');
    if (release.tag) {
      const members = await resolveReleaseMembersAs(supabase, release.tag, [jsonPath], {
        accessible: accessibleProgramSlugs, isAdmin,
      });
      jsonPath = members.get(jsonPath)?.archiveKey ?? null;
      if (!jsonPath) return NextResponse.json({ error: 'Spectrum not found' }, { status: 404 });
    }

    // Stream the sidecar through untouched (perf T2-D2, #508). Bearer
    // requests bypass Vercel's shared cache; `private` states the
    // (program-scoped) truth for the client's own cache (#497).
    const sidecar = await streamSidecar(jsonPath, 'private, max-age=86400, stale-while-revalidate=3600', 'Authorization');
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
