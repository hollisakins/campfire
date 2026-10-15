import { NextRequest, NextResponse } from 'next/server';
import { requestDrTag } from '@/lib/dr/server';
import { getRequestIdentity, getRequestPrincipal } from '@/lib/auth/identity';
import { linkMayDownload } from '@/lib/auth/access-context';
import { generateDownloadUrl, generateDownloadUrls } from '@/lib/r2';
import { releaseDownloadUrls, resolveReleaseMembers } from '@/lib/server/dr-files';
import { trackDownload, extractTargetIdFromFitsPath } from '@/lib/actions/download-tracking';

/**
 * A share link minted with allow_download off may read the catalog but must not
 * reach a single byte. This route authorizes on the SPECTRA row (which such a
 * link can see) and then presigns without consulting storage_objects — where
 * the SQL opt-out lives — so the check has to be made here explicitly. Returns
 * an error response to send, or null when the caller may download.
 */
async function refuseIfLinkWithoutDownloads(): Promise<NextResponse | null> {
  const principal = await getRequestPrincipal();
  if (!principal || !principal.access.isLinkAccount) return null;
  if (linkMayDownload(principal.access)) return null;
  return NextResponse.json(
    { error: 'This shared link does not permit file downloads' },
    { status: 403 },
  );
}

/**
 * GET /api/download?path=<fits_path>
 *
 * Generates a signed URL for downloading a FITS file from R2.
 * Requires authentication.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
  // Under a release (P6c-2) the bytes are the archived copy of the member;
  // a member not archived yet is a 404, never the live file.
  const dr = await requestDrTag();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  const refused = await refuseIfLinkWithoutDownloads();
  if (refused) return refused;

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
    // cat_spectra: the live row, or the release's frozen row (the views carry
    // no FK metadata, so the program is not embedded; RLS already scoped it).
    const { data: spectrum, error: spectrumError } = await supabase
      .from('cat_spectra')
      .select('id, target_id')
      .eq('fits_path', fitsPath)
      .single();

    if (spectrumError || !spectrum) {
      return NextResponse.json(
        { error: 'File not found or access denied' },
        { status: 404 }
      );
    }

    // Generate signed URL (expires in 1 hour)
    let signedUrl: string;
    if (dr) {
      const urls = await releaseDownloadUrls(await resolveReleaseMembers(supabase, dr, [fitsPath]), 3600);
      const archived = urls.get(fitsPath);
      if (!archived) {
        return NextResponse.json({ error: 'File not found or access denied' }, { status: 404 });
      }
      signedUrl = archived;
    } else {
      signedUrl = await generateDownloadUrl(fitsPath, 3600);
    }

    // Track download (fire-and-forget)
    const targetId = await extractTargetIdFromFitsPath(fitsPath);
    trackDownload({
      userId: user.id,
      downloadType: 'fits_single',
      targetIds: targetId ? [targetId] : undefined,
      targetCount: 1,
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
  const { user, supabase } = await getRequestIdentity();
  const dr = await requestDrTag();

  if (!user) {
    return NextResponse.json(
      { error: 'Authentication required' },
      { status: 401 }
    );
  }

  const refused = await refuseIfLinkWithoutDownloads();
  if (refused) return refused;

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
      .from('cat_spectra')
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

    // Generate signed URLs for all files. Batched so dual-read resolves every
    // backend in a single registry query (not one per file).
    const urls: Record<string, string> = {};
    if (dr) {
      // The release's archived copies; members not archived yet are omitted.
      const archived = await releaseDownloadUrls(await resolveReleaseMembers(supabase, dr, paths), 3600);
      for (const path of paths as string[]) {
        const u = archived.get(path);
        if (u) urls[path] = u;
      }
      if (Object.keys(urls).length === 0) {
        return NextResponse.json({ error: 'File not found or access denied' }, { status: 404 });
      }
    } else {
      const signedUrls = await generateDownloadUrls(paths, 3600);
      paths.forEach((path: string, i: number) => {
        urls[path] = signedUrls[i];
      });
    }

    // Track batch download (fire-and-forget)
    const targetIdPromises = paths.map(extractTargetIdFromFitsPath);
    const targetIds = (await Promise.all(targetIdPromises))
      .filter((id): id is string => id !== null);
    const uniqueTargetIds = [...new Set(targetIds)];
    // Use 'fits_object' for single object downloads from detail page
    const downloadType = context === 'object_detail' ? 'fits_object' : 'fits_batch';
    trackDownload({
      userId: user.id,
      downloadType,
      targetIds: uniqueTargetIds,
      targetCount: uniqueTargetIds.length,
      fileCount: Object.keys(urls).length,
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
