import { NextRequest, NextResponse } from 'next/server';
import { isColormapName, isStretchMode, type StretchMode, type ColormapName, type TrilogyParams } from '@fitsgl/core';
import { createServiceClient } from '@/lib/supabase/server';
import { isAdminUser, getLinkScope } from '@/lib/api-helpers';
import { resolveFieldScienceSource, UnknownBandError } from '@/lib/cutout/source';
import { renderFigurePng, rgbHasTrilogyStats } from '@/lib/cutout/figure';
import { fetchShuttersInBox, type FigureShutter } from '@/lib/cutout/shutters';
import { resolveRequestUser, parseScienceParams } from '../science-params';

// Multi-band tile decode on a cold instance can exceed a short function budget (#497).
export const maxDuration = 60;

/** Single-band panel stretches (trilogy is an RGB-composite mode, not offered here). */
const FIGURE_STRETCHES: StretchMode[] = ['linear', 'log', 'sqrt', 'asinh'];
const TRUTHY = new Set(['1', 'true', 'yes', 'on']);

/**
 * GET /api/v1/cutout/figure?field=<f>&ra=<deg>&dec=<deg>&fov=<arcsec>
 *        [&bands=...][&rgb=auto|r,g,b][&rgb_stretch=auto|trilogy|asinh|…]
 *        [&noiselum=0.15][&satpercent=0.001][&shutters=1]
 *        [&size=<px>][&cols=<n>][&stretch=asinh][&colormap=gray]
 *
 * Multi-band cutout figure (epic #337, Phase 5): one labeled North-up panel
 * per band — the classic postage-stamp strip — rendered from the field's
 * FitsGL pyramid and returned as a single PNG. Same engine and transfer
 * functions as the interactive map; per-panel percentile stretch.
 *
 * - `bands` single-band panels (default: every band — unless `rgb` is given,
 *   when the default is none, so `rgb` alone yields just the composite)
 * - `rgb` appends an RGB composite panel: `auto` (the dataset's default
 *   view, or reddest/middle/bluest by wavelength) or three band names `r,g,b`
 * - `rgb_stretch` the composite's transfer: `trilogy` (each band on its own
 *   precomputed levels — the map's faithful composite) or a plain curve over
 *   one shared range (the map's simple RGB); `auto` picks trilogy when the
 *   dataset carries the stats
 * - `noiselum` / `satpercent` trilogy knobs over the producer's tuning
 * - `shutters=1` overlays the NIRSpec MSA shutter footprints in view
 * - `size` panel edge in px, clamped 64–1024 (default 300)
 * - `cols` panels per row (default: all in one row)
 *
 * Auth: Bearer API key / JWT, or the browser cookie session (used by the
 * cutout GUI). Non-admins only reach published-backed pyramids.
 */
