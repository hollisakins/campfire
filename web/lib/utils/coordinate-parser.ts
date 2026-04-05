/**
 * Coordinate parsing utilities for various astronomical coordinate formats
 */

export interface ParsedCoordinates {
  ra: number;   // Right Ascension in decimal degrees (0-360)
  dec: number;  // Declination in decimal degrees (-90 to +90)
}

/**
 * Parse sexagesimal RA in hours format (e.g., "10h02m30.5s" or "10:02:30.5")
 * @returns RA in decimal degrees
 */
function parseSexagesimalRA(raStr: string): number | null {
  // Match formats like "10h02m30.5s" or "10:02:30.5"
  const hmsPattern = /^(\d+)h\s*(\d+)m\s*([\d.]+)s$/i;
  const colonPattern = /^(\d+):(\d+):([\d.]+)$/;

  const match = raStr.match(hmsPattern) || raStr.match(colonPattern);
  if (!match) return null;

  const hours = parseFloat(match[1]);
  const minutes = parseFloat(match[2]);
  const seconds = parseFloat(match[3]);

  if (hours < 0 || hours >= 24 || minutes < 0 || minutes >= 60 || seconds < 0 || seconds >= 60) {
    return null;
  }

  // Convert to decimal degrees: hours * 15 + minutes / 4 + seconds / 240
  return hours * 15 + minutes / 4 + seconds / 240;
}

/**
 * Parse sexagesimal Dec in degrees format (e.g., "-05d18m30.5s" or "-05:18:30.5")
 * @returns Dec in decimal degrees
 */
function parseSexagesimalDec(decStr: string): number | null {
  // Match formats like "+05d18m30.5s", "-05°18'30.5\"", or "-05:18:30.5"
  const dmsPattern = /^([+-]?)(\d+)[d°]\s*(\d+)[m']\s*([\d.]+)[s"]?$/i;
  const colonPattern = /^([+-]?)(\d+):(\d+):([\d.]+)$/;

  const match = decStr.match(dmsPattern) || decStr.match(colonPattern);
  if (!match) return null;

  const sign = match[1] === '-' ? -1 : 1;
  const degrees = parseFloat(match[2]);
  const minutes = parseFloat(match[3]);
  const seconds = parseFloat(match[4]);

  if (degrees < 0 || degrees > 90 || minutes < 0 || minutes >= 60 || seconds < 0 || seconds >= 60) {
    return null;
  }

  // Convert to decimal degrees
  const decimalDegrees = degrees + minutes / 60 + seconds / 3600;
  return sign * decimalDegrees;
}

/**
 * Parse coordinate string in various formats
 * Supported formats:
 * - Decimal degrees: "150.5 -2.3", "150.5, -2.3"
 * - Sexagesimal: "10h02m30s -02d18m30s", "10:02:30 -02:18:30"
 *
 * @param coordStr - Coordinate string to parse
 * @returns Parsed coordinates or null if invalid
 */
export function parseCoordinates(coordStr: string): ParsedCoordinates | null {
  if (!coordStr || coordStr.trim() === '') {
    return null;
  }

  const trimmed = coordStr.trim();

  // Try to split by common delimiters (space, comma, tab)
  const parts = trimmed.split(/[\s,]+/).filter(p => p.length > 0);

  if (parts.length !== 2) {
    return null;
  }

  let ra: number | null = null;
  let dec: number | null = null;

  // Try parsing first part as RA
  if (parts[0].includes('h') || parts[0].includes(':')) {
    // Sexagesimal RA
    ra = parseSexagesimalRA(parts[0]);
  } else {
    // Decimal degrees
    ra = parseFloat(parts[0]);
    if (isNaN(ra)) return null;
  }

  // Try parsing second part as Dec
  if (parts[1].includes('d') || parts[1].includes('°') ||
      parts[1].includes("'") || parts[1].includes('"') ||
      parts[1].includes(':')) {
    // Sexagesimal Dec
    dec = parseSexagesimalDec(parts[1]);
  } else {
    // Decimal degrees
    dec = parseFloat(parts[1]);
    if (isNaN(dec)) return null;
  }

  // Validate ranges
  if (ra === null || dec === null) {
    return null;
  }

  if (ra < 0 || ra >= 360) {
    return null;
  }

  if (dec < -90 || dec > 90) {
    return null;
  }

  return { ra, dec };
}

/**
 * Format distance based on magnitude
 * Uses abbreviated symbols: ° (degrees), ' (arcmin), " (arcsec)
 *
 * @param distanceDegrees - Distance in decimal degrees
 * @returns Formatted string (e.g., "45.3\"", "2.5'", "0.5°")
 */
export function formatDistance(distanceDegrees: number): string {
  const arcsec = distanceDegrees * 3600;

  if (arcsec < 60) {
    // Show in arcseconds
    return `${arcsec.toFixed(1)}"`;
  } else if (arcsec < 3600) {
    // Show in arcminutes
    const arcmin = arcsec / 60;
    return `${arcmin.toFixed(2)}'`;
  } else {
    // Show in degrees
    return `${distanceDegrees.toFixed(3)}°`;
  }
}

/**
 * Convert radius to degrees based on unit
 */
export function convertRadiusToDegrees(radius: number, unit: 'degrees' | 'arcmin' | 'arcsec'): number {
  switch (unit) {
    case 'degrees':
      return radius;
    case 'arcmin':
      return radius / 60;
    case 'arcsec':
      return radius / 3600;
    default:
      return radius;
  }
}
