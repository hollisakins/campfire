import { NextRequest } from 'next/server';
import { createServiceClient } from '@/lib/supabase/server';
import {
  compositeTileThumbnail,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';

/**
 * GET /api/og-image/[id]
 *
 * Serves tile-composited RGB images publicly for social media crawlers.
 * - No authentication required (unlike /api/tile-thumbnail)
 * - Returns image bytes directly
 * - Aggressive caching (1 week) since tiles rarely change
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const targetId = decodeURIComponent(id);

  try {
    const supabase = createServiceClient();

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
      return new Response('Image not found', { status: 404 });
    }

    // Get RGB map layer for this field
    const { data: layers, error: layerErr } = await supabase
      .from('map_layers')
      .select('tile_base_url, min_zoom, max_zoom, tile_size, wcs_params, tile_version, is_default, filter')
      .eq('field', obj.field)
      .order('filter');

    if (layerErr || !layers || layers.length === 0) {
      return new Response('Image not found', { status: 404 });
    }

    const layer: MapLayerInfo = (
      layers.find(l => l.filter === 'rgb')
      || layers.find(l => l.is_default)
      || layers[0]
    ) as MapLayerInfo;

    // Composite thumbnail (no shutters for OG images)
    const png = await compositeTileThumbnail({
      ra: obj.ra,
      dec: obj.dec,
      layer,
      outputSize: 300,
      fovArcsec: 5,
    });

    return new Response(new Uint8Array(png), {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': 'public, max-age=604800', // 1 week
      },
    });
  } catch (error) {
    console.error('Error generating OG image:', error);
    return new Response('Image not found', { status: 404 });
  }
}
