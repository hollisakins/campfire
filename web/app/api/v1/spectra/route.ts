import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { authenticateApiRequest } from '@/lib/api-auth';
import { assertBearerReleaseAccess } from '@/lib/server/bearer-release';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { generateDownloadUrl } from '@/lib/r2';
import { releaseDownloadUrls, resolveReleaseMembersAs } from '@/lib/server/dr-files';

/**
 * GET /api/v1/spectra?path=<fits_path>
 *
 * Download a FITS spectrum file.
 * Requires API key authentication.
 * Generates a signed URL and redirects to it, or returns the URL as JSON.
 *
 * Query parameters:
 * - path: FITS file path in R2 (required)
 * - redirect: if "true", redirects to the signed URL; otherwise returns JSON with URL
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
    // Get the fits_path from query parameters
    const searchParams = request.nextUrl.searchParams;
    const fitsPath = searchParams.get('path');
    const shouldRedirect = searchParams.get('redirect') === 'true';

    if (!fitsPath) {
      return NextResponse.json(
        { error: 'Missing path parameter' },
        { status: 400 }
      );
    }

    // Get accessible programs for this user
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json(
        { error: 'No program access' },
        { status: 403 }
      );
    }

    // Verify user has access to this file by checking if the spectrum exists
    // and belongs to a program the user has access to
    // `dr=<tag>`: browse a data release (P6c-2), gated against the caller's
    // own visibility before the header is forwarded.
    const release = await assertBearerReleaseAccess(request, principal!);
    if (release.response) return release.response;
    const supabase = createServiceClient({ dr: release.tag });

    // Service-role read bypasses RLS, so gate unpublished spectra here: a
    // non-admin must never resolve an draft/revoked FITS by path. No-op in B1.
    const isAdmin = await isAdminUser(userId);

    // cat_spectra: the live row, or the release's frozen row under `dr=`
    // (the views carry no FK metadata, so the program comes from the row's
    // own program_slug, which the scope trigger keeps in step with targets).
    let spectrumQuery = supabase
      .from('cat_spectra')
      .select('id, target_id, program_slug')
      .eq('fits_path', fitsPath);
    if (!isAdmin) {
      spectrumQuery = spectrumQuery.eq('deploy_status', 'published');
    }
    const { data: spectrum, error: spectrumError } = await spectrumQuery.single();

    if (spectrumError || !spectrum) {
      return NextResponse.json(
        { error: 'File not found' },
        { status: 404 }
      );
    }

    // Check if user has access to this program
    const programSlug = spectrum.program_slug as string;
    if (!accessibleProgramSlugs.includes(programSlug)) {
      return NextResponse.json(
        { error: 'Access denied to this file' },
        { status: 403 }
      );
    }

    // Generate signed URL (expires in 1 hour). Under a release: the archived
    // copy, or a 404 for a member not archived yet (P6c-2).
    let signedUrl: string;
    if (release.tag) {
      const members = await resolveReleaseMembersAs(supabase, release.tag, [fitsPath], {
        accessible: accessibleProgramSlugs, isAdmin,
      });
      const urls = await releaseDownloadUrls(members, 3600);
      const archived = urls.get(fitsPath);
      if (!archived) return NextResponse.json({ error: 'File not found' }, { status: 404 });
      signedUrl = archived;
    } else {
      signedUrl = await generateDownloadUrl(fitsPath, 3600);
    }


    // Either redirect or return JSON
    if (shouldRedirect) {
      return NextResponse.redirect(signedUrl);
    } else {
      return NextResponse.json({ url: signedUrl });
    }
  } catch (error) {
    console.error('Error in API /v1/spectra:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
