'use server';

import { getSpectra } from './spectra';
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

interface CsvRow {
  object_id: string;
  field: string;
  ra: number;
  dec: number;
  redshift: number | null;
  redshift_quality: number;
  max_snr: number | null;
  max_exposure_time: number | null;
  num_gratings: number;
  program_id: number;
  program_name: string | null;
  last_inspected_at: string | null;
  last_inspected_by: string | null;
  distance: number | null;
}

/**
 * Generate a CSV file from filtered spectra results.
 * Uses a lightweight RPC that returns flat rows — no JSONB object building
 * or nested spectra subqueries, so it handles large result sets (7k+) without
 * hitting statement timeouts.
 */
export async function generateCSV(
  filters: AdvancedFilterOptions,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
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
      .select('program_id')
      .eq('user_id', user.id);

    const explicitAccessIds = (accessData || []).map(a => a.program_id);

    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('program_id')
      .eq('is_public', true);

    const publicProgramIds = (publicPrograms || []).map(p => p.program_id);
    const accessibleProgramIds = [...new Set([...publicProgramIds, ...explicitAccessIds])];

    if (accessibleProgramIds.length === 0) {
      return { csv: null, error: 'No accessible programs' };
    }

    // Prepare bitmask filters
    const spectralFeaturesMask = filters.spectral_features && filters.spectral_features.length > 0
      ? filters.spectral_features.reduce((acc, val) => acc | val, 0)
      : null;
    const objectFlagsMask = filters.object_flags && filters.object_flags.length > 0
      ? filters.object_flags.reduce((acc, val) => acc | val, 0)
      : null;
    const dqFlagsMask = filters.dq_flags && filters.dq_flags.length > 0
      ? filters.dq_flags.reduce((acc, val) => acc | val, 0)
      : null;

    const sfMode = filters.spectral_features_mode || 'any';
    const sfIncludeAny = sfMode === 'any' ? spectralFeaturesMask : null;
    const sfIncludeAll = sfMode === 'all' ? spectralFeaturesMask : null;
    const sfExclude = sfMode === 'none' ? spectralFeaturesMask : null;

    const ofMode = filters.object_flags_mode || 'any';
    const ofIncludeAny = ofMode === 'any' ? objectFlagsMask : null;
    const ofIncludeAll = ofMode === 'all' ? objectFlagsMask : null;
    const ofExclude = ofMode === 'none' ? objectFlagsMask : null;

    const dqMode = filters.dq_flags_mode || 'any';
    const dqIncludeAny = dqMode === 'any' ? dqFlagsMask : null;
    const dqIncludeAll = dqMode === 'all' ? dqFlagsMask : null;
    const dqExclude = dqMode === 'none' ? dqFlagsMask : null;

    // Coordinate search
    let coordRa: number | null = null;
    let coordDec: number | null = null;
    let radiusDegrees: number | null = null;

    if (filters.coordinate_search) {
      coordRa = filters.coordinate_search.ra;
      coordDec = filters.coordinate_search.dec;
      const { radius, radius_unit } = filters.coordinate_search;
      radiusDegrees =
        radius_unit === 'degrees' ? radius :
        radius_unit === 'arcmin' ? radius / 60 :
        radius / 3600;
    }

    // Search routing
    const searchText = filters.search?.trim() || null;
    const searchScope = filters.search_scope || 'object_id';
    const isCommentSearch = searchScope === 'my_comments' || searchScope === 'all_comments';
    const objectIdSearch = searchScope === 'object_id' ? searchText : null;
    const commentSearch = isCommentSearch ? searchText : null;
    const commentSearchScope = isCommentSearch ? (searchScope === 'my_comments' ? 'just_me' : 'everyone') : null;
    const commentUserId = isCommentSearch ? user.id : null;

    // Call the lightweight CSV export RPC (flat rows, no JSONB).
    // Override PostgREST's default max_rows (5000) since CSV export needs all results.
    const { data, error } = await supabase.rpc('get_csv_export', {
      p_program_ids: accessibleProgramIds,
      p_filter_programs: filters.programs && filters.programs.length > 0 ? filters.programs : null,
      p_fields: filters.fields && filters.fields.length > 0 ? filters.fields : null,
      p_gratings: filters.gratings && filters.gratings.length > 0 ? filters.gratings : null,
      p_gratings_mode: filters.gratings_mode || 'any',
      p_observations: filters.observations && filters.observations.length > 0 ? filters.observations : null,
      p_redshift_quality: filters.redshift_quality && filters.redshift_quality.length > 0 ? filters.redshift_quality : null,
      p_redshift_min: filters.redshift_min ?? null,
      p_redshift_max: filters.redshift_max ?? null,
      p_max_snr_min: filters.max_snr_min ?? null,
      p_max_snr_max: filters.max_snr_max ?? null,
      p_max_exposure_time_min: filters.max_exposure_time_min ?? null,
      p_max_exposure_time_max: filters.max_exposure_time_max ?? null,
      p_spectral_features_include_any: sfIncludeAny,
      p_spectral_features_include_all: sfIncludeAll,
      p_spectral_features_exclude: sfExclude,
      p_object_flags_include_any: ofIncludeAny,
      p_object_flags_include_all: ofIncludeAll,
      p_object_flags_exclude: ofExclude,
      p_dq_flags_include_any: dqIncludeAny,
      p_dq_flags_include_all: dqIncludeAll,
      p_dq_flags_exclude: dqExclude,
      p_search: objectIdSearch,
      p_inspected_only: filters.inspected_only ?? null,
      p_comment_search: commentSearch,
      p_comment_search_scope: commentSearchScope,
      p_comment_user_id: commentUserId,
      p_coord_ra: coordRa,
      p_coord_dec: coordDec,
      p_radius_degrees: radiusDegrees,
      p_sort_column: sortColumn,
      p_sort_direction: sortDirection,
    }).limit(100000);

    if (error) {
      console.error('Error fetching CSV data:', error);
      return { csv: null, error: error.message };
    }

    const rows = (data || []) as CsvRow[];
    const includeDistance = filters.coordinate_search !== null;
    const csv = rowsToCsv(rows, includeDistance);

    // Track CSV download (fire-and-forget)
    const objectIds = rows.map(r => r.object_id);
    trackDownload({
      userId: user.id,
      downloadType: 'csv',
      objectIds,
      objectCount: objectIds.length,
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
 * Convert flat CSV export rows to CSV string
 */
function rowsToCsv(rows: CsvRow[], includeDistance: boolean): string {
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
      row.redshift != null ? row.redshift.toFixed(6) : '',
      row.redshift_quality,
      row.max_snr != null ? row.max_snr.toFixed(2) : '',
      row.max_exposure_time != null ? row.max_exposure_time.toFixed(0) : '',
      row.num_gratings ?? 0,
      row.program_id,
      escapeCsvValue(row.program_name || ''),
      escapeCsvValue(row.last_inspected_at || ''),
      escapeCsvValue(row.last_inspected_by || '')
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
