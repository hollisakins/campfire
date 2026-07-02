'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, Crosshair, RotateCcw } from 'lucide-react';
import {
  FitsImageViewer,
  fetchSciImage,
  STRETCH_MODES,
  COLORMAP_NAMES,
  type StretchMode,
  type ColormapName,
  type PixelReadout,
} from '@/lib/fits';

/**
 * Live in-browser FITS render of a canonical NIRCam exposure's SCI extension
 * (epic #261, N4) — a WebGL2 replacement for the pre-generated `_full.png`.
 * Range-fetches SCI through the admin `/api/nircam-fits` proxy and renders it
 * with adjustable stretch / colormap / display range and pan-zoom.
 */
export default function FitsExposureViewer({ fitsKey }: { fitsKey: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const viewerRef = useRef<FitsImageViewer | null>(null);
  const dragRef = useRef<{ x: number; y: number } | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stretch, setStretch] = useState<StretchMode>('linear');
  const [colormap, setColormap] = useState<ColormapName>('gray');
  const [range, setRange] = useState<{ lo: string; hi: string }>({ lo: '', hi: '' });
  const [readout, setReadout] = useState<PixelReadout | null>(null);

  // Create the viewer once (canvas is stable), plus a non-passive wheel handler
  // and a resize observer.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let viewer: FitsImageViewer;
    try {
      viewer = new FitsImageViewer(canvas);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'WebGL2 unavailable');
      setLoading(false);
      return;
    }
    viewerRef.current = viewer;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      viewer.zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });

    const ro = new ResizeObserver(() => viewer.invalidate());
    ro.observe(canvas);

    return () => {
      canvas.removeEventListener('wheel', onWheel);
      ro.disconnect();
      viewer.destroy();
      viewerRef.current = null;
    };
  }, []);

  // Fetch + load the SCI image whenever the key changes.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setReadout(null);
    const url = `/api/nircam-fits?key=${encodeURIComponent(fitsKey)}`;
    (async () => {
      try {
        const image = await fetchSciImage(url, controller.signal);
        if (controller.signal.aborted) return;
        viewer.setImage(image);
        setStretch(viewer.getStretchMode());
        setColormap(viewer.getColormap());
        const [lo, hi] = viewer.getDisplayRange();
        setRange({ lo: fmt(lo), hi: fmt(hi) });
        setLoading(false);
      } catch (e) {
        if (controller.signal.aborted) return;
        setError(e instanceof Error ? e.message : 'Failed to load FITS');
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [fitsKey]);

  const onStretch = useCallback((mode: StretchMode) => {
    setStretch(mode);
    viewerRef.current?.setStretchMode(mode);
  }, []);

  const onColormap = useCallback((name: ColormapName) => {
    setColormap(name);
    viewerRef.current?.setColormap(name);
  }, []);

  const commitRange = useCallback((lo: string, hi: string) => {
    const nlo = Number(lo);
    const nhi = Number(hi);
    if (Number.isFinite(nlo) && Number.isFinite(nhi) && nhi > nlo) {
      viewerRef.current?.setDisplayRange(nlo, nhi);
    }
  }, []);

  const onAuto = useCallback(() => {
    const v = viewerRef.current;
    if (!v) return;
    v.autoStretch();
    const [lo, hi] = v.getDisplayRange();
    setRange({ lo: fmt(lo), hi: fmt(hi) });
  }, []);

  const onReset = useCallback(() => viewerRef.current?.resetView(), []);

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = { x: e.clientX, y: e.clientY };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const v = viewerRef.current;
    if (!v) return;
    if (dragRef.current) {
      v.panByCss(e.clientX - dragRef.current.x, e.clientY - dragRef.current.y);
      dragRef.current = { x: e.clientX, y: e.clientY };
    }
    const rect = e.currentTarget.getBoundingClientRect();
    setReadout(v.readoutAt(e.clientX - rect.left, e.clientY - rect.top));
  };
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
  };

  return (
    <div className="flex h-full flex-col">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 text-sm">
        <label className="flex items-center gap-1">
          <span className="text-text-secondary text-xs uppercase tracking-wide">Stretch</span>
          <select
            value={stretch}
            onChange={(e) => onStretch(e.target.value as StretchMode)}
            className="rounded border border-border bg-card px-2 py-1 text-text-primary"
          >
            {STRETCH_MODES.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-1">
          <span className="text-text-secondary text-xs uppercase tracking-wide">Colormap</span>
          <select
            value={colormap}
            onChange={(e) => onColormap(e.target.value as ColormapName)}
            className="rounded border border-border bg-card px-2 py-1 text-text-primary"
          >
            {COLORMAP_NAMES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-1">
          <span className="text-text-secondary text-xs uppercase tracking-wide">Min</span>
          <input
            type="text"
            inputMode="decimal"
            value={range.lo}
            onChange={(e) => setRange((r) => ({ ...r, lo: e.target.value }))}
            onBlur={() => commitRange(range.lo, range.hi)}
            onKeyDown={(e) => e.key === 'Enter' && commitRange(range.lo, range.hi)}
            className="w-24 rounded border border-border bg-card px-2 py-1 font-mono text-xs text-text-primary"
          />
        </label>
        <label className="flex items-center gap-1">
          <span className="text-text-secondary text-xs uppercase tracking-wide">Max</span>
          <input
            type="text"
            inputMode="decimal"
            value={range.hi}
            onChange={(e) => setRange((r) => ({ ...r, hi: e.target.value }))}
            onBlur={() => commitRange(range.lo, range.hi)}
            onKeyDown={(e) => e.key === 'Enter' && commitRange(range.lo, range.hi)}
            className="w-24 rounded border border-border bg-card px-2 py-1 font-mono text-xs text-text-primary"
          />
        </label>

        <button
          onClick={onAuto}
          className="flex items-center gap-1 rounded border border-border px-2 py-1 text-text-secondary hover:bg-card-hover"
          title="ZScale auto-stretch"
        >
          <Crosshair className="h-3.5 w-3.5" /> Auto
        </button>
        <button
          onClick={onReset}
          className="flex items-center gap-1 rounded border border-border px-2 py-1 text-text-secondary hover:bg-card-hover"
          title="Reset pan/zoom to fit"
        >
          <RotateCcw className="h-3.5 w-3.5" /> Fit
        </button>
      </div>

      {/* Canvas */}
      <div className="relative min-h-0 flex-1 bg-black">
        <canvas
          ref={canvasRef}
          className="block h-full w-full touch-none"
          style={{ cursor: 'grab' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={() => setReadout(null)}
        />

        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/40">
            <Loader2 className="h-8 w-8 animate-spin text-white/80" />
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 flex items-center justify-center p-6 text-center">
            <p className="text-sm text-red-400">{error}</p>
          </div>
        )}
        {readout && !loading && !error && (
          <div className="pointer-events-none absolute bottom-2 left-2 rounded bg-black/70 px-2 py-1 font-mono text-xs text-white">
            x={readout.x} y={readout.y}{'  '}
            {Number.isFinite(readout.value) ? readout.value.toPrecision(5) : 'NaN'}
          </div>
        )}
      </div>
    </div>
  );
}

function fmt(n: number): string {
  if (!Number.isFinite(n)) return '0';
  const a = Math.abs(n);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return n.toExponential(3);
  return n.toPrecision(5);
}
