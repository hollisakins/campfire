import { NextRequest } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import {
  compositeTileThumbnail,
  TRANSPARENT_GIF,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';

/**
 * GET /api/tile-thumbnail?target_id=<id>&size=<px>&fov=<arcsec>
 *
 * Composites map tiles into a thumbnail PNG centered on the object.
 * Returns a clean RGB cutout without shutter overlays.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  // Auth check
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return new Response(TRANSPARENT_GIF, {
      status: 401,
      headers: { 'Content-Type': 'image/gif' },
    });
  }

  // Parse params
  const params = request.nextUrl.searchParams;
  const targetId = params.get('target_id');
  if (!targetId) {
    return new Response(TRANSPARENT_GIF, {
      status: 400,
      headers: { 'Content-Type': 'image/gif' },
    });
  }

  const size = Math.min(600, Math.max(16, parseInt(params.get('size') || '96', 10)));
  const fov = Math.min(30, Math.max(1, parseFloat(params.get('fov') || '5')));

  try {
    // Look up coordinates — try targets first, fall back to objects
    let obj: { ra: number; dec: number; field: string } | null = null;

    const { data: target } = await supabase
      .from('targets')
      .select('ra, dec, field')
      .eq('target_id', targetId)
      .single();

    if (target) {
      obj = target;
    } else {
      const { data: object } = await supabase
        .from('objects')
        .select('ra, dec, field')
        .eq('object_id', targetId)
        .single();
      obj = object;
    }

    if (!obj) {
      return new Response(TRANSPARENT_GIF, {
        status: 404,
        headers: { 'Content-Type': 'image/gif' },
      });
    }

    // Get RGB map layer for this field
    const { data: layers, error: layerErr } = await supabase
      .from('map_layers')
      .select('tile_base_url, min_zoom, max_zoom, tile_size, wcs_params, tile_version, is_default, filter')
      .eq('field', obj.field)
      .order('filter');

    if (layerErr || !layers || layers.length === 0) {
      return new Response(TRANSPARENT_GIF, {
        status: 404,
        headers: { 'Content-Type': 'image/gif' },
      });
    }

    const layer: MapLayerInfo = (
      layers.find(l => l.filter === 'rgb')
      || layers.find(l => l.is_default)
      || layers[0]
    ) as MapLayerInfo;

    // Composite the thumbnail
    const png = await compositeTileThumbnail({
      ra: obj.ra,
      dec: obj.dec,
      layer,
      outputSize: size,
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
    console.error('Error generating tile thumbnail:', error);
    return new Response(TRANSPARENT_GIF, {
      status: 500,
      headers: { 'Content-Type': 'image/gif' },
    });
  }
}
