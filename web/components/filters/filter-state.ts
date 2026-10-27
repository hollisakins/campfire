/**
 * The filter state after a card edit: the tree tidied, and the column
 * toggles kept honest (a line or band column names a row the tree still has;
 * the first row of a kind turns its column on).
 */
import type { FilterOptions } from '@/lib/actions/filter-params';
import type { TreeNode, TreeRoot } from '@/lib/catalog-query/tree';
import { measurementKeys, tidyConditions } from '@/lib/catalog-query/vocabulary';

export function applyConditions(filters: FilterOptions, next: TreeNode[], root: TreeRoot): FilterOptions {
  const conditions = tidyConditions(next, root === 'spectra' ? 'spectra' : 'objects');
  const lines = measurementKeys(conditions, 'line');
  const bands = measurementKeys(conditions, 'band');
  const before = { lines: measurementKeys(filters.conditions, 'line'), bands: measurementKeys(filters.conditions, 'band') };
  let show_line = filters.show_line && lines.includes(filters.show_line) ? filters.show_line : null;
  let show_band = filters.show_band && bands.includes(filters.show_band) ? filters.show_band : null;
  if (!show_line) show_line = lines.find((k) => !before.lines.includes(k)) ?? null;
  if (!show_band) show_band = bands.find((k) => !before.bands.includes(k)) ?? null;
  return { ...filters, conditions, show_line, show_band };
}
