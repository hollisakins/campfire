// Types and constants for spectra actions
// Separated from spectra.ts to avoid "use server" export restrictions

export type SortDirection = 'asc' | 'desc';

// Phase D: targets list view is deprecated and the table won't render it any
// more, but the literal stays in the type union so URL parsing / download /
// adjacent-id helpers still compile while the surrounding call sites are
// migrated to objects mode. Treat 'targets' here as an alias for 'objects'.
export type ViewMode = 'spectra' | 'objects' | 'targets';

// Spectra-mode sort columns (must match get_filtered_spectra_paginated whitelist)
export const SPECTRA_SORT_COLUMNS = ['target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating', 'distance'] as const;

// Objects-mode sort columns (must match get_filtered_objects_paginated whitelist).
// Phase D: best_redshift / best_redshift_quality renamed to redshift / redshift_quality.
export const OBJECTS_SORT_COLUMNS = ['object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality', 'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z', 'distance'] as const;

// Union of all valid sort columns (all modes)
export const VALID_SORT_COLUMNS = [
  'target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality',
  'max_snr', 'max_exposure_time', 'signal_to_noise', 'exposure_time', 'grating', 'distance',
  'object_id', 'n_targets', 'n_spectra', 'photo_z'
] as const;
export type SortColumn = typeof VALID_SORT_COLUMNS[number];

/** Default sort column for a given view mode */
export function defaultSortColumn(viewMode: ViewMode): SortColumn {
  return viewMode === 'objects' ? 'object_id' : 'target_id';
}

/** Check if a sort column is valid for the given view mode */
export function isValidSortColumn(column: string, viewMode: ViewMode): boolean {
  // Phase D: 'targets' ViewMode collapses onto the objects column whitelist.
  const valid = viewMode === 'spectra' ? SPECTRA_SORT_COLUMNS : OBJECTS_SORT_COLUMNS;
  return (valid as readonly string[]).includes(column);
}
