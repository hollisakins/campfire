import { NextRequest } from 'next/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getS3Client, getBucketName } from '@/lib/storage';
import { createClient } from '@/lib/supabase/server';
import { isKnownKey, parseKey } from '@/lib/layout';

/**
 * GET /api/nircam-preview?key=<r2_key>
 *
 * Admin-only proxy for NIRCam exposure preview PNGs (both `_preview.png`
 * thumbnails and `_full.png` editor canvases). The R2 bucket isn't
 * served via a public URL for these objects, so the editor and admin
 * table fetch them through this same-origin endpoint.
 *
 * `key` must start with `nircam/exposures/` — this prevents the route
 * from being abused as an arbitrary R2 read endpoint.
 */
export async function GET(request: NextRequest) {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return new Response('Unauthorized', { status: 401 });
  }

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) {
    return new Response('Forbidden', { status: 403 });
  }

  const key = request.nextUrl.searchParams.get('key');
  // Validate against the layout contract: a known data-bucket key (rejects
  // traversal/unsafe), restricted to NIRCam exposure preview/full PNGs.
  if (!key || !isKnownKey(key, { bucket: 'data' })) {
    return new Response('Invalid key', { status: 400 });
  }
  const productType = parseKey(key).productType;
  if (productType !== 'nircam_exposure_preview' && productType !== 'nircam_exposure_full') {
    return new Response('Invalid key', { status: 400 });
  }

  try {
    const obj = await getS3Client('data').send(new GetObjectCommand({
      Bucket: getBucketName('data'),
      Key: key,
    }));

    if (!obj.Body) {
      return new Response('Not Found', { status: 404 });
    }

    return new Response(obj.Body.transformToWebStream(), {
      status: 200,
      headers: {
        'Content-Type': obj.ContentType || 'image/png',
        // Previews are immutable per `_preview.png` / `_full.png` rebuild
        // (the pipeline's CFP_PREV stamp gates regeneration). Long-cache
        // the response; the editor invalidates by reloading the page.
        'Cache-Control': 'private, max-age=3600, stale-while-revalidate=86400',
      },
    });
  } catch (err: unknown) {
    const e = err as { $metadata?: { httpStatusCode?: number }; name?: string };
    if (e?.name === 'NoSuchKey' || e?.$metadata?.httpStatusCode === 404) {
      return new Response('Not Found', { status: 404 });
    }
    console.error('nircam-preview proxy error:', err);
    return new Response('Internal Error', { status: 500 });
  }
}
