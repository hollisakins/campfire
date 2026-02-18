/**
 * WCS coordinate conversion utilities for the map viewer.
 *
 * Since output tiles are always North-up (no rotation), the CD matrix is diagonal:
 *   CD1_1 = -pixel_scale (RA decreases with increasing pixel x)
 *   CD2_2 = +pixel_scale (Dec increases with increasing pixel y)
 *   CD1_2 = CD2_1 = 0
 */

export interface WCSParams {
  crpix1: number;
  crpix2: number;
  crval1: number; // RA reference (degrees)
  crval2: number; // Dec reference (degrees)
  cd1_1: number;  // -pixel_scale_deg / cos(dec)
  cd2_2: number;  // +pixel_scale_deg
  naxis1: number;
  naxis2: number;
}

/**
 * Convert pixel coordinates to sky coordinates (RA/Dec in degrees).
 */
export function pixelToSky(
  wcs: WCSParams,
  pixX: number,
  pixY: number
): { ra: number; dec: number } {
  const ra = wcs.crval1 + wcs.cd1_1 * (pixX - wcs.crpix1);
  const dec = wcs.crval2 + wcs.cd2_2 * (pixY - wcs.crpix2);
  return { ra, dec };
}

/**
 * Convert sky coordinates (RA/Dec) to pixel coordinates.
 */
export function skyToPixel(
  wcs: WCSParams,
  ra: number,
  dec: number
): { x: number; y: number } {
  const x = wcs.crpix1 + (ra - wcs.crval1) / wcs.cd1_1;
  const y = wcs.crpix2 + (dec - wcs.crval2) / wcs.cd2_2;
  return { x, y };
}

/**
 * Convert Leaflet L.CRS.Simple coordinates to sky coordinates.
 * In our setup: leaflet lat = pixel Y, leaflet lng = pixel X.
 */
export function leafletToSky(
  wcs: WCSParams,
  lat: number,
  lng: number
): { ra: number; dec: number } {
  return pixelToSky(wcs, lng, lat);
}

/**
 * Convert sky coordinates to Leaflet LatLng values.
 */
export function skyToLeaflet(
  wcs: WCSParams,
  ra: number,
  dec: number
): { lat: number; lng: number } {
  const { x, y } = skyToPixel(wcs, ra, dec);
  return { lat: y, lng: x };
}

/**
 * Format RA in sexagesimal (HH:MM:SS.ss).
 */
export function formatRA(raDeg: number): string {
  const ra = ((raDeg % 360) + 360) % 360;
  const hours = ra / 15;
  const h = Math.floor(hours);
  const m = Math.floor((hours - h) * 60);
  const s = ((hours - h) * 60 - m) * 60;
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`;
}

/**
 * Format Dec in sexagesimal (+DD:MM:SS.s).
 */
export function formatDec(decDeg: number): string {
  const sign = decDeg >= 0 ? '+' : '-';
  const dec = Math.abs(decDeg);
  const d = Math.floor(dec);
  const m = Math.floor((dec - d) * 60);
  const s = ((dec - d) * 60 - m) * 60;
  return `${sign}${d.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
}
