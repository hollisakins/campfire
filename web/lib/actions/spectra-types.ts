// Types and constants for spectra actions
// Separated from spectra.ts to avoid "use server" export restrictions

export type SortDirection = 'asc' | 'desc';

export type ViewMode = 'spectra' | 'objects';

/**
 * Maximum number of FITS files (spectra) bundled into a single in-browser ZIP.
 *
 * Expressed in spectra/file units — the unit the download actually acts on — NOT
 * objects. One sky-object commonly spans 2-3 gratings (e.g. the PRISM + G395M
 * EMBER/ZENITH pairing), so an objects-mode result of N objects can fan out to
 * far more than N spectra. The server action (generateFitsDownloadUrl) enforces
 * this cap and refuses — rather than silently truncating — when a filter set
 * exceeds it, so a user never walks away with a biased first-N-of-M sample. The
 * client gate imports the same constant so the button and the action can't
 * disagree on the limit.
 */
export const FITS_DOWNLOAD_FILE_LIMIT = 500;

/**
 * Maximum number of rows in a catalog CSV export (objects or spectra,
 * whichever unit the view mode exports in). Enforced server-side by
 * generateCSV — its keyset pagination loop stops at this cap — and surfaced
 * as a truncation warning in the download dropdown. Shared constant so the
 * warning and the actual behavior can't disagree.
 */
export const CSV_EXPORT_ROW_LIMIT = 50000;

// Spectra-mode sort columns (must match get_filtered_spectra_paginated whitelist)
export const SPECTRA_SORT_COLUMNS = ['spectrum_id', 'target_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift', 'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating', 'distance'] as const;

// Objects-mode sort columns (must match get_filtered_objects_paginated whitelist).
export const OBJECTS_SORT_COLUMNS = ['object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality', 'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z', 'distance'] as const;

// Union of all valid sort columns (all modes)
export const VALID_SORT_COLUMNS = [
  'spectrum_id', 'target_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift', 'redshift_quality',
  'redshift_auto',
  'max_snr', 'max_exposure_time', 'signal_to_noise', 'exposure_time', 'grating', 'distance',
  'object_id', 'n_targets', 'n_spectra', 'photo_z'
] as const;
export type SortColumn = typeof VALID_SORT_COLUMNS[number];

/** Default sort column for a given view mode */
export function defaultSortColumn(viewMode: ViewMode): SortColumn {
  return viewMode === 'objects' ? 'object_id' : 'spectrum_id';
}

/** Check if a sort column is valid for the given view mode */
export function isValidSortColumn(column: string, viewMode: ViewMode): boolean {
  const valid = viewMode === 'spectra' ? SPECTRA_SORT_COLUMNS : OBJECTS_SORT_COLUMNS;
  return (valid as readonly string[]).includes(column);
}
