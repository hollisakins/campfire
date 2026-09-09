// Turn reprojected float pixels into RGBA (epic #337, Phase 5), using the viewer's
// own transfer functions from `@fitsgl/core` (`scaleValue` normalize+clamp+stretch,
// `colormapRGB` LUT) so a server cutout matches the interactive map exactly. NaN
// (no-data) pixels are written transparent so the cutout composites over the page.
//
// Row order: the input arrays are FITS bottom-up (row 0 = south — `reproject.ts`
// keeps the standard N-up WCS with CD2_2 > 0), while RGBA consumers (`sharp`,
// canvas) are raster top-down (row 0 = top). The renderers flip rows here so the
// encoded image is actually North-up — the same convention boundary the legacy
// stack handles in `tile-compositing.ts`'s `fitsToWorldPixel`.

import {
  applyStretch,
  colormapRGB,
  COLORMAP_SIZE,
  weightedTrilogyPixel,
  type BandWeight,
  type ColormapName,
  type StretchMode,
  type TrilogyLevels,
} from '@fitsgl/core';

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

/** Panel scaling: display limits in units of the cutout's own noise
 *  (`snr`: `median + [lo, hi]·σ`, σ from the MAD) or robust percentiles. */
export type Scaling = 'snr' | 'percentile';
/** Default SNR window, in σ: black at -5σ, white at +8σ. */
export const DEFAULT_SNR_RANGE: readonly [number, number] = [-5, 8];

/**
 * Display limits in units of the local noise: the median of the finite
 * pixels as the sky level and 1.4826·MAD as a robust σ (bright sources do
 * not inflate it the way a plain RMS would), so every band's panel reads
 * the same way — black at `loSigma` below sky, white at `hiSigma` above —
 * whatever its depth. Falls back to percentile limits when the cutout has
 * no measurable noise (constant or empty data).
 */
export function snrLimits(data: Float32Array, loSigma = DEFAULT_SNR_RANGE[0], hiSigma = DEFAULT_SNR_RANGE[1]): Limits {
  const finite: number[] = [];
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    if (Number.isFinite(v)) finite.push(v);
  }
  if (finite.length === 0) return { lo: 0, hi: 1 };
  finite.sort((a, b) => a - b);
  const median = finite[finite.length >> 1];
  const dev = finite.map((v) => Math.abs(v - median)).sort((a, b) => a - b);
  const sigma = 1.4826 * dev[dev.length >> 1];
  if (!(sigma > 0) || !(hiSigma > loSigma)) return percentileLimits(data);
  return { lo: median + loSigma * sigma, hi: median + hiSigma * sigma };
}

/** Single-band → RGBA via a stretch + colormap LUT. NaN → transparent.
 *  Input rows are FITS bottom-up; output RGBA rows are raster top-down. */
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
  for (let y = 0; y < height; y++) {
    const src = y * width;
    const dst = (height - 1 - y) * width; // FITS bottom-up → raster top-down
    for (let x = 0; x < width; x++) {
      const v = data[src + x];
      if (Number.isNaN(v)) continue; // leave transparent (rgba is zero-filled)
      const o = (dst + x) * 4;
      const norm = scaleValue(v, limits.lo, limits.hi, stretch, trilogyK);
      const idx = Math.min(maxIdx, Math.max(0, Math.round(norm * maxIdx))) * 3;
      rgba[o] = lut[idx];
      rgba[o + 1] = lut[idx + 1];
      rgba[o + 2] = lut[idx + 2];
      rgba[o + 3] = 255;
    }
  }
  return rgba;
}

/** Per-channel RGB composite (R←band0, G←band1, B←band2), each with its own stretch.
 *  Input rows are FITS bottom-up; output RGBA rows are raster top-down. */
export function renderRGB(
  channels: readonly [Float32Array, Float32Array, Float32Array],
  width: number,
  height: number,
  opts: {
    limits: readonly [Limits, Limits, Limits];
    stretch: StretchMode;
    /** Trilogy softening: one shared `k`, or per-channel `[kR, kG, kB]` (each
     *  band's levels solve its own `k`, matching the viewer's applyTrilogy). */
    trilogyK?: number | readonly number[];
  },
): Uint8ClampedArray {
  const { limits, stretch, trilogyK } = opts;
  const kFor = (c: number): number | undefined =>
    typeof trilogyK === 'number' ? trilogyK : trilogyK?.[c];
  const rgba = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) {
    const src = y * width;
    const dst = (height - 1 - y) * width; // FITS bottom-up → raster top-down
    for (let x = 0; x < width; x++) {
      const o = (dst + x) * 4;
      let any = false;
      for (let c = 0; c < 3; c++) {
        const v = channels[c][src + x];
        if (Number.isNaN(v)) continue;
        any = true;
        rgba[o + c] = Math.round(scaleValue(v, limits[c].lo, limits[c].hi, stretch, kFor(c)) * 255);
      }
      if (any) rgba[o + 3] = 255; // opaque where at least one band has data
    }
  }
  return rgba;
}

/**
 * Weighted multi-band trilogy composite — the map's faithful composite (Dan
 * Coe's trilogy): each band normalized over its own `[x0, x2]` and stretched
 * with its own `k`, channels as weight-averaged sums. Per-pixel arithmetic is
 * the core's `weightedTrilogyPixel`, the CPU reference the shader transcribes,
 * so a cutout and the map agree by construction. `bands`, `levels` and
 * `weights` are parallel. Opaque where at least one band has data.
 * Input rows are FITS bottom-up; output RGBA rows are raster top-down.
 */
export function renderWeightedTrilogy(
  bands: readonly Float32Array[],
  width: number,
  height: number,
  opts: { levels: readonly TrilogyLevels[]; weights: readonly BandWeight[] },
): Uint8ClampedArray {
  const { levels, weights } = opts;
  if (bands.length !== levels.length || bands.length !== weights.length) {
    throw new Error('renderWeightedTrilogy: bands, levels and weights must be parallel');
  }
  const n = bands.length;
  const rgba = new Uint8ClampedArray(width * height * 4);
  const values = new Array<number>(n);
  for (let y = 0; y < height; y++) {
    const src = y * width;
    const dst = (height - 1 - y) * width; // FITS bottom-up → raster top-down
    for (let x = 0; x < width; x++) {
      let any = false;
      for (let i = 0; i < n; i++) {
        const v = bands[i][src + x];
        values[i] = v;
        if (!Number.isNaN(v)) any = true;
      }
      if (!any) continue; // leave transparent
      const [r, g, b] = weightedTrilogyPixel(values, levels, weights);
      const o = (dst + x) * 4;
      rgba[o] = Math.round(r * 255);
      rgba[o + 1] = Math.round(g * 255);
      rgba[o + 2] = Math.round(b * 255);
      rgba[o + 3] = 255;
    }
  }
  return rgba;
}
