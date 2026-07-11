// FitsGL cutout core (epic #337, Phase 5) — the server-side engine that turns a
// deployed tile pyramid into a North-up cutout, reading the same `.fits.fz`
// supertiles the browser renders. Pure of any Next/route/`sharp` coupling so it
// unit-tests and ports cleanly; PNG encoding lives in `./encode` (server-only).
//
//   plan (which level + tiles)  →  assemble (decode native region)
//     →  reproject (native → N-up output grid)  →  render (stretch/colormap → RGBA)
//
// Single-band → colormap; three bands → per-channel RGB composite. Both match the
// interactive viewer's transfer functions (`@fitsgl/core`).

import type { StretchMode, ColormapName } from '@fitsgl/core';
import type { Manifest } from './manifest';
import { planCutout } from './plan';
import { assembleRegion } from './assemble';
import { reprojectToNorthUp } from './reproject';
import { percentileLimits, renderSingleBand, renderRGB, type Limits } from './render';

export type { Manifest } from './manifest';
export { loadManifest, parseManifest } from './manifest';

/** One band of a cutout: its parsed manifest + the tile directory holding its `.fits.fz`. */
export interface BandSource {
  manifest: Manifest;
  /** Tile directory URL for this band (where `manifest.json` + supertiles live). */
  baseUrl: string;
}

export interface CutoutRequest {
  /** ICRS centre `[ra, dec]` in degrees. */
  center: [number, number];
  /** Square field of view in arcsec. */
  fovArcsec: number;
  /** Square output size in pixels. */
  outputSize: number;
  stretch?: StretchMode;
  /** Single-band colormap (ignored for a 3-band RGB composite). */
  colormap?: ColormapName;
  /** Display limits: `'auto'` (per-band percentile) or explicit per band. */
  limits?: 'auto' | Limits | Limits[];
}

export interface CutoutRGBA {
  /** Raster order (row 0 = top = North) — ready for `sharp`/canvas encoding. */
  rgba: Uint8ClampedArray;
  width: number;
  height: number;
  /** The N-up output WCS (flat FITS header). NB: describes the underlying
   *  FITS bottom-up float array, NOT the row-flipped `rgba` raster. */
  outputWcsHeader: Record<string, number | string>;
}

/** Reproject one band to the N-up output grid, returning its float pixels + WCS. */
async function bandToOutput(band: BandSource, center: [number, number], fovArcsec: number, outputSize: number) {
  const plan = planCutout(band.manifest, center, fovArcsec, { outputSize, rounding: 'nearest' });
  const region = await assembleRegion(plan, band.baseUrl);
  const scale = fovArcsec / outputSize;
  return reprojectToNorthUp(region, plan.level, center[0], center[1], scale, outputSize, outputSize);
}

/** Render a cutout to RGBA (single-band colormap, or 3-band per-channel RGB). */
export async function renderCutout(bands: BandSource[], req: CutoutRequest): Promise<CutoutRGBA> {
  if (bands.length !== 1 && bands.length !== 3) {
    throw new Error(`cutout needs 1 (single-band) or 3 (RGB) bands, got ${bands.length}`);
  }
  const stretch = req.stretch ?? 'asinh';
  const outs = await Promise.all(
    bands.map((b) => bandToOutput(b, req.center, req.fovArcsec, req.outputSize)),
  );
  const { width, height, outputWcsHeader } = outs[0];

  const limitFor = (i: number): Limits =>
    req.limits === undefined || req.limits === 'auto'
      ? percentileLimits(outs[i].data)
      : Array.isArray(req.limits)
        ? req.limits[i]
        : req.limits;

  let rgba: Uint8ClampedArray;
  if (bands.length === 1) {
    rgba = renderSingleBand(outs[0].data, width, height, {
      limits: limitFor(0),
      stretch,
      colormap: req.colormap ?? 'gray',
    });
  } else {
    rgba = renderRGB([outs[0].data, outs[1].data, outs[2].data], width, height, {
      limits: [limitFor(0), limitFor(1), limitFor(2)],
      stretch,
    });
  }
  return { rgba, width, height, outputWcsHeader };
}
