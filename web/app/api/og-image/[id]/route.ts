import { NextRequest } from 'next/server';
import { createServiceClient } from '@/lib/supabase/server';
import {
  compositeTileThumbnail,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';
import { resolveFieldCutoutSourceResult, sourceMatchesDatasetVersion } from '@/lib/cutout/source';
import { renderDisplayCutoutPng } from '@/lib/cutout/display';
import { getAssetVersions } from '@/lib/asset-version';
import { cutoutStoreFor, cutoutStoreRead, storeCutoutInBackground } from '@/lib/cutout/store';

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
 *
 * Content-addressed store (perf T2-D3, #509): a cutout already rendered for
 * this (field, imaging version, 300 px, 5") is streamed from the store —
 * streamed rather than redirected, since not every social crawler follows a
 * 302 for an image — and a fresh render is written to it after the response.
 */
const OG_SIZE = 300;
const OG_FOV = 5;
const PUBLIC_WEEK = 'public, max-age=604800';

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
    const [{ source: fitsglSrc, failed: fitsglUnresolved }, versions] = await Promise.all([
      resolveFieldCutoutSourceResult(supabase, obj.field, { requirePublic: true }),
      getAssetVersions(),
    ]);

    const fieldVersion = versions.byField[obj.field];
    const sourceMatchesVersion = sourceMatchesDatasetVersion(
      fitsglSrc, versions.fitsglDatasetVersions[obj.field],
    );
    const store = fieldVersion
      ? cutoutStoreFor({ field: obj.field, version: fieldVersion, size: OG_SIZE, fov: OG_FOV, ra: obj.ra, dec: obj.dec })
      : null;
    if (store) {
      const stored = await cutoutStoreRead(store.key);
      if (stored) {
        return new Response(stored, {
          status: 200,
          headers: { 'Content-Type': 'image/png', 'Cache-Control': PUBLIC_WEEK },
        });
      }
    }

    // A legacy composite rendered because the FitsGL source could not be
    // resolved or its render failed is not stored under the FitsGL key (see
    // /api/tile-thumbnail).
    let fitsglFailed = fitsglUnresolved;

    if (fitsglSrc) {
      try {
        const png = await renderDisplayCutoutPng(fitsglSrc, {
          ra: obj.ra,
          dec: obj.dec,
          fovArcsec: OG_FOV,
          outputSize: OG_SIZE,
        });
        if (store && sourceMatchesVersion) storeCutoutInBackground(store.key, png);
        return new Response(new Uint8Array(png), {
          status: 200,
          headers: {
            'Content-Type': 'image/png',
            'Cache-Control': PUBLIC_WEEK, // 1 week
          },
        });
      } catch (err) {
        console.error('FitsGL OG-image render failed; falling back to PNG tiles:', err);
        fitsglFailed = true;
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
      outputSize: OG_SIZE,
      fovArcsec: OG_FOV,
    });
    if (store && !fitsglFailed) storeCutoutInBackground(store.key, png);

    return new Response(new Uint8Array(png), {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': PUBLIC_WEEK, // 1 week
      },
    });
  } catch (error) {
    console.error('Error generating OG image:', error);
    return new Response('Image not found', { status: 404 });
  }
}
