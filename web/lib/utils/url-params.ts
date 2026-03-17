/**
 * Shared utilities for URL parameter handling across the spectra catalog.
 * Used to preserve filter, sort, and pagination state in URLs.
 */

import type { AdvancedFilterOptions, SearchScope, FilterMode } from '@/components/spectra/SpectraFilterBar';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import { VALID_SORT_COLUMNS } from '@/lib/actions/spectra-types';

// Valid search scope values
const VALID_SEARCH_SCOPES: SearchScope[] = ['object_id', 'my_comments', 'all_comments'];

// Valid filter mode values
const VALID_FILTER_MODES: FilterMode[] = ['any', 'all', 'none'];

// Default page size for pagination
export const DEFAULT_PAGE_SIZE = 50;

/**
 * Parse filter options from URL search parameters
 */
export function parseFiltersFromURL(searchParams: URLSearchParams): AdvancedFilterOptions {
  const parseArray = (key: string): string[] => {
    const value = searchParams.get(key);
    return value ? value.split(',').filter(Boolean) : [];
  };

  const parseNumberArray = (key: string): number[] => {
    const value = searchParams.get(key);
    return value ? value.split(',').filter(Boolean).map(Number) : [];
  };

  const parseNumber = (key: string): number | null => {
    const value = searchParams.get(key);
    return value ? parseFloat(value) : null;
  };

  const parseBoolean = (key: string): boolean | null => {
    const value = searchParams.get(key);
    if (value === 'true') return true;
    if (value === 'false') return false;
    return null;
  };

  const parseMode = (key: string): FilterMode => {
    const value = searchParams.get(key);
    if (VALID_FILTER_MODES.includes(value as FilterMode)) {
      return value as FilterMode;
    }
    return 'any';
  };

  // Parse coordinate search parameters
  const coordRa = parseNumber('coord_ra');
  const coordDec = parseNumber('coord_dec');
  const coordRadius = parseNumber('coord_radius');
  const coordUnit = searchParams.get('coord_unit') as 'degrees' | 'arcmin' | 'arcsec' | null;

  const coordinateSearch = (coordRa !== null && coordDec !== null && coordRadius !== null && coordUnit !== null)
    ? { ra: coordRa, dec: coordDec, radius: coordRadius, radius_unit: coordUnit }
    : null;

  // Parse search scope (default to 'object_id')
  const scopeParam = searchParams.get('scope');
  const searchScope: SearchScope = VALID_SEARCH_SCOPES.includes(scopeParam as SearchScope)
    ? (scopeParam as SearchScope)
    : 'object_id';

  return {
    programs: parseArray('programs'),
    fields: parseArray('fields'),
    gratings: parseArray('gratings'),
    observations: parseArray('observations'),
    redshift_quality: parseNumberArray('quality'),
    coordinate_search: coordinateSearch,
    redshift_min: parseNumber('z_min'),
    redshift_max: parseNumber('z_max'),
    max_snr_min: parseNumber('snr_min'),
    max_snr_max: parseNumber('snr_max'),
    max_exposure_time_min: parseNumber('exp_min'),
    max_exposure_time_max: parseNumber('exp_max'),
    spectral_features: parseNumberArray('features'),
    object_flags: parseNumberArray('obj_flags'),
    dq_flags: parseNumberArray('dq_flags'),
    inspected_only: parseBoolean('inspected'),
    search: searchParams.get('search') || '',
    search_scope: searchScope,
    // Filter modes (default to 'any')
    gratings_mode: parseMode('gratings_mode'),
    spectral_features_mode: parseMode('features_mode'),
    object_flags_mode: parseMode('obj_flags_mode'),
    dq_flags_mode: parseMode('dq_flags_mode'),
  };
}

/**
 * Parse pagination parameters from URL search parameters
 */
export function parsePaginationFromURL(searchParams: URLSearchParams): { page: number; pageSize: number } {
  const page = parseInt(searchParams.get('page') || '1', 10);
  const pageSize = parseInt(searchParams.get('pageSize') || String(DEFAULT_PAGE_SIZE), 10);
  return {
    page: isNaN(page) || page < 1 ? 1 : page,
    pageSize: isNaN(pageSize) || pageSize < 1 ? DEFAULT_PAGE_SIZE : pageSize,
  };
}

/**
 * Parse sorting parameters from URL search parameters
 */
