/**
 * Shared utilities for URL parameter handling across the spectra catalog.
 * Used to preserve filter, sort, and pagination state in URLs.
 */

import type { AdvancedFilterOptions, SearchScope, FilterMode } from '@/components/spectra/SpectraFilterBar';
import type { SortColumn, SortDirection, ViewMode } from '@/lib/actions/spectra-types';
import { VALID_SORT_COLUMNS, isValidSortColumn, defaultSortColumn } from '@/lib/actions/spectra-types';
import { isFilterableLine } from '@/lib/linelist';
import { encodeConditions, parseConditions } from '@/lib/catalog-query/tree';
import { filterNodes, legacyNodes, rootOf } from '@/lib/catalog-query/legacy';
import { measurementKeys, rerootConditions } from '@/lib/catalog-query/vocabulary';
import type { TreeRoot } from '@/lib/catalog-query/tree';
import { DEFAULT_FILTERS } from '@/lib/actions/filter-params';

// Valid search scope values
const VALID_SEARCH_SCOPES: SearchScope[] = ['target_id', 'my_comments', 'all_comments'];

// Valid filter mode values
const VALID_FILTER_MODES: FilterMode[] = ['any', 'all', 'none'];

// Default page size for pagination
export const DEFAULT_PAGE_SIZE = 50;

/**
 * Parse filter options from URL search parameters. The tree is the state
 * (phase 3b): `q` carries it, read against the view's root, and the legacy
 * parameters of an older link (`programs=`, `z_min=`, `line=`, …) become its
 * leading nodes. Only the search box lives outside the tree.
 */
export function parseFiltersFromURL(searchParams: URLSearchParams, root?: TreeRoot): AdvancedFilterOptions {
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
    : 'target_id';

  const filters: AdvancedFilterOptions = {
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
    // only a filterable catalog name (a doublet total or a stand-alone line) is
    // accepted from a URL: the name is echoed into RPC params and CSV headers
    line: (() => { const v = searchParams.get('line')?.trim() || ''; return v && isFilterableLine(v) ? v : null; })(),
    line_snr_min: parseNumber('line_snr_min'),
    line_snr_max: parseNumber('line_snr_max'),
    line_include_stale: parseBoolean('line_stale') === true,
    // a band name is per-field catalog config, not a fixed catalog, so there
    // is nothing to validate it against — an unknown name simply matches no
    // photometry row (see buildFilterParams).
    band: searchParams.get('band')?.trim() || null,
    band_mag_min: parseNumber('band_mag_min'),
    band_mag_max: parseNumber('band_mag_max'),
    band_snr_min: parseNumber('band_snr_min'),
    band_snr_max: parseNumber('band_snr_max'),
    list_ids: parseNumberArray('tags'),
    dq_flags: parseNumberArray('dq_flags'),
    inspected_only: parseBoolean('inspected'),
    needs_review: parseBoolean('needs_review'),
    has_photometry: parseBoolean('has_photometry'),
    search: searchParams.get('search') || '',
    search_scope: searchScope,
    // Filter modes (default to 'any')
    gratings_mode: parseMode('gratings_mode'),
    dq_flags_mode: parseMode('dq_flags_mode'),
    list_ids_mode: parseMode('tags_mode'),
    conditions: [],
    show_line: null,
    show_band: null,
  };
  // an unreadable q is dropped whole rather than half-applied
  const urlRoot = rootOf(parseViewModeFromURL(searchParams));
  const conditions = parseConditions(searchParams.get('q'), urlRoot) ?? [];
  // a caller that queries another root than the link's view (the object
  // page's prev / next, the inspection queue) reads the tree against its own
  const parsed = [...legacyNodes(filters, urlRoot), ...conditions];
  const all = root && root !== urlRoot ? rerootConditions(parsed, urlRoot, root) : parsed;
  // the columns follow the rows: the first line and the first band filtered on
  return {
    ...DEFAULT_FILTERS,
    search: filters.search,
    search_scope: filters.search_scope,
    conditions: all,
    show_line: measurementKeys(all, 'line')[0] ?? null,
    show_band: measurementKeys(all, 'band')[0] ?? null,
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
 * Parse view mode from URL search parameters.
 * Legacy `view=targets` collapses to `objects` so old bookmarks keep working.
 */
export function parseViewModeFromURL(searchParams: URLSearchParams): ViewMode {
  const view = searchParams.get('view');
  if (view === 'spectra') return 'spectra';
  return 'objects';
}

/**
 * Parse sorting parameters from URL search parameters.
 * Validates the sort column against the current view mode.
 */
export function parseSortingFromURL(searchParams: URLSearchParams, viewMode: ViewMode = 'objects'): { sortColumn: SortColumn; sortDirection: SortDirection } {
  const fallback = defaultSortColumn(viewMode);
  const sort = searchParams.get('sort') || fallback;
  const dir = searchParams.get('dir') || 'asc';
  const isValid = VALID_SORT_COLUMNS.includes(sort as SortColumn) && isValidSortColumn(sort, viewMode);
  return {
    sortColumn: isValid ? (sort as SortColumn) : fallback,
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
  sortDirection: SortDirection = 'asc',
  viewMode: ViewMode = 'objects'
): URLSearchParams {
  const params = new URLSearchParams();

  // the whole filter state is the tree (any pill a caller still carries in
  // its legacy slot is folded in)
  const q = encodeConditions(filterNodes(filters, rootOf(viewMode)));
  if (q) {
    params.set('q', q);
  }
  if (filters.search) {
    params.set('search', filters.search);
  }
  // Only include search_scope if not default
  if (filters.search_scope && filters.search_scope !== 'target_id') {
    params.set('scope', filters.search_scope);
  }
  // Only include pagination params if not default values
  if (page > 1) {
    params.set('page', page.toString());
  }
  if (pageSize !== DEFAULT_PAGE_SIZE) {
    params.set('pageSize', pageSize.toString());
  }
  // Only include view mode when non-default (objects)
  if (viewMode !== 'objects') {
    params.set('view', viewMode);
  }
  // Only include sorting params when non-default
  const defaultSort = defaultSortColumn(viewMode);
  if (sortColumn !== defaultSort) {
    params.set('sort', sortColumn);
  }
  if (sortDirection !== 'asc') {
    params.set('dir', sortDirection);
  }

  return params;
}
