import { NextRequest, NextResponse } from 'next/server';
import { isColormapName, type StretchMode, type ColormapName } from '@fitsgl/core';
import { createServiceClient } from '@/lib/supabase/server';
import { isAdminUser } from '@/lib/api-helpers';
import { resolveFieldScienceSource, UnknownBandError } from '@/lib/cutout/source';
import { renderFigurePng } from '@/lib/cutout/figure';
import { resolveRequestUser, parseScienceParams } from '../science-params';

/** Single-band panel stretches (trilogy is an RGB-composite mode, not offered here). */
const FIGURE_STRETCHES: StretchMode[] = ['linear', 'log', 'sqrt', 'asinh'];

/**
 * GET /api/v1/cutout/figure?field=<f>&ra=<deg>&dec=<deg>&fov=<arcsec>
 *        [&bands=...][&size=<px>][&cols=<n>][&stretch=asinh][&colormap=gray]
 *
 * Multi-band cutout figure (epic #337, Phase 5): one labeled North-up panel
 * per band — the classic postage-stamp strip — rendered from the field's
 * FitsGL pyramid and returned as a single PNG. Same engine and transfer
 * functions as the interactive map; per-panel percentile stretch.
 *
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
  const { field, ra, dec, fovArcsec, bands } = parsed;

  const params = request.nextUrl.searchParams;
  const parsedSize = parseInt(params.get('size') || '300', 10);
  if (!Number.isFinite(parsedSize)) {
    return NextResponse.json({ error: 'Invalid parameter: size must be a number' }, { status: 400 });
  }
  const panelSize = Math.min(1024, Math.max(64, parsedSize));

  let cols: number | undefined;
  const colsParam = params.get('cols');
  if (colsParam !== null) {
    cols = parseInt(colsParam, 10);
    if (!Number.isFinite(cols) || cols < 1) {
      return NextResponse.json({ error: 'Invalid parameter: cols must be a positive integer' }, { status: 400 });
    }
  }

  const stretch = (params.get('stretch') ?? 'asinh') as StretchMode;
  if (!FIGURE_STRETCHES.includes(stretch)) {
    return NextResponse.json(
      { error: `Invalid stretch; one of: ${FIGURE_STRETCHES.join(', ')}` },
      { status: 400 }
    );
  }
  const colormap = params.get('colormap') ?? 'gray';
  if (!isColormapName(colormap)) {
    return NextResponse.json({ error: 'Invalid colormap' }, { status: 400 });
  }

  try {
    const isAdmin = await isAdminUser(userId);
    const supabase = createServiceClient();

    let src;
    try {
      src = await resolveFieldScienceSource(supabase, field, {
        requirePublic: !isAdmin,
        bands,
      });
    } catch (err) {
      if (err instanceof UnknownBandError) {
        return NextResponse.json(
          { error: err.message, available_bands: err.available },
          { status: 400 }
        );
      }
      throw err;
    }
    if (!src) {
      return NextResponse.json(
        { error: 'No FitsGL dataset for this field' },
        { status: 404 }
      );
    }

    const png = await renderFigurePng(src, {
      center: [ra, dec],
      fovArcsec,
      panelSize,
      cols,
      stretch,
      colormap: colormap as ColormapName,
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
