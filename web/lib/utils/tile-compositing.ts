/**
 * Server-side tile compositing utilities.
 *
 * Fetches map tiles and composites them into a cropped thumbnail
 * centered on an object's RA/Dec, with optional shutter overlay.
 */

import sharp from 'sharp';
import { skyToPixel, type WCSParams } from './wcs';

// ============================================
// Constants
// ============================================

const SHUTTER_WIDTH_ARCSEC = 0.22;
const SHUTTER_HEIGHT_ARCSEC = 0.46;

// Transparent 1x1 GIF (used as fallback/placeholder)
export const TRANSPARENT_GIF = Buffer.from(
  'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7',
  'base64',
);

// ============================================
// Types
// ============================================

export interface MapLayerInfo {
  tile_base_url: string;
  min_zoom: number;
  max_zoom: number;
  tile_size: number;
  wcs_params: WCSParams;
  tile_version: number;
}

export interface ShutterInfo {
  object_id: string;
  center_ra: number;
  center_dec: number;
  position_angle: number;
  shutter_state: 'source' | 'open' | 'stuck_closed';
}

export interface CompositingOptions {
  ra: number;
  dec: number;
  objectId: string;
  layer: MapLayerInfo;
  outputSize: number;
  fovArcsec: number;
  shutters?: ShutterInfo[];
}

// ============================================
// Tile math
// ============================================

function computeTileRegion(
  wcs: WCSParams,
  layer: MapLayerInfo,
  ra: number,
  dec: number,
  outputSize: number,
  fovArcsec: number,
) {
  const tileSize = layer.tile_size;
  const maxZoom = layer.max_zoom;

  // Convert RA/Dec to FITS pixel coords
  const fitsPixel = skyToPixel(wcs, ra, dec);

  // Pixels per arcsecond at native (max zoom) resolution
  const pixPerArcsec = 1 / (Math.abs(wcs.cd2_2) * 3600);
  const nativePixelsInFov = fovArcsec * pixPerArcsec;

  // Choose zoom: we want outputSize pixels to cover nativePixelsInFov native pixels
  const idealZoom = maxZoom + Math.log2(outputSize / nativePixelsInFov);
  const fetchZoom = Math.min(maxZoom, Math.max(layer.min_zoom, Math.round(idealZoom)));
  const zoomScale = Math.pow(2, fetchZoom - maxZoom);

  // Tile-space coordinates (Y flipped from FITS convention)
  const nTilesY = Math.ceil(wcs.naxis2 * zoomScale / tileSize);
  const cx = fitsPixel.x * zoomScale;
  const cy = nTilesY * tileSize - fitsPixel.y * zoomScale;

  // How many tile-space pixels to extract (will be resized to outputSize)
  const cropPixels = nativePixelsInFov * zoomScale;

  // Bounding box in tile-space
  const left = cx - cropPixels / 2;
  const top = cy - cropPixels / 2;

  // Which tiles to fetch
  const nTilesX = Math.ceil(wcs.naxis1 * zoomScale / tileSize);
  const tileXMin = Math.max(0, Math.floor(left / tileSize));
  const tileXMax = Math.min(nTilesX - 1, Math.floor((left + cropPixels) / tileSize));
  const tileYMin = Math.max(0, Math.floor(top / tileSize));
  const tileYMax = Math.min(nTilesY - 1, Math.floor((top + cropPixels) / tileSize));

  return {
    fetchZoom,
    zoomScale,
    left,
    top,
    cropPixels,
    tileXMin,
    tileXMax,
    tileYMin,
    tileYMax,
    tileSize,
    nTilesY,
    fitsPixel,
    pixPerArcsec,
  };
}

// ============================================
// Tile fetching
// ============================================

async function fetchTile(
  baseUrl: string,
  z: number,
  x: number,
  y: number,
  version: number,
  tileSize: number,
): Promise<Buffer> {
  const url = `${baseUrl}/${z}/${x}/${y}.png?v=${version}`;
  try {
    const res = await fetch(url, { next: { revalidate: 3600 } });
    if (!res.ok) {
      // Return transparent tile on 404/error
      return await sharp({
        create: { width: tileSize, height: tileSize, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 0 } },
      }).png().toBuffer();
    }
    return Buffer.from(await res.arrayBuffer());
  } catch {
    return await sharp({
      create: { width: tileSize, height: tileSize, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 0 } },
    }).png().toBuffer();
  }
}

// ============================================
// Shutter SVG overlay
// ============================================

