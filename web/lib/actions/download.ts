'use server';

import { getSpectra } from './spectra';
import type { SortColumn, SortDirection, ViewMode } from './spectra-types';
import { FITS_DOWNLOAD_FILE_LIMIT, CSV_EXPORT_ROW_LIMIT } from './spectra-types';
import type { FilterOptions } from './filter-params';
import { trackDownload } from './download-tracking';
import { createClient } from '@/lib/supabase/server';
import { getAccessibleProgramSlugs } from '@/lib/accessible-programs';
import { paginateRpcKeyset } from '@/lib/supabase/paginate';
import { buildFilterParams } from './filter-params';
import { DQ_FLAGS } from '@/lib/flags';
import type { FlagDef } from '@/lib/flags';
import { generateDownloadUrls } from '@/lib/r2';

const WORKER_URL = process.env.NEXT_PUBLIC_WORKER_DOWNLOAD_URL || 'http://localhost:8787';
const JWT_SECRET = process.env.WORKER_JWT_SECRET;

// Presigned-URL lifetime. Must outlive the WHOLE client-side download (the browser
// fetches every file through the proxy, then zips), so keep it generous — 6h, the
// same value the observation manifest / storage-presign routes use.
const PRESIGN_TTL_SECONDS = 21600;

interface DownloadFile {
  proxyUrl: string; // ready-to-fetch Worker proxy URL (?url=<presigned>&sig=<hmac>)
  filename: string;
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
  redshift: number | null;
  redshift_quality: number;
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
  id: number;                  // spectra PK — keyset cursor, not a CSV column
  spectrum_id: string | null;
  target_id: string;
  grating: string;
  field: string;
  observation: string;         // sort-only, not a CSV column
  ra: number;
  dec: number;
  redshift: number | null;          // sort-only; parent-object state lives in the objects CSV
  redshift_quality: number | null;  // sort-only
  redshift_auto: number | null;
  signal_to_noise: number | null;
  exposure_time: number | null;
  fits_path: string;
  program_slug: string;
  program_name: string | null;
  distance: number | null;
  dq_flags: number;
  lists: string | null;        // semicolon-separated list slugs
}

/**
 * Generate a CSV file from filtered spectra results.
 *
 * Rows are fetched through keyset-paginated RPCs (issue #412): each page is an
 * index-bounded scan continuing from a unique-key cursor, so total DB work
 * stays proportional to the result set — offset paging re-executed and
 * re-sorted the whole query per page and hit the 8s statement timeout at
 * ~16k rows. The RPCs return rows in cursor-key order; the user's on-screen
 * sort is applied here in JS (see sortCsvRows). Exports are capped at
 * CSV_EXPORT_ROW_LIMIT, matching the warning shown in the download dropdown.
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

    // Which programs can this user access? One RPC to the SQL authority
    // (accessible_program_slugs) rather than a hand-rolled grants + public
    // union: the union is wrong for link accounts (scoped program only, no
    // is_public) and admins (every program). See web/lib/accessible-programs.ts.
    const accessibleProgramSlugs = await getAccessibleProgramSlugs(supabase);

    if (accessibleProgramSlugs.length === 0) {
      return { csv: null, error: 'No accessible programs' };
    }

    const rpcParams = buildFilterParams(filters, accessibleProgramSlugs, user.id);

    const includeDistance = filters.coordinate_search !== null;

    if (viewMode === 'objects') {
      // Objects mode: one row per sky-object (cross-program grouped position)
      // Strip target-only params that the objects RPC doesn't accept
      /* eslint-disable @typescript-eslint/no-unused-vars */
      const {
        p_observations: _obs,
        p_dq_flags_include_any: _dq1, p_dq_flags_include_all: _dq2, p_dq_flags_exclude: _dq3,
        ...objectsParams
      } = rpcParams;
      /* eslint-enable @typescript-eslint/no-unused-vars */

      const { data: rows, error: rpcError } = await paginateRpcKeyset<ObjectsCsvRow>(
        supabase, 'get_csv_export_objects', { ...objectsParams },
        { cursorParam: 'p_after_object_id', getCursor: r => r.object_id, maxRows: CSV_EXPORT_ROW_LIMIT },
      );

      if (rpcError) {
        console.error('Error fetching objects CSV data:', rpcError);
        return { csv: null, error: rpcError.message };
      }

      sortCsvRows(
        rows,
        includeDistance ? OBJECTS_CSV_SORT.distance : (OBJECTS_CSV_SORT[sortColumn] ?? OBJECTS_CSV_SORT.object_id),
        includeDistance ? 'asc' : sortDirection,
        [r => r.object_id],
      );
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
    const { data: rows, error: rpcError } = await paginateRpcKeyset<SpectraCsvRow>(
      supabase, 'get_csv_export_spectra', { ...rpcParams },
      { cursorParam: 'p_after_id', getCursor: r => r.id, maxRows: CSV_EXPORT_ROW_LIMIT },
    );

    if (rpcError) {
      console.error('Error fetching spectra CSV data:', rpcError);
      return { csv: null, error: rpcError.message };
    }

    sortCsvRows(
      rows,
      includeDistance ? SPECTRA_CSV_SORT.distance : (SPECTRA_CSV_SORT[sortColumn] ?? SPECTRA_CSV_SORT.spectrum_id),
      includeDistance ? 'asc' : sortDirection,
      [r => r.target_id, r => r.grating],
    );
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

