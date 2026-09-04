'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import { lookupInCache, isAtCacheBoundary, type NavLookupResult } from '@/lib/navigation-cache';
import { useAdjacentObjectsQuery } from '@/lib/hooks/useAdjacentObjectsQuery';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';

interface ObjectNavigationProps {
  /** IAU object_id of the current object. */
  targetId: string;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  /** The list page's URL parameter string (filters + sort): navigation links
   *  carry it, the session cache is validated against it, and the server
   *  fallback (GET /api/objects/adjacent) parses it. */
  filterStr: string;
  className?: string;
}

interface CacheLookup {
  hit: NavLookupResult | null;
  atStart: boolean;
  atEnd: boolean;
}

/**
 * Client component for detail page navigation.
 * Uses sessionStorage cache for instant lookup, falls back to a server query
 * on a miss or at a page boundary.
 */
export function ObjectNavigation({
  targetId,
  sortColumn,
  sortDirection,
  filterStr,
  className = '',
}: ObjectNavigationProps) {
  const basePath = '/nirspec/objects';
  const sortKey = `${sortColumn}_${sortDirection}`;

  // sessionStorage is client-only: read it in an effect so the server render
  // and the first client render agree (both "loading").
  const [cache, setCache] = useState<CacheLookup | null>(null);
  useEffect(() => {
    const hit = lookupInCache(targetId, filterStr, sortKey);
    const boundary = hit ? isAtCacheBoundary(targetId) : { atStart: false, atEnd: false };
    setCache({ hit, ...boundary });
  }, [targetId, filterStr, sortKey]);

  // Server fallback only on a cache miss or at a boundary (a direction the
  // cached page cannot answer). A GET route, not a server action (#506): it
  // runs alongside the page's other reads and aborts if the user moves on.
  const needServer = cache !== null && (!cache.hit || cache.atStart || cache.atEnd);
  const adjacent = useAdjacentObjectsQuery(targetId, filterStr, needServer);

  // Server answers win where they exist; the cache fills the rest (and
  // everything, if the server call fails).
  const hit = cache?.hit ?? null;
  const server = adjacent.data;
  const prev = server ? server.prev : hit?.prev ?? null;
  const next = server ? server.next : hit?.next ?? null;
  const index = server && server.currentIndex > 0 ? server.currentIndex : hit?.index ?? 0;
  const total = server && server.total > 0 ? server.total : hit?.total ?? 0;
  const loading = cache === null || (needServer && adjacent.isPending);

  // Build navigation URLs
  const prevHref = prev
    ? `${basePath}/${encodeURIComponent(prev)}${filterStr ? `?${filterStr}` : ''}`
    : undefined;

  const nextHref = next
    ? `${basePath}/${encodeURIComponent(next)}${filterStr ? `?${filterStr}` : ''}`
    : undefined;

  return (
    <div className={`flex items-center space-x-4 ${className}`}>
      {prevHref ? (
        <Link
          href={prevHref}
          className="p-2 rounded-lg hover:bg-card dark:hover:bg-card-hover transition-colors text-text-primary"
          aria-label="Previous object"
        >
          <ChevronLeft className="w-5 h-5" />
        </Link>
      ) : (
        <div className="p-2 text-text-secondary dark:text-text-tertiary opacity-50">
          <ChevronLeft className="w-5 h-5" />
        </div>
      )}

      <span className="text-sm font-medium text-text-primary min-w-[60px] text-center">
        {loading ? (
          <Loader2 className="w-4 h-4 animate-spin inline" />
        ) : index > 0 && total > 0 ? (
          `${index} of ${total}`
        ) : (
          '? of ?'
        )}
      </span>

      {nextHref ? (
        <Link
          href={nextHref}
          className="p-2 rounded-lg hover:bg-card dark:hover:bg-card-hover transition-colors text-text-primary"
          aria-label="Next object"
        >
          <ChevronRight className="w-5 h-5" />
        </Link>
      ) : (
        <div className="p-2 text-text-secondary dark:text-text-tertiary opacity-50">
          <ChevronRight className="w-5 h-5" />
        </div>
      )}
    </div>
  );
}