function generateShutterSvg(
  opts: CompositingOptions,
  region: ReturnType<typeof computeTileRegion>,
): Buffer | null {
  const { shutters, objectId, fovArcsec } = opts;
  const { outputSize } = opts;
  if (!shutters || shutters.length === 0) return null;

  const { left, top, cropPixels, zoomScale, nTilesY } = region;
  const wcs = opts.layer.wcs_params;
  const tileSize = opts.layer.tile_size;
  const arcsecPerOutputPx = fovArcsec / outputSize;

  const rects: string[] = [];

  for (const shutter of shutters) {
    const sp = skyToPixel(wcs, shutter.center_ra, shutter.center_dec);
    const sx = sp.x * zoomScale;
    const sy = nTilesY * tileSize - sp.y * zoomScale;

    // Position relative to crop region, scaled to output
    const outX = (sx - left) * (outputSize / cropPixels);
    const outY = (sy - top) * (outputSize / cropPixels);

    // Skip if outside output bounds (with some padding)
    if (outX < -20 || outX > outputSize + 20 || outY < -20 || outY > outputSize + 20) continue;

    // Shutter dimensions in output pixels
    const wPx = SHUTTER_WIDTH_ARCSEC / arcsecPerOutputPx;
    const hPx = SHUTTER_HEIGHT_ARCSEC / arcsecPerOutputPx;

    const isCurrentObject = shutter.object_id === objectId;
    const isStuck = shutter.shutter_state === 'stuck_closed';

    let fill: string, stroke: string, fillOpacity: number, strokeOpacity: number, strokeWidth: number;
    let strokeDash = '';

    if (isStuck) {
      fill = 'none'; stroke = '#ef4444'; strokeDash = ' stroke-dasharray="3,2"';
      fillOpacity = 0; strokeOpacity = 1; strokeWidth = 1.5;
    } else if (isCurrentObject) {
      fill = '#00ff00'; stroke = '#00ff00';
      fillOpacity = 0.2; strokeOpacity = 1; strokeWidth = 1;
    } else {
      fill = '#888888'; stroke = '#888888';
      fillOpacity = 0.05; strokeOpacity = 0.3; strokeWidth = 0.5;
    }

    rects.push(
      `<rect x="${-wPx / 2}" y="${-hPx / 2}" width="${wPx}" height="${hPx}" ` +
      `fill="${fill}" fill-opacity="${fillOpacity}" ` +
      `stroke="${stroke}" stroke-opacity="${strokeOpacity}" stroke-width="${strokeWidth}"` +
      `${strokeDash} ` +
      `transform="translate(${outX},${outY}) rotate(${-shutter.position_angle})" />`,
    );
  }

  if (rects.length === 0) return null;

  return Buffer.from(
    `<svg width="${outputSize}" height="${outputSize}" xmlns="http://www.w3.org/2000/svg">${rects.join('')}</svg>`,
  );
}

// ============================================
// Main compositing function
// ============================================

/**
 * Composite map tiles into a thumbnail PNG centered on the given coordinates.
 * Optionally draws shutter overlays.
 */
export async function compositeTileThumbnail(
  opts: CompositingOptions,
): Promise<Buffer> {
  const { layer, ra, dec, outputSize, fovArcsec } = opts;
  const wcs = layer.wcs_params;

  // Compute which tiles we need
  const region = computeTileRegion(wcs, layer, ra, dec, outputSize, fovArcsec);
  const { fetchZoom, left, top, cropPixels, tileXMin, tileXMax, tileYMin, tileYMax, tileSize } = region;

  // Fetch all needed tiles in parallel
  const tilePromises: Promise<{ buffer: Buffer; tx: number; ty: number }>[] = [];
  for (let ty = tileYMin; ty <= tileYMax; ty++) {
    for (let tx = tileXMin; tx <= tileXMax; tx++) {
      tilePromises.push(
        fetchTile(layer.tile_base_url, fetchZoom, tx, ty, layer.tile_version, tileSize)
          .then(buffer => ({ buffer, tx, ty })),
      );
    }
  }
  const tiles = await Promise.all(tilePromises);

  // Canvas covering all fetched tiles
  const canvasWidth = (tileXMax - tileXMin + 1) * tileSize;
  const canvasHeight = (tileYMax - tileYMin + 1) * tileSize;

  // Composite tiles onto canvas
  const composites = tiles.map(({ buffer, tx, ty }) => ({
    input: buffer,
    left: (tx - tileXMin) * tileSize,
    top: (ty - tileYMin) * tileSize,
  }));

  // Extract the region of interest and resize
  const cropLeft = Math.max(0, Math.round(left - tileXMin * tileSize));
  const cropTop = Math.max(0, Math.round(top - tileYMin * tileSize));
  const cropSize = Math.max(1, Math.min(
    Math.round(cropPixels),
    canvasWidth - cropLeft,
    canvasHeight - cropTop,
  ));

  let result = await sharp({
    create: {
      width: canvasWidth,
      height: canvasHeight,
      channels: 4,
      background: { r: 0, g: 0, b: 0, alpha: 255 },
    },
  })
    .composite(composites)
    .extract({ left: cropLeft, top: cropTop, width: cropSize, height: cropSize })
    .resize(outputSize, outputSize, { kernel: sharp.kernel.nearest })
    .png()
    .toBuffer();

  // Add shutter overlay if requested
  const shutterSvg = generateShutterSvg(opts, region);
  if (shutterSvg) {
    result = await sharp(result)
      .composite([{ input: shutterSvg, top: 0, left: 0 }])
      .png()
      .toBuffer();
  }

  return result;
}
