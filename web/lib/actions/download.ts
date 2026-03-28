'use server';

import { getSpectra } from './spectra';
import type { SortColumn, SortDirection, ViewMode } from './spectra-types';
import type { FilterOptions } from './filter-params';
import { trackDownload } from './download-tracking';
import { createClient } from '@/lib/supabase/server';
import { paginateRpc } from '@/lib/supabase/paginate';
import { buildFilterParams } from './filter-params';
import { SPECTRAL_FEATURES, OBJECT_FLAGS, DQ_FLAGS } from '@/lib/flags';
import type { FlagDef } from '@/lib/flags';

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

interface CsvRow {
  target_id: string;
  field: string;
  ra: number;
  dec: number;
  redshift: number | null;
  redshift_quality: number;
  max_snr: number | null;
  max_exposure_time: number | null;
  num_gratings: number;
  program_slug: string;
  program_name: string | null;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  distance: number | null;
  spectral_features: number;
  object_flags: number;
  dq_flags: number;
}

interface SpectraCsvRow {
  target_id: string;
  grating: string;
  field: string;
  ra: number;
  dec: number;
  redshift: number | null;
  redshift_quality: number;
  signal_to_noise: number | null;
  exposure_time: number | null;
  fits_path: string;
  program_slug: string;
  program_name: string | null;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  distance: number | null;
  spectral_features: number;
  object_flags: number;
  dq_flags: number;
}

/**
 * Generate a CSV file from filtered spectra results.
 * Uses a lightweight RPC that returns flat rows — no JSONB object building
 * or nested spectra subqueries, so it handles large result sets (7k+) without
 * hitting statement timeouts.
 */
export async function generateCSV(
  filters: FilterOptions,
  sortColumn: SortColumn = 'target_id',
  sortDirection: SortDirection = 'asc',
  viewMode: ViewMode = 'targets'
): Promise<{ csv: string | null; error: string | null }> {
  try {
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();

    if (!user) {
      return { csv: null, error: 'Not authenticated' };
    }

    // Determine accessible programs (same logic as getSpectra)
    const { data: accessData } = await supabase
      .from('user_program_access')
      .select('program_slug')
      .eq('user_id', user.id);

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);

    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('slug')
      .eq('is_public', true);

    const publicProgramSlugs = (publicPrograms || []).map(p => p.slug);
    const accessibleProgramSlugs = [...new Set([...publicProgramSlugs, ...explicitAccessSlugs])];

    if (accessibleProgramSlugs.length === 0) {
      return { csv: null, error: 'No accessible programs' };
    }

    const rpcParams = {
      ...buildFilterParams(filters, accessibleProgramSlugs, user.id),
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
    };

    const includeDistance = filters.coordinate_search !== null;

    if (viewMode === 'spectra') {
      // Spectra mode: one row per (target_id, grating)
      const { data: rows, error: rpcError } = await paginateRpc<SpectraCsvRow>(
        supabase, 'get_csv_export_spectra', rpcParams,
      );

      if (rpcError) {
        console.error('Error fetching spectra CSV data:', rpcError);
        return { csv: null, error: rpcError.message };
      }
      const csv = spectraRowsToCsv(rows, includeDistance);

      const targetIds = [...new Set(rows.map(r => r.target_id))];
      trackDownload({
        userId: user.id,
        downloadType: 'csv',
        targetIds,
        targetCount: targetIds.length,
        fileCount: 1,
        filterSnapshot: filters as unknown as Record<string, unknown>,
      });

      return { csv, error: null };
    }

    // Objects mode: one row per object (existing behavior)
    const { data: rows, error: rpcError } = await paginateRpc<CsvRow>(
      supabase, 'get_csv_export', rpcParams,
    );

    if (rpcError) {
      console.error('Error fetching CSV data:', rpcError);
      return { csv: null, error: rpcError.message };
    }
    const csv = rowsToCsv(rows, includeDistance);

    // Track CSV download (fire-and-forget)
    const targetIds = rows.map(r => r.target_id);
    trackDownload({
      userId: user.id,
      downloadType: 'csv',
      targetIds,
      targetCount: targetIds.length,
      fileCount: 1,
      filterSnapshot: filters as unknown as Record<string, unknown>,
    });

    return { csv, error: null };
  } catch (error) {
    console.error('Error generating CSV:', error);
    return { csv: null, error: 'Failed to generate CSV file' };
  }
}

/**
 * Expand a bitmask into individual 0/1 values for each flag definition
 */
function expandBitmask(bitmask: number, flags: FlagDef[]): number[] {
  return flags.map(flag => (bitmask & flag.value) !== 0 ? 1 : 0);
}

/**
 * Convert flat CSV export rows to CSV string
 */
