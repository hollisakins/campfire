// Types and constants for spectra actions
// Separated from spectra.ts to avoid "use server" export restrictions

export type SortDirection = 'asc' | 'desc';

// View mode for the spectra list page
export type ViewMode = 'targets' | 'spectra' | 'objects';

// Target-mode sort columns (must match get_filtered_target_ids whitelist)
export const TARGET_SORT_COLUMNS = ['target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time', 'distance'] as const;

// Spectra-mode sort columns (must match get_filtered_spectra_paginated whitelist)
export const SPECTRA_SORT_COLUMNS = ['target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating', 'distance'] as const;

// Objects-mode sort columns (must match get_filtered_objects_paginated whitelist)
export const OBJECTS_SORT_COLUMNS = ['object_id', 'field', 'ra', 'dec', 'best_redshift', 'best_redshift_quality', 'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'distance'] as const;

// Union of all valid sort columns (all modes)
export const VALID_SORT_COLUMNS = [
  'target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality',
  'max_snr', 'max_exposure_time', 'signal_to_noise', 'exposure_time', 'grating', 'distance',
  'object_id', 'best_redshift', 'best_redshift_quality', 'n_targets', 'n_spectra'
] as const;
export type SortColumn = typeof VALID_SORT_COLUMNS[number];

/** Default sort column for a given view mode */
export function defaultSortColumn(viewMode: ViewMode): SortColumn {
  return viewMode === 'objects' ? 'object_id' : 'target_id';
}

/** Check if a sort column is valid for the given view mode */
export function isValidSortColumn(column: string, viewMode: ViewMode): boolean {
  const valid = viewMode === 'spectra'
    ? SPECTRA_SORT_COLUMNS
    : viewMode === 'objects'
      ? OBJECTS_SORT_COLUMNS
      : TARGET_SORT_COLUMNS;
  return (valid as readonly string[]).includes(column);
}
