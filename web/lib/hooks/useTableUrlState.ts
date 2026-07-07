'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useDebouncedValue } from './useDebouncedValue';

// ---------------------------------------------------------------------------
// Generic URL-backed table state (filters + sort + pagination) for admin lists.
//
// Mirrors the public explorer's URL-as-state wiring (app/nirspec/page.tsx +
// lib/utils/url-params.ts) in a table-agnostic form: state lives in the URL so
// it survives back-navigation, refresh, and deep links; filter changes are
// debounced before hitting the URL and the query key. Deliberately a new hook
// rather than a generalization of url-params.ts — that module is typed to the
// spectra filter model and shared with public pages (blast radius).
//
// The codec MUST be a stable reference (module-level const), as must
// sortWhitelist and defaultSort — they are dependencies of URL parsing.
// ---------------------------------------------------------------------------

export type SortDirection = 'asc' | 'desc';

export interface SortState {
  column: string;
  direction: SortDirection;
}

export interface ParamCodec<TFilters> {
  /** Read filter values from URL params (absent params → defaults). */
  parse: (sp: URLSearchParams) => TFilters;
  /** Write filter values into URL params, omitting defaults/empties. */
  serialize: (filters: TFilters, sp: URLSearchParams) => void;
}

/**
 * Codec for the common admin case: a flat map of string-valued facets where
 * '' means unset. Pass explicit defaults for facets whose initial value is not
 * '' (e.g. the storage page's status=active default) — a default value is
 * omitted from the URL and restored on parse.
 */
export function flatFilterCodec<K extends string>(
  keys: readonly K[],
  defaults?: Partial<Record<K, string>>,
): ParamCodec<Record<K, string>> {
  return {
    parse: (sp) => {
      const out = {} as Record<K, string>;
      for (const k of keys) out[k] = sp.get(k) ?? defaults?.[k] ?? '';
      return out;
    },
    serialize: (filters, sp) => {
      for (const k of keys) {
        const def = defaults?.[k] ?? '';
        if (filters[k] !== def) sp.set(k, filters[k]);
      }
    },
  };
}

export interface TableUrlStateConfig<TFilters> {
  codec: ParamCodec<TFilters>;
  /** Valid sort keys — the same list the backing RPC whitelists. */
  sortWhitelist: readonly string[];
  defaultSort: SortState;
  defaultPageSize?: number;
  debounceMs?: number;
}

export interface TableUrlState<TFilters> {
  filters: TFilters;
  /** Debounced filters — use these for the query key / fetch. */
  debouncedFilters: TFilters;
  isDebouncing: boolean;
  sort: SortState;
  /** 1-based */
  page: number;
  pageSize: number;
  setFilters: (f: TFilters) => void;
  setSort: (column: string, direction: SortDirection) => void;
  setPage: (page: number) => void;
  setPageSize: (n: number) => void;
  resetFilters: () => void;
}

export function useTableUrlState<TFilters>(
  config: TableUrlStateConfig<TFilters>,
): TableUrlState<TFilters> {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const {
    codec,
    sortWhitelist,
    defaultSort,
    defaultPageSize = 50,
    debounceMs = 300,
  } = config;

  const parseAll = useCallback(
    (sp: URLSearchParams) => {
      const sortCol = sp.get('sort');
      const dir = sp.get('dir');
      return {
        filters: codec.parse(sp),
        sort: {
          column:
            sortCol && sortWhitelist.includes(sortCol) ? sortCol : defaultSort.column,
          direction:
            dir === 'asc' || dir === 'desc' ? dir : defaultSort.direction,
        } as SortState,
        page: Math.max(1, parseInt(sp.get('page') ?? '1', 10) || 1),
        pageSize: Math.max(
          1,
          parseInt(sp.get('ps') ?? String(defaultPageSize), 10) || defaultPageSize,
        ),
      };
    },
    [codec, sortWhitelist, defaultSort, defaultPageSize],
  );

  const [state, setState] = useState(() =>
    parseAll(new URLSearchParams(searchParams.toString())),
  );

  const { debouncedValue: debouncedFilters, isDebouncing } = useDebouncedValue(
    state.filters,
    debounceMs,
  );

  const serialize = useCallback(
    (filters: TFilters, sort: SortState, page: number, pageSize: number) => {
      const sp = new URLSearchParams();
      codec.serialize(filters, sp);
      if (
        sort.column !== defaultSort.column ||
        sort.direction !== defaultSort.direction
      ) {
        sp.set('sort', sort.column);
        sp.set('dir', sort.direction);
      }
      if (page > 1) sp.set('page', String(page));
      if (pageSize !== defaultPageSize) sp.set('ps', String(pageSize));
      return sp.toString();
    },
    [codec, defaultSort, defaultPageSize],
  );

  // Tracks the last search string this hook wrote (or synced from), so the
  // two effects below don't feed each other.
  const lastWritten = useRef(searchParams.toString());

  // State → URL (debounced filters so typing doesn't spam history).
  useEffect(() => {
    const next = serialize(debouncedFilters, state.sort, state.page, state.pageSize);
    if (next !== lastWritten.current) {
      lastWritten.current = next;
      router.replace(`${pathname}${next ? `?${next}` : ''}`, { scroll: false });
    }
  }, [debouncedFilters, state.sort, state.page, state.pageSize, serialize, pathname, router]);

  // URL → state, for changes we didn't write: browser back/forward and
  // in-app navigation to the same route with different params (dashboard
  // deep links). This is what makes "filters preserved on back" actually work.
  useEffect(() => {
    const current = searchParams.toString();
    if (current !== lastWritten.current) {
      lastWritten.current = current;
      setState(parseAll(new URLSearchParams(current)));
    }
  }, [searchParams, parseAll]);

  const setFilters = useCallback((f: TFilters) => {
    setState((s) => ({ ...s, filters: f, page: 1 }));
  }, []);

  const setSort = useCallback((column: string, direction: SortDirection) => {
    setState((s) => ({ ...s, sort: { column, direction }, page: 1 }));
  }, []);

  const setPage = useCallback((page: number) => {
    setState((s) => ({ ...s, page: Math.max(1, page) }));
  }, []);

  const setPageSize = useCallback((n: number) => {
    setState((s) => ({ ...s, pageSize: n, page: 1 }));
  }, []);

  const resetFilters = useCallback(() => {
    setState((s) => ({
      ...s,
      filters: codec.parse(new URLSearchParams()),
      page: 1,
    }));
  }, [codec]);

  return {
    filters: state.filters,
    debouncedFilters,
    isDebouncing,
    sort: state.sort,
    page: state.page,
    pageSize: state.pageSize,
    setFilters,
    setSort,
    setPage,
    setPageSize,
    resetFilters,
  };
}
