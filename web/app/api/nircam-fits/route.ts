import { NextRequest } from 'next/server';
import { promises as fs } from 'fs';
import path from 'path';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import {
  getS3ClientForBackend,
  getBucketNameForBackend,
  type DataBackend,
} from '@/lib/storage';
import { createClient } from '@/lib/supabase/server';
import { isKnownKey, parseKey } from '@/lib/layout';

export const runtime = 'nodejs';

/**
 * GET /api/nircam-fits?key=<canonical OSN key>   (HTTP Range required)
 *
 * Admin-only Range-forwarding proxy for canonical NIRCam exposure FITS (epic
 * #261, N4). The in-browser viewer range-fetches the header block, then the
 * ~16 MB uncompressed SCI block, from OSN — which has no CORS, so the browser
 * cannot range-GET it directly; this same-origin route forwards `Range` and
 * returns 206. Restricted to `nircam_exposure` keys so it can't be abused as an
 * arbitrary object read.
 *
 * When `CAMPFIRE_LOCAL_DATA_ROOT` is set and the file exists on disk (a reducer
 * running `npm run dev` on the same machine as `campfire-data`), it is served
 * from the local filesystem — so the viewer works before a field is deployed.
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

  const key = request.nextUrl.searchParams.get('key');
  if (!key || !isKnownKey(key, { bucket: 'data' })) {
    return new Response('Invalid key', { status: 400 });
  }
  if (parseKey(key).productType !== 'nircam_exposure') {
    return new Response('Invalid key', { status: 400 });
  }

  const rangeHeader = request.headers.get('range'); // e.g. "bytes=40320-16817535"

  // ---- local-filesystem fast path (dev/PoC) --------------------------------
  const localRoot = process.env.CAMPFIRE_LOCAL_DATA_ROOT;
  if (localRoot) {
    const relpath = key.startsWith('data/') ? key.slice('data/'.length) : key;
    const localPath = path.join(localRoot, relpath);
    // Guard against traversal: the resolved path must stay under the root.
    const resolvedRoot = path.resolve(localRoot);
    if (path.resolve(localPath).startsWith(resolvedRoot)) {
      try {
        const stat = await fs.stat(localPath);
        return serveLocalRange(localPath, stat.size, rangeHeader);
      } catch {
        // Not present locally — fall through to cloud storage.
      }
    }
  }

  // ---- cloud storage path (production) -------------------------------------
  try {
    // Each object records its home backend in the registry; default OSN (where
    // canonical exposures live). Admin RLS lets the admin see draft rows.
    const { data: soRow } = await supabase
      .from('storage_objects')
      .select('backend')
      .eq('storage_key', key)
      .maybeSingle();
    const backend: DataBackend = soRow?.backend === 'r2' ? 'r2' : 'osn';

    const obj = await getS3ClientForBackend(backend).send(
      new GetObjectCommand({
        Bucket: getBucketNameForBackend(backend),
        Key: key,
        Range: rangeHeader ?? undefined,
      }),
    );
    if (!obj.Body) return new Response('Not Found', { status: 404 });

    const headers: Record<string, string> = {
      'Content-Type': 'application/fits',
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'private, max-age=3600',
    };
    if (obj.ContentRange) headers['Content-Range'] = obj.ContentRange;
    if (obj.ContentLength != null) headers['Content-Length'] = String(obj.ContentLength);

    return new Response(obj.Body.transformToWebStream(), {
      status: rangeHeader ? 206 : 200,
      headers,
    });
  } catch (err: unknown) {
    const e = err as { $metadata?: { httpStatusCode?: number }; name?: string };
    if (e?.name === 'NoSuchKey' || e?.$metadata?.httpStatusCode === 404) {
      return new Response('Not Found', { status: 404 });
    }
    console.error('nircam-fits proxy error:', err);
    return new Response('Internal Error', { status: 500 });
  }
}

/** Serve a byte range of a local file as a 206 (or the whole file as 200). */
async function serveLocalRange(
  filePath: string,
  total: number,
  rangeHeader: string | null,
): Promise<Response> {
  const commonHeaders: Record<string, string> = {
    'Content-Type': 'application/fits',
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'private, max-age=3600',
  };

  if (!rangeHeader) {
    const buf = await fs.readFile(filePath);
    return new Response(new Uint8Array(buf), {
      status: 200,
      headers: { ...commonHeaders, 'Content-Length': String(total) },
    });
  }

  const m = /bytes=(\d*)-(\d*)/.exec(rangeHeader);
  if (!m) return new Response('Invalid Range', { status: 416 });
  let start = m[1] === '' ? NaN : Number(m[1]);
  let end = m[2] === '' ? NaN : Number(m[2]);
  if (Number.isNaN(start)) {
    // suffix range "bytes=-N": last N bytes
    const suffix = Number.isNaN(end) ? total : end;
    start = Math.max(0, total - suffix);
    end = total - 1;
  } else if (Number.isNaN(end)) {
    end = total - 1;
  }
  if (start < 0 || start > end || start >= total) {
    return new Response('Range Not Satisfiable', {
      status: 416,
      headers: { 'Content-Range': `bytes */${total}` },
    });
  }
  end = Math.min(end, total - 1);
  const length = end - start + 1;

  const fh = await fs.open(filePath, 'r');
  try {
    const buf = Buffer.alloc(length);
    await fh.read(buf, 0, length, start);
    return new Response(new Uint8Array(buf), {
      status: 206,
      headers: {
        ...commonHeaders,
        'Content-Range': `bytes ${start}-${end}/${total}`,
        'Content-Length': String(length),
      },
    });
  } finally {
    await fh.close();
  }
}
