/**
 * RA/Dec coordinate-grid (graticule) drawing (epic #337, Phase 4.5). FitsGL ships
 * `drawGraticule` but doesn't re-export it from `@fitsgl/core/react`, so we rebuild
 * it on the public API: `getWcs` + `screenToImage` (client→world) bound the visible
 * sky, then each iso-RA / iso-Dec line is traced by sampling `skyToPix` → world →
 * `imageToScreen`, so the grid stays correct under the TAN projection, pan/zoom and
 * North-up. (Swap for FitsGL's own `drawGraticule` if it's ever exported — see the
 * epic notes.)
 */

import { pixToSky, skyToPix, type TanWcs } from '@fitsgl/core';
import type { FitsViewerCore } from '@fitsgl/core/react';

/** Candidate grid steps in degrees: 10°…1° … arcmin … arcsec. */
const STEPS = [
  10, 5, 2, 1, 0.5, 0.25,
  10 / 60, 5 / 60, 2 / 60, 1 / 60,
  30 / 3600, 15 / 3600, 10 / 3600, 5 / 3600, 2 / 3600, 1 / 3600,
];

/** Largest step giving ~4 divisions across `span` (falls back to the finest). */
function niceStep(span: number): number {
  const target = span / 4;
  for (const s of STEPS) if (s <= target) return s;
  return STEPS[STEPS.length - 1];
}

function fmtDeg(v: number, step: number): string {
  const dp = step >= 1 ? 1 : step >= 1 / 60 ? 3 : 5;
  return `${v.toFixed(dp)}°`;
}

export function drawGraticule(
  ctx: CanvasRenderingContext2D,
  rect: DOMRect,
  viewer: FitsViewerCore | null,
  opts: { line: string; label: string },
): void {
  const wcs: TanWcs | null = viewer?.getWcs() ?? null;
  if (!viewer || !wcs) return;
  const W = rect.width, H = rect.height;
  if (W < 2 || H < 2) return;

  // Bound the visible sky: sample a 5×5 grid of client points → world → sky.
  const ras: number[] = [];
  const decs: number[] = [];
  for (let i = 0; i <= 4; i++) {
    for (let j = 0; j <= 4; j++) {
      const cx = rect.left + (W * i) / 4;
      const cy = rect.top + (H * j) / 4;
      const world = viewer.screenToImage(cx, cy);
      const sky = pixToSky(wcs, world.x, world.y);
      ras.push(sky.ra); decs.push(sky.dec);
    }
  }
  if (ras.length < 4) return;

  // Unwrap RA if the field straddles 0/360.
  let unwrapped = ras;
  if (Math.max(...ras) - Math.min(...ras) > 180) unwrapped = ras.map((r) => (r < 180 ? r + 360 : r));
  const raMin = Math.min(...unwrapped), raMax = Math.max(...unwrapped);
  const decMin = Math.min(...decs), decMax = Math.max(...decs);
  const raStep = niceStep(raMax - raMin);
  const decStep = niceStep(decMax - decMin);
  const SAMPLES = 48;

  ctx.save();
  ctx.lineWidth = 0.6;
  ctx.strokeStyle = opts.line;
  ctx.fillStyle = opts.label;
  ctx.font = '10px ui-monospace, monospace';

  // Iso-Dec lines (constant dec, vary ra).
  for (let dec = Math.ceil(decMin / decStep) * decStep; dec <= decMax; dec += decStep) {
    let left: { x: number; y: number } | null = null;
    ctx.beginPath();
    for (let k = 0; k <= SAMPLES; k++) {
      const ra = raMin + ((raMax - raMin) * k) / SAMPLES;
      const p = project(viewer, wcs, ra, dec, rect);
      if (k === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y);
      if (p.y >= 0 && p.y <= H && (!left || p.x < left.x)) left = p;
    }
    ctx.stroke();
    if (left) ctx.fillText(fmtDeg(dec, decStep), Math.max(4, left.x + 4), Math.min(H - 4, left.y - 3));
  }

  // Iso-RA lines (constant ra, vary dec).
  for (let ra = Math.ceil(raMin / raStep) * raStep; ra <= raMax; ra += raStep) {
    let bottom: { x: number; y: number } | null = null;
    ctx.beginPath();
    for (let k = 0; k <= SAMPLES; k++) {
      const dec = decMin + ((decMax - decMin) * k) / SAMPLES;
      const p = project(viewer, wcs, ra, dec, rect);
      if (k === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y);
      if (p.x >= 0 && p.x <= W && (!bottom || p.y > bottom.y)) bottom = p;
    }
    ctx.stroke();
    if (bottom) ctx.fillText(fmtDeg(((ra % 360) + 360) % 360, raStep), Math.min(W - 40, bottom.x + 4), Math.max(12, bottom.y - 4));
  }
  ctx.restore();
}

/** Project a sky point to canvas-local coords via the viewer's accessors. */
function project(viewer: FitsViewerCore, wcs: TanWcs, ra: number, dec: number, rect: DOMRect) {
  const w = skyToPix(wcs, ((ra % 360) + 360) % 360, dec);
  const s = viewer.imageToScreen(w.x, w.y);
  return { x: s.x - rect.left, y: s.y - rect.top };
}
