import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { deriveSibling } from '@/lib/layout';
import { streamSidecar } from '@/lib/server/sidecar-stream';

/**
 * One fitted line of the sidecar's summary: enough to place and label it on
 * the plot. The full record (EWs, kinematics, errors of everything) is on the
 * `spectrum_line_fits` row, served by /api/objects/lines. Non-finite values in
 * the deploy-side FITS are JSON null (python/campfire/deploy/generate.py).
 */
export interface LineFitLine {
  name: string;
  component: 'narrow' | 'broad' | 'doublet';
  wave_obs: number | null;
  flux: number | null;
  flux_err: number | null;
  snr: number | null;
  flags: number;
  blend_into: string | null;
}

/**
 * The `_lines.json` sidecar (layout kind `nirspec_lines_json`, emitted by
 * `campfire deploy lines` from the `_lines.fits` MODEL extension): the fitted
 * model and the continuum on the spectrum's own wavelength grid in fν (μJy,
 * the unit of the spectrum payload), null outside the fitted windows, plus
 * the provenance scalars of the fit. What the spectrum plot's "Lines" toggle
 * draws.
 */
export interface LineFitData {
  fit_version: string;
  z_used: number | null;
  z_source: string;
  z_quality: number | null;
  z_fit: number | null;
  dv: number | null;
  sigma_v: number | null;
  chi2: number | null;
  dof: number | null;
  n_lines: number | null;
  n_detected: number | null;
  wave: (number | null)[];
  model_fnu: (number | null)[];
  cont_fnu: (number | null)[];
  lines: LineFitLine[];
}

/**
 * GET /api/line-fit?path=<fits_path>
 *
 * Streams the emission-line fit sidecar of a spectrum. The fallback byte
 * path when the delivery front is not configured or its url failed; the
 * client asks /api/spectrum/sidecars first (`lines` / `has_lines`). Mirrors
 * /api/redshift-fit.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  const fitsPath = request.nextUrl.searchParams.get('path');
  if (!fitsPath) {
    return NextResponse.json({ error: 'Missing path parameter' }, { status: 400 });
  }

  try {
    // Access is the spectrum's: the sidecar is readable iff the spectrum row
    // is (program access, publish gate, share-link scope — all under RLS).
    const { data: spectrum, error: spectrumError } = await supabase
      .from('spectra')
      .select('id')
      .eq('fits_path', fitsPath)
      .single();

    if (spectrumError || !spectrum) {
      return NextResponse.json({ error: 'File not found or access denied' }, { status: 404 });
    }

    const linesJsonPath = deriveSibling(fitsPath, 'nirspec_lines_json');
    // Program-scoped, cookie-authenticated: browser-cacheable for a day, never
    // shared-cacheable (#497). A re-fit re-deploys under a new content hash;
    // the day-long private cache is the same trade-off /api/redshift-fit makes.
    const sidecar = await streamSidecar(linesJsonPath, 'private, max-age=86400, stale-while-revalidate=3600');
    if (sidecar.status === 'ok') return sidecar.response;
    if (sidecar.status === 'missing') {
      // No line fit for this spectrum (not inspected yet, or not deployed).
      return NextResponse.json({ error: 'Line fit not available for this spectrum' }, { status: 404 });
    }
    console.error('Failed to fetch line-fit JSON:', sidecar.upstreamStatus);
    return NextResponse.json({ error: 'Failed to fetch line fit' }, { status: 502 });
  } catch (error) {
    console.error('Error fetching line fit:', error);
    return NextResponse.json({ error: 'Failed to fetch line fit' }, { status: 500 });
  }
}
