'use client';

/**
 * useDisplayStretch (epic #337, Phase 4.5) — the imperative bridge between the
 * Display panel and the FitsGL `CoreViewer`. The band, stretch *mode*, colormap
 * and north-up ride the controlled `deriveViewerConfig` path; the black/white
 * points do NOT (deriveViewerConfig deliberately omits stretch ranges — a
 * high-frequency drag shouldn't churn React/reload), so they are driven here
 * through the ref handle, exactly as `<FitsExplorer>` does.
 *
 * Responsibilities:
 *  - expose the active channel(s) (1 single, or R/G/B) with each band's precomputed
 *    histogram + current [min,max] for the fine-adjust control;
 *  - apply the limit presets (auto / zscale / minmax / 99.5%);
 *  - apply the precomputed trilogy levels when the trilogy curve is selected;
 *  - seed the handles from the viewer's auto-stretch after a band/mode switch
 *    (called once per pending change from the parent's `onFrame`).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ExplorerBand, ExplorerState, FitsViewerHandle } from '@fitsgl/core/react';

export type ChannelKey = 'single' | 'r' | 'g' | 'b';
export type LimitPreset = 'auto' | 'zscale' | 'minmax' | 'p995';

const RGB_TINT: Record<'r' | 'g' | 'b', string> = { r: '#ef4444', g: '#22c55e', b: '#3b82f6' };

export interface DisplayChannel {
  key: ChannelKey;
  band: ExplorerBand;
  histogram?: { counts: number[] | Float32Array; lo: number; hi: number };
  min: number;
  max: number;
  /** Bar tint for the histogram (accent in single mode, channel colour in RGB). */
  color?: string;
}

interface UseDisplayStretchArgs {
  handleRef: React.RefObject<FitsViewerHandle | null>;
  bands: ExplorerBand[];
  /** Null until the dataset's default view has been derived. */
  state: ExplorerState | null;
  /** Bumped on viewer (re)ready so seeding re-arms after a band reload. */
  readyTick: number;
}

/** Fallback range for a channel before the viewer reports a real stretch. */
function fallbackRange(band: ExplorerBand): { min: number; max: number } {
  if (band.zscale) return { min: band.zscale[0], max: band.zscale[1] };
  if (band.histogram) return { min: band.histogram.lo, max: band.histogram.hi };
  return { min: 0, max: 1 };
}

