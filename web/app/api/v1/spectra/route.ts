import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { generateDownloadUrl } from '@/lib/r2';

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
  const userId = await validateAuth(request);

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
    const supabase = createServiceClient();

    // Service-role read bypasses RLS, so gate unpublished spectra here: a
    // non-admin must never resolve an draft/revoked FITS by path. No-op in B1.
    const isAdmin = await isAdminUser(userId);

    let spectrumQuery = supabase
      .from('spectra')
      .select(`
        id,
        target_id,
        targets!inner (
          program_slug
        )
      `)
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
    const targets = spectrum.targets as { program_slug: string } | { program_slug: string }[];
    const programSlug = Array.isArray(targets) ? targets[0].program_slug : targets.program_slug;
    if (!accessibleProgramSlugs.includes(programSlug)) {
      return NextResponse.json(
        { error: 'Access denied to this file' },
        { status: 403 }
      );
    }

    // Generate signed URL (expires in 1 hour)
    const signedUrl = await generateDownloadUrl(fitsPath, 3600);


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
