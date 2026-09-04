import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import {
  compositeTileThumbnail,
  TRANSPARENT_GIF,
  type MapLayerInfo,
} from '@/lib/utils/tile-compositing';
import { resolveFieldCutoutSourceResult } from '@/lib/cutout/source';
import { renderDisplayCutoutPng } from '@/lib/cutout/display';
import { getAssetVersions } from '@/lib/asset-version';
import { cutoutStoreFor, cutoutStoreHas, snapFov, storeCutoutInBackground, storeSizeFor } from '@/lib/cutout/store';
import { catalogIsPublic, publicProgramSlugs } from '@/lib/server/public-programs';

// Tile decode + reprojection + PNG encode can exceed a short function budget
// for a 600 px render on a cold instance (#497).
export const maxDuration = 60;

// Program-scoped, cookie-authenticated bytes: the browser may keep them for a
// week (the URL carries the field's asset version, so a re-deploy changes it),
// but they must never enter Vercel's shared edge cache, which serves `public`
// responses to anyone asking for the same URL — cookie or not (#497).
const PRIVATE_LONG = 'private, max-age=604800, stale-while-revalidate=86400';
// A store hit is a 302 the browser may keep for a day (not a week): the
// bucket's lifecycle rule can expire a stored cutout, and a cached redirect
// must not point at a gone object for longer than that. The next miss simply
// re-renders and re-stores (#509).
const STORE_REDIRECT_CACHE = 'private, max-age=86400';

type Kind = 'object' | 'target';

/**
 * GET /api/tile-thumbnail?target_id=<id>&kind=object|target&size=<px>&fov=<arcsec>[&v=<asset version>]
 *
 * Thumbnail PNG centered on the object — a clean RGB (or single-band) cutout
 * without shutter overlays. Fields with a deployed FitsGL pyramid render
 * North-up from the FITS tiles (epic #337, Phase 5); others keep the legacy
 * PNG-tile compositing until it is retired per-field.
 *
 * `kind` says which catalog `target_id` names. Object and target ids live in
 * different tables and are not guaranteed disjoint, so probing one then the
 * other can silently render the wrong patch of sky; callers state it. A
 * request without `kind` keeps the historical targets-then-objects probe.
 * `v` is an opaque cache-key token (lib/asset-version.ts) and is not read.
 *
 * Content-addressed store (perf T2-D3, #509): after the access check, a
 * cutout already rendered for the same (field, imaging version, size, fov,
 * ra, dec) answers with a 302 to its CDN url; otherwise it is rendered, sent,
 * and written to the store after the response. `size` is rounded up to the
 * store's ladder (64 / 300 / 600) so the list, bucket and object page share
 * renders; the `<img>` scales down.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
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

  const kindParam = params.get('kind');
  if (kindParam !== null && kindParam !== 'object' && kindParam !== 'target') {
    return new Response(TRANSPARENT_GIF, {
      status: 400,
      headers: { 'Content-Type': 'image/gif' },
    });
  }
  const kind = kindParam as Kind | null;

  const size = Math.min(600, Math.max(16, parseInt(params.get('size') || '96', 10)));
  // Snapped to the store's 0.1" grid so the key space stays bounded (#509);
  // the difference is invisible at thumbnail sizes.
  const fov = snapFov(Math.min(30, Math.max(1, parseFloat(params.get('fov') || '5'))));

  try {
    // Look up coordinates in the table the caller named (legacy: targets, then objects)
    let obj: { ra: number; dec: number; field: string; programs: string[] } | null = null;

    if (kind !== 'object') {
      const { data: target } = await supabase
        .from('targets')
        .select('ra, dec, field, program_slug')
        .eq('target_id', targetId)
        .maybeSingle();
      if (target) obj = { ra: target.ra, dec: target.dec, field: target.field, programs: [target.program_slug] };
    }
    if (!obj && kind !== 'target') {
      const { data: object } = await supabase
        .from('objects')
        .select('ra, dec, field, programs')
        .eq('object_id', targetId)
        .maybeSingle();
      if (object) obj = { ra: object.ra, dec: object.dec, field: object.field, programs: object.programs ?? [] };
    }

    if (!obj) {
      return new Response(TRANSPARENT_GIF, {
        status: 404,
        headers: { 'Content-Type': 'image/gif' },
      });
    }

    // FitsGL path: render from the field's FITS pyramid when one is deployed
    // (RLS scopes draft-backed datasets to admins). Falls through to the
    // legacy PNG tiles on any render failure while both stacks coexist.
    const [{ source: fitsglSrc, failed: fitsglUnresolved }, versions, publicSlugs] = await Promise.all([
      resolveFieldCutoutSourceResult(supabase, obj.field),
      getAssetVersions(),
      publicProgramSlugs(),
    ]);

    // Store lookup: only renders from public imagery OF A PUBLIC TARGET are
    // stored or served from the store. A draft-backed dataset stays a
    // private, uncached render for the admin who can see it; so does any
    // target of a private program — the stored object sits at an unsigned
    // public url keyed by its coordinates, which must not outlive the
    // caller's authorization (#509).
    const storeSize = storeSizeFor(size);
    const publicImagery = fitsglSrc ? fitsglSrc.isPublic : true;
    const publicTarget = catalogIsPublic(obj.programs, publicSlugs);
    const fieldVersion = versions.byField[obj.field];
    const store = publicImagery && publicTarget && fieldVersion
      ? cutoutStoreFor({ field: obj.field, version: fieldVersion, size: storeSize, fov, ra: obj.ra, dec: obj.dec })
      : null;
    if (store && (await cutoutStoreHas(store.key))) {
      return NextResponse.redirect(store.url, { status: 302, headers: { 'Cache-Control': STORE_REDIRECT_CACHE } });
    }

    // Only round the render up to the store's ladder when the store is in
    // play; otherwise the request renders at exactly the size it asked for.
    const outputSize = store ? storeSize : size;
    // A legacy composite rendered because the FitsGL source could not be
    // resolved or its render *failed* must not be stored under the key a
    // FitsGL render will later want (a transient failure would pin the
    // lower-quality image until the field re-deploys).
    let fitsglFailed = fitsglUnresolved;

    if (fitsglSrc) {
      try {
        const png = await renderDisplayCutoutPng(fitsglSrc, {
          ra: obj.ra,
          dec: obj.dec,
          fovArcsec: fov,
          outputSize,
        });
        if (store) storeCutoutInBackground(store.key, png);
        return new Response(new Uint8Array(png), {
          status: 200,
          headers: {
            'Content-Type': 'image/png',
            // A draft-backed render only an admin's RLS could see must not
            // even sit in that admin's browser cache under this URL.
            'Cache-Control': fitsglSrc.isPublic ? PRIVATE_LONG : 'private, no-store',
          },
        });
      } catch (err) {
        console.error('FitsGL thumbnail render failed; falling back to PNG tiles:', err);
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
      outputSize,
      fovArcsec: fov,
    });
    if (store && !fitsglFailed) storeCutoutInBackground(store.key, png);

    return new Response(new Uint8Array(png), {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': PRIVATE_LONG,
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
