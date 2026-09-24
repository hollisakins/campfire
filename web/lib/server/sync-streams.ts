import 'server-only';

import type { PostgrestError, SupabaseClient } from '@supabase/supabase-js';

/**
 * One page of a `/api/v1/sync/*` catalog stream, as its RPC returns it.
 *
 * Shared by the five sync routes and the nightly snapshot builder
 * (`/api/cron/sync-snapshot`), so a snapshot row is byte-for-byte the row a
 * live walk returns: the RPC name, its argument mapping and the field the
 * rows come back in live here and nowhere else.
 */
export type SyncStreamName = 'objects' | 'spectra' | 'storage' | 'photometry' | 'lines';

export const SYNC_STREAMS: readonly SyncStreamName[] = [
  'objects',
  'spectra',
  'storage',
  'photometry',
  'lines',
];

export interface SyncPageRequest {
  programSlugs: string[];
  /** objects / spectra only: scopes the objects' `lists` field to this user. */
  userId: string | null;
  updatedSince: string | null;
  limit: number;
  includeCounts: boolean;
  includeUnpublished: boolean;
  /** Keyset cursor: the previous page's last `cursorKey` value. */
  after: string | number | null;
  /** storage only: product-kind / observation / field scope. */
  productTypes?: string[] | null;
  observations?: string[] | null;
  fields?: string[] | null;
  /** objects only: the snapshot extras walk (see get_objects_for_sync). */
  filterProgramSlugs?: string[] | null;
}

export interface SyncPage {
  rows: Record<string, unknown>[];
  total: number;
  /** Absent for photometry / lines, whose RPCs return no accessible count. */
  totalAccessible: number | null;
  deletedIds: unknown[];
}

interface StreamSpec {
  rpc: string;
  /** Field of the RPC's single result row that carries the page's rows. */
  rowsField: string;
  /** Row field the client pages on (the next request's `after`). */
  cursorKey: string;
  args(req: SyncPageRequest): Record<string, unknown>;
}

const STREAMS: Record<SyncStreamName, StreamSpec> = {
  objects: {
    rpc: 'get_objects_for_sync',
    rowsField: 'objects',
    cursorKey: 'object_id',
    args: (r) => ({
      p_program_slugs: r.programSlugs,
      p_user_id: r.userId,
      p_updated_since: r.updatedSince,
      p_limit: r.limit,
      p_include_counts: r.includeCounts,
      p_include_unpublished: r.includeUnpublished,
      p_after_object_id: r.after,
      // Sent only in extras mode, so a normal walk still resolves against an
      // RPC that predates the parameter (the migration and the Vercel deploy
      // land independently on merge).
      ...(r.filterProgramSlugs ? { p_filter_program_slugs: r.filterProgramSlugs } : {}),
    }),
  },
  spectra: {
    rpc: 'get_spectra_for_sync',
    rowsField: 'spectra',
    cursorKey: 'spectrum_id',
    args: (r) => ({
      p_program_slugs: r.programSlugs,
      p_user_id: r.userId,
      p_updated_since: r.updatedSince,
      p_limit: r.limit,
      p_include_counts: r.includeCounts,
      p_include_unpublished: r.includeUnpublished,
      p_after_spectrum_id: r.after,
    }),
  },
  storage: {
    rpc: 'get_storage_objects_for_sync',
    rowsField: 'objects',
    cursorKey: 'id',
    args: (r) => ({
      p_program_slugs: r.programSlugs,
      p_updated_since: r.updatedSince,
      p_limit: r.limit,
      p_include_counts: r.includeCounts,
      p_include_unpublished: r.includeUnpublished,
      p_after_id: r.after,
    }),
  },
  photometry: {
    rpc: 'get_photometry_for_sync',
    rowsField: 'photometry_records',
    cursorKey: 'id',
    args: (r) => ({
      p_program_slugs: r.programSlugs,
      p_updated_since: r.updatedSince,
      p_limit: r.limit,
      p_include_unpublished: r.includeUnpublished,
      p_include_counts: r.includeCounts,
      p_after_id: r.after,
    }),
  },
  lines: {
    rpc: 'get_line_fits_for_sync',
    rowsField: 'line_fit_records',
    cursorKey: 'spectrum_id',
    args: (r) => ({
      p_program_slugs: r.programSlugs,
      p_updated_since: r.updatedSince,
      p_limit: r.limit,
      p_include_unpublished: r.includeUnpublished,
      p_include_counts: r.includeCounts,
      p_after_id: r.after,
    }),
  },
};

export function syncCursorKey(stream: SyncStreamName): string {
  return STREAMS[stream].cursorKey;
}

/** Storage scope arguments, only when the caller asked for a scope. */
function storageScopeArgs(r: SyncPageRequest): Record<string, string[]> {
  const scope: Record<string, string[]> = {};
  if (r.productTypes) scope.p_product_types = r.productTypes;
  if (r.observations) scope.p_observations = r.observations;
  if (r.fields) scope.p_fields = r.fields;
  return scope;
}

/**
 * Fetch one page of a sync stream. The client must be the service-role
 * client: the RPCs trust `programSlugs`, which the caller resolved from the
 * requester's access.
 */
export async function fetchSyncPage(
  supabase: SupabaseClient,
  stream: SyncStreamName,
  req: SyncPageRequest,
): Promise<{ page: SyncPage; error: null } | { page: null; error: PostgrestError }> {
  const spec = STREAMS[stream];
  const baseArgs = spec.args(req);
  const scopeArgs = stream === 'storage' ? storageScopeArgs(req) : {};
  const scoped = Object.keys(scopeArgs).length > 0;

  let { data, error } = await supabase.rpc(spec.rpc, { ...baseArgs, ...scopeArgs });

  // Deploy window: the Vercel build and the Supabase migration land
  // independently on merge. If this build runs against a storage RPC that does
  // not know the scope parameters yet (PostgREST: no matching function), fall
  // back to the unscoped call -- the client filters the rows it receives, so
  // the only cost is an unfiltered page in that window.
  if (error && scoped && error.code === 'PGRST202') {
    console.warn('sync storage: RPC without scope parameters; retrying unscoped');
    ({ data, error } = await supabase.rpc(spec.rpc, baseArgs));
  }

  if (error) return { page: null, error };

  const result = (data?.[0] ?? {}) as Record<string, unknown>;
  const rows = (result[spec.rowsField] as Record<string, unknown>[] | null) ?? [];
  return {
    page: {
      rows,
      total: Number(result.total_count ?? 0),
      totalAccessible:
        'total_accessible_count' in result ? Number(result.total_accessible_count ?? 0) : null,
      // Absent from an RPC that predates the column.
      deletedIds: (result.deleted_ids as unknown[] | null) ?? [],
    },
    error: null,
  };
}