function rowsToCsv(rows: CsvRow[], includeDistance: boolean): string {
  const columns = [
    'target_id',
    'field',
    'ra',
    'dec',
    'redshift',
    'redshift_quality',
    'max_snr',
    'max_exposure_time',
    'num_gratings',
    'program_slug',
    'program_name',
    'last_inspected_at',
    'last_inspected_by',
    ...SPECTRAL_FEATURES.map(f => `sf_${f.key}`),
    ...OBJECT_FLAGS.map(f => `flag_${f.key}`),
    ...DQ_FLAGS.map(f => `dq_${f.key}`),
  ];

  if (includeDistance) {
    columns.splice(4, 0, 'distance_degrees');
  }

  const csvRows: string[] = [columns.join(',')];

  for (const row of rows) {
    const values: (string | number)[] = [
      escapeCsvValue(row.target_id),
      escapeCsvValue(row.field),
      row.ra.toFixed(8),
      row.dec.toFixed(8),
    ];

    if (includeDistance) {
      values.push(row.distance != null ? row.distance.toFixed(8) : '');
    }

    values.push(
      row.redshift != null ? row.redshift.toFixed(6) : '',
      row.redshift_quality,
      row.max_snr != null ? row.max_snr.toFixed(2) : '',
      row.max_exposure_time != null ? row.max_exposure_time.toFixed(0) : '',
      row.num_gratings ?? 0,
      escapeCsvValue(row.program_slug),
      escapeCsvValue(row.program_name || ''),
      escapeCsvValue(row.last_inspected_at || ''),
      escapeCsvValue(row.last_inspected_by || ''),
      ...expandBitmask(row.spectral_features, SPECTRAL_FEATURES),
      ...expandBitmask(row.object_flags, OBJECT_FLAGS),
      ...expandBitmask(row.dq_flags, DQ_FLAGS),
    );

    csvRows.push(values.join(','));
  }

  return csvRows.join('\n');
}

/**
 * Convert spectra-mode CSV export rows to CSV string (one row per spectrum)
 */
function spectraRowsToCsv(rows: SpectraCsvRow[], includeDistance: boolean): string {
  const columns = [
    'target_id',
    'grating',
    'field',
    'ra',
    'dec',
    'redshift',
    'redshift_quality',
    'signal_to_noise',
    'exposure_time',
    'fits_path',
    'program_slug',
    'program_name',
    'last_inspected_at',
    'last_inspected_by',
    ...SPECTRAL_FEATURES.map(f => `sf_${f.key}`),
    ...OBJECT_FLAGS.map(f => `flag_${f.key}`),
    ...DQ_FLAGS.map(f => `dq_${f.key}`),
  ];

  if (includeDistance) {
    columns.splice(5, 0, 'distance_degrees');
  }

  const csvRows: string[] = [columns.join(',')];

  for (const row of rows) {
    const values: (string | number)[] = [
      escapeCsvValue(row.target_id),
      escapeCsvValue(row.grating),
      escapeCsvValue(row.field),
      row.ra.toFixed(8),
      row.dec.toFixed(8),
    ];

    if (includeDistance) {
      values.push(row.distance != null ? row.distance.toFixed(8) : '');
    }

    values.push(
      row.redshift != null ? row.redshift.toFixed(6) : '',
      row.redshift_quality,
      row.signal_to_noise != null ? row.signal_to_noise.toFixed(2) : '',
      row.exposure_time != null ? row.exposure_time.toFixed(0) : '',
      escapeCsvValue(row.fits_path),
      escapeCsvValue(row.program_slug),
      escapeCsvValue(row.program_name || ''),
      escapeCsvValue(row.last_inspected_at || ''),
      escapeCsvValue(row.last_inspected_by || ''),
      ...expandBitmask(row.spectral_features, SPECTRAL_FEATURES),
      ...expandBitmask(row.object_flags, OBJECT_FLAGS),
      ...expandBitmask(row.dq_flags, DQ_FLAGS),
    );

    csvRows.push(values.join(','));
  }

  return csvRows.join('\n');
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
  filters: FilterOptions,
  sortColumn: SortColumn = 'target_id',
  sortDirection: SortDirection = 'asc'
): Promise<{
  files: DownloadFile[] | null;
  token: string | null;
  workerUrl: string | null;
  zipFilename: string | null;
  error: string | null;
}> {
  try {
    if (!JWT_SECRET) {
      return { files: null, token: null, workerUrl: null, zipFilename: null, error: 'Server configuration error: JWT secret not set' };
    }

    // Get user for tracking
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();

    // Fetch filtered results (limit to 200 objects)
    const result = await getSpectra(
      filters,
      1, // page
      200, // pageSize - limit to 200 objects
      sortColumn,
      sortDirection
    );

    if (result.error) {
      return { files: null, token: null, workerUrl: null, zipFilename: null, error: result.error };
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
      return { files: null, token: null, workerUrl: null, zipFilename: null, error: 'No FITS files found for selected objects' };
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

    // Track ZIP download (fire-and-forget)
    if (user) {
      const targetIds = result.spectra.map(s => s.target_id);
      trackDownload({
        userId: user.id,
        downloadType: 'fits_zip',
        targetIds,
        targetCount: targetIds.length,
        fileCount: files.length,
        filterSnapshot: filters as unknown as Record<string, unknown>,
      });
    }

    return { files, token, workerUrl: WORKER_URL, zipFilename, error: null };
  } catch (error) {
    console.error('Error generating FITS download URL:', error);
    return { files: null, token: null, workerUrl: null, zipFilename: null, error: 'Failed to generate download URL' };
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
