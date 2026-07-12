'use client';

import React, { useEffect, useRef, useState } from 'react';
import { ImageIcon } from 'lucide-react';

interface ExpmapBrowserProps {
  /** Field filters, in display order (the vertical tab list). */
  filters: string[];
  /** filter -> presigned expmap-plot PNG URL (missing filters render a placeholder). */
  expmapPlots: Record<string, string>;
  /** Presigned `<field>_layout.png` URL for the all-filter overlay, if deployed. */
  layoutUrl: string | null;
}

/**
 * The field-overview exposure-map panel: left vertical filter tabs (↑/↓
 * keyboard nav when the tab list is focused), the selected filter's dark
 * expmap plot, and an all-filter overlay toggle that swaps in the field
 * layout plot (stacked coverage + tile outlines).
 */
export const ExpmapBrowser: React.FC<ExpmapBrowserProps> = ({
  filters,
  expmapPlots,
  layoutUrl,
}) => {
  const [current, setCurrent] = useState<string | null>(filters[0] ?? null);
  const [overlay, setOverlay] = useState(false);
  const [failedUrls, setFailedUrls] = useState<Set<string>>(new Set());
  const tabsRef = useRef<HTMLDivElement>(null);

  // Keep the selection valid if the filter list changes (field switch).
  useEffect(() => {
    setCurrent((c) => (c && filters.includes(c) ? c : filters[0] ?? null));
  }, [filters]);

  const pick = (f: string) => {
    setCurrent(f);
    setOverlay(false);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    if (filters.length === 0) return;
    const i = current ? filters.indexOf(current) : 0;
    const next = (i + (e.key === 'ArrowDown' ? 1 : -1) + filters.length) % filters.length;
    pick(filters[next]);
  };

  // Scroll the active tab into view on keyboard navigation.
  useEffect(() => {
    tabsRef.current
      ?.querySelector('[data-active="true"]')
      ?.scrollIntoView({ block: 'nearest' });
  }, [current]);

  const shownUrl = overlay ? layoutUrl : current ? expmapPlots[current] ?? null : null;
  const imgOk = shownUrl && !failedUrls.has(shownUrl);

  return (
    <div>
      <div className="flex items-center justify-between gap-3 mb-2.5">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
          Exposure map · filter coverage
        </h3>
        <div className="flex items-center gap-3.5">
          <span className="hidden sm:inline text-[11px] text-text-tertiary">
            <kbd className="font-mono text-[10px] border border-border rounded px-1">↑</kbd>{' '}
            <kbd className="font-mono text-[10px] border border-border rounded px-1">↓</kbd>{' '}
            to browse
          </span>
          {layoutUrl && (
            <label className="flex items-center gap-1.5 text-[11.5px] text-text-tertiary cursor-pointer select-none">
              <input
                type="checkbox"
                checked={overlay}
                onChange={(e) => setOverlay(e.target.checked)}
                className="accent-[var(--primary)]"
              />
              all-filter overlay
            </label>
          )}
        </div>
      </div>

      <div className="flex bg-background border border-border rounded-lg overflow-hidden">
        {/* Vertical filter tabs */}
        <div
          ref={tabsRef}
          tabIndex={0}
          role="tablist"
          aria-orientation="vertical"
          onKeyDown={onKeyDown}
          className="w-[86px] flex-none border-r border-border p-1 overflow-y-auto max-h-80 outline-none focus:ring-2 focus:ring-inset focus:ring-primary/25"
        >
          {filters.map((f) => {
            const active = f === current && !overlay;
            return (
              <button
                key={f}
                type="button"
                role="tab"
                aria-selected={active}
                data-active={active}
                onClick={() => pick(f)}
                className={`w-full text-left font-mono text-[11px] px-2 py-1 rounded transition-colors ${
                  active
                    ? 'bg-primary/10 text-primary font-semibold'
                    : 'text-text-secondary hover:bg-card-hover'
                }`}
              >
                {f.toUpperCase()}
              </button>
            );
          })}
        </div>

        {/* Plot well — expmap plots are baked dark; keep the well dark in both themes */}
        <div className="flex-1 p-3 bg-[#0d0b12] min-h-[200px] flex items-center justify-center">
          {imgOk ? (
            // Presigned cross-origin PNG; plain <img> (see NircamFieldCard).
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={shownUrl as string}
              alt={overlay ? 'All-filter coverage overlay' : `${current?.toUpperCase()} exposure map`}
              className="max-w-full max-h-80 rounded"
              onError={() =>
                setFailedUrls((prev) => new Set(prev).add(shownUrl as string))
              }
            />
          ) : (
            <div className="flex flex-col items-center gap-2 text-text-tertiary py-10">
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

      <p className="text-[11.5px] text-text-tertiary mt-2">
        {overlay ? (
          <>Stacked exposure across all filters, with tile outlines.</>
        ) : current ? (
          <>
            <span className="font-mono font-semibold text-text-secondary">
              {current.toUpperCase()}
            </span>{' '}
            · exposure time per pixel (seconds), shared color scale across filters
          </>
        ) : null}
      </p>
    </div>
  );
};
