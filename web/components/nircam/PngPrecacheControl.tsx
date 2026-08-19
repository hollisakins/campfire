'use client';

import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { Download, HardDrive, Loader2, Trash2, X } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { getNircamExposureIds } from '@/lib/actions/nircam-exposures';
import type { SortState } from '@/lib/hooks/useTableUrlState';
import {
  cancelPngWarm,
  clearPngStore,
  ensurePngStoreReady,
  estimateStorage,
  getPngStoreStats,
  getPngStoreVersion,
  getWarmState,
  startPngWarm,
  subscribePngStore,
  PNG_STORE_TTL_MS,
  type PngWarmResult,
} from '@/lib/nircam-png-store';

function fmtBytes(n: number): string {
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB', 'PB']) {
    if (Math.abs(v) < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v} B`;
}

// Rough per-exposure budget for the pre-warm size hint (full-res PNGs run
// ~5–6 MB); the progress readout shows real bytes once the warm is running.
const EST_BYTES_PER_EXPOSURE = 6 * 1024 * 1024;

function resultNotice(r: PngWarmResult): string | null {
  if (r.error) return r.error;
  if (r.aborted) {
    return `Stopped — ${r.done - r.failed}/${r.total} cached so far (resume any time).`;
  }
  if (r.failed > 0) {
    return `Done with ${r.failed} failed download${r.failed !== 1 ? 's' : ''} — run again to retry them.`;
  }
  return null;
}

interface PngPrecacheControlProps {
  /** The list page's active (debounced) filter values, by URL param key. */
  filters: Record<string, string>;
  sort: SortState;
  /** Matching-exposure count from the table query (sizes the warm up front). */
  total: number;
}

/**
 * "Pre-download all PNGs" control for the /admin/nircam list page (sits
 * between the filter bar and the table). Starts a warm of the display PNG of
 * every exposure in the CURRENT filtered set — in list order, resumable,
 * cancellable — into the durable IndexedDB store. The warm itself is module
 * state (lib/nircam-png-store.ts): this control only starts, renders, and
 * cancels it, so navigating into an exposure to begin inspecting does NOT
 * stop the download loop — coming back shows it still running. The cached
 * total and the explicit clear button live here too; expiry is otherwise
 * automatic (PNG_STORE_TTL_MS after each image is stored).
 */
export function PngPrecacheControl({ filters, sort, total }: PngPrecacheControlProps) {
  useSyncExternalStore(subscribePngStore, getPngStoreVersion, getPngStoreVersion);
  const stats = getPngStoreStats();
  const warm = getWarmState();

  // Covers the ids round trip between the click and the module warm starting,
  // so the button can't double-fire; everything after that renders from the
  // module's warm state.
  const [starting, setStarting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [headroom, setHeadroom] = useState<{ usage: number; quota: number } | null>(null);

  useEffect(() => {
    ensurePngStoreReady();
    estimateStorage().then(setHeadroom);
    // Deliberately NO abort on unmount: the warm keeps running while the
    // operator steps into the triage flow — that's the point of warming.
  }, []);

  const startWarm = async () => {
    setLocalError(null);
    setStarting(true);
    try {
      const { ids, error } = await getNircamExposureIds({
        field: filters.field || undefined,
        filter: filters.filter || undefined,
        detector: filters.detector || undefined,
        reviewStatus: filters.review || undefined,
        stage: filters.stage || undefined,
        correction: filters.correction || undefined,
        sortColumn: sort.column,
        sortDirection: sort.direction,
      });
      if (error || ids.length === 0) {
        setLocalError(error ?? 'No exposures match the current filters.');
        return;
      }
      setStarting(false);
      // Module-scoped: outlives this component; result surfaces via
      // getWarmState().lastResult whenever a control is mounted to show it.
      await startPngWarm(ids);
      estimateStorage().then(setHeadroom);
    } finally {
      setStarting(false);
    }
  };

  const estNeeded = total * EST_BYTES_PER_EXPOSURE;
  const free = headroom ? Math.max(0, headroom.quota - headroom.usage) : null;
  const ttlHours = Math.round(PNG_STORE_TTL_MS / 3_600_000);
  const notice = localError ?? (warm.lastResult ? resultNotice(warm.lastResult) : null);

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mb-4 px-4 py-2.5 rounded-lg border border-border bg-card text-sm">
      <div className="flex items-center gap-2 text-text-secondary">
        <HardDrive className="w-4 h-4 shrink-0" />
        {!stats.hydrated ? (
          <span>Checking image cache…</span>
        ) : stats.count > 0 ? (
          <span className="tabular-nums">
            {stats.count} image{stats.count !== 1 ? 's' : ''} cached ({fmtBytes(stats.bytes)})
            <span className="text-text-tertiary"> · auto-clears {ttlHours} h after download</span>
          </span>
        ) : (
          <span>No images cached</span>
        )}
      </div>

      {warm.running && warm.progress ? (
        <div className="flex items-center gap-3 flex-1 min-w-48">
          <div className="h-2 flex-1 min-w-24 rounded-full bg-surface-2 overflow-hidden">
            <div
              className="h-full rounded-full bg-primary transition-[width]"
              style={{ width: `${warm.progress.total > 0 ? (warm.progress.done / warm.progress.total) * 100 : 0}%` }}
            />
          </div>
          <span className="text-xs tabular-nums text-text-secondary whitespace-nowrap">
            {warm.progress.done}/{warm.progress.total} · {fmtBytes(warm.progress.bytes)}
            {warm.progress.failed > 0 && <> · {warm.progress.failed} failed</>}
          </span>
          <Button size="sm" variant="secondary" onClick={cancelPngWarm}>
            <X className="w-3.5 h-3.5 mr-1" /> Stop
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-3 ml-auto">
          {notice && <span className="text-xs text-text-secondary">{notice}</span>}
          {total > 0 && (
            <span
              className="text-xs text-text-tertiary tabular-nums whitespace-nowrap"
              title="Rough size of the current filtered set at ~6 MB per exposure, against this browser's storage headroom (already-cached images are skipped, so re-runs only fetch what's missing)."
            >
              ~{fmtBytes(estNeeded)}{free != null && <> · {fmtBytes(free)} free</>}
            </span>
          )}
          <Button
            size="sm"
            onClick={startWarm}
            disabled={!stats.hydrated || starting || total === 0}
            title="Download the display PNG of every exposure matching the current filters into this browser, so stepping through the queue never waits on the network. Keeps running if you start inspecting."
          >
            {!stats.hydrated || starting ? (
              <Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" />
            ) : (
              <Download className="w-3.5 h-3.5 mr-1" />
            )}
            Pre-download {total > 0 ? total : ''} PNG{total !== 1 ? 's' : ''}
          </Button>
          {stats.count > 0 && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                setLocalError(null);
                clearPngStore().then(() => estimateStorage().then(setHeadroom));
              }}
              title="Delete all cached exposure images from this browser now."
            >
              <Trash2 className="w-3.5 h-3.5 mr-1" /> Clear
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
