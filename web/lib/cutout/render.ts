// Turn reprojected float pixels into RGBA (epic #337, Phase 5), using the viewer's
// own transfer functions from `@fitsgl/core` (`scaleValue` normalize+clamp+stretch,
// `colormapRGB` LUT) so a server cutout matches the interactive map exactly. NaN
// (no-data) pixels are written transparent so the cutout composites over the page.

import { applyStretch, colormapRGB, COLORMAP_SIZE, type StretchMode, type ColormapName } from '@fitsgl/core';

export interface Limits {
  lo: number;
  hi: number;
}

/** `@fitsgl/core`'s `scaleChannel`: linear-normalize over [lo, hi], clamp, then stretch.
 *  (Inlined because the core re-exports `applyStretch` but not the `scaleValue` wrapper.) */
function scaleValue(v: number, lo: number, hi: number, mode: StretchMode, trilogyK?: number): number {
  const t = (v - lo) / (hi - lo);
  const norm = t < 0 ? 0 : t > 1 ? 1 : t;
  return applyStretch(norm, mode, trilogyK);
}

/** Robust display limits from finite data by percentile (defaults ~ the map's 99.5%). */
export function percentileLimits(data: Float32Array, loPct = 0.5, hiPct = 99.5): Limits {
  const finite: number[] = [];
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    if (Number.isFinite(v)) finite.push(v);
  }
  if (finite.length === 0) return { lo: 0, hi: 1 };
  finite.sort((a, b) => a - b);
  const at = (p: number) => finite[Math.min(finite.length - 1, Math.max(0, Math.round((p / 100) * (finite.length - 1))))];
  const lo = at(loPct);
  let hi = at(hiPct);
  if (!(hi > lo)) hi = lo + 1e-6;
  return { lo, hi };
}

/** Single-band → RGBA via a stretch + colormap LUT. NaN → transparent. */
export function renderSingleBand(
  data: Float32Array,
  width: number,
  height: number,
  opts: { limits: Limits; stretch: StretchMode; colormap: ColormapName; trilogyK?: number },
): Uint8ClampedArray {
  const { limits, stretch, colormap, trilogyK } = opts;
  const lut = colormapRGB(colormap);
  const rgba = new Uint8ClampedArray(width * height * 4);
  const maxIdx = COLORMAP_SIZE - 1;
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    const o = i * 4;
    if (Number.isNaN(v)) continue; // leave transparent (rgba is zero-filled)
    const norm = scaleValue(v, limits.lo, limits.hi, stretch, trilogyK);
    const idx = Math.min(maxIdx, Math.max(0, Math.round(norm * maxIdx))) * 3;
    rgba[o] = lut[idx];
    rgba[o + 1] = lut[idx + 1];
    rgba[o + 2] = lut[idx + 2];
    rgba[o + 3] = 255;
  }
  return rgba;
}

/** Per-channel RGB composite (R←band0, G←band1, B←band2), each with its own stretch. */
export function renderRGB(
  channels: readonly [Float32Array, Float32Array, Float32Array],
  width: number,
  height: number,
  opts: { limits: readonly [Limits, Limits, Limits]; stretch: StretchMode; trilogyK?: number },
): Uint8ClampedArray {
  const { limits, stretch, trilogyK } = opts;
  const rgba = new Uint8ClampedArray(width * height * 4);
  const n = width * height;
  for (let i = 0; i < n; i++) {
    const o = i * 4;
    let any = false;
    for (let c = 0; c < 3; c++) {
      const v = channels[c][i];
      if (Number.isNaN(v)) continue;
      any = true;
      rgba[o + c] = Math.round(scaleValue(v, limits[c].lo, limits[c].hi, stretch, trilogyK) * 255);
    }
    if (any) rgba[o + 3] = 255; // opaque where at least one band has data
  }
  return rgba;
}
