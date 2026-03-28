'use client';

import { useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { getSpectrumById, getAdjacentTargetIds, type FilterOptions } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import type { NavigationData } from './useMultiObjectCache';

interface UseObjectNavigationOptions {
  filters: Partial<FilterOptions>;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  /** When true, fetchObject only fetches spectrum data (skips getAdjacentTargetIds) */
  skipNavQuery?: boolean;
}

export function useObjectNavigation(options: UseObjectNavigationOptions) {
  const { filters, sortColumn, sortDirection, skipNavQuery } = options;
  const router = useRouter();

  const [isNavigating, setIsNavigating] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);

  // AbortController for canceling in-flight requests
  const abortControllerRef = useRef<AbortController | null>(null);

  /**
   * Fetch object data (spectrum + RGB + navigation) in parallel
   */
  const fetchObject = useCallback(async (targetId: string): Promise<NavigationData | null> => {
    console.log(`[Navigation] Fetching target: ${targetId}`);

    // Cancel previous request
    if (abortControllerRef.current) {
      console.log('[Navigation] Aborting previous request');
      abortControllerRef.current.abort();
    }

    // Create new controller
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      // Fetch spectrum data (and optionally navigation data in parallel)
      const spectrumPromise = getSpectrumById(targetId);
      const navPromise = skipNavQuery
        ? Promise.resolve({ prev: null, next: null, currentIndex: 0, total: 0 })
        : getAdjacentTargetIds(targetId, filters, sortColumn, sortDirection);

      const [spectrumResult, adjacentIds] = await Promise.all([spectrumPromise, navPromise]);

      // Use API route URL directly as image src (browser follows redirect automatically)
      const rgbUrl = `/api/tile-thumbnail?target_id=${encodeURIComponent(targetId)}`;

      // Check if aborted during fetch
      if (controller.signal.aborted) {
        console.log('[Navigation] Request was aborted');
        return null;
      }

      // Handle authentication
      if (!spectrumResult.isAuthenticated) {
        console.error('[Navigation] User not authenticated');
        router.push('/login?redirect=' + encodeURIComponent(window.location.pathname));
        return null;
      }

      // Handle not found or error
      if (!spectrumResult.spectrum) {
        console.error(`[Navigation] Spectrum not found: ${targetId}`, spectrumResult.error);
        setNavigationError(spectrumResult.error || 'Target not found');
        return null;
      }

      // Clear error on success
      setNavigationError(null);

      return {
        spectrum: spectrumResult.spectrum,
        rgbImageUrl: rgbUrl,
        nav: {
          prev: adjacentIds.prev,
          next: adjacentIds.next,
          index: adjacentIds.currentIndex,
          total: adjacentIds.total,
        },
      };
    } catch (error) {
      // Handle abort gracefully
      if (error instanceof Error && error.name === 'AbortError') {
        console.log('[Navigation] Request aborted');
        return null;
      }

      // Handle other errors
      console.error('[Navigation] Fetch failed:', error);
      setNavigationError(error instanceof Error ? error.message : 'Failed to load target');
      return null;
    }
  }, [filters, sortColumn, sortDirection, skipNavQuery, router]);

  /**
   * Navigate to object with auto-save integration
   */
  const navigateTo = useCallback(async (
    targetId: string,
    onBeforeNavigate: () => Promise<boolean>
  ): Promise<NavigationData | null> => {
    console.log(`[Navigation] Navigating to: ${targetId}`);
    setIsNavigating(true);
    setNavigationError(null);

    try {
      // Call pre-navigation hook (auto-save)
      const canProceed = await onBeforeNavigate();

      if (!canProceed) {
        console.log('[Navigation] Navigation blocked by onBeforeNavigate');
        setIsNavigating(false);
        return null;
      }

      // Fetch new target data
      const data = await fetchObject(targetId);
      setIsNavigating(false);

      return data;
    } catch (error) {
      console.error('[Navigation] Navigation failed:', error);
      setNavigationError(error instanceof Error ? error.message : 'Navigation failed');
      setIsNavigating(false);
      return null;
    }
  }, [fetchObject]);

  /**
   * Lightweight fetch for prefetching adjacent objects.
   * Does NOT use the shared AbortController, so it won't cancel
   * (or be cancelled by) user-initiated navigation.
   */
  const prefetchObject = useCallback(async (targetId: string): Promise<NavigationData | null> => {
    try {
      const spectrumResult = await getSpectrumById(targetId);

      if (!spectrumResult.isAuthenticated || !spectrumResult.spectrum) {
        return null;
      }

      const rgbUrl = `/api/tile-thumbnail?target_id=${encodeURIComponent(targetId)}`;

      return {
        spectrum: spectrumResult.spectrum,
        rgbImageUrl: rgbUrl,
        nav: { prev: null, next: null, index: 0, total: 0 },
      };
    } catch {
      return null;
    }
  }, []);

  return {
    isNavigating,
    navigationError,
    setNavigationError,
    navigateTo,
    fetchObject,
    prefetchObject,
  };
}
