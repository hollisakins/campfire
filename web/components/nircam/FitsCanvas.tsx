'use client';

import React, { useEffect, useRef } from 'react';
import {
  FitsImageViewer,
  fetchSciImage, resolveFitsSource,
  type StretchMode,
  type ColormapName,
} from '@/lib/fits';

/**
 * A native-resolution WebGL2 render of a NIRCam exposure's SCI extension, sized
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
  fitsKey, width, height, stretch, colormap, vmin, vmax, onLoad, onError,
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

  // Fetch + load the SCI image whenever the key changes.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const controller = new AbortController();
    (async () => {
      try {
        const source = await resolveFitsSource(fitsKey, controller.signal);
        const image = await fetchSciImage(source, controller.signal);
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
  }, [fitsKey]);

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
