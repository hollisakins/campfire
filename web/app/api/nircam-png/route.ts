import { NextRequest, NextResponse } from 'next/server';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { frontUrlFor } from '@/lib/server/cdn-front';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import {
  getS3ClientForBackend,
  getBucketNameForBackend,
  type DataBackend,
} from '@/lib/storage';

export const runtime = 'nodejs';
// Streams a multi-MB PNG from OSN through the function (#497).
export const maxDuration = 60;

/**
 * GET /api/nircam-png?id=<exposure id>[&resolve=1]
 *
 * Admin-only access to one exposure's *display* PNG — the full-res mask
 * surface when the exposure has one, else the preview — for the triage
 * pre-download warm (lib/nircam-png-store.ts). The storage key is re-derived
 * server-side from nircam_exposures, never taken from the client, so the
 * route can't be used as an arbitrary object read.
 *
 * `resolve=1` (the warm's first call) answers JSON `{ url, kind }`: `url` is a
 * content-addressed url on the delivery front (perf T2-D1, #507) the browser
 * fetches directly — CORS-readable, edge-cached per content hash — and `kind`
 * says which byte it names ('full' | 'preview') so the store files it under
 * the right slot. `url` is null when the front is not configured; the client
 * then fetches the bytes from this route without `resolve`.
 *
 * Without `resolve`, the bytes stream through the function (the fallback):
 * OSN serves no CORS headers, so a presigned url cannot be READ by page
 * JavaScript. X-Png-Kind carries the kind. no-store: the caller persists the
 * bytes itself, and letting the HTTP cache keep a second multi-GB copy helps
 * nobody.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return new Response('Unauthorized', { status: 401 });

  if (!(await isAdminUser(user.id))) return new Response('Forbidden', { status: 403 });

  const idParam = request.nextUrl.searchParams.get('id');
  const id = Number(idParam);
  if (!idParam || !Number.isInteger(id) || id <= 0) {
    return new Response('Invalid id', { status: 400 });
  }

  const { data: row } = await supabase
    .from('nircam_exposures')
    .select('png_path, full_png_path')
    .eq('id', id)
    .maybeSingle();
  if (!row) return new Response('Not Found', { status: 404 });

  const key = row.full_png_path ?? row.png_path;
  const kind = row.full_png_path ? 'full' : 'preview';
  if (!key) return new Response('Not Found', { status: 404 });

  if (request.nextUrl.searchParams.get('resolve') === '1') {
    // A front url is stable for at least one presign window (6 h), so the
    // browser may keep this answer for an hour — with Vary: Cookie, since
    // sign-out does not clear the HTTP cache and this names admin-only
    // content (D-C).
    const url = await frontUrlFor(key);
    return NextResponse.json(
      { url, kind },
      { headers: { 'Cache-Control': 'private, max-age=3600', Vary: 'Cookie' } },
    );
  }

  try {
    // Same backend resolution as presignExposurePngs: the object's home
    // backend from the registry, defaulting OSN where canonical PNGs live.
    const { data: soRow } = await supabase
      .from('storage_objects')
      .select('backend')
      .eq('storage_key', key)
      .eq('status', 'active')
      .maybeSingle();
    const backend: DataBackend = soRow?.backend === 'r2' ? 'r2' : 'osn';

    const obj = await getS3ClientForBackend(backend).send(
      new GetObjectCommand({
        Bucket: getBucketNameForBackend(backend),
        Key: key,
      }),
    );
    if (!obj.Body) return new Response('Not Found', { status: 404 });

    const headers: Record<string, string> = {
      'Content-Type': 'image/png',
      'Cache-Control': 'private, no-store',
      'X-Png-Kind': kind,
    };
    if (obj.ContentLength != null) headers['Content-Length'] = String(obj.ContentLength);

    return new Response(obj.Body.transformToWebStream(), { status: 200, headers });
  } catch (err: unknown) {
    const e = err as { $metadata?: { httpStatusCode?: number }; name?: string };
    if (e?.name === 'NoSuchKey' || e?.$metadata?.httpStatusCode === 404) {
      return new Response('Not Found', { status: 404 });
    }
    console.error('nircam-png proxy error:', err);
    return new Response('Internal Error', { status: 500 });
  }
}
