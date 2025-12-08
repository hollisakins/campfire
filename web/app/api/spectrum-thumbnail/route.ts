import { NextRequest } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import { generateDownloadUrl } from '@/lib/r2';

// Grating priority order
const GRATING_PRIORITY = ['PRISM', 'G395M', 'G235M', 'G140M'];

// SVG dimensions
const SVG_WIDTH = 120;
const SVG_HEIGHT = 40;
const PADDING = 3;

interface SpectrumData {
  wave: number[];
  fnu: (number | null)[];
}

/**
 * Downsample spectrum data to a target number of points
 */
function downsampleSpectrum(
  wave: number[],
  fnu: (number | null)[],
  targetPoints: number = 100
): { wave: number[]; fnu: number[] } {
  // Filter out null values and pair with wavelength
  const validPairs: [number, number][] = [];
  for (let i = 0; i < wave.length && i < fnu.length; i++) {
    if (fnu[i] !== null && !isNaN(fnu[i] as number) && isFinite(fnu[i] as number)) {
      validPairs.push([wave[i], fnu[i] as number]);
    }
  }

  if (validPairs.length === 0) {
    return { wave: [], fnu: [] };
  }

  // If we have fewer points than target, return as-is
  if (validPairs.length <= targetPoints) {
    return {
      wave: validPairs.map(p => p[0]),
      fnu: validPairs.map(p => p[1]),
    };
  }

  // Downsample by taking every nth point
  const step = Math.ceil(validPairs.length / targetPoints);
  const downsampled: [number, number][] = [];

  for (let i = 0; i < validPairs.length; i += step) {
    downsampled.push(validPairs[i]);
  }

  // Ensure we include the last point
  if (downsampled[downsampled.length - 1] !== validPairs[validPairs.length - 1]) {
    downsampled.push(validPairs[validPairs.length - 1]);
  }

  return {
    wave: downsampled.map(p => p[0]),
    fnu: downsampled.map(p => p[1]),
  };
}

/**
 * Generate SVG path from spectrum data
 */
function generateSVGPath(fnu: number[]): string {
  if (fnu.length === 0) return '';

  // Normalize flux to 0-1 range
  const minFnu = Math.min(...fnu);
  const maxFnu = Math.max(...fnu);
  const range = maxFnu - minFnu;

  // Avoid division by zero
  if (range === 0) {
    // Flat line in the middle
    const y = SVG_HEIGHT / 2;
    return `M ${PADDING} ${y} L ${SVG_WIDTH - PADDING} ${y}`;
  }

  const plotWidth = SVG_WIDTH - 2 * PADDING;
  const plotHeight = SVG_HEIGHT - 2 * PADDING;

  // Generate path points
  const pathPoints: string[] = [];

  for (let i = 0; i < fnu.length; i++) {
    const x = PADDING + (i / (fnu.length - 1)) * plotWidth;
    // Normalize and invert Y (SVG Y increases downward)
    const normalizedY = (fnu[i] - minFnu) / range;
    const y = PADDING + (1 - normalizedY) * plotHeight;

    if (i === 0) {
      pathPoints.push(`M ${x.toFixed(1)} ${y.toFixed(1)}`);
    } else {
      pathPoints.push(`L ${x.toFixed(1)} ${y.toFixed(1)}`);
    }
  }

  return pathPoints.join(' ');
}

/**
 * Generate complete SVG string
 */
function generateSVG(fnu: number[], hasData: boolean = true): string {
  if (!hasData || fnu.length === 0) {
    // Return a placeholder SVG with a simple horizontal line
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SVG_WIDTH} ${SVG_HEIGHT}" width="${SVG_WIDTH}" height="${SVG_HEIGHT}">
  <rect width="100%" height="100%" fill="#f3f4f6"/>
  <line x1="${PADDING}" y1="${SVG_HEIGHT / 2}" x2="${SVG_WIDTH - PADDING}" y2="${SVG_HEIGHT / 2}" stroke="#d1d5db" stroke-width="1"/>
</svg>`;
  }

  const path = generateSVGPath(fnu);

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SVG_WIDTH} ${SVG_HEIGHT}" width="${SVG_WIDTH}" height="${SVG_HEIGHT}">
  <rect width="100%" height="100%" fill="#f8fafc"/>
  <path d="${path}" fill="none" stroke="#3b82f6" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;
}

/**
 * GET /api/spectrum-thumbnail?object_id=<object_id>
 *
 * Generates an SVG sparkline thumbnail of the spectrum for the given object.
 * Selects grating by priority: PRISM > G395M > G235M > G140M
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  // Check authentication
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return new Response(generateSVG([], false), {
      status: 401,
      headers: {
        'Content-Type': 'image/svg+xml',
      },
    });
  }

  // Get object_id from query parameters
  const searchParams = request.nextUrl.searchParams;
  const objectId = searchParams.get('object_id');

  if (!objectId) {
    return new Response(generateSVG([], false), {
      status: 400,
      headers: {
        'Content-Type': 'image/svg+xml',
      },
    });
  }

  try {
    // Get available spectra for this object
    const { data: spectra, error: spectraError } = await supabase
      .from('spectra')
      .select('grating, fits_path')
      .eq('object_id', objectId);

    if (spectraError || !spectra || spectra.length === 0) {
      return new Response(generateSVG([], false), {
        headers: {
          'Content-Type': 'image/svg+xml',
          'Cache-Control': 'public, max-age=86400',
        },
      });
    }

    // Select grating by priority
    let selectedSpectrum = null;
    for (const grating of GRATING_PRIORITY) {
      selectedSpectrum = spectra.find(s => s.grating === grating);
      if (selectedSpectrum) break;
    }

    // Fallback to first available if none match priority list
    if (!selectedSpectrum) {
      selectedSpectrum = spectra[0];
    }

    // Get JSON path from FITS path
    const jsonPath = selectedSpectrum.fits_path.replace('.fits', '.json');

    // Generate signed URL for the JSON file
    const signedUrl = await generateDownloadUrl(jsonPath, 3600);

    // Check if R2 is configured
    if (signedUrl.startsWith('#download-placeholder')) {
      return new Response(generateSVG([], false), {
        headers: {
          'Content-Type': 'image/svg+xml',
          'Cache-Control': 'public, max-age=86400',
        },
      });
    }

    // Fetch the JSON data from R2
    const response = await fetch(signedUrl);

    if (!response.ok) {
      console.error('Failed to fetch spectrum JSON:', response.status);
      return new Response(generateSVG([], false), {
        headers: {
          'Content-Type': 'image/svg+xml',
          'Cache-Control': 'public, max-age=86400',
        },
      });
    }

    const data: SpectrumData = await response.json();

    // Downsample and generate SVG
    const { fnu } = downsampleSpectrum(data.wave, data.fnu, 100);
    const svg = generateSVG(fnu, true);

    return new Response(svg, {
      headers: {
        'Content-Type': 'image/svg+xml',
        'Cache-Control': 'public, max-age=86400', // 24 hour cache
      },
    });
  } catch (error) {
    console.error('Error generating spectrum thumbnail:', error);
    return new Response(generateSVG([], false), {
      headers: {
        'Content-Type': 'image/svg+xml',
        'Cache-Control': 'public, max-age=3600', // 1 hour cache for errors
      },
    });
  }
}
