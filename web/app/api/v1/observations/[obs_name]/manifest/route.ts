import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import { generateDownloadUrl } from '@/lib/r2';

/**
 * GET /api/v1/observations/{obs_name}/manifest
 *
 * Generate a download manifest for an observation, including signed URLs
 * for all FITS files. Used by the CLI sync system.
 *
 * Signed URLs expire after 6 hours to allow time for slow connections.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ obs_name: string }> }
) {
  const userId = await validateAuth(request);

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

    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    // Get observation metadata (program, field, counts)
    const { data: obsStats, error: obsError } = await supabase.rpc('get_observation_stats', {
      p_program_slugs: accessibleProgramSlugs,
    });

    if (obsError) {
      console.error('Error fetching observation stats:', obsError);
      return NextResponse.json(
        { error: 'Failed to fetch observation data' },
        { status: 500 }
      );
    }

    const obsInfo = (obsStats || []).find(
      (o: { observation: string }) => o.observation === obs_name
    );

    if (!obsInfo) {
      return NextResponse.json(
        { error: 'Observation not found or access denied' },
        { status: 404 }
      );
    }

    // Get all spectra for this observation
    const { data: spectra, error: spectraError } = await supabase.rpc('get_observation_manifest', {
      p_obs_name: obs_name,
      p_program_slugs: accessibleProgramSlugs,
    });

    if (spectraError) {
      console.error('Error fetching manifest:', spectraError);
      return NextResponse.json(
        { error: 'Failed to fetch spectra data' },
        { status: 500 }
      );
    }

    const spectraList = spectra || [];

    // Generate signed URLs (6-hour expiry = 21600 seconds)
    const urlExpiresAt = new Date(Date.now() + 21600 * 1000).toISOString();
    const signedUrls = await Promise.all(
      spectraList.map((s: { fits_path: string }) => generateDownloadUrl(s.fits_path, 21600))
    );

    // Build response with download URLs
    const spectraWithUrls = spectraList.map(
      (s: {
        spectra_id: number;
        target_id: string;
        grating: string;
        fits_path: string;
        file_hash: string | null;
        file_size: number | null;
        signal_to_noise: number | null;
        reduction_version: string;
      }, i: number) => ({
        spectra_id: s.spectra_id,
        target_id: s.target_id,
        grating: s.grating,
        fits_path: s.fits_path,
        file_hash: s.file_hash,
        file_size: s.file_size,
        signal_to_noise: s.signal_to_noise,
        reduction_version: s.reduction_version,
        download_url: signedUrls[i],
      })
    );

    // Track download (fire-and-forget)
    const targetIds = [...new Set(spectraList.map((s: { target_id: string }) => s.target_id))];
    supabase
      .from('download_log')
      .insert({
        user_id: userId,
        download_type: 'fits_sync',
        target_ids: targetIds,
        target_count: targetIds.length,
        file_count: spectraList.length,
        filter_snapshot: { observation: obs_name },
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
      observation: obsInfo.observation,
      program_slug: obsInfo.program_slug,
      program_name: obsInfo.program_name,
      field: obsInfo.field,
      target_count: obsInfo.target_count,
      spectrum_count: obsInfo.spectrum_count,
      total_size_bytes: obsInfo.total_size_bytes,
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
