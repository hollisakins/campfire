import { NextRequest } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import {
  compositeTileThumbnail,
  TRANSPARENT_GIF,
  type MapLayerInfo,
  type ShutterInfo,
} from '@/lib/utils/tile-compositing';

/**
 * GET /api/tile-thumbnail?object_id=<id>&size=<px>&shutters=<bool>&fov=<arcsec>
 *
 * Composites map tiles into a thumbnail PNG centered on the object.
 * Optionally draws shutter overlays for the detail/inspection views.
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
  const objectId = params.get('object_id');
  if (!objectId) {
    return new Response(TRANSPARENT_GIF, {
      status: 400,
      headers: { 'Content-Type': 'image/gif' },
    });
  }

  const size = Math.min(600, Math.max(16, parseInt(params.get('size') || '96', 10)));
  const fov = Math.min(30, Math.max(1, parseFloat(params.get('fov') || '5')));
  const withShutters = params.get('shutters') === 'true';

  try {
    // Look up object coordinates
    const { data: obj, error: objErr } = await supabase
      .from('objects')
      .select('ra, dec, field')
      .eq('object_id', objectId)
      .single();

    if (objErr || !obj) {
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

    // Fetch shutters if requested
    let shutters: ShutterInfo[] = [];
    if (withShutters) {
      const { data: shutterData } = await supabase.rpc('get_nearby_shutters', {
        p_ra: obj.ra,
        p_dec: obj.dec,
        p_radius_arcsec: fov,
        p_field: obj.field,
      });
      if (shutterData) {
        shutters = shutterData as ShutterInfo[];
      }
    }

    // Composite the thumbnail
    const png = await compositeTileThumbnail({
      ra: obj.ra,
      dec: obj.dec,
      objectId,
      layer,
      outputSize: size,
      fovArcsec: fov,
      shutters: withShutters ? shutters : undefined,
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
