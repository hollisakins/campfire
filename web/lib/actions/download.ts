'use server';

import { getSpectra } from './spectra';
import type { SortColumn, SortDirection, ViewMode } from './spectra-types';
import type { FilterOptions } from './filter-params';
import { trackDownload } from './download-tracking';
import { createClient } from '@/lib/supabase/server';
import { paginateRpc } from '@/lib/supabase/paginate';
import { buildFilterParams } from './filter-params';
import { DQ_FLAGS } from '@/lib/flags';
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

interface PhotometryBands {
  [band: string]: {
    flux: number;
    flux_err: number;
    wav?: number;
    wav_min?: number;
    wav_max?: number;
  };
}

interface ObjectsCsvRow {
  object_id: string;
  field: string;
  ra: number;
  dec: number;
  best_redshift: number | null;
  best_redshift_quality: number;
  n_targets: number;
  n_spectra: number;
  programs: string;            // semicolon-separated
  gratings: string;            // semicolon-separated
  max_snr: number | null;
  max_exposure_time: number | null;
  member_target_ids: string;   // semicolon-separated
  distance: number | null;
  lists: string | null;        // semicolon-separated list slugs
  has_photometry: boolean;
  photo_z: number | null;
  photo_z_err_lo: number | null;
  photo_z_err_hi: number | null;
  photometry: { flux_unit: string; bands: PhotometryBands } | null;
}

interface SpectraCsvRow {
  spectrum_id?: string;
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
  dq_flags: number;
  lists: string | null;        // semicolon-separated list slugs
}

/**
 * Generate a CSV file from filtered spectra results.
 * Uses a lightweight RPC that returns flat rows — no JSONB object building
 * or nested spectra subqueries, so it handles large result sets (7k+) without
 * hitting statement timeouts.
 */
export async function generateCSV(
  filters: FilterOptions,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc',
  viewMode: ViewMode = 'objects'
): Promise<{ csv: string | null; error: string | null }> {
  try {
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();

    if (!user) {
      return { csv: null, error: 'Not authenticated' };
    }

    // Determine accessible programs (parallel queries)
    const [{ data: accessData }, { data: publicPrograms }] = await Promise.all([
      supabase.from('user_program_access').select('program_slug').eq('user_id', user.id),
      supabase.from('programs').select('slug').eq('is_public', true),
    ]);

    const explicitAccessSlugs = (accessData || []).map(a => a.program_slug);
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

    if (viewMode === 'objects') {
      // Objects mode: one row per sky-object (cross-program grouped position)
      // Strip target-only params that the objects RPC doesn't accept
      const {
        p_observations: _obs,
        p_spectral_features_include_any: _sf1, p_spectral_features_include_all: _sf2, p_spectral_features_exclude: _sf3,
        p_dq_flags_include_any: _dq1, p_dq_flags_include_all: _dq2, p_dq_flags_exclude: _dq3,
        p_comment_search: _cs, p_comment_search_scope: _css, p_comment_user_id: _cu,
        ...objectsParams
      } = { ...rpcParams, p_sort_column: sortColumn, p_sort_direction: sortDirection };

      const { data: rows, error: rpcError } = await paginateRpc<ObjectsCsvRow>(
        supabase, 'get_csv_export_objects', objectsParams,
      );

      if (rpcError) {
        console.error('Error fetching objects CSV data:', rpcError);
        return { csv: null, error: rpcError.message };
      }
      const csv = objectsRowsToCsv(rows, includeDistance);

      const objectIds = rows.map(r => r.object_id);
      trackDownload({
        userId: user.id,
        downloadType: 'csv',
        targetIds: objectIds,
        targetCount: objectIds.length,
        fileCount: 1,
        filterSnapshot: filters as unknown as Record<string, unknown>,
      });

      return { csv, error: null };
    }

    // Spectra mode: one row per (target_id, grating).
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
    ...DQ_FLAGS.map(f => `dq_${f.key}`),
    'tags',
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
      ...expandBitmask(row.dq_flags, DQ_FLAGS),
      escapeCsvValue(row.lists || ''),
    );

    csvRows.push(values.join(','));
  }

  return csvRows.join('\n');
}

