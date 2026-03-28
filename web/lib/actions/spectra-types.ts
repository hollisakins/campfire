// Types and constants for spectra actions
// Separated from spectra.ts to avoid "use server" export restrictions

export type SortDirection = 'asc' | 'desc';

// View mode for the spectra list page
export type ViewMode = 'targets' | 'spectra';

// Target-mode sort columns (must match get_filtered_target_ids whitelist)
export const TARGET_SORT_COLUMNS = ['target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time', 'distance'] as const;

// Spectra-mode sort columns (must match get_filtered_spectra_paginated whitelist)
export const SPECTRA_SORT_COLUMNS = ['target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating', 'distance'] as const;

// Union of all valid sort columns (both modes)
export const VALID_SORT_COLUMNS = [
  'target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality',
  'max_snr', 'max_exposure_time', 'signal_to_noise', 'exposure_time', 'grating', 'distance'
] as const;
export type SortColumn = typeof VALID_SORT_COLUMNS[number];

/** Check if a sort column is valid for the given view mode */
export function isValidSortColumn(column: string, viewMode: ViewMode): boolean {
  const valid = viewMode === 'spectra' ? SPECTRA_SORT_COLUMNS : TARGET_SORT_COLUMNS;
  return (valid as readonly string[]).includes(column);
}
