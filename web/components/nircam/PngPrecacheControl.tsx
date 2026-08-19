'use client';

import React, { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { Check, Download, HardDrive, Loader2, Trash2, X } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import type { SortState } from '@/lib/hooks/useTableUrlState';
import {
  cancelPngWarm,
  clearPngStore,
  ensurePngStoreReady,
  estimateStorage,
  getPngStoreStats,
  getPngStoreVersion,
  getStoredPng,
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

interface PngManifestEntry {
  id: number;
  bytes: number | null;
}

/**
 * GET /api/nircam-png/manifest — deliberately a route fetch, NOT a server
 * action: Next serializes server actions per client, and this scan as an
 * action queued ahead of the table's own fetch on every filter change,
 * freezing the table until the manifest landed. A fetch runs alongside the
 * actions and aborts cleanly when the filters change again.
 */
async function fetchPngManifest(
  query: string,
  signal?: AbortSignal,
): Promise<{ entries: PngManifestEntry[]; error?: string }> {
  const res = await fetch(`/api/nircam-png/manifest?${query}`, { signal });
  if (!res.ok) return { entries: [], error: `Manifest fetch failed (HTTP ${res.status})` };
  return res.json();
}

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
}

/**
 * "Pre-download all PNGs" control for the /admin/nircam list page (sits
 * between the filter bar and the table). Starts a warm of the display PNG of
 * every UNCACHED exposure in the CURRENT filtered set — in list order,
 * resumable, cancellable — into the durable IndexedDB store. The warm itself
 * is module state (lib/nircam-png-store.ts): this control only starts,
 * renders, and cancels it, so navigating into an exposure to begin inspecting
 * does NOT stop the download loop — coming back shows it still running.
 *
 * The button and the size hint are driven by the PNG manifest for the current
 * filters (getNircamExposurePngManifest): downloadable exposures with their
 * exact registry sizes. Cached ids are subtracted live, so the count is
 * "what this click would download", the bytes are the registry truth rather
 * than a per-image guess, and a fully-cached set greys the button out. The
 * cached total and the explicit clear button live here too; expiry is
 * otherwise automatic (PNG_STORE_TTL_MS after each image is stored).
 */
export function PngPrecacheControl({ filters, sort }: PngPrecacheControlProps) {
  useSyncExternalStore(subscribePngStore, getPngStoreVersion, getPngStoreVersion);
  const stats = getPngStoreStats();
  const warm = getWarmState();

  // Covers the manifest round trip between the click and the module warm
  // starting, so the button can't double-fire; everything after that renders
  // from the module's warm state.
  const [starting, setStarting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [headroom, setHeadroom] = useState<{ usage: number; quota: number } | null>(null);
  // Downloadable exposures + exact sizes for the current filters; null while
  // loading or after a failed fetch (the button then falls back to a
  // fetch-at-click flow rather than wedging).
  const [manifest, setManifest] = useState<PngManifestEntry[] | null>(null);
  const [manifestLoading, setManifestLoading] = useState(false);

  useEffect(() => {
    ensurePngStoreReady();
    estimateStorage().then(setHeadroom);
    // Deliberately NO abort on unmount: the warm keeps running while the
    // operator steps into the triage flow — that's the point of warming.
  }, []);

  const manifestQuery = useMemo(() => {
    const sp = new URLSearchParams();
    for (const key of ['field', 'filter', 'detector', 'review', 'stage', 'correction'] as const) {
      if (filters[key]) sp.set(key, filters[key]);
    }
    sp.set('sort', sort.column);
    sp.set('dir', sort.direction);
    return sp.toString();
  }, [filters, sort]);

  useEffect(() => {
    const controller = new AbortController();
    setManifest(null);
    setManifestLoading(true);
    fetchPngManifest(manifestQuery, controller.signal).then(
      (res) => {
        if (controller.signal.aborted) return;
        setManifestLoading(false);
        if (!res.error) setManifest(res.entries);
      },
      () => { if (!controller.signal.aborted) setManifestLoading(false); },
    );
    return () => controller.abort();
  }, [manifestQuery]);

  // Live split of the manifest against the store: recomputed on every store
  // change (hydration, each warmed image, clear), so the count is always
  // "what this click would download" and flips to the all-cached state the
  // moment the last image lands.
  const uncached = useMemo(
    () => manifest?.filter((e) => !getStoredPng(e.id)) ?? null,
    // getPngStoreVersion() ties the memo to store changes surfaced by the
    // useSyncExternalStore subscription above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [manifest, getPngStoreVersion()],
  );
  let knownBytes = 0;
  let unknownSizes = 0;
  if (uncached) {
    for (const e of uncached) {
      if (e.bytes != null) knownBytes += e.bytes;
      else unknownSizes++;
    }
  }
  const uncachedCount = uncached?.length ?? null;
  const allCached = manifest !== null && manifest.length > 0 && uncachedCount === 0;

  const startWarm = async () => {
    setLocalError(null);
    setStarting(true);
    try {
      let ids = uncached?.map((e) => e.id) ?? null;
      if (ids === null) {
        // Manifest fetch failed or hasn't landed — get one now so the click
        // still works (startPngWarm skips cached ids on its own).
        const res = await fetchPngManifest(manifestQuery);
        if (res.error) {
          setLocalError(res.error);
          return;
        }
        setManifest(res.entries);
        ids = res.entries.map((e) => e.id);
      }
      if (ids.length === 0) return;
      setStarting(false);
      // Module-scoped: outlives this component; result surfaces via
      // getWarmState().lastResult whenever a control is mounted to show it.
      await startPngWarm(ids);
      estimateStorage().then(setHeadroom);
    } finally {
      setStarting(false);
    }
  };

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
          {uncached !== null && uncachedCount! > 0 && (
            <span
              className="text-xs text-text-tertiary tabular-nums whitespace-nowrap"
              title="Exact size of the not-yet-cached images in the current filtered set (from the storage registry), against this browser's storage headroom."
            >
              {unknownSizes > 0 ? '≥' : ''}{fmtBytes(knownBytes)}
              {free != null && <> · {fmtBytes(free)} free</>}
            </span>
          )}
          <Button
            size="sm"
            onClick={startWarm}
            disabled={
              !stats.hydrated || starting || manifestLoading ||
              (manifest !== null && uncachedCount === 0)
            }
            title={allCached
              ? 'Every image in the current filtered set is already cached in this browser.'
              : 'Download the display PNG of every not-yet-cached exposure matching the current filters into this browser, so stepping through the queue never waits on the network. Keeps running if you start inspecting.'}
          >
            {!stats.hydrated || starting || manifestLoading ? (
              <><Loader2 className="w-3.5 h-3.5 mr-1 animate-spin" /> Pre-download PNGs</>
            ) : allCached ? (
              <><Check className="w-3.5 h-3.5 mr-1" /> All PNGs cached</>
            ) : uncachedCount !== null ? (
              <><Download className="w-3.5 h-3.5 mr-1" /> Pre-download {uncachedCount} PNG{uncachedCount !== 1 ? 's' : ''}</>
            ) : (
              // Manifest fetch failed — the click fetches one and proceeds.
              <><Download className="w-3.5 h-3.5 mr-1" /> Pre-download PNGs</>
            )}
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
