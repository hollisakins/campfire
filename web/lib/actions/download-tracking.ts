'use server';

import { createServiceClient } from '@/lib/supabase/server';
import { headers } from 'next/headers';

export type DownloadType = 'fits_single' | 'fits_batch' | 'fits_zip' | 'csv' | 'sed_plot';

export interface TrackDownloadParams {
  userId: string;
  downloadType: DownloadType;
  objectIds?: string[];
  objectCount?: number;
  fileCount?: number;
  filterSnapshot?: Record<string, unknown>;
}

/**
 * Track a download event in the download_log table.
 * Uses fire-and-forget pattern - errors are logged but don't block the download.
 * Uses service client to bypass RLS.
 */
export async function trackDownload(params: TrackDownloadParams): Promise<void> {
  try {
    const supabase = createServiceClient();
    const headersList = await headers();

    // Extract IP and user agent from request headers
    const ipAddress = headersList.get('x-forwarded-for')?.split(',')[0]?.trim()
      || headersList.get('x-real-ip')
      || null;
    const userAgent = headersList.get('user-agent') || null;

    const { error } = await supabase
      .from('download_log')
      .insert({
        user_id: params.userId,
        download_type: params.downloadType,
        object_ids: params.objectIds || null,
        object_count: params.objectCount ?? params.objectIds?.length ?? null,
        file_count: params.fileCount ?? null,
        filter_snapshot: params.filterSnapshot || null,
        ip_address: ipAddress,
        user_agent: userAgent,
      });

    if (error) {
      console.error('Failed to track download:', error);
    }
  } catch (error) {
    // Fire-and-forget - log but don't throw
    console.error('Error in trackDownload:', error);
  }
}

/**
 * Helper to extract object ID from a FITS path.
 * Paths are like: "spectra/observation/objectid_grating.fits"
 */
export async function extractObjectIdFromFitsPath(fitsPath: string): Promise<string | null> {
  try {
    const filename = fitsPath.split('/').pop() || '';
    // Remove .fits extension and grating suffix
    // Pattern: objectid_grating.fits -> objectid
    const match = filename.match(/^(.+?)_[^_]+\.fits$/);
    return match ? match[1] : filename.replace('.fits', '');
  } catch {
    return null;
  }
}
