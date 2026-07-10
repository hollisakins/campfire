'use client';

/**
 * FitsglOverlays (epic #337, Phase 4.5) — the Canvas2D layers stacked over the GL
 * canvas: the RA/Dec graticule and the ruler line. Both reproject every drawn frame
 * from the viewer's public accessors, so the parent calls the imperative `redraw()`
 * from its `onFrame` (React state per frame would thrash). The ruler also installs a
 * `PointerTool` via the viewer handle while the ruler tool is active, feeding its
 * live measurement back to the status pill through `onMeasure`.
 */

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react';
import type { PointerTool } from '@fitsgl/core';
import type { FitsViewerHandle } from '@fitsgl/core/react';
import { drawGraticule } from './graticule';
import { drawRuler, measureRuler, type RulerGeometry, type RulerMeasurement, type Point } from './ruler';

export interface FitsglOverlaysHandle {
  redraw(): void;
}

interface FitsglOverlaysProps {
  handleRef: React.RefObject<FitsViewerHandle | null>;
  graticule: boolean;
  tool: 'pan' | 'ruler';
  /** Bumped on viewer (re)ready — re-installs the pointer tool after a reload. */
  readyTick: number;
  accent: string;
  gridLine: string;
  gridLabel: string;
  onMeasure: (m: RulerMeasurement | null) => void;
}

/** Size a canvas to its CSS box × DPR, clear it, and hand back a CSS-space ctx. */
function prepare(canvas: HTMLCanvasElement | null): { ctx: CanvasRenderingContext2D; rect: DOMRect } | null {
  if (!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (w < 2 || h < 2) return null;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, rect: canvas.getBoundingClientRect() };
}

export const FitsglOverlays = forwardRef<FitsglOverlaysHandle, FitsglOverlaysProps>(function FitsglOverlays(
  { handleRef, graticule, tool, readyTick, accent, gridLine, gridLabel, onMeasure },
  ref,
) {
  const gratRef = useRef<HTMLCanvasElement | null>(null);
  const rulerRef = useRef<HTMLCanvasElement | null>(null);
  const geom = useRef<RulerGeometry | null>(null);

  // Latest values through refs so the fixed PointerTool never captures stale props.
  const onMeasureRef = useRef(onMeasure);
  const accentRef = useRef(accent);
  useEffect(() => { onMeasureRef.current = onMeasure; }, [onMeasure]);
  useEffect(() => { accentRef.current = accent; }, [accent]);

  const redraw = useCallback(() => {
    const viewer = handleRef.current?.getViewer() ?? null;
    const g = prepare(gratRef.current);
    if (g && graticule && viewer) drawGraticule(g.ctx, g.rect, viewer, { line: gridLine, label: gridLabel });
    const r = prepare(rulerRef.current);
    if (r && tool === 'ruler' && viewer) drawRuler(r.ctx, r.rect, viewer, geom.current, accentRef.current);
  }, [handleRef, graticule, tool, gridLine, gridLabel]);

  useImperativeHandle(ref, () => ({ redraw }), [redraw]);

  // Redraw when a layer toggles (a frame may not follow a pure UI toggle).
  useEffect(() => { redraw(); }, [redraw]);

  // Install / tear down the ruler PointerTool while the ruler tool is active.
  useEffect(() => {
    const h = handleRef.current;
    if (!h) return;
    if (tool !== 'ruler') {
      h.setTool(null);
      geom.current = null;
      onMeasureRef.current(null);
      redraw();
      return;
    }
    const measure = (a: Point, b: Point): RulerMeasurement =>
      measureRuler(h.getViewer()?.getWcs() ?? null, a, b);
    const commit = (a: Point, b: Point, dragging: boolean) => {
      const m = measure(a, b);
      geom.current = { a, b, dragging, ...m };
      onMeasureRef.current(m);
      redraw();
    };
    const rulerTool: PointerTool = {
      cursor: 'crosshair',
      onPointerDown(world) { commit({ x: world.x, y: world.y }, { x: world.x, y: world.y }, true); },
      onPointerMove(world) { const g = geom.current; if (g) commit(g.a, { x: world.x, y: world.y }, true); },
      onPointerUp(world) { const g = geom.current; if (g) commit(g.a, { x: world.x, y: world.y }, false); },
    };
    h.setTool(rulerTool);
    return () => { h.setTool(null); };
  }, [tool, readyTick, handleRef, redraw]);

  return (
    <>
      <canvas ref={gratRef} className="pointer-events-none absolute inset-0 z-[400] h-full w-full" aria-hidden />
      <canvas ref={rulerRef} className="pointer-events-none absolute inset-0 z-[400] h-full w-full" aria-hidden />
    </>
  );
});