type CsvSortValue = string | number | null | undefined;

// Sortable columns per mode, keyed by SortColumn. Doubles as the whitelist the
// old SQL ORDER BY enforced: an unknown column falls back to the mode default.
const OBJECTS_CSV_SORT: Partial<Record<SortColumn, (r: ObjectsCsvRow) => CsvSortValue>> = {
  object_id: r => r.object_id,
  field: r => r.field,
  ra: r => r.ra,
  dec: r => r.dec,
  redshift: r => r.redshift,
  redshift_quality: r => r.redshift_quality,
  n_targets: r => r.n_targets,
  n_spectra: r => r.n_spectra,
  max_snr: r => r.max_snr,
  max_exposure_time: r => r.max_exposure_time,
  photo_z: r => r.photo_z,
  distance: r => r.distance,
};

const SPECTRA_CSV_SORT: Partial<Record<SortColumn, (r: SpectraCsvRow) => CsvSortValue>> = {
  spectrum_id: r => r.spectrum_id,
  target_id: r => r.target_id,
  field: r => r.field,
  observation: r => r.observation,
  program_slug: r => r.program_slug,
  ra: r => r.ra,
  dec: r => r.dec,
  redshift: r => r.redshift,
  redshift_quality: r => r.redshift_quality,
  redshift_auto: r => r.redshift_auto,
  signal_to_noise: r => r.signal_to_noise,
  exposure_time: r => r.exposure_time,
  grating: r => r.grating,
  distance: r => r.distance,
};

/**
 * Order rows for the CSV in place. The keyset RPCs return rows in cursor-key
 * order (issue #412); the user's on-screen sort is applied here instead —
 * sorting the full export in JS costs milliseconds, versus the database
 * re-sorting the entire result set once per page. Mirrors the old SQL
 * semantics: distance wins whenever a coordinate search is active (callers
 * pass the distance accessor and 'asc'), nulls sort last in both directions,
 * and ties break ascending on the stable unique key(s).
 */
function sortCsvRows<T>(
  rows: T[],
  primary: ((r: T) => CsvSortValue) | undefined,
  direction: SortDirection,
  tiebreaks: ((r: T) => CsvSortValue)[],
): void {
  const dir = direction === 'desc' ? -1 : 1;
  const accessors: [(r: T) => CsvSortValue, number][] = [
    ...(primary ? [[primary, dir] as [(r: T) => CsvSortValue, number]] : []),
    ...tiebreaks.map(tb => [tb, 1] as [(r: T) => CsvSortValue, number]),
  ];
  rows.sort((x, y) => {
    for (const [accessor, d] of accessors) {
      const a = accessor(x);
      const b = accessor(y);
      if (a == null || b == null) {
        if (a != null) return -1; // NULLS LAST regardless of direction
        if (b != null) return 1;
        continue;
      }
      if (a !== b) return a < b ? -1 * d : d;
    }
    return 0;
  });
}

/**
 * Convert spectra-mode CSV export rows to CSV string (one row per spectrum)
 */
