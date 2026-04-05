'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import { lookupInCache, isAtCacheBoundary } from '@/lib/navigation-cache';
import { getAdjacentTargetIds, getAdjacentObjectIds, type FilterOptions } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection, ViewMode } from '@/lib/actions/spectra-types';

interface ObjectNavigationProps {
  targetId: string;
  filters: Partial<FilterOptions>;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  filterStr: string; // URL params string for navigation links
  viewMode?: ViewMode;
  className?: string;
}

interface NavState {
  prev: string | null;
  next: string | null;
  index: number;
  total: number;
  loading: boolean;
  source: 'cache' | 'server' | 'none';
}

/**
 * Client component for detail page navigation.
 * Uses hybrid approach: sessionStorage cache for instant lookup,
 * falls back to server query when cache misses.
 * Supports both targets and objects view modes.
 */
export function ObjectNavigation({
  targetId,
  filters,
  sortColumn,
  sortDirection,
  filterStr,
  viewMode = 'targets',
  className = '',
}: ObjectNavigationProps) {
  const [nav, setNav] = useState<NavState>({
    prev: null,
    next: null,
    index: 0,
    total: 0,
    loading: true,
    source: 'none',
  });

  const isObjectsMode = viewMode === 'objects';
  const basePath = isObjectsMode ? '/nirspec/objects' : '/nirspec/targets';

  useEffect(() => {
    let cancelled = false;

    async function loadNavigation() {
      // First, try sessionStorage cache for instant response
      const sortKey = `${sortColumn}_${sortDirection}`;
      const cached = lookupInCache(targetId, filterStr, sortKey);

      if (cached) {
        // Check if we're at a boundary and might need server data
        const boundary = isAtCacheBoundary(targetId);

        // If we have valid prev/next from cache, use it
        if (!boundary.atStart && !boundary.atEnd) {
          if (!cancelled) {
            setNav({
              prev: cached.prev,
              next: cached.next,
              index: cached.index,
              total: cached.total,
              loading: false,
              source: 'cache',
            });
          }
          return;
        }

        // At boundary - show cached data but fetch server data for missing direction
        if (!cancelled) {
          setNav({
            prev: cached.prev,
            next: cached.next,
            index: cached.index,
            total: cached.total,
            loading: true, // Still loading the missing prev/next
            source: 'cache',
          });
        }
      }

      // Fall back to server query
      try {
        const result = isObjectsMode
          ? await getAdjacentObjectIds(targetId, filters, sortColumn, sortDirection)
          : await getAdjacentTargetIds(targetId, filters, sortColumn, sortDirection);

        if (!cancelled) {
          setNav({
            prev: result.prev,
            next: result.next,
            index: result.currentIndex,
            total: result.total,
            loading: false,
            source: 'server',
          });
        }
      } catch (error) {
        console.error('Failed to fetch adjacent items:', error);
        if (!cancelled) {
          // Keep cached data if available, otherwise show unknown state
          setNav(prev => ({
            ...prev,
            loading: false,
            source: prev.source === 'cache' ? 'cache' : 'none',
          }));
        }
      }
    }

    loadNavigation();

    return () => {
      cancelled = true;
    };
  }, [targetId, filters, sortColumn, sortDirection, filterStr, isObjectsMode]);

  // Build navigation URLs
  const prevHref = nav.prev
    ? `${basePath}/${encodeURIComponent(nav.prev)}${filterStr ? `?${filterStr}` : ''}`
    : undefined;

  const nextHref = nav.next
    ? `${basePath}/${encodeURIComponent(nav.next)}${filterStr ? `?${filterStr}` : ''}`
    : undefined;

  return (
    <div className={`flex items-center space-x-4 ${className}`}>
      {prevHref ? (
        <Link
          href={prevHref}
          className="p-2 rounded-lg hover:bg-card dark:hover:bg-slate-700 transition-colors text-text-primary dark:text-slate-100"
          aria-label="Previous object"
        >
          <ChevronLeft className="w-5 h-5" />
        </Link>
      ) : (
        <div className="p-2 text-text-secondary dark:text-slate-500 opacity-50">
          <ChevronLeft className="w-5 h-5" />
        </div>
      )}

      <span className="text-sm font-medium text-text-primary dark:text-slate-100 min-w-[60px] text-center">
        {nav.loading ? (
          <Loader2 className="w-4 h-4 animate-spin inline" />
        ) : nav.index > 0 && nav.total > 0 ? (
          `${nav.index} of ${nav.total}`
        ) : (
          '? of ?'
        )}
      </span>

      {nextHref ? (
        <Link
          href={nextHref}
          className="p-2 rounded-lg hover:bg-card dark:hover:bg-slate-700 transition-colors text-text-primary dark:text-slate-100"
          aria-label="Next object"
        >
          <ChevronRight className="w-5 h-5" />
        </Link>
      ) : (
        <div className="p-2 text-text-secondary dark:text-slate-500 opacity-50">
          <ChevronRight className="w-5 h-5" />
        </div>
      )}
    </div>
  );
}
