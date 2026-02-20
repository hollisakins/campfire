/**
 * WCS coordinate conversion utilities for the map viewer.
 *
 * Uses TAN (gnomonic) projection for accurate pixel ↔ sky conversion.
 * The CD matrix is diagonal (North-up, no rotation):
 *   CD1_1 = -pixel_scale_deg / cos(dec_ref)
 *   CD2_2 = +pixel_scale_deg
 *   CD1_2 = CD2_1 = 0
 */

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

export interface WCSParams {
  crpix1: number;
  crpix2: number;
  crval1: number; // RA reference (degrees)
  crval2: number; // Dec reference (degrees)
  cd1_1: number;  // -pixel_scale_deg / cos(dec_ref)
  cd2_2: number;  // +pixel_scale_deg
  naxis1: number;
  naxis2: number;
}

/**
 * Convert pixel coordinates to sky coordinates (RA/Dec in degrees)
 * using TAN (gnomonic) projection.
 *
 * Pixel coordinates are 0-based (matching Leaflet / array indexing).
 * CRPIX from the FITS header is 1-based, so we subtract 1.
 */
export function pixelToSky(
  wcs: WCSParams,
  pixX: number,
  pixY: number
): { ra: number; dec: number } {
  // Intermediate world coordinates (degrees)
  // CRPIX is FITS 1-based; Leaflet pixel centers are at row - 0.5.
  // Combined: crpix - 1 (1-based→0-based) - 0.5 (pixel center) = crpix - 1.5
  const xi = wcs.cd1_1 * (pixX - (wcs.crpix1 - 1.5));
  const eta = wcs.cd2_2 * (pixY - (wcs.crpix2 - 1.5));

  // Convert to radians for TAN deprojection
  const xiRad = xi * DEG2RAD;
  const etaRad = eta * DEG2RAD;
  const dec0Rad = wcs.crval2 * DEG2RAD;
  const ra0Rad = wcs.crval1 * DEG2RAD;

  const sinDec0 = Math.sin(dec0Rad);
  const cosDec0 = Math.cos(dec0Rad);
  const denom = cosDec0 - etaRad * sinDec0;

  const ra = ra0Rad + Math.atan2(xiRad, denom);
  const dec = Math.atan2(
    (sinDec0 + etaRad * cosDec0) * Math.cos(ra - ra0Rad),
    denom
  );

  return { ra: ra * RAD2DEG, dec: dec * RAD2DEG };
}

/**
 * Convert sky coordinates (RA/Dec) to pixel coordinates
 * using TAN (gnomonic) projection.
 */
export function skyToPixel(
  wcs: WCSParams,
  ra: number,
  dec: number
): { x: number; y: number } {
  const raRad = ra * DEG2RAD;
  const decRad = dec * DEG2RAD;
  const ra0Rad = wcs.crval1 * DEG2RAD;
  const dec0Rad = wcs.crval2 * DEG2RAD;

  const sinDec = Math.sin(decRad);
  const cosDec = Math.cos(decRad);
  const sinDec0 = Math.sin(dec0Rad);
  const cosDec0 = Math.cos(dec0Rad);
  const deltaRa = raRad - ra0Rad;
  const cosD = Math.cos(deltaRa);

  // TAN projection: (xi, eta) in radians
  const denom = sinDec * sinDec0 + cosDec * cosDec0 * cosD;
  const xiRad = (cosDec * Math.sin(deltaRa)) / denom;
  const etaRad = (sinDec * cosDec0 - cosDec * sinDec0 * cosD) / denom;

  // Convert to degrees and then to Leaflet pixel coords via inverse CD matrix
  // CRPIX is FITS 1-based; Leaflet pixel centers are at row - 0.5.
  // Combined: crpix - 1 (1-based→0-based) - 0.5 (pixel center) = crpix - 1.5
  const xi = xiRad * RAD2DEG;
  const eta = etaRad * RAD2DEG;
  const x = (wcs.crpix1 - 1.5) + xi / wcs.cd1_1;
  const y = (wcs.crpix2 - 1.5) + eta / wcs.cd2_2;

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