function spectraRowsToCsv(rows: SpectraCsvRow[], includeDistance: boolean): string {
  // Spectra-mode CSV contains only per-spectrum info. redshift /
  // redshift_quality / last_inspected_* are parent-object state and belong in
  // the objects CSV. The per-spectrum auto-fit redshift is surfaced as
  // redshift_auto.
  const columns = [
    'target_id',
    'grating',
    'field',
    'ra',
    'dec',
    'redshift_auto',
    'signal_to_noise',
    'exposure_time',
    'fits_path',
    'program_slug',
    'program_name',
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
      row.redshift_auto != null ? row.redshift_auto.toFixed(6) : '',
      row.signal_to_noise != null ? row.signal_to_noise.toFixed(2) : '',
      row.exposure_time != null ? row.exposure_time.toFixed(0) : '',
      escapeCsvValue(row.fits_path),
      escapeCsvValue(row.program_slug),
      escapeCsvValue(row.program_name || ''),
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
    'redshift',
    'redshift_quality',
    'n_observations',
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
      row.redshift != null ? row.redshift.toFixed(6) : '',
      row.redshift_quality,
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
 * Authorize a bulk FITS download and return ready-to-fetch Worker proxy URLs.
 *
 * The key set is derived server-side under the user's RLS session, each key is
 * presigned against whichever backend homes it (dual-read: R2 or OSN), and each
 * presigned URL is HMAC-signed so the credential-free proxy Worker will fetch
 * only URLs we authorized. The browser fetches each proxy URL (which supplies
 * CORS) and zips the results client-side.
 *
 * The download unit follows the view the user filtered in. In spectra mode the
 * on-screen rows ARE spectra, so the spectra RPC's per-spectrum filtering is
 * exactly the visible set. In objects mode the filters select OBJECTS on
 * aggregate semantics (e.g. max_snr = the object's best spectrum), so the same
 * parameters must NOT be re-run through the spectra RPC: there `max_snr_min`
 * drops each sibling spectrum below the cutoff individually, silently omitting
 * files of objects the table shows as matching (a PRISM whose G395M carried the
 * object past the cutoff simply vanished from the ZIP). Instead, derive the
 * object set with the objects RPC and download every published spectrum of its
 * member targets.
 */
export async function generateFitsDownloadUrl(
  filters: FilterOptions,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc',
  viewMode: ViewMode = 'objects'
): Promise<{
  files: DownloadFile[] | null;
  zipFilename: string | null;
  error: string | null;
}> {
  try {
    if (!JWT_SECRET) {
      return { files: null, zipFilename: null, error: 'Server configuration error: JWT secret not set' };
    }

    // Get user for tracking
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();

    let keys: string[];
    let downloadTargetIds: string[];

    if (viewMode === 'objects') {
      // Objects view: resolve the matching objects with the SAME RPC the table
      // uses, so the download set is exactly the objects on screen.
      const result = await getSpectra(
        filters,
        1, // page
        FITS_DOWNLOAD_FILE_LIMIT, // pageSize (objects ≤ files, so overflow is caught below)
        sortColumn,
        sortDirection,
        'objects'
      );

      if (result.error) {
        return { files: null, zipFilename: null, error: result.error };
      }

      if (result.total > result.spectra.length) {
        return {
          files: null, zipFilename: null,
          error: `This filter set matches ${result.total.toLocaleString()} objects, which exceeds the ${FITS_DOWNLOAD_FILE_LIMIT.toLocaleString()}-file ZIP limit. Refine your filters, or use the CSV export (which includes every fits_path) to fetch the full set.`,
        };
      }

      // Member targets are already scoped to the caller's accessible programs
      // by the objects RPC; re-reading spectra below runs under the caller's
      // RLS session, so nothing outside their access is ever presigned.
      const memberTargetIds = [...new Set(
        result.spectra.flatMap(obj => (obj.member_targets ?? []).map(m => m.target_id))
      )];

      // Chunked so the `.in()` list never overflows the PostgREST request URL.
      const rows: { fits_path: string; target_id: string }[] = [];
      const TARGET_CHUNK = 100;
      for (let i = 0; i < memberTargetIds.length; i += TARGET_CHUNK) {
        const { data, error: queryError } = await supabase
          .from('spectra')
          .select('fits_path, target_id')
          .eq('deploy_status', 'published')
          .in('target_id', memberTargetIds.slice(i, i + TARGET_CHUNK));
        if (queryError) {
          console.error('Error authorizing FITS download:', queryError);
          return { files: null, zipFilename: null, error: 'Failed to authorize download' };
        }
        rows.push(...(data ?? []));
      }

      // Deterministic archive order: group files by target.
      rows.sort((a, b) =>
        a.target_id.localeCompare(b.target_id) || a.fits_path.localeCompare(b.fits_path)
      );
      keys = [...new Set(rows.map(r => r.fits_path))];
      downloadTargetIds = [...new Set(rows.map(r => r.target_id))];

      // The UI gate counts objects, but objects fan out to several spectra, so
      // the file total can exceed the limit while the gate stays enabled.
      // Refuse rather than truncating into a biased first-N-of-M ZIP.
      if (keys.length > FITS_DOWNLOAD_FILE_LIMIT) {
        return {
          files: null, zipFilename: null,
          error: `This filter set matches ${keys.length.toLocaleString()} spectra, which exceeds the ${FITS_DOWNLOAD_FILE_LIMIT.toLocaleString()}-file ZIP limit. Refine your filters, or use the CSV export (which includes every fits_path) to fetch the full set.`,
        };
      }
    } else {
      // Spectra view: one on-screen row per (target, grating); the spectra RPC's
      // per-spectrum filtering matches the visible rows exactly, and
      // result.total is the count over spectra (the same unit this download
      // acts on).
      const result = await getSpectra(
        filters,
        1, // page
        FITS_DOWNLOAD_FILE_LIMIT, // pageSize
        sortColumn === 'object_id' ? 'target_id' : sortColumn,
        sortDirection,
        'spectra'
      );

      if (result.error) {
        return { files: null, zipFilename: null, error: result.error };
      }

      // Guard against silent truncation: result.total is the authoritative
      // spectra count from the same RPC; if it exceeds the page we pulled,
      // refuse with a clear, actionable error rather than handing back a
      // biased first-N-of-M ZIP that looks complete (a reproducibility hazard).
      if (result.total > result.spectra.length) {
        return {
          files: null, zipFilename: null,
          error: `This filter set has ${result.total.toLocaleString()} spectra, which exceeds the ${FITS_DOWNLOAD_FILE_LIMIT.toLocaleString()}-file ZIP limit. Refine your filters, or use the CSV export (which includes every fits_path) to fetch the full set.`,
        };
      }

      keys = result.spectra.flatMap(obj => obj.spectra.map(spec => spec.fits_path));
      downloadTargetIds = [...new Set(result.spectra.map(s => s.target_id))];
    }

    const filenames = keys.map(k => k.split('/').pop() || k);

    if (keys.length === 0) {
      return { files: null, zipFilename: null, error: 'No FITS files found for selected objects' };
    }

    // Presign each key against its home backend (dual-read), then HMAC-sign the
    // presigned URL so the proxy only fetches URLs we authorized.
    const urls = await generateDownloadUrls(keys, PRESIGN_TTL_SECONDS);
    const files: DownloadFile[] = await Promise.all(
      urls.map(async (signedUrl, i) => {
        const sig = await signUrlSignature(signedUrl, JWT_SECRET);
        const proxyUrl = `${WORKER_URL}/proxy?url=${encodeURIComponent(signedUrl)}&sig=${sig}`;
        return { proxyUrl, filename: filenames[i] };
      })
    );

    // Generate ZIP filename with date
    const now = new Date();
    const dateStr = now.toISOString().split('T')[0].replace(/-/g, ''); // YYYYMMDD
    const zipFilename = `campfire_download_${dateStr}.zip`;

    // Track ZIP download (fire-and-forget)
    if (user) {
      trackDownload({
        userId: user.id,
        downloadType: 'fits_zip',
        targetIds: downloadTargetIds,
        targetCount: downloadTargetIds.length,
        fileCount: files.length,
        filterSnapshot: filters as unknown as Record<string, unknown>,
      });
    }

    return { files, zipFilename, error: null };
  } catch (error) {
    console.error('Error generating FITS download URL:', error);
    return { files: null, zipFilename: null, error: 'Failed to generate download URL' };
  }
}

/**
 * Authorize a single object's spectra for bulk download and return ready-to-fetch
 * Worker proxy URLs plus a ZIP filename.
 *
 * Client-supplied paths are never trusted: the authorized set is re-derived
 * server-side by querying `spectra` under the caller's RLS session, so a path the
 * user can't see (private program, forged) is simply never presigned. Each
 * authorized key is presigned against its home backend (dual-read: R2 or OSN) and
 * HMAC-signed so the credential-free proxy Worker only fetches URLs we authorized.
 * The browser fetches each proxy URL (which supplies CORS) and zips the results
 * client-side — the same path the results-table bulk download uses.
 */
export async function generateObjectFitsDownloadUrls(
  fitsPaths: string[],
  targetId: string,
): Promise<{
  files: DownloadFile[] | null;
  zipFilename: string | null;
  error: string | null;
}> {
  try {
    if (!JWT_SECRET) {
      return { files: null, zipFilename: null, error: 'Server configuration error: JWT secret not set' };
    }

    if (!fitsPaths || fitsPaths.length === 0) {
      return { files: null, zipFilename: null, error: 'No FITS files provided' };
    }

    // Enforce the same cap the results-table ZIP path uses. A real object never
    // has this many spectra, but this action is a POST endpoint any authenticated
    // client can call directly, so bound the request server-side before the DB
    // query / presign / oversized in-browser ZIP.
    if (fitsPaths.length > FITS_DOWNLOAD_FILE_LIMIT) {
      return {
        files: null, zipFilename: null,
        error: `Too many files requested (${fitsPaths.length.toLocaleString()}); the ${FITS_DOWNLOAD_FILE_LIMIT}-file ZIP limit applies here too.`,
      };
    }

    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();
    if (!user) {
      return { files: null, zipFilename: null, error: 'Not authenticated' };
    }

    // Re-derive the authorized key set under the caller's RLS session. Never
    // presign a client-supplied path the DB won't return for this user. Pull
    // target_id too so download tracking records the real member targets (a
    // merged object spans several) rather than the display object id.
    const { data: rows, error: queryError } = await supabase
      .from('spectra')
      .select('fits_path, target_id')
      .in('fits_path', fitsPaths);

    if (queryError) {
      console.error('Error authorizing object FITS download:', queryError);
      return { files: null, zipFilename: null, error: 'Failed to authorize download' };
    }

    const keys = [...new Set((rows || []).map((r) => r.fits_path as string))];
    if (keys.length === 0) {
      return { files: null, zipFilename: null, error: 'No FITS files found or access denied' };
    }

    const filenames = keys.map((k) => k.split('/').pop() || k);

    // Presign each authorized key against its home backend (dual-read), then
    // HMAC-sign the presigned URL so the proxy only fetches URLs we authorized.
    const urls = await generateDownloadUrls(keys, PRESIGN_TTL_SECONDS);
    const files: DownloadFile[] = await Promise.all(
      urls.map(async (signedUrl, i) => {
        const sig = await signUrlSignature(signedUrl, JWT_SECRET);
        const proxyUrl = `${WORKER_URL}/proxy?url=${encodeURIComponent(signedUrl)}&sig=${sig}`;
        return { proxyUrl, filename: filenames[i] };
      })
    );

    const zipFilename = `${targetId}_spectra.zip`;

    // Track object-detail bulk download (fire-and-forget). Log the actual member
    // target ids of the downloaded spectra — a merged object fans out to several.
    const targetIds = [...new Set((rows || []).map((r) => r.target_id as string).filter(Boolean))];
    trackDownload({
      userId: user.id,
      downloadType: 'fits_object',
      targetIds: targetIds.length > 0 ? targetIds : [targetId],
      targetCount: targetIds.length || 1,
      fileCount: files.length,
    });

    return { files, zipFilename, error: null };
  } catch (error) {
    console.error('Error generating object FITS download URLs:', error);
    return { files: null, zipFilename: null, error: 'Failed to generate download URLs' };
  }
}

/**
 * Authorize NIRCam mosaic downloads and return ready-to-fetch Worker proxy URLs,
 * keyed by canonical storage key (file_path).
 *
 * Client-supplied keys are never trusted: the authorized set is re-derived
 * server-side by querying `nircam_images` under the caller's RLS session. Because
 * the table's RLS returns only published mosaics to non-admins (and all rows to
 * admins), the intersection of the requested paths with what the query returns is
 * exactly the set the caller may download — a draft/revoked or forged key simply
 * never gets presigned. Each authorized key is presigned against its home backend
 * (dual-read: OSN or R2) and HMAC-signed so the credential-free proxy Worker will
 * fetch only URLs we authorized. Requested keys that aren't authorized are absent
 * from the returned map.
 */
export async function generateNircamMosaicDownloadUrls(
  filePaths: string[]
): Promise<{ urls: Record<string, string>; error: string | null }> {
  try {
    if (!JWT_SECRET) {
      return { urls: {}, error: 'Server configuration error: JWT secret not set' };
    }

    if (filePaths.length === 0) {
      return { urls: {}, error: null };
    }

    // Re-derive the authorized key set server-side under the caller's RLS
    // session. Never presign a client-supplied path we can't see in the DB.
    const supabase = await createClient();
    const { data: rows, error: queryError } = await supabase
      .from('nircam_images')
      .select('file_path')
      .in('file_path', filePaths);

    if (queryError) {
      console.error('Error authorizing NIRCam mosaic download:', queryError);
      return { urls: {}, error: 'Failed to authorize download' };
    }

    const authorizedKeys = [...new Set((rows || []).map((r) => r.file_path as string))];
    if (authorizedKeys.length === 0) {
      return { urls: {}, error: null };
    }

    // Presign each authorized key against its home backend (dual-read) with an
    // attachment-filename override (these URLs are browser-NAVIGATED, so the
    // filename must ride Content-Disposition), then HMAC-sign the presigned URL
    // so the proxy only fetches URLs we authorized.
    const signed = await generateDownloadUrls(authorizedKeys, PRESIGN_TTL_SECONDS, {
      attachment: true,
    });
    const urls: Record<string, string> = {};
    await Promise.all(
      authorizedKeys.map(async (key, i) => {
        const sig = await signUrlSignature(signed[i], JWT_SECRET);
        urls[key] = `${WORKER_URL}/proxy?url=${encodeURIComponent(signed[i])}&sig=${sig}`;
      })
    );

    return { urls, error: null };
  } catch (error) {
    console.error('Error generating NIRCam mosaic download URLs:', error);
    return { urls: {}, error: 'Failed to generate download URLs' };
  }
}

/**
 * Authorize NIRCam exposure-map downloads and return ready-to-fetch Worker proxy
 * URLs, keyed by canonical storage key.
 *
 * Same trust model as `generateNircamMosaicDownloadUrls`: client-supplied keys are
 * never trusted. The authorized set is re-derived server-side from `storage_objects`
 * (product_type `nircam_expmap`, active) under the caller's RLS session, so
 * `select_storage_objects_by_access` returns only published (or, for admins, all)
 * rows — a draft/revoked or forged key is simply never presigned. Each authorized
 * key is presigned against its home backend (dual-read: OSN or R2) and HMAC-signed
 * so the credential-free proxy Worker only fetches URLs we authorized.
 */
export async function generateNircamExpmapDownloadUrls(
  storageKeys: string[]
): Promise<{ urls: Record<string, string>; error: string | null }> {
  try {
    if (!JWT_SECRET) {
      return { urls: {}, error: 'Server configuration error: JWT secret not set' };
    }
    if (storageKeys.length === 0) {
      return { urls: {}, error: null };
    }

    const supabase = await createClient();
    const { data: rows, error: queryError } = await supabase
      .from('storage_objects')
      .select('storage_key')
      .eq('product_type', 'nircam_expmap')
      .eq('status', 'active')
      .in('storage_key', storageKeys);

    if (queryError) {
      console.error('Error authorizing NIRCam expmap download:', queryError);
      return { urls: {}, error: 'Failed to authorize download' };
    }

    const authorizedKeys = [...new Set((rows || []).map((r) => r.storage_key as string))];
    if (authorizedKeys.length === 0) {
      return { urls: {}, error: null };
    }

    // Attachment presign: browser-navigated URLs, filename via Content-Disposition.
    const signed = await generateDownloadUrls(authorizedKeys, PRESIGN_TTL_SECONDS, {
      attachment: true,
    });
    const urls: Record<string, string> = {};
    await Promise.all(
      authorizedKeys.map(async (key, i) => {
        const sig = await signUrlSignature(signed[i], JWT_SECRET);
        urls[key] = `${WORKER_URL}/proxy?url=${encodeURIComponent(signed[i])}&sig=${sig}`;
      })
    );

    return { urls, error: null };
  } catch (error) {
    console.error('Error generating NIRCam expmap download URLs:', error);
    return { urls: {}, error: 'Failed to generate download URLs' };
  }
}

/**
 * HMAC-SHA256(secret, url), base64url-encoded — the per-URL signature the proxy
 * Worker verifies. Web Crypto API (same primitive both ends).
 */
async function signUrlSignature(url: string, secret: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(url));
  return base64UrlEncode(signature);
}

/**
 * Base64URL-encode the HMAC signature (ArrayBuffer).
 */
function base64UrlEncode(data: ArrayBuffer): string {
  return Buffer.from(data).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}
