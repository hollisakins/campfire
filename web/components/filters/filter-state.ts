/**
 * The filter state after a card edit: the tree tidied, and the columns
 * derived from it (every line and band filtered on that every selected row
 * is sure to have is a column of the table, #660).
 */
import type { FilterOptions } from '@/lib/actions/filter-params';
import type { TreeNode, TreeRoot } from '@/lib/catalog-query/tree';
import { columnKeys, tidyConditions } from '@/lib/catalog-query/vocabulary';

export function withColumns<T extends Pick<FilterOptions, 'conditions'>>(filters: T): T & Pick<FilterOptions, 'show_lines' | 'show_bands'> {
  return {
    ...filters,
    show_lines: columnKeys(filters.conditions, 'line'),
    show_bands: columnKeys(filters.conditions, 'band'),
  };
}

export function applyConditions(filters: FilterOptions, next: TreeNode[], root: TreeRoot): FilterOptions {
  return withColumns({ ...filters, conditions: tidyConditions(next, root === 'spectra' ? 'spectra' : 'objects') });
}
