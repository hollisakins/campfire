// Types and constants for spectra actions
// Separated from spectra.ts to avoid "use server" export restrictions

export type SortDirection = 'asc' | 'desc';

// Valid sort columns (must match RPC function whitelist)
export const VALID_SORT_COLUMNS = ['object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr'] as const;
export type SortColumn = typeof VALID_SORT_COLUMNS[number];
