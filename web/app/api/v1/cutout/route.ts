import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { validateAuth } from '@/lib/api-auth';
import { getAccessiblePrograms } from '@/lib/api-helpers';
import {
  compositeTileThumbnail,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';
import type { WCSParams } from '@/lib/utils/wcs';

/**
 * GET /api/v1/cutout?object_id=<id>&size=<px>&fov=<arcsec>
 *
 * Returns a PNG cutout image centered on the object, composited from
 * pre-generated RGB map tiles. No shutter overlays — clients render
 * those as vectors (SVG in browser, matplotlib patches in Python).
 *
 * Query parameters:
 * - object_id (required): Object identifier
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

    const fov = Math.min(30, Math.max(1, parseFloat(params.get('fov') || '5')));

    // Look up object
    const { data: obj, error: objErr } = await supabase
      .from('objects')
      .select('ra, dec, field, program_slug')
      .eq('object_id', objectId)
      .single();

    if (objErr || !obj) {
      return NextResponse.json(
        { error: 'Object not found' },
        { status: 404 }
      );
    }

    if (!accessibleProgramSlugs.includes(obj.program_slug)) {
      return NextResponse.json(
        { error: 'Access denied to this object' },
        { status: 403 }
      );
    }

    // Get RGB map layer for this field
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

    // Compute native resolution (pixels in FOV at the tile's pixel scale)
    const wcs = layer.wcs_params as WCSParams;
    const pixPerArcsec = 1 / (Math.abs(wcs.cd2_2) * 3600);
    const nativeSize = Math.round(fov * pixPerArcsec);

    // Use requested size or native resolution, clamped to 16–2048
    const sizeParam = params.get('size');
    const outputSize = sizeParam
      ? Math.min(2048, Math.max(16, parseInt(sizeParam, 10)))
      : Math.min(2048, Math.max(16, nativeSize));

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
        'Cache-Control': 'private, max-age=3600',
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
