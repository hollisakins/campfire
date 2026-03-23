/**
 * Client-side shutter overlay computation.
 *
 * Converts shutter geometry (RA/Dec/PA) into pixel-space rectangles
 * for rendering as an SVG overlay on top of a cutout image.
 *
 * Uses small-angle arcsecond offsets from the cutout center — valid
 * for the FOVs used in practice (1–30 arcsec).
 */

/** NIRSpec shutter width in arcseconds */
export const SHUTTER_WIDTH_ARCSEC = 0.22;
/** NIRSpec shutter height in arcseconds */
export const SHUTTER_HEIGHT_ARCSEC = 0.46;

export interface ShutterGeometry {
  object_id: string;
  center_ra: number;
  center_dec: number;
  position_angle: number;
  shutter_state: 'source' | 'open' | 'stuck_closed';
}

export interface ShutterRect {
  /** Center X in output pixels */
  x: number;
  /** Center Y in output pixels */
  y: number;
  /** Width in output pixels */
  width: number;
  /** Height in output pixels */
  height: number;
  /** Rotation angle in degrees (for SVG transform) */
  rotation: number;
  /** Fill color */
  fill: string;
  fillOpacity: number;
  /** Stroke color */
  stroke: string;
  strokeOpacity: number;
  strokeWidth: number;
  /** Optional dash array for dashed outlines */
  strokeDasharray?: string;
}

/**
 * Compute SVG rectangle descriptors for shutter overlays.
 *
 * @param shutters - Array of shutter geometry from the server
 * @param centerRa - RA of the cutout center (degrees)
 * @param centerDec - Dec of the cutout center (degrees)
 * @param fovArcsec - Field of view of the cutout (arcseconds)
 * @param displaySize - CSS display size of the image (pixels)
 * @param objectId - Current object ID (highlighted in green)
 */
export function computeShutterRects(
  shutters: ShutterGeometry[],
  centerRa: number,
  centerDec: number,
  fovArcsec: number,
  displaySize: number,
  objectId: string,
): ShutterRect[] {
  const pxPerArcsec = displaySize / fovArcsec;
  const cosDecFactor = Math.cos(centerDec * Math.PI / 180);
  const halfSize = displaySize / 2;

  // Shutter dimensions in pixels
  const wPx = SHUTTER_WIDTH_ARCSEC * pxPerArcsec;
  const hPx = SHUTTER_HEIGHT_ARCSEC * pxPerArcsec;

  const rects: ShutterRect[] = [];

  for (const shutter of shutters) {
    // Offset from center in arcseconds
    // RA increases to the left in sky coords, but in our image E is left
    // so positive dRA = negative pixel X offset (standard astronomical convention)
    const dra = (shutter.center_ra - centerRa) * cosDecFactor * 3600;
    const ddec = (shutter.center_dec - centerDec) * 3600;

    // Convert to pixel coordinates (image: +X = East = -RA, +Y = down = -Dec)
    const x = halfSize - dra * pxPerArcsec;
    const y = halfSize - ddec * pxPerArcsec;

    // Skip if outside bounds (with padding)
    const pad = 20;
    if (x < -pad || x > displaySize + pad || y < -pad || y > displaySize + pad) continue;

    const isCurrentObject = shutter.object_id === objectId;
    const isStuck = shutter.shutter_state === 'stuck_closed';

    let rect: ShutterRect;

    if (isStuck) {
      rect = {
        x, y, width: wPx, height: hPx,
        rotation: -shutter.position_angle,
        fill: 'none', fillOpacity: 0,
        stroke: '#ef4444', strokeOpacity: 1, strokeWidth: 1.5,
        strokeDasharray: '3,2',
      };
    } else if (isCurrentObject) {
      rect = {
        x, y, width: wPx, height: hPx,
        rotation: -shutter.position_angle,
        fill: '#00ff00', fillOpacity: 0.2,
        stroke: '#00ff00', strokeOpacity: 1, strokeWidth: 1,
      };
    } else {
      rect = {
        x, y, width: wPx, height: hPx,
        rotation: -shutter.position_angle,
        fill: '#aaaaaa', fillOpacity: 0.15,
        stroke: '#cccccc', strokeOpacity: 0.65, strokeWidth: 0.8,
      };
    }

    rects.push(rect);
  }

  return rects;
}
