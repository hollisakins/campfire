'use client';

import React, { useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { fetchS2dCell, HduNotFoundError, paintToImageData, type S2dCell } from '@/lib/fits';
import type { StretchMode } from '@/lib/fits';
import type { ColormapName } from '@/lib/fits';
import type { SpectrumExposure } from '@/lib/types';

interface Props {
  exposure: SpectrumExposure | null;
  /** Shared [vmin, vmax] across the whole grid (null until the page computes it). */
  range: [number, number] | null;
  stretch: StretchMode;
  colormap: ColormapName;
  /** Whether to draw the background-subtracted view (S2D_BKGSUB_SCI) vs S2D_SCI. */
  bkgsub: boolean;
  /** Report the fetched pixels up so the page can compute the shared stretch. */
  onData: (id: number, data: Float32Array) => void;
  /** Report that this cell reached a terminal state (success OR absent/error) so
   * the page can compute the shared stretch once ALL cells have settled — not
   * only once all have succeeded (absent cells never call onData). */
  onSettled: (id: number) => void;
}

/** Median across the dispersion axis (columns) → one value per row. Mirrors the
 * PDF's `np.nanmedian(data, axis=1)`. NaNs dropped per row. */
function crossDispersionProfile(data: Float32Array, width: number, height: number): Float32Array {
  const prof = new Float32Array(height);
  const buf: number[] = [];
  for (let r = 0; r < height; r++) {
    buf.length = 0;
    for (let c = 0; c < width; c++) {
      const v = data[r * width + c]!;
      if (Number.isFinite(v)) buf.push(v);
    }
    if (buf.length === 0) { prof[r] = NaN; continue; }
    buf.sort((a, b) => a - b);
    const m = buf.length >> 1;
    prof[r] = buf.length % 2 ? buf[m]! : (buf[m - 1]! + buf[m]!) / 2;
  }
  return prof;
}

function ProfileSvg({ data, width, height }: { data: Float32Array; width: number; height: number }) {
  const prof = crossDispersionProfile(data, width, height);
  let lo = Infinity, hi = -Infinity;
  for (const v of prof) if (Number.isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!Number.isFinite(lo) || hi <= lo) return null;
  const pts: string[] = [];
  for (let r = 0; r < height; r++) {
    const v = prof[r]!;
    if (!Number.isFinite(v)) continue;
    const x = ((v - lo) / (hi - lo)) * 100;
    const y = 100 - (r / (height - 1)) * 100; // origin='lower': row 0 at bottom
    pts.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="w-10 h-full">
      <polyline points={pts.join(' ')} fill="none" stroke="currentColor" strokeWidth={1.5}
        vectorEffect="non-scaling-stroke" className="text-primary" />
    </svg>
  );
}

export default function NodCell({ exposure, range, stretch, colormap, bkgsub, onData, onSettled }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [cell, setCell] = useState<S2dCell | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'absent' | 'error'>('loading');
  const [errMsg, setErrMsg] = useState<string>('');

  const key = exposure?.storage_key ?? null;
  useEffect(() => {
    if (!exposure) { setState('absent'); return; }
    if (!key) { setState('absent'); return; }
    let cancelled = false;
    setState('loading');
    const controller = new AbortController();
    const url = `/api/nircam-fits?key=${encodeURIComponent(key)}`;
    fetchS2dCell(url, { extname: bkgsub ? 'S2D_BKGSUB_SCI' : 'S2D_SCI', signal: controller.signal })
      .then((c) => {
        if (cancelled) return;
        setCell(c);
        setState('ready');
        onData(exposure.id, c.data);
        onSettled(exposure.id);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e?.name === 'AbortError') return;  // unmount/dep change — not settled
        setState(e instanceof HduNotFoundError ? 'absent' : 'error');
        if (!(e instanceof HduNotFoundError)) setErrMsg(e instanceof Error ? e.message : 'render failed');
        onSettled(exposure.id);  // settled without data — unblocks the shared-stretch gate
      });
    return () => { cancelled = true; controller.abort(); };
  }, [key, bkgsub, exposure, onData, onSettled]);

  // Paint once we have both the pixels and the shared stretch range.
  useEffect(() => {
    if (state !== 'ready' || !cell || !range) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    canvas.width = cell.width;
    canvas.height = cell.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.putImageData(paintToImageData(cell.data, cell.width, cell.height, range[0], range[1], stretch, colormap), 0, 0);
  }, [state, cell, range, stretch, colormap]);

  if (!exposure) {
    return <div className="flex items-center justify-center h-full text-text-tertiary text-xs">—</div>;
  }
  if (state === 'absent') {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center text-text-tertiary text-[10px] p-1">
        <span>no rectified view</span>
        <span className="opacity-70">re-run with rectify</span>
      </div>
    );
  }
  if (state === 'error') {
    return (
      <div className="flex items-center justify-center h-full text-center text-red-500 text-[10px] p-1" title={errMsg}>
        render error
      </div>
    );
  }

  const stkshtrs = cell?.primaryHeader.getString('STKSHTRS');
  const shutsta = cell?.sciHeader?.getString('SHUTSTA');

  return (
    <div className="flex h-full gap-1">
      <div className="relative flex-1 min-w-0 bg-black/40 rounded overflow-hidden">
        {state === 'loading' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <Loader2 className="w-4 h-4 animate-spin text-text-secondary" />
          </div>
        )}
        <canvas
          ref={canvasRef}
          className="w-full h-full"
          style={{ imageRendering: 'pixelated', objectFit: 'fill' }}
        />
        {(stkshtrs || shutsta) && (
          <div className="absolute bottom-0 left-0 right-0 px-1 py-0.5 bg-black/50 text-[9px] font-mono text-white/80 truncate"
            title={`STKSHTRS=${stkshtrs ?? ''} SHUTSTA=${shutsta ?? ''}`}>
            {shutsta ?? stkshtrs}
          </div>
        )}
      </div>
      {cell && <ProfileSvg data={cell.data} width={cell.width} height={cell.height} />}
    </div>
  );
}
