import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms, isAdminUser } from '@/lib/api-helpers';
import {
  compositeTileThumbnail,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';
import type { WCSParams } from '@/lib/utils/wcs';
import { resolveFieldCutoutSource } from '@/lib/cutout/source';
import { renderDisplayCutoutPng } from '@/lib/cutout/display';

// Tile decode + reprojection + PNG encode for up to 2048 px on a cold instance (#497).
export const maxDuration = 60;

// Bearer-authenticated and program-scoped: bearer requests bypass Vercel's
// shared cache anyway, so `private` states the truth for the client's own
// cache instead of an inert `public` (#497).
const PRIVATE_LONG = 'private, max-age=604800, stale-while-revalidate=86400';

/**
 * GET /api/v1/cutout?object_id=<id>&size=<px>&fov=<arcsec>
 *
 * Returns a PNG cutout image centered on the object. Fields with a deployed
 * FitsGL pyramid render North-up from the FITS tiles (epic #337, Phase 5);
 * others composite from the legacy pre-generated RGB map tiles. No shutter
 * overlays — clients render those as vectors (SVG in browser, matplotlib
 * patches in Python).
 *
 * Query parameters:
 * - object_id (required): Object identifier (IAU name from objects.object_id)
 * - size (optional): Output size in pixels. Defaults to native resolution
 *   for the requested FOV. Clamped to 16–2048.
 * - fov (optional, default 5): Field of view in arcseconds, clamped to 1–30.
 */
export async function GET(request: NextRequest) {
  const userId = await validateAuth(request);

  if (!userId) {
    return NextResponse.json(
      { error: 'Invalid or missing API key' },
      { status: 401 }
    );
  }

  try {
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Access control
    const accessibleProgramSlugs = await getAccessiblePrograms(userId);
    if (accessibleProgramSlugs.length === 0) {
      return NextResponse.json(
        { error: 'No accessible programs' },
        { status: 403 }
      );
    }

    // Parse params
    const params = request.nextUrl.searchParams;
    const objectId = params.get('object_id');
    if (!objectId) {
      return NextResponse.json(
        { error: 'Missing required parameter: object_id' },
        { status: 400 }
      );
    }

    const parsedFov = parseFloat(params.get('fov') || '5');
    if (!Number.isFinite(parsedFov)) {
      return NextResponse.json(
        { error: 'Invalid parameter: fov must be a finite number' },
        { status: 400 }
      );
    }
    const fov = Math.min(30, Math.max(1, parsedFov));

    // Look up object. Service-role read bypasses RLS, so gate objects with no
    // published spectrum behind admin. No-op in B1.
    const isAdmin = await isAdminUser(userId);
    let objQuery = supabase
      .from('objects')
      .select('ra, dec, field, programs')
      .eq('object_id', objectId);
    if (!isAdmin) {
      objQuery = objQuery.eq('has_published_spectrum', true);
    }
    const { data: obj, error: objErr } = await objQuery.single();

    if (objErr || !obj) {
      return NextResponse.json(
        { error: 'Object not found' },
        { status: 404 }
      );
    }

    const objectPrograms: string[] = obj.programs ?? [];
    if (!objectPrograms.some(p => accessibleProgramSlugs.includes(p))) {
      return NextResponse.json(
        { error: 'Access denied to this object' },
        { status: 403 }
      );
    }

    // No link-scope check here: this route is bearer-only (validateAuth), and
    // validateAuth refuses link accounts outright — a link visitor's browser
    // session never reaches it, and the cookie-capable cutout routes
    // (/cutout/fits, /cutout/figure) carry their own scope checks.

    // Requested size, validated once; the default (native resolution for the
    // FOV) depends on which tile stack serves the field, so it's applied below.
    const sizeParam = params.get('size');
    let requestedSize: number | null = null;
    if (sizeParam !== null) {
      requestedSize = parseInt(sizeParam, 10);
      if (!Number.isFinite(requestedSize)) {
        return NextResponse.json(
          { error: 'Invalid parameter: size must be a number' },
          { status: 400 }
        );
      }
    }
    const clampSize = (px: number) => Math.min(2048, Math.max(16, px));

    // FitsGL path (epic #337, Phase 5). Service-role client bypasses RLS, so
    // non-admins mirror the fitsgl_datasets policy via requirePublic; admins
    // may render from draft-backed pyramids (matching the map).
    const fitsglSrc = await resolveFieldCutoutSource(supabase, obj.field, {
      requirePublic: !isAdmin,
    });
    if (fitsglSrc) {
      try {
        const outputSize = clampSize(
          requestedSize ?? Math.round(fov / fitsglSrc.nativeScaleArcsec),
        );
        const png = await renderDisplayCutoutPng(fitsglSrc, {
          ra: obj.ra,
          dec: obj.dec,
          fovArcsec: fov,
          outputSize,
        });
        return new Response(new Uint8Array(png), {
          status: 200,
          headers: {
            'Content-Type': 'image/png',
            // A draft-backed render (admin API key) must not sit in any
            // cache keyed on the URL alone.
            'Cache-Control': fitsglSrc.isPublic ? PRIVATE_LONG : 'private, no-store',
          },
        });
      } catch (err) {
        console.error('FitsGL cutout render failed; falling back to PNG tiles:', err);
      }
    }

    // Legacy path: composite from pre-generated RGB map tiles.
    const { data: layers, error: layerErr } = await supabase
      .from('map_layers')
      .select('tile_base_url, min_zoom, max_zoom, tile_size, wcs_params, tile_version, is_default, filter')
      .eq('field', obj.field)
      .order('filter');

    if (layerErr || !layers || layers.length === 0) {
      return NextResponse.json(
        { error: 'No map layers found for this field' },
        { status: 404 }
      );
    }

    const layer: MapLayerInfo = (
      layers.find(l => l.filter === 'rgb')
      || layers.find(l => l.is_default)
      || layers[0]
    ) as MapLayerInfo;

    // Native resolution (pixels in FOV at the tile's pixel scale)
    const wcs = layer.wcs_params as WCSParams;
    const pixPerArcsec = 1 / (Math.abs(wcs.cd2_2) * 3600);
    const outputSize = clampSize(requestedSize ?? Math.round(fov * pixPerArcsec));

    // Composite the thumbnail
    const png = await compositeTileThumbnail({
      ra: obj.ra,
      dec: obj.dec,
      layer,
      outputSize,
      fovArcsec: fov,
    });

    return new Response(new Uint8Array(png), {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': PRIVATE_LONG,
      },
    });
  } catch (error) {
    console.error('Error in API /v1/cutout:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}
