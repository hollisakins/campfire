// Science FITS cutouts (epic #337, Phase 5): direct native-pixel crops from the
// FitsGL tile pyramid — plan at the requested (default native) level, decode the
// covering tiles, and emit one float32 IMAGE extension per band carrying the
// level's own WCS with CRPIX shifted to the crop. NO resampling, NO stretch:
// the pixels are the fpack tiles' values verbatim, which is exactly what
// "direct cutouts from the tiles" means. Pure of sharp/Next (unit-testable);
// the figure (PNG) sibling lives in `./figure`.

import { planCutout } from './plan';
import { assembleRegion } from './assemble';
import { encodeFitsCutout, type FitsBandCutout } from './fits';
import type { FieldScienceSource } from './source';

/** Per-band pixel budget (4096² ≈ 64 MB of float32). */
export const MAX_PIXELS_PER_BAND = 4096 * 4096;
/** Whole-request budget across bands (≈ 256 MB of float32). */
export const MAX_PIXELS_TOTAL = 4 * MAX_PIXELS_PER_BAND;

/** Request exceeds the pixel budget — the route turns this into a 400 with advice. */
export class CutoutTooLargeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CutoutTooLargeError';
  }
}

export interface ScienceCutoutRequest {
  /** ICRS centre `[ra, dec]` in degrees. */
  center: [number, number];
  /** Square field of view in arcsec. */
  fovArcsec: number;
  /** Output pixel scale (arcsec/px) → pyramid level selection; omit for native. */
  targetScaleArcsec?: number;
  /** Extra primary-header context (field name, request provenance). */
  field: string;
}

/**
 * Build a multi-extension FITS cutout from a resolved science source.
 * Bands are assembled sequentially to bound peak memory.
 */
export async function buildFitsCutout(
  src: FieldScienceSource,
  req: ScienceCutoutRequest,
): Promise<Buffer> {
  const cutouts: FitsBandCutout[] = [];
  let totalPixels = 0;

  for (const band of src.bands) {
    const plan = planCutout(band.manifest, req.center, req.fovArcsec, {
      targetScaleArcsec: req.targetScaleArcsec,
      rounding: 'nearest',
    });
    const w = Math.max(0, plan.pixelBbox.x1 - plan.pixelBbox.x0);
    const h = Math.max(0, plan.pixelBbox.y1 - plan.pixelBbox.y0);
    if (w * h > MAX_PIXELS_PER_BAND) {
      throw new CutoutTooLargeError(
        `${band.name}: ${w}x${h} px exceeds the ${Math.sqrt(MAX_PIXELS_PER_BAND)}^2 per-band budget; ` +
          'reduce fov or pass a coarser scale',
      );
    }
    totalPixels += w * h;
    if (totalPixels > MAX_PIXELS_TOTAL) {
      throw new CutoutTooLargeError(
        `request totals ${totalPixels} px across bands (budget ${MAX_PIXELS_TOTAL}); ` +
          'reduce fov, bands, or pass a coarser scale',
      );
    }

    const region = await assembleRegion(plan, band.baseUrl);
    cutouts.push({
      name: band.name,
      data: region.data,
      width: region.width,
      height: region.height,
      wcs: plan.level.wcs,
      origin: [region.x0, region.y0],
      pixelScaleArcsec: plan.level.pixelScaleArcsec,
      levelZ: plan.level.z,
    });
  }

  return encodeFitsCutout(cutouts, {
    primary: {
      FIELD: req.field,
      DATASET: src.datasetPrefix,
      RA_CEN: req.center[0],
      DEC_CEN: req.center[1],
      FOV_AS: req.fovArcsec,
    },
    comments: [
      'CAMPFIRE FitsGL science cutout: direct crop of the tile pyramid.',
      'Each extension carries its pyramid level WCS (CRPIX shifted).',
    ],
  });
}
