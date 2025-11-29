/**
 * Coordinate conversion utilities for astronomical coordinates
 */

/**
 * Convert decimal degrees to sexagesimal format for Right Ascension (hours)
 * @param degrees - RA in decimal degrees (0-360)
 * @returns Formatted string like "02h17m52.90s"
 */
export function decimalToRAHours(degrees: number): string {
  // Convert degrees to hours (24 hours = 360 degrees)
  const hours = degrees / 15;

  const h = Math.floor(hours);
  const remainderMinutes = (hours - h) * 60;
  const m = Math.floor(remainderMinutes);
  const s = (remainderMinutes - m) * 60;

  return `${h.toString().padStart(2, '0')}h${m.toString().padStart(2, '0')}m${s.toFixed(2)}s`;
}

/**
 * Convert decimal degrees to sexagesimal format for Declination (degrees)
 * @param degrees - Dec in decimal degrees (-90 to +90)
 * @returns Formatted string like "-05°05'16.83\""
 */
export function decimalToDecDegrees(degrees: number): string {
  const sign = degrees >= 0 ? '+' : '-';
  const absDegrees = Math.abs(degrees);

  const d = Math.floor(absDegrees);
  const remainderMinutes = (absDegrees - d) * 60;
  const m = Math.floor(remainderMinutes);
  const s = (remainderMinutes - m) * 60;

  return `${sign}${d.toString().padStart(2, '0')}°${m.toString().padStart(2, '0')}'${s.toFixed(2)}"`;
}

/**
 * Format coordinates in both decimal and sexagesimal formats
 * @param ra - Right Ascension in decimal degrees
 * @param dec - Declination in decimal degrees
 * @returns Object with both formats
 */
export function formatCoordinates(ra: number, dec: number) {
  return {
    decimal: {
      ra: ra.toFixed(6),
      dec: dec.toFixed(6),
      combined: `${ra.toFixed(6)}, ${dec.toFixed(6)}`,
    },
    sexagesimal: {
      ra: decimalToRAHours(ra),
      dec: decimalToDecDegrees(dec),
      combined: `${decimalToRAHours(ra)}, ${decimalToDecDegrees(dec)}`,
    },
  };
}
