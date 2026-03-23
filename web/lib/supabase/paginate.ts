import type { SupabaseClient } from '@supabase/supabase-js';

const DEFAULT_PAGE_SIZE = 1000;

/**
 * Paginate through all results of a Supabase RPC call.
 *
 * PostgREST silently truncates results at the configured max_rows limit.
 * This utility loops through .range() pages until all rows are collected.
 */
export async function paginateRpc<T = Record<string, unknown>>(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  supabase: SupabaseClient<any, any>,
  fnName: string,
  args: Record<string, unknown>,
  pageSize: number = DEFAULT_PAGE_SIZE,
): Promise<{ data: T[]; error: Error | null }> {
  const allRows: T[] = [];
  let offset = 0;

  while (true) {
    const { data, error } = await supabase
      .rpc(fnName, args)
      .range(offset, offset + pageSize - 1);

    if (error) {
      return { data: allRows, error: new Error(error.message) };
    }

    if (!data || (data as T[]).length === 0) break;
    allRows.push(...(data as T[]));
    if ((data as T[]).length < pageSize) break;
    offset += pageSize;
  }

  return { data: allRows, error: null };
}

/**
 * Paginate through all results of a Supabase table/view query.
 *
 * Accepts a factory callback that returns a fresh query builder on each call.
 * This is necessary because the Supabase query builder is mutable — calling
 * .range() on the same builder twice would corrupt internal state.
 */
export async function paginateQuery<T = Record<string, unknown>>(
  buildQuery: () => {
    range: (from: number, to: number) => PromiseLike<{
      data: T[] | null;
      error: { message: string } | null;
    }>;
  },
  pageSize: number = DEFAULT_PAGE_SIZE,
): Promise<{ data: T[]; error: Error | null }> {
  const allRows: T[] = [];
  let offset = 0;

  while (true) {
    const { data, error } = await buildQuery()
      .range(offset, offset + pageSize - 1);

    if (error) {
      return { data: allRows, error: new Error(error.message) };
    }

    if (!data || data.length === 0) break;
    allRows.push(...data);
    if (data.length < pageSize) break;
    offset += pageSize;
  }

  return { data: allRows, error: null };
}
