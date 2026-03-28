'use client';

import { useState, useEffect, useCallback } from 'react';
import { getInspectionQueueIds, type FilterOptions } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';

interface UseInspectionQueueOptions {
  initialTargetId: string;
  filters: Partial<FilterOptions>;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
}

interface InspectionQueueState {
  /** Ordered list of target IDs in the queue */
  ids: string[];
  /** Current index in the queue (-1 if not found) */
  index: number;
  /** Total number of targets in the queue */
  total: number;
  /** Previous target ID, or null if at start */
  prev: string | null;
  /** Next target ID, or null if at end */
  next: string | null;
  /** Whether the queue is still loading */
  loading: boolean;
  /** Whether the queue is empty (all inspected / no matches) */
  isEmpty: boolean;
  /** Error message, if any */
  error: string | null;
  /** Whether the initial target was not in the queue (already inspected) */
  redirected: boolean;
}

export function useInspectionQueue(options: UseInspectionQueueOptions): InspectionQueueState & {
  /** Update the current position to a specific target ID */
  goTo: (targetId: string) => void;
  /** Get the first target ID in the queue (for redirect) */
  firstId: string | null;
} {
  const { initialTargetId, filters, sortColumn, sortDirection } = options;

  const [ids, setIds] = useState<string[]>([]);
  const [index, setIndex] = useState(-1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [redirected, setRedirected] = useState(false);

  // Fetch queue on mount (once)
  useEffect(() => {
    let cancelled = false;

    async function fetchQueue() {
      try {
        const result = await getInspectionQueueIds(filters, sortColumn, sortDirection);

        if (cancelled) return;

        if (result.error) {
          setError(result.error);
          setLoading(false);
          return;
        }

        const queueIds = result.ids;
        setIds(queueIds);

        if (queueIds.length === 0) {
          setIndex(-1);
          setLoading(false);
          return;
        }

        // Find the initial target in the queue
        const initialIndex = queueIds.indexOf(initialTargetId);
        if (initialIndex >= 0) {
          setIndex(initialIndex);
        } else {
          // Target not in queue (already inspected) — start at first item
          setIndex(0);
          setRedirected(true);
        }

        setLoading(false);
      } catch (err) {
        if (cancelled) return;
        console.error('[InspectionQueue] Failed to fetch queue:', err);
        setError(err instanceof Error ? err.message : 'Failed to load queue');
        setLoading(false);
      }
    }

    fetchQueue();
    return () => { cancelled = true; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  // Intentionally empty deps: fetch once on mount with initial values

  const goTo = useCallback((targetId: string) => {
    const newIndex = ids.indexOf(targetId);
    if (newIndex >= 0) {
      setIndex(newIndex);
    }
  }, [ids]);

  const total = ids.length;
  const isEmpty = !loading && total === 0;
  const prev = index > 0 ? ids[index - 1] : null;
  const next = index < total - 1 ? ids[index + 1] : null;
  const firstId = total > 0 ? ids[0] : null;

  return {
    ids,
    index: total > 0 ? index + 1 : 0, // 1-based for display
    total,
    prev,
    next,
    loading,
    isEmpty,
    error,
    redirected,
    goTo,
    firstId,
  };
}