export async function GET(request: NextRequest) {
  const userId = await resolveRequestUser(request);
  if (!userId) {
    return NextResponse.json({ error: 'Invalid or missing credentials' }, { status: 401 });
  }

  const parsed = parseScienceParams(request);
  if (parsed instanceof NextResponse) return parsed;
  // (no `scale` here: figure level selection follows the panel size)
  const { field, ra, dec, fovArcsec } = parsed;

  const params = request.nextUrl.searchParams;
  const bad = (error: string) => NextResponse.json({ error }, { status: 400 });

  const parsedSize = parseInt(params.get('size') || '300', 10);
  if (!Number.isFinite(parsedSize)) return bad('Invalid parameter: size must be a number');
  const panelSize = Math.min(1024, Math.max(64, parsedSize));

  let cols: number | undefined;
  const colsParam = params.get('cols');
  if (colsParam !== null) {
    cols = parseInt(colsParam, 10);
    if (!Number.isFinite(cols) || cols < 1) return bad('Invalid parameter: cols must be a positive integer');
  }

  const stretch = (params.get('stretch') ?? 'asinh') as StretchMode;
  if (!FIGURE_STRETCHES.includes(stretch)) {
    return bad(`Invalid stretch; one of: ${FIGURE_STRETCHES.join(', ')}`);
  }
  const colormap = params.get('colormap') ?? 'gray';
  if (!isColormapName(colormap)) return bad('Invalid colormap');

  // RGB composite panel.
  let rgb: 'auto' | string[] | undefined;
  const rgbParam = params.get('rgb');
  if (rgbParam !== null) {
    const v = rgbParam.trim().toLowerCase();
    if (v === '' || v === 'auto' || TRUTHY.has(v)) {
      rgb = 'auto';
    } else {
      const names = v.split(',').map((b) => b.trim()).filter(Boolean);
      if (names.length !== 3) return bad('Invalid parameter: rgb must be "auto" or three band names r,g,b');
      rgb = names;
    }
  }
  const rgbStretchParam = (params.get('rgb_stretch') ?? 'auto').trim().toLowerCase();
  if (rgbStretchParam !== 'auto' && !isStretchMode(rgbStretchParam)) {
    return bad('Invalid rgb_stretch; one of: auto, trilogy, linear, log, sqrt, asinh');
  }
  const trilogy: Partial<TrilogyParams> = {};
  const noiselumParam = params.get('noiselum');
  if (noiselumParam !== null) {
    const v = parseFloat(noiselumParam);
    if (!(v > 0 && v < 1)) return bad('Invalid parameter: noiselum must be in (0, 1)');
    trilogy.noiselum = v;
  }
  const satpercentParam = params.get('satpercent');
  if (satpercentParam !== null) {
    const v = parseFloat(satpercentParam);
    if (!(v > 0 && v <= 50)) return bad('Invalid parameter: satpercent must be in (0, 50]');
    trilogy.satpercent = v;
  }
  const wantShutters = TRUTHY.has((params.get('shutters') ?? '').trim().toLowerCase());

  // `rgb` alone means "just the composite": single-band panels then need an
  // explicit `bands`. Without `rgb` the historical default (every band) holds.
  const bands = parsed.bands ?? (rgb !== undefined ? [] : undefined);

  try {
    // Service-role authorization, so the link scope is enforced here (see the
    // sibling /fits route): only the link's own field, 404 indistinguishable
    // from a missing dataset. No allow_download gate — a rendered PNG is
    // display, not FITS bytes, matching the RLS split (nircam_images vs
    // storage_objects).
    const linkScope = await getLinkScope(userId);
    if (linkScope && (!linkScope.active || linkScope.field === null || linkScope.field !== field)) {
      return NextResponse.json({ error: 'No FitsGL dataset for this field' }, { status: 404 });
    }

    const isAdmin = await isAdminUser(userId);
    const supabase = createServiceClient();

    // The shutters policy narrows a share-link account to its one observation,
    // which the field-scoped figure never has — so a link gets no overlay.
    // Otherwise mirror the policy's publication gate (lib/cutout/shutters.ts).
    // Shutters partly inside the box still matter, so search a little wider
    // than the half-FOV; the panel's nested <svg> clips the rest.
    const shuttersPromise: Promise<FigureShutter[]> =
      wantShutters && !linkScope
        ? fetchShuttersInBox(supabase, {
            field, ra, dec, halfArcsec: fovArcsec * 0.75, includeUnpublished: isAdmin,
          })
        : Promise.resolve([]);
    // An early 4xx below must not leave this rejection unobserved; the real
    // `await` further down still surfaces the failure.
    shuttersPromise.catch(() => undefined);

    let src;
    try {
      src = await resolveFieldScienceSource(supabase, field, { requirePublic: !isAdmin, bands, rgb });
    } catch (err) {
      if (err instanceof UnknownBandError) {
        return NextResponse.json({ error: err.message, available_bands: err.available }, { status: 400 });
      }
      throw err;
    }
    if (!src) {
      return NextResponse.json({ error: 'No FitsGL dataset for this field' }, { status: 404 });
    }
    if (rgb !== undefined && !src.rgb) {
      return bad('An RGB composite needs a dataset with at least 3 bands');
    }
    let rgbStretch: StretchMode | undefined;
    if (src.rgb && rgb !== undefined) {
      const hasStats = rgbHasTrilogyStats(src.rgb);
      rgbStretch = rgbStretchParam === 'auto' ? (hasStats ? 'trilogy' : 'asinh') : rgbStretchParam;
      if (rgbStretch === 'trilogy' && !hasStats) {
        return bad('rgb_stretch=trilogy needs precomputed trilogy stats on every RGB band; this dataset has none');
      }
    }
    if (src.bands.length === 0 && !rgbStretch) return bad('No panels requested');

    const shutters = await shuttersPromise;
    const png = await renderFigurePng(src, {
      center: [ra, dec],
      fovArcsec,
      panelSize,
      cols,
      stretch,
      colormap: colormap as ColormapName,
      ...(rgbStretch && { rgb: { stretch: rgbStretch, trilogy } }),
      shutters,
    });

    return new Response(new Uint8Array(png), {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': 'private, max-age=3600',
      },
    });
  } catch (error) {
    console.error('Error in API /v1/cutout/figure:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
