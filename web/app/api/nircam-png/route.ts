import { NextRequest } from 'next/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import {
  getS3ClientForBackend,
  getBucketNameForBackend,
  type DataBackend,
} from '@/lib/storage';
import { createClient } from '@/lib/supabase/server';

export const runtime = 'nodejs';
// Streams a multi-MB PNG from OSN through the function (#497).
export const maxDuration = 60;

/**
 * GET /api/nircam-png?id=<exposure id>
 *
 * Admin-only same-origin proxy for one exposure's *display* PNG — the
 * full-res mask surface when the exposure has one, else the preview — for the
 * triage pre-download warm (lib/nircam-png-store.ts). Display rendering keeps
 * using presigned OSN URLs in <img> (no CORS needed there); the warm has to
 * READ the bytes to store them in IndexedDB, and OSN serves no CORS headers,
 * so — exactly like /api/nircam-fits — the readable bytes come through this
 * same-origin route. The storage key is re-derived server-side from
 * nircam_exposures, never taken from the client, so the route can't be used
 * as an arbitrary object read.
 *
 * X-Png-Kind says which byte was served ('full' | 'preview') so the store
 * files it under the right slot. no-store: the caller persists the bytes
 * itself, and letting the HTTP cache keep a second multi-GB copy helps nobody.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return new Response('Unauthorized', { status: 401 });

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();
  if (!profile?.is_admin) return new Response('Forbidden', { status: 403 });

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
