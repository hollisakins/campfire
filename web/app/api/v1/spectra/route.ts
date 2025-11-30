import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateApiKey } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
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
  const userId = await validateApiKey(request);

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
    const accessibleProgramIds = await getAccessiblePrograms(userId);

    if (accessibleProgramIds.length === 0) {
      return NextResponse.json(
        { error: 'No program access' },
        { status: 403 }
      );
    }

    // Verify user has access to this file by checking if the spectrum exists
    // and belongs to a program the user has access to
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    const { data: spectrum, error: spectrumError } = await supabase
      .from('spectra')
      .select(`
        id,
        object_id,
        objects!inner (
          program_id
        )
      `)
      .eq('fits_path', fitsPath)
      .single();

    if (spectrumError || !spectrum) {
      return NextResponse.json(
        { error: 'File not found' },
        { status: 404 }
      );
    }

    // Check if user has access to this program
    const programId = (spectrum.objects as any).program_id;
    if (!accessibleProgramIds.includes(programId)) {
      return NextResponse.json(
        { error: 'Access denied to this file' },
        { status: 403 }
      );
    }

    // Generate signed URL (expires in 1 hour)
    const signedUrl = await generateDownloadUrl(fitsPath, 3600);

    // Check if it's a placeholder URL (R2 not configured)
    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured' },
        { status: 503 }
      );
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
