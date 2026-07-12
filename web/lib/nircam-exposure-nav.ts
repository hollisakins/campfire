// The URL contract between the admin NIRCam list page and the exposure detail
// page. The list's row links carry the active filter+sort state as query
// params; the detail page parses them back and asks
// get_admin_exposure_neighbors for prev/next within that same filtered,
// ordered set. Because the context lives in the URL (not sessionStorage),
// nav survives refresh, direct entry, and shared links.

import type { ExposureFilters, ExposureSort } from '@/lib/actions/nircam-exposures';
import { EXPOSURE_SORT_KEYS } from '@/lib/admin/sort-keys';

export const EXPOSURE_FILTER_PARAM_KEYS = [
  'field', 'filter', 'detector', 'review', 'stage', 'correction',
] as const;

export type ExposureFilterParams = Record<
  (typeof EXPOSURE_FILTER_PARAM_KEYS)[number],
  string
>;

/** URL params (list page state) → the action/RPC filter+sort shape. */
export function parseExposureNavParams(
  sp: URLSearchParams,
): ExposureFilters & ExposureSort {
  const sort = sp.get('sort');
  const dir = sp.get('dir');
  return {
    field: sp.get('field') || undefined,
    filter: sp.get('filter') || undefined,
    detector: sp.get('detector') || undefined,
    reviewStatus: sp.get('review') || undefined,
    stage: sp.get('stage') || undefined,
    correction: sp.get('correction') || undefined,
    sortColumn:
      sort && (EXPOSURE_SORT_KEYS as readonly string[]).includes(sort) ? sort : undefined,
    sortDirection: dir === 'asc' || dir === 'desc' ? dir : undefined,
  };
}

/** List page state → the query string appended to detail-page row links. */
export function buildExposureNavQuery(
  filters: Partial<ExposureFilterParams>,
  sort?: { column: string; direction: 'asc' | 'desc' },
  defaultSort?: { column: string; direction: 'asc' | 'desc' },
): string {
  const sp = new URLSearchParams();
  for (const key of EXPOSURE_FILTER_PARAM_KEYS) {
    const v = filters[key];
    if (v) sp.set(key, v);
  }
  if (
    sort &&
    defaultSort &&
    (sort.column !== defaultSort.column || sort.direction !== defaultSort.direction)
  ) {
    sp.set('sort', sort.column);
    sp.set('dir', sort.direction);
  }
  return sp.toString();
}
