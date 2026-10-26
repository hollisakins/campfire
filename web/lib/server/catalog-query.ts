import 'server-only';
import type { SupabaseClient } from '@supabase/supabase-js';
import type { FilterOptions } from '@/lib/actions/filter-params';
import { filterNodes } from '@/lib/catalog-query/legacy';
import { treeOf, type TreeRoot } from '@/lib/catalog-query/tree';

/**
 * The tree path of the portal's filter state (Catalog Read Model phase 3b).
 *
 * The list pages hand the tree to `catalog_query_objects` / `_spectra`
 * (one round trip, the wrapper feeds the legacy list RPC); every other
 * consumer of the filter state (the map, the inspection queue, the CSV
 * exports, the prev/next navigation) evaluates it once here and passes the
 * ids to its legacy RPC's `p_object_ids` / `p_spectrum_ids`, ANDed with the
 * legacy parameters it already sends. The pills are in both, which is
 * idempotent; the free conditions are in the tree only.
 */

/** The RPC-side tree for a filter state, or null when nothing selects. */
export function filterTree(filters: Partial<FilterOptions> | undefined, root: TreeRoot) {
  return treeOf(filterNodes(filters, root));
}

/** A tree the database refused (SQLSTATE 22023) as the message to show, else null. */
export function treeRejection(error: { code?: string; message?: string } | null | undefined): string | null {
  return error && error.code === '22023' && error.message ? error.message.replace(/^catalog_query: /, '') : null;
}

/**
 * The ids `root` rows the filter state selects, evaluated under the caller's
 * session (RLS) with the explicit scope the RPC also takes.
 */
export async function matchFilterIds(
  supabase: SupabaseClient,
  root: TreeRoot,
  filters: Partial<FilterOptions> | undefined,
  programSlugs: string[],
  viewerId: string | null,
): Promise<{ ids: number[]; error?: string }> {
  const tree = filterTree(filters, root);
  if (!tree) return { ids: [] };
  const { data, error } = await supabase.rpc('catalog_match_ids', {
    p_root: root,
    p_tree: tree,
    p_program_slugs: programSlugs,
    p_include_unpublished: false,
    p_viewer_id: viewerId,
  });
  if (error) {
    return { ids: [], error: treeRejection(error) ?? error.message };
  }
  return { ids: Array.isArray(data) ? (data as number[]) : [] };
}
