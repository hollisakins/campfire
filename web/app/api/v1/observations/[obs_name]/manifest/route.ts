import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/service';
import { authenticateApiRequest } from '@/lib/api-auth';
import { assertBearerReleaseAccess } from '@/lib/server/bearer-release';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import { generateDownloadUrls } from '@/lib/r2';
import { releaseDownloadUrls, resolveReleaseMembersAs } from '@/lib/server/dr-files';

/**
 * GET /api/v1/observations/{obs_name}/manifest
 *
 * Generate a download manifest for an observation, including signed URLs
 * for all FITS files. Used by the CLI sync system.
 *
 * Signed URLs expire after 6 hours to allow time for slow connections.
 *
 * Under `dr=<tag>` (P6c-2) the list is the release's frozen spectra and every
 * url is the member's ARCHIVED copy; a member the byte pass has not archived
 * yet keeps its row with `download_url: null`. Never a live key.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ obs_name: string }> }
) {
  const principal = await authenticateApiRequest(request);
  const userId = principal?.userId ?? null;

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing authentication' },
      { status: 401 }
    );
  }

  try {
    const { obs_name } = await params;
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);

    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json(
        { error: 'Observation not found or access denied' },
        { status: 404 }
      );
    }

    // `dr=<tag>`: browse a data release (P6c-2), gated against the caller's
    // own visibility before the header is forwarded.
    const release = await assertBearerReleaseAccess(request, principal!);
    if (release.response) return release.response;
    const supabase = createServiceClient({ dr: release.tag });

    // Admins syncing an observation need its full manifest, including
    // draft spectra; non-admins only ever see published rows. No-op in B1.
    const includeUnpublished = await isAdminUser(userId);

    // Get all spectra for this observation (the main payload)
    const { data: spectra, error: spectraError } = await supabase.rpc('get_observation_manifest', {
      p_obs_name: obs_name,
      p_program_slugs: accessibleProgramSlugs,
      p_include_unpublished: includeUnpublished,
    });

    if (spectraError) {
      console.error('Error fetching manifest:', spectraError);
      return NextResponse.json(
        { error: 'Failed to fetch spectra data' },
        { status: 500 }
      );
    }

    const spectraList = spectra || [];

    if (spectraList.length === 0) {
      return NextResponse.json(
        { error: 'Observation not found or access denied' },
        { status: 404 }
      );
    }

    // Derive observation metadata from manifest results (avoids expensive all-obs aggregation)
    const targetIds = [...new Set(spectraList.map((s: { target_id: string }) => s.target_id))];
    const totalSizeBytes = spectraList.reduce((sum: number, s: { file_size: number | null }) => sum + (s.file_size || 0), 0);

    // Lightweight query for program/field info (single row, indexed).
    // cat_targets: the live row, or the release's frozen row (views carry no
    // FK metadata, so the program name is a second small read).
    const { data: obsTarget } = await supabase
      .from('cat_targets')
      .select('program_slug, field')
      .eq('observation', obs_name)
      .in('program_slug', accessibleProgramSlugs)
      .limit(1)
      .maybeSingle();
    let programName: string | null = null;
    if (obsTarget?.program_slug) {
      const { data: prog } = await supabase
        .from('programs')
        .select('program_name')
        .eq('slug', obsTarget.program_slug)
        .maybeSingle();
      programName = prog?.program_name ?? null;
    }

    // Generate signed URLs (6-hour expiry = 21600 seconds). Batched so dual-read
    // resolves every backend in a single registry query (not one per spectrum).
    // Under a release: the archived copies only, null for a pending member.
    const urlExpiresAt = new Date(Date.now() + 21600 * 1000).toISOString();
    const paths = spectraList.map((s: { fits_path: string }) => s.fits_path);
    let signedUrls: (string | null)[];
    if (release.tag) {
      const members = await resolveReleaseMembersAs(supabase, release.tag, paths, {
        accessible: accessibleProgramSlugs, isAdmin: includeUnpublished,
      });
      const urls = await releaseDownloadUrls(members, 21600);
      signedUrls = paths.map((p: string) => urls.get(p) ?? null);
    } else {
      signedUrls = await generateDownloadUrls(paths, 21600);
    }

    // Build response with download URLs
    const spectraWithUrls = spectraList.map(
      (s: {
        spectra_id: number;
        spectrum_id: string;
        target_id: string;
        grating: string;
        fits_path: string;
        file_hash: string | null;
        file_size: number | null;
        signal_to_noise: number | null;
        cfpipe_version: string | null;
      }, i: number) => ({
        spectra_id: s.spectra_id,
        spectrum_id: s.spectrum_id,
        target_id: s.target_id,
        grating: s.grating,
        fits_path: s.fits_path,
        file_hash: s.file_hash,
        file_size: s.file_size,
        signal_to_noise: s.signal_to_noise,
        cfpipe_version: s.cfpipe_version,
        download_url: signedUrls[i],
      })
    );

    // Track download (fire-and-forget)
    supabase
      .from('download_log')
      .insert({
        user_id: userId,
        download_type: 'fits_sync',
        target_ids: targetIds,
        target_count: targetIds.length,
        file_count: spectraList.length,
        filter_snapshot: { observation: obs_name, ...(release.tag ? { dr: release.tag } : {}) },
        ip_address: request.headers.get('x-forwarded-for')?.split(',')[0]?.trim()
          || request.headers.get('x-real-ip')
          || null,
        user_agent: request.headers.get('user-agent') || null,
      })
      .then(
        () => {},
        (err) => console.error('Failed to track sync download:', err)
      );

    return NextResponse.json({
      observation: obs_name,
      ...(release.tag ? { dr: release.tag } : {}),
      program_slug: obsTarget?.program_slug || null,
      program_name: programName,
      field: obsTarget?.field || null,
      target_count: targetIds.length,
      spectrum_count: spectraList.length,
      total_size_bytes: totalSizeBytes,
      url_expires_at: urlExpiresAt,
      spectra: spectraWithUrls,
    });
  } catch (error) {
    console.error('Error in GET /api/v1/observations/[obs_name]/manifest:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
