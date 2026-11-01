'use client';

import React, { useEffect, useRef } from 'react';
import {
  FitsImageViewer,
  fetchSciImage, fetchCompressedImage, subtractImages, resolveFitsSource,
  type SciImage,
  type StretchMode,
  type ColormapName,
} from '@/lib/fits';

/** What the canvas draws for a NIRSpec rate exposure: the rate SCI, the 2-D
 * master background (`MBKG2D` of the `_mbkg.fits`, DN/s like SCI), or their
 * difference. NIRCam and plain rate hosts only ever draw `rate`. */
export type FitsView = 'rate' | 'mbkg' | 'rate_minus_mbkg';

// Decoded images (~16 MB each) of hosts that toggle views, so a toggle does not
// refetch: rate + master background + difference for the current exposure and
// the previous one. Hosts without a master background cache nothing (as before).
const IMAGE_CACHE_SIZE = 6;
const imageCache = new Map<string, SciImage>();

function cacheGet(key: string): SciImage | undefined {
  const hit = imageCache.get(key);
  if (hit) { imageCache.delete(key); imageCache.set(key, hit); }
  return hit;
}

function cacheSet(key: string, image: SciImage): SciImage {
  imageCache.set(key, image);
  while (imageCache.size > IMAGE_CACHE_SIZE) imageCache.delete(imageCache.keys().next().value!);
  return image;
}

async function loadSci(key: string, signal: AbortSignal): Promise<SciImage> {
  const hit = cacheGet(`sci:${key}`);
  if (hit) return hit;
  const image = await fetchSciImage(await resolveFitsSource(key, signal), signal);
  return cacheSet(`sci:${key}`, image);
}

async function loadMbkg(key: string, signal: AbortSignal): Promise<SciImage> {
  const hit = cacheGet(`mbkg:${key}`);
  if (hit) return hit;
  const image = await fetchCompressedImage(await resolveFitsSource(key, signal), 'MBKG2D', signal);
  return cacheSet(`mbkg:${key}`, image);
}

async function loadView(
  fitsKey: string, mbkgKey: string | undefined, view: FitsView, signal: AbortSignal,
): Promise<SciImage> {
  if (!mbkgKey) return fetchSciImage(await resolveFitsSource(fitsKey, signal), signal);
  if (view === 'rate') return loadSci(fitsKey, signal);
  if (view === 'mbkg') return loadMbkg(mbkgKey, signal);
  const key = `diff:${fitsKey}:${mbkgKey}`;
  const hit = cacheGet(key);
  if (hit) return hit;
  const [rate, mbkg] = await Promise.all([loadSci(fitsKey, signal), loadMbkg(mbkgKey, signal)]);
  return cacheSet(key, subtractImages(rate, mbkg));
}

/**
 * A native-resolution WebGL2 render of an exposure's SCI extension (or, for a
 * NIRSpec rate exposure, its 2-D master background or rate minus it), sized
 * to the image's pixel grid so a host can CSS-transform it exactly like the
 * legacy `<img>` PNG (epic #261, N5). Fully controlled: stretch / colormap /
 * display range come in as props; the component owns only the fetch + GL. It
 * ZScales on load and reports the resulting range via `onLoad` so the host can
 * seed its min/max controls. `pointer-events-none` so the overlay above it
 * receives all interaction.
 */
export interface FitsCanvasLoad {
  width: number;
  height: number;
  vmin: number;
  vmax: number;
}

interface Props {
  fitsKey: string;
  /** Canonical `nirspec_mbkg` key; needed for the two master-background views. */
  mbkgKey?: string;
  view?: FitsView;
  width: number;
  height: number;
  stretch: StretchMode;
  colormap: ColormapName;
  /** Display interval; when `vmax <= vmin` the on-load ZScale range is kept. */
  vmin: number;
  vmax: number;
  onLoad?: (info: FitsCanvasLoad) => void;
  onError?: (msg: string) => void;
}

export default function FitsCanvas({
  fitsKey, mbkgKey, view = 'rate', width, height, stretch, colormap, vmin, vmax, onLoad, onError,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const viewerRef = useRef<FitsImageViewer | null>(null);

  // Stable callback refs so the fetch effect doesn't re-run on parent re-render.
  const onLoadRef = useRef(onLoad);
  const onErrorRef = useRef(onError);
  onLoadRef.current = onLoad;
  onErrorRef.current = onError;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    try {
      viewerRef.current = new FitsImageViewer(canvas, { interactive: false });
    } catch (e) {
      onErrorRef.current?.(e instanceof Error ? e.message : 'WebGL2 unavailable');
      return;
    }
    return () => {
      viewerRef.current?.destroy();
      viewerRef.current = null;
    };
  }, []);

  // Fetch + load the image whenever the key or the view changes.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const controller = new AbortController();
    (async () => {
      try {
        const image = await loadView(fitsKey, mbkgKey, view, controller.signal);
        if (controller.signal.aborted) return;
        viewer.setImage(image);          // auto-ZScales
        viewer.setStretchMode(stretch);
        viewer.setColormap(colormap);
        const [lo, hi] = viewer.getDisplayRange();
        onLoadRef.current?.({ width: image.width, height: image.height, vmin: lo, vmax: hi });
      } catch (e) {
        if (controller.signal.aborted) return;
        onErrorRef.current?.(e instanceof Error ? e.message : 'Failed to load FITS');
      }
    })();
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitsKey, mbkgKey, view]);

  useEffect(() => { viewerRef.current?.setStretchMode(stretch); }, [stretch]);
  useEffect(() => { viewerRef.current?.setColormap(colormap); }, [colormap]);
  useEffect(() => {
    if (vmax > vmin) viewerRef.current?.setDisplayRange(vmin, vmax);
  }, [vmin, vmax]);

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      className="absolute inset-0 pointer-events-none"
      style={{ width, height, imageRendering: 'pixelated' }}
    />
  );
}
