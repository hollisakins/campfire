'use client';

import React, { useEffect, useRef } from 'react';
import {
  FitsImageViewer,
  fetchSciImage, fetchCompressedImage, subtractImages, resolveFitsSource, zscaleLimits,
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

// One display range for all three views of an exposure: the ZScale of the rate
// image. Each view's own ZScale would put the master background and the
// residual on different scales from the rate, so the views could not be
// compared by eye.
const rateLimitsCache = new Map<string, [number, number] | null>();

async function rateLimits(fitsKey: string, signal: AbortSignal): Promise<[number, number] | null> {
  if (rateLimitsCache.has(fitsKey)) return rateLimitsCache.get(fitsKey)!;
  const limits = zscaleLimits((await loadSci(fitsKey, signal)).data);
  rateLimitsCache.set(fitsKey, limits);
  while (rateLimitsCache.size > IMAGE_CACHE_SIZE) rateLimitsCache.delete(rateLimitsCache.keys().next().value!);
  return limits;
}

/**
 * A native-resolution WebGL2 render of an exposure's SCI extension (or, for a
 * NIRSpec rate exposure, its 2-D master background or rate minus it), sized
 * to the image's pixel grid so a host can CSS-transform it exactly like the
 * legacy `<img>` PNG (epic #261, N5). Fully controlled: stretch / colormap /
 * display range come in as props; the component owns only the fetch + GL. It
 * ZScales on load and reports the resulting range via `onLoad` so the host can
 * seed its min/max controls. With a master background the three views share
 * the rate's ZScale, reported once per exposure: switching views keeps the
 * host's range (automatic or set by hand). `pointer-events-none` so the
 * overlay above it receives all interaction.
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
  /** A load failed (its message), or null once a view switch loaded after one failed. */
  onError?: (msg: string | null) => void;
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
  // The host's current range, read when a view switch reloads the image, and
  // the exposure whose range was last reported to it.
  const rangeRef = useRef<[number, number]>([vmin, vmax]);
  rangeRef.current = [vmin, vmax];
  const reportedRef = useRef<string | null>(null);

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
      // the decoded images (~16 MB each) are for toggling views while the page is open
      imageCache.clear();
      rateLimitsCache.clear();
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
        const shared = mbkgKey ? await rateLimits(fitsKey, controller.signal) : null;
        if (controller.signal.aborted) return;
        viewer.setImage(image);          // auto-ZScales
        viewer.setStretchMode(stretch);
        viewer.setColormap(colormap);
        const exposure = `${fitsKey}|${mbkgKey ?? ''}`;
        const [hostLo, hostHi] = rangeRef.current;
        if (mbkgKey && reportedRef.current === exposure && hostHi > hostLo) {
          // a view switch of the same exposure: keep the host's range (onLoad would reset
          // it); clear an error an earlier view left
          viewer.setDisplayRange(hostLo, hostHi);
          onErrorRef.current?.(null);
          return;
        }
        if (shared) viewer.setDisplayRange(shared[0], shared[1]);
        const [lo, hi] = viewer.getDisplayRange();
        reportedRef.current = exposure;
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