/**
 * Collect all unique band names from photometry rows, sorted by wavelength
 */
function collectSortedBands(rows: ObjectsCsvRow[]): string[] {
  const bandWavs = new Map<string, number>();
  for (const row of rows) {
    if (!row.photometry?.bands) continue;
    for (const [band, data] of Object.entries(row.photometry.bands)) {
      if (!bandWavs.has(band) && data.wav != null) {
        bandWavs.set(band, data.wav);
      } else if (!bandWavs.has(band)) {
        bandWavs.set(band, Infinity);
      }
    }
  }
  return [...bandWavs.entries()]
    .sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]))
    .map(([band]) => band);
}

/**
 * Convert objects-mode CSV export rows to CSV string
 */
function objectsRowsToCsv(rows: ObjectsCsvRow[], includeDistance: boolean): string {
  const sortedBands = collectSortedBands(rows);

  const columns = [
    'object_id',
    'field',
    'ra',
    'dec',
    'best_redshift',
    'best_redshift_quality',
    'n_targets',
    'n_spectra',
    'programs',
    'gratings',
    'max_snr',
    'max_exposure_time',
    'member_target_ids',
    'tags',
    'has_photometry',
    'photo_z',
    'photo_z_err_lo',
    'photo_z_err_hi',
    ...sortedBands.flatMap(b => [`f_${b}`, `e_${b}`]),
  ];

  if (includeDistance) {
    columns.splice(4, 0, 'distance_degrees');
  }

  const csvRows: string[] = [columns.join(',')];

  for (const row of rows) {
    const values: (string | number)[] = [
      escapeCsvValue(row.object_id),
      escapeCsvValue(row.field),
      row.ra.toFixed(8),
      row.dec.toFixed(8),
    ];

    if (includeDistance) {
      values.push(row.distance != null ? row.distance.toFixed(8) : '');
    }

    values.push(
      row.best_redshift != null ? row.best_redshift.toFixed(6) : '',
      row.best_redshift_quality,
      row.n_targets,
      row.n_spectra,
      escapeCsvValue(row.programs || ''),
      escapeCsvValue(row.gratings || ''),
      row.max_snr != null ? row.max_snr.toFixed(2) : '',
      row.max_exposure_time != null ? row.max_exposure_time.toFixed(0) : '',
      escapeCsvValue(row.member_target_ids || ''),
      escapeCsvValue(row.lists || ''),
      row.has_photometry ? 1 : 0,
      row.photo_z != null ? row.photo_z.toFixed(6) : '',
      row.photo_z_err_lo != null ? row.photo_z_err_lo.toFixed(6) : '',
      row.photo_z_err_hi != null ? row.photo_z_err_hi.toFixed(6) : '',
    );

    // Expand photometry bands
    const bands = row.photometry?.bands;
    for (const band of sortedBands) {
      const data = bands?.[band];
      if (data) {
        values.push(data.flux.toFixed(6), data.flux_err.toFixed(6));
      } else {
        values.push('', '');
      }
    }

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
export async function generateCsvFilename(viewMode: string = 'objects'): Promise<string> {
  const now = new Date();
  const timestamp = now
    .toISOString()
    .replace(/[-:]/g, '')
    .replace('T', '_')
    .substring(0, 15); // YYYYMMDD_HHMMSS
  return `campfire_${viewMode}_${timestamp}.csv`;
}

/**
 * Generate download URL for FITS files
 * Creates a JWT token with file list and returns Worker URL
 */
export async function generateFitsDownloadUrl(
  filters: FilterOptions,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc',
  viewMode: ViewMode = 'objects'
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

    // Fetch filtered results (limit to 200 items) via spectra mode — that RPC
    // returns one row per (target, grating) with the FITS path attached.
    const result = await getSpectra(
      filters,
      1, // page
      200, // pageSize
      sortColumn === 'object_id' ? 'target_id' : sortColumn,
      sortDirection,
      'spectra'
    );

    if (result.error) {
      return { files: null, token: null, workerUrl: null, zipFilename: null, error: result.error };
    }

    // Extract all FITS file paths from spectra on each target
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
