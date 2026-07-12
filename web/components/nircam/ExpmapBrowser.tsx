'use client';

import React, { useEffect, useRef, useState } from 'react';
import { ImageIcon } from 'lucide-react';

interface ExpmapBrowserProps {
  /** Field filters, in display order (the vertical tab list). */
  filters: string[];
  /** filter -> presigned expmap-plot PNG URL (missing filters render a placeholder). */
  expmapPlots: Record<string, string>;
}

// The plot well matches the baked-dark expmap PNGs; the tab rail sits on a
// slightly lighter dark so the selected tab (well-colored) reads as connected
// to the plot. Hard dusk values on purpose — the well stays dark in both app
// themes, like the map surface.
const WELL_BG = '#0d0b12';

/**
 * The field-overview exposure-map panel: dark vertical filter tabs beside a
 * square plot well (matching the exported PNG aspect), ↑/↓ keyboard nav when
 * the tab list is focused.
 */
export const ExpmapBrowser: React.FC<ExpmapBrowserProps> = ({
  filters,
  expmapPlots,
}) => {
  const [current, setCurrent] = useState<string | null>(filters[0] ?? null);
  const [failedUrls, setFailedUrls] = useState<Set<string>>(new Set());
  const tabsRef = useRef<HTMLDivElement>(null);

  // Keep the selection valid if the filter list changes (field switch).
  useEffect(() => {
    setCurrent((c) => (c && filters.includes(c) ? c : filters[0] ?? null));
  }, [filters]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    if (filters.length === 0) return;
    const i = current ? filters.indexOf(current) : 0;
    const next = (i + (e.key === 'ArrowDown' ? 1 : -1) + filters.length) % filters.length;
    setCurrent(filters[next]);
  };

  // Scroll the active tab into view on keyboard navigation.
  useEffect(() => {
    tabsRef.current
      ?.querySelector('[data-active="true"]')
      ?.scrollIntoView({ block: 'nearest' });
  }, [current]);

  const shownUrl = current ? expmapPlots[current] ?? null : null;
  const imgOk = shownUrl && !failedUrls.has(shownUrl);

  return (
    <div className="flex w-full rounded-lg overflow-hidden border border-border bg-[#161320]">
      {/* Vertical filter tabs — selected tab merges into the plot well */}
      <div
        ref={tabsRef}
        tabIndex={0}
        role="tablist"
        aria-orientation="vertical"
        onKeyDown={onKeyDown}
        className="w-[86px] flex-none overflow-y-auto py-1.5 outline-none focus:ring-1 focus:ring-inset focus:ring-primary/40"
      >
        {filters.map((f) => {
          const active = f === current;
          return (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={active}
              data-active={active}
              onClick={() => setCurrent(f)}
              className={`w-full text-left font-mono text-[11px] px-2.5 py-1.5 transition-colors ${
                active
                  ? 'bg-[#0d0b12] text-[#f1ecf6] font-semibold'
                  : 'text-[#8b8398] hover:text-[#b8aec6] hover:bg-[#1d1927]'
              }`}
            >
              {f.toUpperCase()}
            </button>
          );
        })}
      </div>

      {/* Square plot well — matches the exported PNG aspect */}
      <div
        className="flex-1 aspect-square flex items-center justify-center p-2"
        style={{ background: WELL_BG }}
      >
        {imgOk ? (
          // Presigned cross-origin PNG; plain <img> (see NircamFieldCard).
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={shownUrl as string}
            alt={`${current?.toUpperCase()} exposure map`}
            className="max-w-full max-h-full"
            onError={() =>
              setFailedUrls((prev) => new Set(prev).add(shownUrl as string))
            }
          />
        ) : (
          <div className="flex flex-col items-center gap-2 text-text-tertiary">
            <ImageIcon className="w-7 h-7 opacity-40" />
            <span className="text-xs">
              {filters.length === 0
                ? 'No exposure maps available'
                : 'Plot not yet deployed for this filter'}
            </span>
          </div>
        )}
      </div>
    </div>
  );
};
