import type { SupabaseClient, User } from '@supabase/supabase-js';

const DEFAULT_PAGE_SIZE = 5000;

/**
 * Find an auth user by email, paging through auth.admin.listUsers.
 *
 * GoTrue's listUsers defaults to 50 users per page and supabase-js has no
 * direct lookup-by-email admin API, so a bare listUsers() call only ever sees
 * the first 50 accounts — a duplicate-email check built on it silently stops
 * working once an instance grows past that. Requires a service-role client.
 */
export async function findAuthUserByEmail(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  serviceClient: SupabaseClient<any, any>,
  email: string,
): Promise<{ user: User | null; error: Error | null }> {
  const normalized = email.trim().toLowerCase();
  const perPage = 1000;

  for (let page = 1; ; page++) {
    const { data, error } = await serviceClient.auth.admin.listUsers({ page, perPage });

    if (error) {
      return { user: null, error: new Error(error.message) };
    }

    const match = data.users.find(u => u.email?.toLowerCase() === normalized);
    if (match) return { user: match, error: null };
    if (data.users.length < perPage) return { user: null, error: null };
  }
}

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
 * Keyset-paginate through all results of a Supabase RPC.
 *
 * Unlike paginateRpc above, which pages with PostgREST .range() — LIMIT/OFFSET
 * applied OUTSIDE a set-returning function, so every page re-executes and
 * re-sorts the entire result set — this drives RPCs that accept an explicit
 * cursor + page-size argument and apply the LIMIT inside the query. Each page
 * is then one index-bounded scan from the cursor, and total work across the
 * whole export stays proportional to the result set (issue #412).
 *
 * The RPC must return rows in ascending cursor-key order and cap its output at
 * the page size it was passed (a shorter page signals the last one).
 */
export async function paginateRpcKeyset<T = Record<string, unknown>>(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  supabase: SupabaseClient<any, any>,
  fnName: string,
  args: Record<string, unknown>,
  opts: {
    /** RPC argument name that carries the cursor (null on the first page). */
    cursorParam: string;
    /** Extracts the cursor value from the last row of a page. */
    getCursor: (row: T) => string | number;
    /** Stop after this many rows (result is truncated to exactly this count). */
    maxRows?: number;
    pageSize?: number;
  },
): Promise<{ data: T[]; error: Error | null }> {
  const pageSize = opts.pageSize ?? DEFAULT_PAGE_SIZE;
  const allRows: T[] = [];
  let cursor: string | number | null = null;

  while (true) {
    const { data, error } = await supabase.rpc(fnName, {
      ...args,
      [opts.cursorParam]: cursor,
      p_page_size: pageSize,
    });

    if (error) {
      return { data: allRows, error: new Error(error.message) };
    }

    const rows = (data ?? []) as T[];
    allRows.push(...rows);
    if (opts.maxRows != null && allRows.length >= opts.maxRows) {
      allRows.length = opts.maxRows;
      break;
    }
    if (rows.length < pageSize) break;
    cursor = opts.getCursor(rows[rows.length - 1]);
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
