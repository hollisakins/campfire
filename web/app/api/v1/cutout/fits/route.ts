import { NextRequest, NextResponse } from 'next/server';
import { createServiceClient } from '@/lib/supabase/server';
import { isAdminUser } from '@/lib/api-helpers';
import { resolveFieldScienceSource, UnknownBandError } from '@/lib/cutout/source';
import { buildFitsCutout, CutoutTooLargeError } from '@/lib/cutout/science';
import { resolveRequestUser, parseScienceParams, safeToken } from '../science-params';

/**
 * GET /api/v1/cutout/fits?field=<f>&ra=<deg>&dec=<deg>&fov=<arcsec>
 *                        [&bands=f115w,f277w,f444w][&scale=<arcsec/px>]
 *
 * Science FITS cutout from a field's FitsGL tile pyramid (epic #337, Phase 5):
 * a direct crop of the tiles at the requested (default native) pyramid level —
 * no resampling, no stretch. Multi-extension FITS: empty primary + one float32
 * IMAGE extension per band, each carrying the level's WCS with CRPIX shifted
 * to the crop. Pixels are the display pyramid's RICE-quantized values
 * (~0.03% photometry-faithful; flagged in the headers).
 *
 * - `fov` arcsec, clamped 0.5–600 (default 10)
 * - `bands` comma-separated subset (default: every band in the dataset)
 * - `scale` selects a coarser pyramid level for wide fields (default native)
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
  const { field, ra, dec, fovArcsec, bands, targetScaleArcsec } = parsed;

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

    let fits: Buffer;
    try {
      fits = await buildFitsCutout(src, { center: [ra, dec], fovArcsec, targetScaleArcsec, field });
    } catch (err) {
      if (err instanceof CutoutTooLargeError) {
        return NextResponse.json({ error: err.message }, { status: 400 });
      }
      throw err;
    }

    const filename = `campfire_${safeToken(field)}_${ra.toFixed(5)}_${dec.toFixed(5)}_${fovArcsec}as.fits`;
    return new Response(new Uint8Array(fits), {
      status: 200,
      headers: {
        'Content-Type': 'application/fits',
        'Content-Disposition': `attachment; filename="${filename}"`,
        'Cache-Control': 'private, max-age=3600',
      },
    });
  } catch (error) {
    console.error('Error in API /v1/cutout/fits:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
