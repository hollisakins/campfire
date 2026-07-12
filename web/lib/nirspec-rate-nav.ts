// The URL contract between the admin NIRSpec rate list page and the rate detail
// page. Mirrors nircam-exposure-nav.ts: the list's row links carry the active
// filter+sort state as query params; the detail page parses them back and derives
// prev/next within that same filtered, ordered set. Context lives in the URL, so
// nav survives refresh, direct entry, and shared links.

import type { RateFilters, RateSort } from '@/lib/actions/nirspec-rate';
import { NIRSPEC_RATE_SORT_KEYS } from '@/lib/admin/sort-keys';

export const RATE_FILTER_PARAM_KEYS = [
  'observation', 'detector', 'review', 'grating',
] as const;

export type RateFilterParams = Record<
  (typeof RATE_FILTER_PARAM_KEYS)[number],
  string
>;

/** URL params (list page state) → the action filter+sort shape. */
export function parseRateNavParams(
  sp: URLSearchParams,
): RateFilters & RateSort {
  const sort = sp.get('sort');
  const dir = sp.get('dir');
  return {
    observation: sp.get('observation') || undefined,
    detector: sp.get('detector') || undefined,
    reviewStatus: sp.get('review') || undefined,
    grating: sp.get('grating') || undefined,
    sortColumn:
      sort && (NIRSPEC_RATE_SORT_KEYS as readonly string[]).includes(sort) ? sort : undefined,
    sortDirection: dir === 'asc' || dir === 'desc' ? dir : undefined,
  };
}

/** List page state → the query string appended to detail-page row links. */
export function buildRateNavQuery(
  filters: Partial<RateFilterParams>,
  sort?: { column: string; direction: 'asc' | 'desc' },
  defaultSort?: { column: string; direction: 'asc' | 'desc' },
): string {
  const sp = new URLSearchParams();
  for (const key of RATE_FILTER_PARAM_KEYS) {
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
