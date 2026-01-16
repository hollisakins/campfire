import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';
import { trackDownload, extractObjectIdFromFitsPath } from '@/lib/actions/download-tracking';

/**
 * GET /api/download?path=<fits_path>
 *
 * Generates a signed URL for downloading a FITS file from R2.
 * Requires authentication.
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
    // Verify user has access to this file by checking if the spectrum exists
    // and belongs to a program the user has access to
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
        { error: 'File not found or access denied' },
        { status: 404 }
      );
    }

    // Generate signed URL (expires in 1 hour)
    const signedUrl = await generateDownloadUrl(fitsPath, 3600);

    // Check if it's a placeholder URL (R2 not configured)
    if (signedUrl.startsWith('#download-placeholder')) {
      return NextResponse.json(
        { error: 'Download service not configured. Please contact administrator.' },
        { status: 503 }
      );
    }

    // Track download (fire-and-forget)
    const objectId = await extractObjectIdFromFitsPath(fitsPath);
    trackDownload({
      userId: user.id,
      downloadType: 'fits_single',
      objectIds: objectId ? [objectId] : undefined,
      objectCount: 1,
      fileCount: 1,
    });

    return NextResponse.json({ url: signedUrl });
  } catch (error) {
    console.error('Error generating download URL:', error);
    return NextResponse.json(
      { error: 'Failed to generate download URL' },
      { status: 500 }
    );
  }
}

/**
 * POST /api/download
 *
 * Generates signed URLs for multiple FITS files.
 * Body: { paths: string[] }
 */
export async function POST(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  try {
    const body = await request.json();
    const { paths, context } = body; // context: 'object_detail' for single object downloads

    if (!paths || !Array.isArray(paths) || paths.length === 0) {
      return NextResponse.json(
        { error: 'Missing or invalid paths array' },
        { status: 400 }
      );
    }

    // Limit batch size
    if (paths.length > 10) {
      return NextResponse.json(
        { error: 'Maximum 10 files per batch' },
        { status: 400 }
      );
    }

    // Verify user has access to all files
    const { data: spectra, error: spectraError } = await supabase
      .from('spectra')
      .select('fits_path')
      .in('fits_path', paths);

    if (spectraError) {
      return NextResponse.json(
        { error: 'Failed to verify file access' },
        { status: 500 }
      );
    }

    // Check that all requested paths are accessible
    const accessiblePaths = new Set(spectra?.map(s => s.fits_path) || []);
    const unauthorizedPaths = paths.filter(p => !accessiblePaths.has(p));

    if (unauthorizedPaths.length > 0) {
      return NextResponse.json(
        { error: 'Access denied to some files', unauthorizedPaths },
        { status: 403 }
      );
    }

    // Generate signed URLs for all files
    const urls: Record<string, string> = {};
    for (const path of paths) {
      const signedUrl = await generateDownloadUrl(path, 3600);

      // Check if R2 is configured
      if (signedUrl.startsWith('#download-placeholder')) {
        return NextResponse.json(
          { error: 'Download service not configured. Please contact administrator.' },
          { status: 503 }
        );
      }

      urls[path] = signedUrl;
    }

    // Track batch download (fire-and-forget)
    const objectIdPromises = paths.map(extractObjectIdFromFitsPath);
    const objectIds = (await Promise.all(objectIdPromises))
      .filter((id): id is string => id !== null);
    const uniqueObjectIds = [...new Set(objectIds)];
    // Use 'fits_object' for single object downloads from detail page
    const downloadType = context === 'object_detail' ? 'fits_object' : 'fits_batch';
    trackDownload({
      userId: user.id,
      downloadType,
      objectIds: uniqueObjectIds,
      objectCount: uniqueObjectIds.length,
      fileCount: paths.length,
    });

    return NextResponse.json({ urls });
  } catch (error) {
    console.error('Error generating download URLs:', error);
    return NextResponse.json(
      { error: 'Failed to generate download URLs' },
      { status: 500 }
    );
  }
}