export function parseSortingFromURL(searchParams: URLSearchParams): { sortColumn: SortColumn; sortDirection: SortDirection } {
  const sort = searchParams.get('sort') || 'object_id';
  const dir = searchParams.get('dir') || 'asc';
  return {
    sortColumn: VALID_SORT_COLUMNS.includes(sort as SortColumn) ? (sort as SortColumn) : 'object_id',
    sortDirection: dir === 'desc' ? 'desc' : 'asc',
  };
}

/**
 * Convert filter state, pagination, and sorting to URL search parameters
 */
export function filtersToURLParams(
  filters: AdvancedFilterOptions,
  page: number = 1,
  pageSize: number = DEFAULT_PAGE_SIZE,
  sortColumn: SortColumn = 'object_id',
  sortDirection: SortDirection = 'asc'
): URLSearchParams {
  const params = new URLSearchParams();

  if (filters.programs.length > 0) {
    params.set('programs', filters.programs.join(','));
  }
  if (filters.fields.length > 0) {
    params.set('fields', filters.fields.join(','));
  }
  if (filters.gratings.length > 0) {
    params.set('gratings', filters.gratings.join(','));
  }
  if (filters.observations.length > 0) {
    params.set('observations', filters.observations.join(','));
  }
  if (filters.redshift_quality.length > 0) {
    params.set('quality', filters.redshift_quality.join(','));
  }
  if (filters.coordinate_search !== null) {
    params.set('coord_ra', filters.coordinate_search.ra.toString());
    params.set('coord_dec', filters.coordinate_search.dec.toString());
    params.set('coord_radius', filters.coordinate_search.radius.toString());
    params.set('coord_unit', filters.coordinate_search.radius_unit);
  }
  if (filters.redshift_min !== null) {
    params.set('z_min', filters.redshift_min.toString());
  }
  if (filters.redshift_max !== null) {
    params.set('z_max', filters.redshift_max.toString());
  }
  if (filters.max_snr_min !== null) {
    params.set('snr_min', filters.max_snr_min.toString());
  }
  if (filters.max_snr_max !== null) {
    params.set('snr_max', filters.max_snr_max.toString());
  }
  if (filters.max_exposure_time_min !== null) {
    params.set('exp_min', filters.max_exposure_time_min.toString());
  }
  if (filters.max_exposure_time_max !== null) {
    params.set('exp_max', filters.max_exposure_time_max.toString());
  }
  if (filters.spectral_features.length > 0) {
    params.set('features', filters.spectral_features.join(','));
  }
  if (filters.object_flags.length > 0) {
    params.set('obj_flags', filters.object_flags.join(','));
  }
  if (filters.dq_flags.length > 0) {
    params.set('dq_flags', filters.dq_flags.join(','));
  }
  if (filters.inspected_only !== null) {
    params.set('inspected', filters.inspected_only.toString());
  }
  if (filters.search) {
    params.set('search', filters.search);
  }
  // Only include search_scope if not default
  if (filters.search_scope && filters.search_scope !== 'object_id') {
    params.set('scope', filters.search_scope);
  }
  // Only include filter modes if not default ('any') and filter is active
  if (filters.gratings.length > 0 && filters.gratings_mode && filters.gratings_mode !== 'any') {
    params.set('gratings_mode', filters.gratings_mode);
  }
  if (filters.spectral_features.length > 0 && filters.spectral_features_mode && filters.spectral_features_mode !== 'any') {
    params.set('features_mode', filters.spectral_features_mode);
  }
  if (filters.object_flags.length > 0 && filters.object_flags_mode && filters.object_flags_mode !== 'any') {
    params.set('obj_flags_mode', filters.object_flags_mode);
  }
  if (filters.dq_flags.length > 0 && filters.dq_flags_mode && filters.dq_flags_mode !== 'any') {
    params.set('dq_flags_mode', filters.dq_flags_mode);
  }
  // Only include pagination params if not default values
  if (page > 1) {
    params.set('page', page.toString());
  }
  if (pageSize !== DEFAULT_PAGE_SIZE) {
    params.set('pageSize', pageSize.toString());
  }
  // Only include sorting params when non-default
  if (sortColumn !== 'object_id') {
    params.set('sort', sortColumn);
  }
  if (sortDirection !== 'asc') {
    params.set('dir', sortDirection);
  }

  return params;
}
