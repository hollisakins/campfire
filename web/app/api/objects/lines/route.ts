import { NextRequest } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';

/** One catalog line's record in a fit (spectrum_line_fits.lines[<name>]). */
export interface ObjectLineRecord {
  label?: string;
  component: 'narrow' | 'broad' | 'doublet';
  wave_rest: number | null;
  wave_obs: number | null;
  flux: number | null;
  flux_err: number | null;
  snr: number | null;
  ew_rest: number | null;
  ew_rest_err: number | null;
  cont: number | null;
  cont_err: number | null;
  dv: number | null;
  dv_err: number | null;
  sigma_v: number | null;
  sigma_v_err: number | null;
  flags: number;
  blend_into: string | null;
  tied_to: string | null;
  members?: string[];
}

/** One spectrum's fit row plus its live staleness. */
export interface ObjectLineFit {
  spectrum_id: number;
  target_id: string;
  grating: string;
  z_used: number;
  z_source: 'inspected' | 'auto';
  z_quality: number;
  z_fit: number | null;
  dv: number | null;
  sigma_v: number | null;
  kin_source: string | null;
  n_lines: number;
  n_detected: number;
  n_broad: number;
  fit_version: string;
  cfpipe_version: string | null;
  fitted_at: string | null;
  stale_redshift: boolean;
  stale_spectrum: boolean;
  lines: Record<string, ObjectLineRecord>;
}

export interface ObjectLinesResponse {
  fits: ObjectLineFit[];
}

const MAX_IDS = 50;
const CACHE = { 'Cache-Control': 'private, max-age=60' };

/**
 * GET /api/objects/lines?spectra=<id>,<id>,...
 *
 * The emission-line fits of an object's member spectra (spectrum_line_fits
 * rows keyed by spectra.id, with the staleness flags of
 * spectrum_line_fits_status), for the object page's "Emission lines"
 * section. Queried under the caller's RLS: a fit is visible iff its spectrum
 * is, so an id the viewer may not read simply does not come back. A GET
 * route, not an action (decision D-C, #506): the section fetches when it
 * scrolls into view and must not queue behind the page's mutations.
 */
export async function GET(request: NextRequest) {
  const principal = await getRequestPrincipal();
  if (!principal) return Response.json({ error: 'Unauthorized' }, { status: 401 });

  const raw = request.nextUrl.searchParams.get('spectra') ?? '';
  const ids = Array.from(new Set(
    raw.split(',').map((s) => parseInt(s, 10)).filter((n) => Number.isInteger(n) && n > 0),
  ));
  if (ids.length === 0) return Response.json({ error: 'Missing spectra ids' }, { status: 400 });
  if (ids.length > MAX_IDS) return Response.json({ error: `At most ${MAX_IDS} spectra per request` }, { status: 400 });

  try {
    const [fitsRes, statusRes] = await Promise.all([
      principal.supabase
        .from('spectrum_line_fits')
        .select('spectrum_id, target_id, grating, z_used, z_source, z_quality, z_fit, dv, sigma_v, kin_source, n_lines, n_detected, n_broad, fit_version, cfpipe_version, fitted_at, lines')
        .in('spectrum_id', ids),
      principal.supabase
        .from('spectrum_line_fits_status')
        .select('spectrum_id, stale_redshift, stale_spectrum')
        .in('spectrum_id', ids),
    ]);
    if (fitsRes.error) {
      console.error('object lines error:', fitsRes.error);
      return Response.json({ error: fitsRes.error.message }, { status: 500 });
    }
    const stale = new Map<number, { stale_redshift: boolean; stale_spectrum: boolean }>();
    for (const row of statusRes.data ?? []) {
      stale.set(row.spectrum_id as number, {
        stale_redshift: Boolean(row.stale_redshift),
        stale_spectrum: Boolean(row.stale_spectrum),
      });
    }
    const fits: ObjectLineFit[] = (fitsRes.data ?? []).map((row) => {
      const st = stale.get(row.spectrum_id as number);
      return {
        spectrum_id: row.spectrum_id as number,
        target_id: row.target_id as string,
        grating: row.grating as string,
        z_used: row.z_used as number,
        z_source: row.z_source as 'inspected' | 'auto',
        z_quality: row.z_quality as number,
        z_fit: (row.z_fit as number | null) ?? null,
        dv: (row.dv as number | null) ?? null,
        sigma_v: (row.sigma_v as number | null) ?? null,
        kin_source: (row.kin_source as string | null) ?? null,
        n_lines: row.n_lines as number,
        n_detected: row.n_detected as number,
        n_broad: row.n_broad as number,
        fit_version: row.fit_version as string,
        cfpipe_version: (row.cfpipe_version as string | null) ?? null,
        fitted_at: (row.fitted_at as string | null) ?? null,
        stale_redshift: st?.stale_redshift ?? false,
        stale_spectrum: st?.stale_spectrum ?? false,
        lines: (row.lines ?? {}) as Record<string, ObjectLineRecord>,
      };
    });
    const body: ObjectLinesResponse = { fits };
    return Response.json(body, { headers: CACHE });
  } catch (err) {
    console.error('object lines error:', err);
    return Response.json({ error: 'Failed to load emission lines' }, { status: 500 });
  }
}
