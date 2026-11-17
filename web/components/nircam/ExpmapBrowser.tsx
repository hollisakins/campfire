'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ImageIcon } from 'lucide-react';

/** One exposure-map tab: a filter of one instrument. */
export interface ExpmapTab {
  /** Lookup key into `expmapPlots` (`<instrument>/<filter>`, expmapPlotKey). */
  key: string;
  /** What the tab reads (the filter name). */
  label: string;
  /** The instrument's name, shown once above its tabs; omitted when the field
   *  has a single instrument. */
  group?: string;
}

interface ExpmapBrowserProps {
  /** The tabs, in display order (the vertical tab list). */
  tabs: ExpmapTab[];
  /** tab key -> presigned expmap-plot PNG URL (missing ones render a placeholder). */
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
 * the tab list is focused. A field with several imaging instruments lists
 * each instrument's filters under its name.
 */
export const ExpmapBrowser: React.FC<ExpmapBrowserProps> = ({
  tabs,
  expmapPlots,
}) => {
  const keys = useMemo(() => tabs.map((t) => t.key), [tabs]);
  // The dependency is the key list's content, not the array's identity: the
  // parent rebuilds `tabs` on every render.
  const keySig = keys.join('|');
  const [current, setCurrent] = useState<string | null>(keys[0] ?? null);
  const [failedUrls, setFailedUrls] = useState<Set<string>>(new Set());
  const tabsRef = useRef<HTMLDivElement>(null);

  // Keep the selection valid if the tab list changes (field switch).
  useEffect(() => {
    setCurrent((c) => (c && keys.includes(c) ? c : keys[0] ?? null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keySig]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    if (keys.length === 0) return;
    const i = current ? keys.indexOf(current) : 0;
    const next = (i + (e.key === 'ArrowDown' ? 1 : -1) + keys.length) % keys.length;
    setCurrent(keys[next]);
  };

  // Scroll the active tab into view on keyboard navigation.
  useEffect(() => {
    tabsRef.current
      ?.querySelector('[data-active="true"]')
      ?.scrollIntoView({ block: 'nearest' });
  }, [current]);

  const currentTab = tabs.find((t) => t.key === current) ?? null;
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
        {tabs.map((t, i) => {
          const active = t.key === current;
          const startsGroup = t.group !== undefined && (i === 0 || tabs[i - 1].group !== t.group);
          return (
            <React.Fragment key={t.key}>
              {startsGroup && (
                <div className={`px-2.5 pb-1 text-[9px] font-semibold uppercase tracking-wider text-[#6f6880] ${i === 0 ? 'pt-1' : 'pt-3'}`}>
                  {t.group}
                </div>
              )}
              <button
                type="button"
                role="tab"
                aria-selected={active}
                data-active={active}
                onClick={() => setCurrent(t.key)}
                className={`w-full text-left font-mono text-[11px] px-2.5 py-1.5 transition-colors ${
                  active
                    ? 'bg-[#0d0b12] text-[#f1ecf6] font-semibold'
                    : 'text-[#8b8398] hover:text-[#b8aec6] hover:bg-[#1d1927]'
                }`}
              >
                {t.label}
              </button>
            </React.Fragment>
          );
        })}
      </div>

      {/* Plot well — the panel's height comes from the PLOT's intrinsic
          aspect at the well's width (the grid cell minus the tab rail), so
          the plot always fills the region edge-to-edge with no letterboxing.
          Square only as the placeholder fallback. */}
      <div
        className={`flex-1 min-w-0 flex items-center justify-center ${imgOk ? '' : 'aspect-square'}`}
        style={{ background: WELL_BG }}
      >
        {imgOk ? (
          // Presigned cross-origin PNG; plain <img> (see NircamFieldCard).
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={shownUrl as string}
            alt={`${[currentTab?.group, currentTab?.label].filter(Boolean).join(' ')} exposure map`}
            className="block w-full h-auto"
            onError={() =>
              setFailedUrls((prev) => new Set(prev).add(shownUrl as string))
            }
          />
        ) : (
          <div className="flex flex-col items-center gap-2 text-text-tertiary">
            <ImageIcon className="w-7 h-7 opacity-40" />
            <span className="text-xs">
              {tabs.length === 0
                ? 'No exposure maps available'
                : 'Plot not yet deployed for this filter'}
            </span>
          </div>
        )}
      </div>
    </div>
  );
};
