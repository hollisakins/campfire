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
import { isTrilogyComposite, trilogyComposite } from '@fitsgl/core/react';
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

  // Apply precomputed trilogy levels (with the live knobs) when the trilogy
  // curve is active. A weighted composite takes one stats per participating
  // band, in `trilogyComposite` order — the same order `deriveViewerConfig`
  // builds the multiband view from, so it matches the viewer's band managers.
  // The try/catch absorbs the one-frame race where the viewer's source hasn't
  // caught up with the state yet (the readyTick pass re-applies).
  useEffect(() => {
    if (!trilogy || !state) return;
    const v = handleRef.current?.getViewer();
    if (!v) return;
    try {
      if (isTrilogyComposite(state)) {
        const comp = trilogyComposite(state);
        const stats = comp.map((e) => bandByName.get(e.band)?.trilogy);
        if (stats.every((s): s is NonNullable<typeof s> => !!s)) {
          v.applyTrilogy(stats, state.trilogyParams);
        }
      } else {
        const stat = bandByName.get(state.band)?.trilogy;
        if (stat) v.applyTrilogy(stat, state.trilogyParams);
      }
    } catch {
      // source/state transient mismatch — the next ready/frame pass re-applies
    }
  }, [trilogy, state, bandByName, handleRef, readyTick]);

  // Push per-band trilogy weights imperatively (like the stretch handles). The
  // viewer deliberately EXCLUDES the multiband weights from its source-rebuild
  // signature (`viewSignature` in @fitsgl/core) — only a change to the *set* of
  // bands forces a `setSource` — so a pure weight tweak on the same band set never
  // reaches the GPU through the controlled `deriveViewerConfig` config. Mirror
  // FitsGL's own <FitsExplorer> shell and re-push the weights here. `setBandWeights`
  // requires the count to match the resident band set, so guard on the `multiband`
  // source mode and absorb the one-frame race where a band-set change hasn't
  // rebuilt the source yet (that pending `setSource` then carries the new weights).
  useEffect(() => {
    if (!state || !isTrilogyComposite(state)) return;
    const v = handleRef.current?.getViewer();
    if (!v || v.sourceMode !== 'multiband') return;
    try {
      v.setBandWeights(trilogyComposite(state).map((e) => e.weight));
    } catch {
      // band set mid-rebuild — the pending source swap carries the right weights
    }
  }, [state, handleRef, readyTick]);

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

  /** Simple-RGB shared limits: one [min, max] pushed to all three channels — no
   *  independent per-band stretch (that's what keeps a simple composite
   *  interpretable; the bands share flux units, so shared cuts are physical). */
  const setSharedHandle = useCallback(
    (min: number, max: number) => {
      const v = handleRef.current?.getViewer();
      if (!v) return;
      for (const role of ['r', 'g', 'b'] as const) v.setChannelStretch(role, min, max);
      setRanges((prev) => ({
        ...prev,
        r: { min, max },
        g: { min, max },
        b: { min, max },
      }));
    },
    [handleRef],
  );

  /** The one range shown by the simple-RGB shared control: after any shared op
   *  all channels agree; before one (fresh source auto-stretch), merge to the
   *  envelope (min of mins / max of maxes) so every band's data stays visible. */
  const sharedRange = useMemo<{ min: number; max: number } | null>(() => {
    if (!state || state.mode !== 'rgb') return null;
    const rs = (['r', 'g', 'b'] as const).map(
      (role) => ranges[role] ?? fallbackRange(bandByName.get(state.rgb[role]) ?? bands[0]),
    );
    return {
      min: Math.min(...rs.map((r) => r.min)),
      max: Math.max(...rs.map((r) => r.max)),
    };
  }, [state, ranges, bandByName, bands]);

  const applyPreset = useCallback(
    async (preset: LimitPreset) => {
      const h = handleRef.current;
      const v = h?.getViewer();
      if (!h || !v || !state) return;
      // Simple RGB shares one range across channels; merge per-channel results
      // to their envelope so no band's data is cut off.
      const shareRgb = (per: Array<{ min: number; max: number } | undefined>): void => {
        const got = per.filter((r): r is { min: number; max: number } => !!r);
        if (got.length === 0) return;
        setSharedHandle(Math.min(...got.map((r) => r.min)), Math.max(...got.map((r) => r.max)));
      };
      if (preset === 'zscale') {
        if (state.mode === 'rgb') {
          shareRgb(
            (['r', 'g', 'b'] as const).map((role) => {
              const z = bandByName.get(state.rgb[role])?.zscale;
              return z ? { min: z[0], max: z[1] } : undefined;
            }),
          );
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
        shareRgb(
          (['r', 'g', 'b'] as const).map((role) => {
            const r = res[role];
            return r ? { min: r[0], max: r[1] } : undefined;
          }),
        );
      } else {
        setRanges((prev) => ({ ...prev, single: { min: res.min, max: res.max } }));
      }
    },
    [handleRef, state, bandByName, setSharedHandle],
  );

  return {
    channels,
    hasZscale,
    hasTrilogy,
    sharedRange,
    setHandle,
    setSharedHandle,
    applyPreset,
    seedFromFrame,
  };
}
