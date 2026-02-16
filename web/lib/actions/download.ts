'use server';

import { getSpectra } from './spectra';
import type { SpectrumObject } from '@/lib/types';
import type { SortColumn, SortDirection } from './spectra-types';
import { AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';
import { trackDownload } from './download-tracking';
import { createClient } from '@/lib/supabase/server';

// JWT signing using Web Crypto API
const WORKER_URL = process.env.NEXT_PUBLIC_WORKER_DOWNLOAD_URL || 'http://localhost:8787';
const JWT_SECRET = process.env.WORKER_JWT_SECRET;

interface DownloadFile {
  key: string;
  filename: string;
}

interface DownloadPayload {
  files: DownloadFile[];
  exp: number;
  zipFilename: string;
}

/**
 * Generate a CSV file from filtered spectra results
 * Returns CSV content as a string
 */
export async function generateCSV(
  filters: AdvancedFilterOptions,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
): Promise<{ csv: string | null; error: string | null }> {
  try {
    // Get user for tracking
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();

    // Fetch all results (no pagination limit for CSV export)
    // We'll use a high limit - adjust if datasets grow beyond this
    const result = await getSpectra(
      {
        programs: filters.programs,
        fields: filters.fields,
        gratings: filters.gratings,
        observations: filters.observations,
        redshift_quality: filters.redshift_quality,
        coordinate_search: filters.coordinate_search,
        redshift_min: filters.redshift_min,
        redshift_max: filters.redshift_max,
        max_snr_min: filters.max_snr_min,
        max_snr_max: filters.max_snr_max,
        max_exposure_time_min: filters.max_exposure_time_min,
        max_exposure_time_max: filters.max_exposure_time_max,
        spectral_features: filters.spectral_features,
        object_flags: filters.object_flags,
        dq_flags: filters.dq_flags,
        inspected_only: filters.inspected_only,
        search: filters.search,
      },
      1, // page
      50000, // pageSize - limit for CSV export
      sortColumn,
      sortDirection
    );

    if (result.error) {
      return { csv: null, error: result.error };
    }

    // Convert to CSV format
    const csv = spectraToCsv(result.spectra, filters.coordinate_search !== null);

    // Track CSV download (fire-and-forget)
    if (user) {
      const objectIds = result.spectra.map(s => s.object_id);
      trackDownload({
        userId: user.id,
        downloadType: 'csv',
        objectIds,
        objectCount: objectIds.length,
        fileCount: 1,
        filterSnapshot: filters as unknown as Record<string, unknown>,
      });
    }

    return { csv, error: null };
  } catch (error) {
    console.error('Error generating CSV:', error);
    return { csv: null, error: 'Failed to generate CSV file' };
  }
}

/**
 * Convert spectra array to CSV string
 */
function spectraToCsv(spectra: SpectrumObject[], includeDistance: boolean): string {
  // Define CSV columns
  const columns = [
    'object_id',
    'field',
    'ra',
    'dec',
    'redshift',
    'redshift_quality',
    'max_snr',
    'max_exposure_time',
    'num_gratings',
    'program_id',
    'program_name',
    'last_inspected_at',
    'last_inspected_by',
  ];

  // Add distance column if coordinate search is active
  if (includeDistance) {
    columns.splice(4, 0, 'distance_degrees'); // Insert after dec
  }

  // Build header row
  const rows: string[] = [columns.join(',')];

  // Build data rows
  for (const obj of spectra) {
    const row: (string | number)[] = [
      escapeCsvValue(obj.object_id),
      escapeCsvValue(obj.field),
      obj.ra.toFixed(6),
      obj.dec.toFixed(6),
    ];

    // Add distance if applicable
    if (includeDistance) {
      row.push(obj.distance != null ? obj.distance.toFixed(8) : '');
    }

    // Continue with other columns
    row.push(
      obj.redshift != null ? obj.redshift.toFixed(6) : '',
      obj.redshift_quality,
      obj.max_snr != null ? obj.max_snr.toFixed(2) : '',
      obj.max_exposure_time != null ? obj.max_exposure_time.toFixed(0) : '',
      obj.num_gratings || obj.spectra.length,
      obj.program_id,
      escapeCsvValue(obj.program_name || ''),
      escapeCsvValue(obj.last_inspected_at || ''),
      escapeCsvValue(obj.last_inspected_by || '')
    );

    rows.push(row.join(','));
  }

  return rows.join('\n');
}

/**
 * Escape CSV value (handle commas, quotes, newlines)
 */
function escapeCsvValue(value: string | null | undefined): string {
  if (value == null || value === '') {
    return '';
  }

  const str = String(value);

  // If value contains comma, quote, or newline, wrap in quotes and escape internal quotes
  if (str.includes(',') || str.includes('"') || str.includes('\n')) {
    return `"${str.replace(/"/g, '""')}"`;
  }

  return str;
}

/**
 * Generate filename for CSV download
 */
export async function generateCsvFilename(): Promise<string> {
  const now = new Date();
  const timestamp = now
    .toISOString()
    .replace(/[-:]/g, '')
    .replace('T', '_')
    .substring(0, 15); // YYYYMMDD_HHMMSS
  return `campfire_spectra_${timestamp}.csv`;
}

/**
 * Generate download URL for FITS files
 * Creates a JWT token with file list and returns Worker URL
 */
export async function generateFitsDownloadUrl(
  filters: AdvancedFilterOptions,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
): Promise<{ url: string | null; error: string | null }> {
  try {
    if (!JWT_SECRET) {
      return { url: null, error: 'Server configuration error: JWT secret not set' };
    }

    // Get user for tracking
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();

    // Fetch filtered results (limit to 200 objects)
    const result = await getSpectra(
      {
        programs: filters.programs,
        fields: filters.fields,
        gratings: filters.gratings,
        observations: filters.observations,
        redshift_quality: filters.redshift_quality,
        coordinate_search: filters.coordinate_search,
        redshift_min: filters.redshift_min,
        redshift_max: filters.redshift_max,
        max_snr_min: filters.max_snr_min,
        max_snr_max: filters.max_snr_max,
        max_exposure_time_min: filters.max_exposure_time_min,
        max_exposure_time_max: filters.max_exposure_time_max,
        spectral_features: filters.spectral_features,
        object_flags: filters.object_flags,
        dq_flags: filters.dq_flags,
        inspected_only: filters.inspected_only,
        search: filters.search,
      },
      1, // page
      200, // pageSize - limit to 200 objects
      sortColumn,
      sortDirection
    );

    if (result.error) {
      return { url: null, error: result.error };
    }

    // Extract all FITS file paths
    const files: DownloadFile[] = [];
    for (const obj of result.spectra) {
      for (const spec of obj.spectra) {
        files.push({
          key: spec.fits_path, // R2 object key
          filename: spec.fits_path.split('/').pop() || spec.fits_path, // Just the filename
        });
      }
    }

    if (files.length === 0) {
      return { url: null, error: 'No FITS files found for selected objects' };
    }

    // Generate ZIP filename with date
    const now = new Date();
    const dateStr = now.toISOString().split('T')[0].replace(/-/g, ''); // YYYYMMDD
    const zipFilename = `campfire_download_${dateStr}.zip`;

    // Create JWT payload
    const payload: DownloadPayload = {
      files,
      exp: Date.now() + 10 * 60 * 1000, // Expire in 10 minutes
      zipFilename,
    };

    // Sign JWT
    const token = await signJWT(payload, JWT_SECRET);

    // Construct Worker URL
    const url = `${WORKER_URL}?token=${token}`;

    // Track ZIP download (fire-and-forget)
    if (user) {
      const objectIds = result.spectra.map(s => s.object_id);
      trackDownload({
        userId: user.id,
        downloadType: 'fits_zip',
        objectIds,
        objectCount: objectIds.length,
        fileCount: files.length,
        filterSnapshot: filters as unknown as Record<string, unknown>,
      });
    }

    return { url, error: null };
  } catch (error) {
    console.error('Error generating FITS download URL:', error);
    return { url: null, error: 'Failed to generate download URL' };
  }
}

/**
 * Sign JWT using HMAC SHA-256 (Web Crypto API)
 */
async function signJWT(payload: DownloadPayload, secret: string): Promise<string> {
  // Create header
  const header = {
    alg: 'HS256',
    typ: 'JWT',
  };

  // Encode header and payload
  const headerB64 = base64UrlEncode(JSON.stringify(header));
  const payloadB64 = base64UrlEncode(JSON.stringify(payload));

  // Create signature
  const data = `${headerB64}.${payloadB64}`;
  const encoder = new TextEncoder();
  const keyData = encoder.encode(secret);
  const messageData = encoder.encode(data);

  // Import key
  const key = await crypto.subtle.importKey(
    'raw',
    keyData,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );

  // Sign
  const signature = await crypto.subtle.sign('HMAC', key, messageData);
  const signatureB64 = base64UrlEncode(signature);

  // Return JWT
  return `${data}.${signatureB64}`;
}

/**
 * Base64URL encode (for JWT)
 */
function base64UrlEncode(data: string | ArrayBuffer): string {
  let base64: string;

  if (typeof data === 'string') {
    // String to base64
    base64 = Buffer.from(data).toString('base64');
  } else {
    // ArrayBuffer to base64
    base64 = Buffer.from(data).toString('base64');
  }

  // Convert to base64url
  return base64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}
