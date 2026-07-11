/**
 * Ruler / measure-tool math + Canvas2D drawing (epic #337, Phase 4.5). FitsGL ships
 * `measureRuler`/`drawRuler` but does not re-export them from `@fitsgl/core/react`,
 * so we rebuild them on the public API (the exported spherical helpers + the
 * viewer's `getWcs`/`imageToScreen`). Endpoints are stored in world (native
 * image-pixel) coords so the line stays glued to the sky under pan/zoom/North-up.
 *
 * (If FitsGL later exports its own `measureRuler`/`drawRuler`, swap these for the
 * upstream versions to stay DRY — see the epic notes.)
 */

import {
  pixToSky,
  angularSeparationDeg,
  positionAngleDeg,
  type TanWcs,
} from '@fitsgl/core';
import type { FitsViewerCore } from '@fitsgl/core/react';

export interface Point { x: number; y: number }

export interface RulerMeasurement {
  /** Straight native-pixel distance between the endpoints. */
  pixelDist: number;
  /** Great-circle separation in degrees, or null without a WCS. */
  sepDeg: number | null;
  /** Position angle (North→East) at the first endpoint, or null. */
  paDeg: number | null;
}

export interface RulerGeometry extends RulerMeasurement {
  a: Point;
  b: Point;
  dragging: boolean;
}

/** Measure a two-point ruler in world (native-pixel) coords. Pure. */
export function measureRuler(wcs: TanWcs | null, a: Point, b: Point): RulerMeasurement {
  const pixelDist = Math.hypot(b.x - a.x, b.y - a.y);
  if (!wcs) return { pixelDist, sepDeg: null, paDeg: null };
  const sa = pixToSky(wcs, a.x, a.y);
  const sb = pixToSky(wcs, b.x, b.y);
  return {
    pixelDist,
    sepDeg: angularSeparationDeg(sa, sb),
    paDeg: positionAngleDeg(sa, sb),
  };
}

/**
 * Draw the ruler line + endpoint ticks into a Canvas2D overlay stacked exactly
 * over the viewer. `ctx` is expected pre-scaled to CSS pixels; `rect` is the
 * canvas's client rect (to localise the viewer's client-space `imageToScreen`).
 */
export function drawRuler(
  ctx: CanvasRenderingContext2D,
  rect: DOMRect,
  viewer: FitsViewerCore | null,
  ruler: RulerGeometry | null,
  accent: string,
): void {
  if (!viewer || !ruler) return;
  const pa = viewer.imageToScreen(ruler.a.x, ruler.a.y);
  const pb = viewer.imageToScreen(ruler.b.x, ruler.b.y);
  const ax = pa.x - rect.left, ay = pa.y - rect.top;
  const bx = pb.x - rect.left, by = pb.y - rect.top;

  ctx.save();
  ctx.strokeStyle = accent;
  ctx.fillStyle = accent;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(bx, by);
  ctx.stroke();
  ctx.setLineDash([]);
  for (const [x, y] of [[ax, ay], [bx, by]] as const) {
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}
