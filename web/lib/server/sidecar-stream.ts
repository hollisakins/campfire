// Stream an immutable JSON sidecar from object storage through a route
// untouched (perf T2-D2, #508). The fallback path for when the delivery
// front (lib/server/cdn-front.ts) is not configured: no JSON.parse and
// re-serialize of a multi-hundred-kB payload inside the function, just the
// upstream body with its length.
import 'server-only';

import { generateDownloadUrl } from '@/lib/r2';

export type UpstreamStatus = 'ok' | 'missing' | 'error';

/**
 * Presign `key`, GET it, and pass the body through as `application/json`
 * with `cacheControl`. Returns `missing` on an upstream 404 so the caller can
 * answer its own 404 (a sidecar may legitimately not exist).
 *
 * `vary` names the request header the authorization was keyed on: `Cookie`
 * (default) for the session routes, `Authorization` for the bearer `/api/v1`
 * routes. Every caller caches beyond a session, and sign-out does not clear
 * the HTTP cache (D-C), so the credential header has to be part of the key.
 */
export async function streamSidecar(
  key: string,
  cacheControl: string,
  vary: 'Cookie' | 'Authorization' = 'Cookie',
): Promise<{ status: UpstreamStatus; response?: Response; upstreamStatus?: number }> {
  const signedUrl = await generateDownloadUrl(key, 3600);
  const upstream = await fetch(signedUrl);
  if (upstream.status === 404) {
    upstream.body?.cancel().catch(() => {});
    return { status: 'missing', upstreamStatus: 404 };
  }
  if (!upstream.ok || !upstream.body) {
    upstream.body?.cancel().catch(() => {});
    return { status: 'error', upstreamStatus: upstream.status };
  }
  const headers = new Headers({
    'Content-Type': 'application/json',
    'Cache-Control': cacheControl,
    Vary: vary,
  });
  // Only meaningful when the upstream body is stored verbatim; the platform
  // may still re-encode the function's response and drop it.
  const length = upstream.headers.get('Content-Length');
  if (length && !upstream.headers.get('Content-Encoding')) headers.set('Content-Length', length);
  return { status: 'ok', response: new Response(upstream.body, { status: 200, headers }) };
}
