/**
 * The filter state after a card edit: the tree tidied, and the columns
 * derived from it (every line or band filtered on is a column of the table;
 * until the list RPCs take several, #660, the first of each kind).
 */
import type { FilterOptions } from '@/lib/actions/filter-params';
import type { TreeNode, TreeRoot } from '@/lib/catalog-query/tree';
import { measurementKeys, tidyConditions } from '@/lib/catalog-query/vocabulary';

export function withColumns<T extends Pick<FilterOptions, 'conditions'>>(filters: T): T & Pick<FilterOptions, 'show_line' | 'show_band'> {
  return {
    ...filters,
    show_line: measurementKeys(filters.conditions, 'line')[0] ?? null,
    show_band: measurementKeys(filters.conditions, 'band')[0] ?? null,
  };
}

export function applyConditions(filters: FilterOptions, next: TreeNode[], root: TreeRoot): FilterOptions {
  return withColumns({ ...filters, conditions: tidyConditions(next, root === 'spectra' ? 'spectra' : 'objects') });
}
