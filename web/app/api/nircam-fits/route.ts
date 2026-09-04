import { NextRequest, NextResponse } from 'next/server';
import { isAdminUser } from '@/lib/api-helpers';
import { getRequestIdentity } from '@/lib/auth/identity';
import { frontUrlFor } from '@/lib/server/cdn-front';
import { promises as fs } from 'fs';
import path from 'path';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import {
  getS3ClientForBackend,
  getBucketNameForBackend,
  type DataBackend,
} from '@/lib/storage';
import { isKnownKey, parseKey } from '@/lib/layout';

export const runtime = 'nodejs';

// Product types this proxy is allowed to serve. NIRCam exposures (epic #261),
// NIRSpec rate files (review loop P3), and canonical NIRSpec spectrum-exposures
// (P5 nods renderer — the S2D_* rectified cutouts) — all uncompressed FITS the
// in-browser decoder range-fetches. Keeps the route from being an arbitrary read.
const FITS_ALLOWED_PRODUCTS = new Set([
  'nircam_exposure', 'nirspec_rate', 'nirspec_spectrum_exposure',
]);

/**
 * GET /api/nircam-fits?key=<canonical OSN key>[&resolve=1]   (HTTP Range required)
 *
 * Admin-only access to canonical NIRCam exposure FITS (epic #261, N4). The
 * in-browser viewer range-fetches the header block, then the ~16 MB
 * uncompressed SCI block. OSN has no CORS, so the browser cannot range-GET it
 * directly; two ways around that:
 *
 *   - `resolve=1` answers JSON `{ url }`: a content-addressed url on the
 *     delivery front (perf T2-D1, #507) that the browser range-fetches
 *     directly — CORS-readable, 206-capable, edge-cached per content hash
 *     (a Range miss streams through and the Worker fills the slot with its
 *     own full GET in the background, so the second view of a file hits).
 *     `url` is null when the front is not configured or the file is served
 *     from the local filesystem (below); the client then range-fetches this
 *     route without `resolve`.
 *   - without `resolve`, this same-origin route forwards `Range` upstream and
 *     returns 206 — the fallback, streaming through the function.
 *
 * Restricted to `nircam_exposure` keys so it can't be abused as an arbitrary
 * object read.
 *
 * When `CAMPFIRE_LOCAL_DATA_ROOT` is set and the file exists on disk (a reducer
 * running `npm run dev` on the same machine as `campfire-data`), it is served
 * from the local filesystem — so the viewer works before a field is deployed.
 */
export async function GET(request: NextRequest) {
  const { user, supabase } = await getRequestIdentity();
  if (!user) return new Response('Unauthorized', { status: 401 });

  if (!(await isAdminUser(user.id))) return new Response('Forbidden', { status: 403 });

  const key = request.nextUrl.searchParams.get('key');
  if (!key || !isKnownKey(key, { bucket: 'data' })) {
    return new Response('Invalid key', { status: 400 });
  }
  if (!FITS_ALLOWED_PRODUCTS.has(parseKey(key).productType)) {
    return new Response('Invalid key', { status: 400 });
  }

  const rangeHeader = request.headers.get('range'); // e.g. "bytes=40320-16817535"
  const resolve = request.nextUrl.searchParams.get('resolve') === '1';
  // Browser-cached beyond a session, so Vary: Cookie — sign-out does not
  // clear the HTTP cache and this answer names admin-only content (D-C).
  const resolveJson = (url: string | null) =>
    NextResponse.json({ url }, { headers: { 'Cache-Control': 'private, max-age=3600', Vary: 'Cookie' } });

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
        if (resolve) return resolveJson(null);
        return serveLocalRange(localPath, stat.size, rangeHeader);
      } catch {
        // Not present locally — fall through to cloud storage.
      }
    }
  }

  // ---- cloud storage path (production) -------------------------------------
  if (resolve) return resolveJson(await frontUrlFor(key));

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
