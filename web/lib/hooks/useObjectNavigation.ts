'use client';

import { useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { getObjectById } from '@/lib/actions/spectra';
import type { ObjectDetail } from '@/lib/types';

export interface ObjectNavigationData {
  object: ObjectDetail;
}

/**
 * Fetches object data by IAU object_id. Queue ordering is handled by useInspectionQueue.
 */
export function useObjectNavigation() {
  const router = useRouter();

  const [isNavigating, setIsNavigating] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);

  const fetchObject = useCallback(async (objectId: string): Promise<ObjectNavigationData | null> => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const result = await getObjectById(objectId);

      if (controller.signal.aborted) return null;

      if (!result.isAuthenticated) {
        router.push('/login?redirect=' + encodeURIComponent(window.location.pathname));
        return null;
      }

      if (!result.object) {
        setNavigationError(result.error || 'Object not found');
        return null;
      }

      setNavigationError(null);
      return { object: result.object };
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') return null;
      console.error('[Navigation] Fetch failed:', error);
      setNavigationError(error instanceof Error ? error.message : 'Failed to load object');
      return null;
    }
  }, [router]);

  const navigateTo = useCallback(async (
    objectId: string,
    onBeforeNavigate: () => Promise<boolean>
  ): Promise<ObjectNavigationData | null> => {
    setIsNavigating(true);
    setNavigationError(null);
    try {
      const canProceed = await onBeforeNavigate();
      if (!canProceed) {
        setIsNavigating(false);
        return null;
      }
      const data = await fetchObject(objectId);
      setIsNavigating(false);
      return data;
    } catch (error) {
      console.error('[Navigation] Navigation failed:', error);
      setNavigationError(error instanceof Error ? error.message : 'Navigation failed');
      setIsNavigating(false);
      return null;
    }
  }, [fetchObject]);

  /** Lightweight prefetch — no shared abort controller. */
  const prefetchObject = useCallback(async (objectId: string): Promise<ObjectNavigationData | null> => {
    try {
      const result = await getObjectById(objectId);
      if (!result.isAuthenticated || !result.object) return null;
      return { object: result.object };
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