export function useDisplayStretch({ handleRef, bands, state, readyTick }: UseDisplayStretchArgs) {
  const bandByName = useMemo(() => {
    const m = new Map<string, ExplorerBand>();
    for (const b of bands) m.set(b.name, b);
    return m;
  }, [bands]);

  // Viewer-reported (or user-dragged) ranges per channel key. Absent → fallback.
  const [ranges, setRanges] = useState<Partial<Record<ChannelKey, { min: number; max: number }>>>({});

  const trilogy = state?.stretch === 'trilogy';

  // The channel(s) currently driving the display.
  const activeRoles = useMemo<Array<{ key: ChannelKey; band: ExplorerBand | undefined; color?: string }>>(() => {
    if (!state) return [];
    if (state.mode === 'rgb') {
      return (['r', 'g', 'b'] as const).map((role) => ({
        key: role,
        band: bandByName.get(state.rgb[role]),
        color: RGB_TINT[role],
      }));
    }
    return [{ key: 'single', band: bandByName.get(state.band) }];
  }, [state, bandByName]);

  const channels = useMemo<DisplayChannel[]>(() => {
    return activeRoles
      .filter((r): r is { key: ChannelKey; band: ExplorerBand; color?: string } => !!r.band)
      .map(({ key, band, color }) => {
        const r = ranges[key] ?? fallbackRange(band);
        return { key, band, histogram: band.histogram, min: r.min, max: r.max, color };
      });
  }, [activeRoles, ranges]);

  const hasZscale = channels.length > 0 && channels.every((c) => !!c.band.zscale);
  const hasTrilogy = channels.length > 0 && channels.every((c) => !!c.band.trilogy);

  // Re-seed the handles whenever the active source identity changes or the viewer
  // reloads (readyTick). The actual read happens on the next drawn frame.
  const channelIdentity = !state
    ? 'none'
    : state.mode === 'rgb'
      ? `rgb:${state.rgb.r}|${state.rgb.g}|${state.rgb.b}`
      : `single:${state.band}`;
  const pendingSeed = useRef(true);
  useEffect(() => {
    pendingSeed.current = true;
  }, [channelIdentity, readyTick]);

  /** Read the viewer's applied stretch once, after a fresh source auto-stretches. */
  const seedFromFrame = useCallback(() => {
    if (!pendingSeed.current || trilogy) return;
    const v = handleRef.current?.getViewer();
    if (!v) return;
    const ds = v.getDisplayState();
    pendingSeed.current = false;
    if (ds.mode === 'rgb') {
      setRanges({
        r: { min: ds.channelMin[0], max: ds.channelMax[0] },
        g: { min: ds.channelMin[1], max: ds.channelMax[1] },
        b: { min: ds.channelMin[2], max: ds.channelMax[2] },
      });
    } else {
      setRanges({ single: { min: ds.stretchMin, max: ds.stretchMax } });
    }
  }, [handleRef, trilogy]);

  // Apply precomputed trilogy levels when the trilogy curve is active.
  useEffect(() => {
    if (!trilogy || !state) return;
    const v = handleRef.current?.getViewer();
    if (!v) return;
    if (state.mode === 'rgb') {
      const stats = (['r', 'g', 'b'] as const).map((role) => bandByName.get(state.rgb[role])?.trilogy);
      if (stats.every((s): s is NonNullable<typeof s> => !!s)) v.applyTrilogy(stats);
    } else {
      const stat = bandByName.get(state.band)?.trilogy;
      if (stat) v.applyTrilogy(stat);
    }
  }, [trilogy, state, bandByName, handleRef, readyTick]);

  /** Fine-adjust: push one channel's black/white point to the GPU + store it. */
  const setHandle = useCallback(
    (key: ChannelKey, min: number, max: number) => {
      const v = handleRef.current?.getViewer();
      if (!v) return;
      if (key === 'single') v.setStretch(min, max);
      else v.setChannelStretch(key, min, max);
      setRanges((prev) => ({ ...prev, [key]: { min, max } }));
    },
    [handleRef],
  );

  const applyPreset = useCallback(
    async (preset: LimitPreset) => {
      const h = handleRef.current;
      const v = h?.getViewer();
      if (!h || !v || !state) return;
      if (preset === 'zscale') {
        if (state.mode === 'rgb') {
          const next: Partial<Record<ChannelKey, { min: number; max: number }>> = {};
          for (const role of ['r', 'g', 'b'] as const) {
            const z = bandByName.get(state.rgb[role])?.zscale;
            if (z) {
              v.setChannelStretch(role, z[0], z[1]);
              next[role] = { min: z[0], max: z[1] };
            }
          }
          setRanges((prev) => ({ ...prev, ...next }));
        } else {
          const z = bandByName.get(state.band)?.zscale;
          if (z) {
            v.setStretch(z[0], z[1]);
            setRanges((prev) => ({ ...prev, single: { min: z[0], max: z[1] } }));
          }
        }
        return;
      }
      // auto / minmax / 99.5% all go through the viewer's percentile autostretch.
      const pctl: Record<Exclude<LimitPreset, 'zscale'>, [number | undefined, number | undefined]> = {
        auto: [undefined, undefined],
        minmax: [0, 1],
        p995: [0.0025, 0.9975],
      };
      const [pLo, pHi] = pctl[preset];
      const res = await v.autoStretch(pLo, pHi);
      if (!res) return;
      if (res.mode === 'rgb') {
        const next: Partial<Record<ChannelKey, { min: number; max: number }>> = {};
        for (const role of ['r', 'g', 'b'] as const) {
          const r = res[role];
          if (r) next[role] = { min: r[0], max: r[1] };
        }
        setRanges((prev) => ({ ...prev, ...next }));
      } else {
        setRanges((prev) => ({ ...prev, single: { min: res.min, max: res.max } }));
      }
    },
    [handleRef, state, bandByName],
  );

  return { channels, hasZscale, hasTrilogy, setHandle, applyPreset, seedFromFrame };
}
