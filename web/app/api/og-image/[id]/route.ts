import { NextRequest } from 'next/server';
import { createServiceClient } from '@/lib/supabase/server';
import {
  compositeTileThumbnail,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';
import { resolveFieldCutoutSource } from '@/lib/cutout/source';
import { renderDisplayCutoutPng } from '@/lib/cutout/display';

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

    // This endpoint is PUBLIC (no auth) and uses the service-role client, so it
    // must surface nothing about unpublished data: require >=1 published
    // spectrum (has_published_spectrum) on the target/object, else 404 below.
    // No-op in B1. (Program-level gating is out of scope — pre-existing #229.)
    const { data: target } = await supabase
      .from('targets')
      .select('ra, dec, field')
      .eq('target_id', targetId)
      .eq('has_published_spectrum', true)
      .single();

    if (target) {
      obj = target;
    } else {
      const { data: object } = await supabase
        .from('objects')
        .select('ra, dec, field')
        .eq('object_id', targetId)
        .eq('has_published_spectrum', true)
        .single();
      obj = object;
    }

    if (!obj) {
      return new Response('Image not found', { status: 404 });
    }

    // FitsGL path (epic #337, Phase 5). Service-role client bypasses RLS, so
    // requirePublic mirrors the fitsgl_datasets policy — an unpublished-backed
    // pyramid never serves this public route.
    const fitsglSrc = await resolveFieldCutoutSource(supabase, obj.field, { requirePublic: true });
    if (fitsglSrc) {
      try {
        const png = await renderDisplayCutoutPng(fitsglSrc, {
          ra: obj.ra,
          dec: obj.dec,
          fovArcsec: 5,
          outputSize: 300,
        });
        return new Response(new Uint8Array(png), {
          status: 200,
          headers: {
            'Content-Type': 'image/png',
            'Cache-Control': 'public, max-age=604800', // 1 week
          },
        });
      } catch (err) {
        console.error('FitsGL OG-image render failed; falling back to PNG tiles:', err);
      }
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
