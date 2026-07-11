// Resample an assembled native-pixel region onto a North-up output grid (epic
// #337, Phase 5). Builds a clean N-up TAN WCS centred on the request and, for each
// output pixel, projects output→sky (`pixToSky`) then sky→native (`skyToPix`) and
// bilinearly samples the native array. Reusing `@fitsgl/core`'s astropy-validated
// TAN both ways keeps the geometry exact; the same output WCS is what the FITS path
// writes into the header. A native (un-rotated) cutout skips this and crops directly.

import { pixToSky, skyToPix, parseWcs, type TanWcs } from '@fitsgl/core';
import type { AssembledRegion } from './assemble';
import type { LevelInfo } from './manifest';
import { levelWcs } from './manifest';

export interface ReprojectResult {
  data: Float32Array;
  width: number;
  height: number;
  /** The N-up output WCS as a flat FITS header dict (for the FITS writer). */
  outputWcsHeader: Record<string, number | string>;
}

/** A North-up, East-left TAN WCS centred on `(ra, dec)` at `scaleArcsec`/px. */
export function northUpWcsHeader(
  ra: number,
  dec: number,
  scaleArcsec: number,
  width: number,
  height: number,
): Record<string, number | string> {
  const scaleDeg = scaleArcsec / 3600;
  return {
    WCSAXES: 2,
    CTYPE1: 'RA---TAN',
    CTYPE2: 'DEC--TAN',
    CUNIT1: 'deg',
    CUNIT2: 'deg',
    CRVAL1: ra,
    CRVAL2: dec,
    CRPIX1: (width + 1) / 2,
    CRPIX2: (height + 1) / 2,
    CD1_1: -scaleDeg, // +x = West → RA increases leftward (East-left)
    CD1_2: 0,
    CD2_1: 0,
    CD2_2: scaleDeg, // +y = South → Dec increases upward (North-up)
    LONPOLE: 180,
    RADESYS: 'ICRS',
  };
}

/** Bilinear sample of a row-major array at continuous `(ax, ay)`; NaN off-grid or on any NaN corner. */
function sampleBilinear(data: Float32Array, w: number, h: number, ax: number, ay: number): number {
  const x0 = Math.floor(ax);
  const y0 = Math.floor(ay);
  if (x0 < 0 || y0 < 0 || x0 + 1 >= w || y0 + 1 >= h) {
    // Allow the exact right/bottom edge via nearest when only just out of the 2×2.
    const xi = Math.round(ax);
    const yi = Math.round(ay);
    if (xi < 0 || yi < 0 || xi >= w || yi >= h) return NaN;
    return data[yi * w + xi];
  }
  const fx = ax - x0;
  const fy = ay - y0;
  const v00 = data[y0 * w + x0];
  const v10 = data[y0 * w + x0 + 1];
  const v01 = data[(y0 + 1) * w + x0];
  const v11 = data[(y0 + 1) * w + x0 + 1];
  if (Number.isNaN(v00) || Number.isNaN(v10) || Number.isNaN(v01) || Number.isNaN(v11)) return NaN;
  return v00 * (1 - fx) * (1 - fy) + v10 * fx * (1 - fy) + v01 * (1 - fx) * fy + v11 * fx * fy;
}

/**
 * Reproject `region` (native level pixels) to a North-up output grid of
 * `outWidth × outHeight` px, centred on `(ra, dec)` at `outputScaleArcsec`/px.
 */
export function reprojectToNorthUp(
  region: AssembledRegion,
  level: LevelInfo,
  ra: number,
  dec: number,
  outputScaleArcsec: number,
  outWidth: number,
  outHeight: number,
): ReprojectResult {
  const nativeWcs: TanWcs = levelWcs(level);
  const outHeader = northUpWcsHeader(ra, dec, outputScaleArcsec, outWidth, outHeight);
  const outWcs = parseWcs(outHeader);
  if (!outWcs) throw new Error('failed to build the output N-up WCS');

  const out = new Float32Array(outWidth * outHeight).fill(NaN);
  for (let j = 0; j < outHeight; j++) {
    for (let i = 0; i < outWidth; i++) {
      // Output array pixel (i, j) → its world coord (i+0.5, j+0.5) → sky → native.
      const sky = pixToSky(outWcs, i + 0.5, j + 0.5);
      const p = skyToPix(nativeWcs, sky.ra, sky.dec);
      // Native world → array index in the assembled region (centre of pixel k at k+0.5).
      const ax = p.x - 0.5 - region.x0;
      const ay = p.y - 0.5 - region.y0;
      out[j * outWidth + i] = sampleBilinear(region.data, region.width, region.height, ax, ay);
    }
  }
  return { data: out, width: outWidth, height: outHeight, outputWcsHeader: outHeader };
}
