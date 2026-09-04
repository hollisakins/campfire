import { NextRequest } from 'next/server';
import { createServiceClient } from '@/lib/supabase/server';
import {
  compositeTileThumbnail,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';
import { resolveFieldCutoutSource } from '@/lib/cutout/source';
import { renderDisplayCutoutPng } from '@/lib/cutout/display';

// Tile decode + reprojection + PNG encode on a cold instance (#497).
export const maxDuration = 60;

type Kind = 'object' | 'target';

/**
 * GET /api/og-image/[id]?kind=object|target[&v=<asset version>]
 *
 * Serves tile-composited RGB images publicly for social media crawlers.
 * - No authentication required (unlike /api/tile-thumbnail)
 * - Returns image bytes directly
 * - Aggressive caching (1 week) since tiles rarely change; the `v` token
 *   (lib/asset-version.ts) changes the URL when a field's imagery is
 *   re-deployed, so the week-long shared cache needs no purge path.
 *
 * Anonymous + shared-cached means this route may only ever show what an
 * anonymous visitor could see: the id must resolve to a published row whose
 * program is public (`programs.is_public`), else 404 (#497). `kind` selects
 * the catalog (see /api/tile-thumbnail); absent, the legacy targets-then-
 * objects probe applies.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const targetId = decodeURIComponent(id);

  const kindParam = request.nextUrl.searchParams.get('kind');
  if (kindParam !== null && kindParam !== 'object' && kindParam !== 'target') {
    return new Response('Image not found', { status: 404 });
  }
  const kind = kindParam as Kind | null;

  try {
    const supabase = createServiceClient();

    // This endpoint is PUBLIC (no auth) and uses the service-role client, so it
    // must surface nothing an anonymous visitor could not see: the row must
    // carry >=1 published spectrum (has_published_spectrum) AND belong to a
    // public program. A target has one program; an object is public when any
    // of its programs is (its coordinates are then already visible).
    const { data: publicPrograms } = await supabase
      .from('programs')
      .select('slug')
      .eq('is_public', true)
      .is('retired_at', null);
    const publicSlugs = (publicPrograms ?? []).map((p) => p.slug);
    if (publicSlugs.length === 0) {
      return new Response('Image not found', { status: 404 });
    }

    let obj: { ra: number; dec: number; field: string } | null = null;

    if (kind !== 'object') {
      const { data: target } = await supabase
        .from('targets')
        .select('ra, dec, field')
        .eq('target_id', targetId)
        .eq('has_published_spectrum', true)
        .in('program_slug', publicSlugs)
        .maybeSingle();
      obj = target;
    }
    if (!obj && kind !== 'target') {
      const { data: object } = await supabase
        .from('objects')
        .select('ra, dec, field')
        .eq('object_id', targetId)
        .eq('has_published_spectrum', true)
        .overlaps('programs', publicSlugs)
        .maybeSingle();
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
