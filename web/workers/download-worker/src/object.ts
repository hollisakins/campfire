/**
 * `/o/<key>` — the content-addressed delivery front for immutable products
 * (perf T2-D1, #507; decision D-D of the 2026-09 audit).
 *
 * The app authorizes a read (RLS), resolves the object's registry row, and
 * mints ONE url per product:
 *
 *   GET /o/<storage key>?h=<content_hash>&e=<unix exp>&r=<unix registered>&t=<token>&u=<presigned upstream>
 *
 * `t` is HMAC-SHA256(JWT_SECRET, "campfire-o-v2\n<key>\n<hash>\n<exp>\n<u>\n<r>"),
 * so a token authorizes exactly one (key, hash, upstream, registration time)
 * tuple until `exp`. The Worker verifies it on EVERY serve, cache hits
 * included, then:
 *
 *   - serves from the Cache API when the (key, hash) pair is cached — Range
 *     requests are answered from the cached full object as 206;
 *   - otherwise fetches the presigned upstream (host-allowlisted, redirects
 *     refused, path must name the token's key), streams it back with the
 *     upstream status, and — for full-object 200s — stores a copy under the
 *     (key, hash) cache key in the background. A Range or HEAD miss streams
 *     the partial answer through untouched and fills the slot with a
 *     separate full-object GET in the background (deduplicated per isolate),
 *     so consumers that only ever range-fetch (the in-browser FITS reader:
 *     header block, then the SCI block) still hit from the second request on.
 *
 * Products are immutable per `storage_objects.content_hash`, NOT per path:
 * re-deploys overwrite in place and register a new hash, so the hash in the
 * cache key is what keeps a stale copy from ever being served. The presigned
 * upstream url churns (SigV4 date + expiry); the app signs it on a fixed time
 * window so the whole `/o/` url is stable long enough for the browser cache.
 *
 * Binding the hash to the bytes: the upstream path is mutable, and a url
 * minted before an in-place overwrite stays valid for up to two presign
 * windows (12 h). The Worker cannot afford a sha256 of the body, so the
 * token carries `r`, the registry row's registration time (bumped on every
 * re-deploy, after the bytes landed), and a full object is only stored when
 * the upstream's Last-Modified is not newer than `r`. An
 * overwritten object is still served — those are the product's current
 * bytes, the same the app's own proxy would stream — but never cached under
 * the old hash; a url minted after the re-deploy carries the new `r` and
 * fills the new slot. Conditional requests (If-None-Match /
 * If-Modified-Since) are answered 304 from whichever copy would have served.
 *
 * CORS is `*`: the token IS the authorization (no cookies ride along), and a
 * fetch that reached here via a cross-origin redirect carries `Origin: null`,
 * which only `*` satisfies.
 */

import { verifySignature } from './auth';
import { isAllowedFetchUrl } from './guards';

export interface ObjectEnv {
  JWT_SECRET: string;
  ALLOWED_FETCH_HOSTS: string;
}

/** Minimal ExecutionContext surface (typed loosely so unit tests can omit it). */
export interface ObjectContext {
  waitUntil(promise: Promise<unknown>): void;
}

const TOKEN_VERSION = 'campfire-o-v2';

/** Browser-side lifetime of a served object. The url carries the hash and a
 * token that expires, so a cached copy can never outlive its authorization by
 * more than this; `immutable` stops revalidation churn on reload. `private`
 * because the url embeds a per-mint token — shared caches must not key on it
 * (the Worker's own Cache API store is the shared layer, keyed by hash). */
const SERVE_CACHE_CONTROL = 'private, max-age=86400, immutable';
/** Stored-copy lifetime in the Cache API (per-colo, LRU-evicted anyway). */
const STORE_CACHE_CONTROL = 'public, max-age=31536000, immutable';

const ALLOW_HEADERS = 'Range, If-None-Match, If-Modified-Since';
const EXPOSE_HEADERS =
  'Content-Length, Content-Range, Accept-Ranges, Content-Type, ETag, Last-Modified, X-Cache';

/** The message a token signs — mirrored by the app's mint (lib/server/cdn-front.ts). */
export function objectTokenMessage(key: string, hash: string, exp: string, upstream: string, registered: string): string {
  return [TOKEN_VERSION, key, hash, exp, upstream, registered].join('\n');
}

/** Whether a full upstream answer is the registered bytes and may fill the
 * (key, hash) slot: its Last-Modified must exist and not postdate the
 * registration. Upload completes before registry upsert, and both timestamps
 * have second precision, so equality is valid. Any positive tolerance would
 * let a rapid in-place redeploy populate the old hash's immutable cache slot.
 * No Last-Modified => not provably the registered object => never cached. */
export function isRegisteredBytes(upstreamHeaders: Headers, registeredAt: number): boolean {
  const lm = upstreamHeaders.get('Last-Modified');
  if (!lm) return false;
  const lmSeconds = Date.parse(lm) / 1000;
  if (!Number.isFinite(lmSeconds)) return false;
  return lmSeconds <= registeredAt;
}

/** A conditional request whose validators match the answer gets a 304. ETag
 * comparison is weak (`W/` stripped) — the stored copy carries the upstream's
 * ETag verbatim, so a browser's revalidation of an earlier serve matches. */
export function isNotModified(request: Request, answer: Headers): boolean {
  const inm = request.headers.get('If-None-Match');
  if (inm) {
    const etag = answer.get('ETag');
    if (!etag) return false;
    if (inm.trim() === '*') return true;
    const strip = (t: string) => t.trim().replace(/^W\//, '');
    return inm.split(',').some((t) => strip(t) === strip(etag));
  }
  const ims = request.headers.get('If-Modified-Since');
  if (ims) {
    const lm = answer.get('Last-Modified');
    if (!lm) return false;
    const a = Date.parse(lm), b = Date.parse(ims);
    return Number.isFinite(a) && Number.isFinite(b) && a <= b;
  }
  return false;
}

/** Decode the `/o/<key>` path segment-wise; null on anything that is not a
 * plain relative object key (empty / dot segments, undecodable escapes). */
export function decodeObjectKey(rawPath: string): string | null {
  if (!rawPath) return null;
  const segments: string[] = [];
  for (const raw of rawPath.split('/')) {
    let seg: string;
    try {
      seg = decodeURIComponent(raw);
    } catch {
      return null;
    }
    if (seg === '' || seg === '.' || seg === '..' || seg.includes('/')) return null;
    segments.push(seg);
  }
  return segments.join('/');
}

/** A presigned upstream url may only name the token's key: its path must be
 * `/<key>` (virtual-hosted bucket) or `/<bucket>/<key>` (path-style). Without
 * this, a valid token for one key could fill that key's cache slot with the
 * bytes of another presigned object. */
export function upstreamPathMatchesKey(upstream: string, key: string): boolean {
  let path: string;
  try {
    path = decodeURIComponent(new URL(upstream).pathname);
  } catch {
    return false;
  }
  const suffix = '/' + key;
  if (!path.endsWith(suffix)) return false;
  const prefix = path.slice(0, path.length - suffix.length);
  return prefix === '' || /^\/[^/]+$/.test(prefix);
}

/** Content-Type by product extension; object stores tend to answer
 * `application/octet-stream` for everything (or `binary/octet-stream` on
 * RGW), which a browser cannot do much with. */
export function contentTypeFor(key: string, upstream: string | null): string {
  const lower = key.toLowerCase();
  if (lower.endsWith('.json')) return 'application/json';
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'image/jpeg';
  if (lower.endsWith('.fits') || lower.endsWith('.fits.gz')) return 'application/fits';
  if (lower.endsWith('.gz')) return 'application/gzip';
  if (upstream && !/octet-stream/i.test(upstream)) return upstream;
  return 'application/octet-stream';
}

function encodeKeyPath(key: string): string {
  return key.split('/').map(encodeURIComponent).join('/');
}

/** The Cache API key for a (key, hash) pair — an on-zone url so it works on
 * the custom domain; never the presigned upstream url, which churns. */
export function objectCacheKey(origin: string, key: string, hash: string): string {
  return `${origin}/_cache/o/${encodeKeyPath(key)}?h=${encodeURIComponent(hash)}`;
}

function getCache(): Cache | undefined {
  return (globalThis as unknown as { caches?: { default?: Cache } }).caches?.default;
}

function objectError(message: string, status: number): Response {
  return new Response(message, {
    status,
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-store',
    },
  });
}

function preflight(): Response {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
      'Access-Control-Allow-Headers': ALLOW_HEADERS,
      'Access-Control-Max-Age': '86400',
    },
  });
}

const PASSTHROUGH_HEADERS = ['Content-Length', 'Content-Range', 'ETag', 'Last-Modified'];

/** The outward response for a served (hit or miss) object. */
function serve(src: Response, key: string, xcache: 'HIT' | 'MISS', request: Request): Response {
  const method = request.method;
  const headers = new Headers();
  headers.set('Content-Type', contentTypeFor(key, src.headers.get('Content-Type')));
  for (const h of PASSTHROUGH_HEADERS) {
    const v = src.headers.get(h);
    if (v) headers.set(h, v);
  }
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Cache-Control', SERVE_CACHE_CONTROL);
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Access-Control-Expose-Headers', EXPOSE_HEADERS);
  headers.set('X-Cache', xcache);
  if (src.status === 200 && isNotModified(request, headers)) {
    src.body?.cancel().catch(() => {});
    headers.delete('Content-Length');
    return new Response(null, { status: 304, headers });
  }
  if (method === 'HEAD') {
    src.body?.cancel().catch(() => {});
    return new Response(null, { status: src.status, headers });
  }
  return new Response(src.body, { status: src.status, headers });
}

/** Full-object fills in flight on this isolate, keyed by cache key, so the
 * two Range requests a FITS view issues against a cold slot cost one
 * upstream GET, not two. */
const fills = new Map<string, Promise<void>>();

/** Fetch the whole object and store it under the (key, hash) slot. Never
 * throws; a failed fill just leaves the slot cold for the next miss. */
function fillInBackground(cache: Cache, cacheKey: string, key: string, upstream: string, registeredAt: number): Promise<void> {
  const inflight = fills.get(cacheKey);
  if (inflight) return inflight;
  const fill = (async () => {
    try {
      const full = await fetch(upstream, { redirect: 'manual' });
      if (full.status !== 200 || !full.body || !isRegisteredBytes(full.headers, registeredAt)) {
        if (full.status === 200) console.warn('not caching', key, ': upstream is newer than its registration');
        full.body?.cancel().catch(() => {});
        return;
      }
      await cache.put(new Request(cacheKey), new Response(full.body, { status: 200, headers: storedHeaders(full.headers, key) }));
    } catch (err) {
      console.error('background fill failed for', key, err);
    } finally {
      fills.delete(cacheKey);
    }
  })();
  fills.set(cacheKey, fill);
  return fill;
}

/** Headers for the stored copy: only what a later serve re-derives from. */
function storedHeaders(src: Headers, key: string): Headers {
  const headers = new Headers();
  headers.set('Content-Type', contentTypeFor(key, src.get('Content-Type')));
  for (const h of ['Content-Length', 'ETag', 'Last-Modified']) {
    const v = src.get(h);
    if (v) headers.set(h, v);
  }
  headers.set('Cache-Control', STORE_CACHE_CONTROL);
  return headers;
}

export async function handleObject(
  request: Request,
  url: URL,
  env: ObjectEnv,
  ctx?: ObjectContext,
): Promise<Response> {
  if (request.method === 'OPTIONS') return preflight();
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return objectError('Method not allowed', 405);
  }
  if (!env.JWT_SECRET) {
    return objectError('Front misconfigured: JWT_SECRET is not set', 503);
  }

  const key = decodeObjectKey(url.pathname.slice('/o/'.length));
  const hash = url.searchParams.get('h');
  const exp = url.searchParams.get('e');
  const registered = url.searchParams.get('r');
  const token = url.searchParams.get('t');
  const upstream = url.searchParams.get('u');
  if (!key || !hash || !exp || !registered || !token || !upstream) {
    return objectError('Missing key, h, e, r, t or u', 400);
  }

  // Authorization on every serve, cache hits included.
  const expSeconds = Number(exp);
  if (!/^\d+$/.test(exp) || !Number.isFinite(expSeconds) || expSeconds * 1000 < Date.now()) {
    return objectError('Token expired', 403);
  }
  if (!/^\d+$/.test(registered)) return objectError('Invalid registration time', 400);
  const registeredAt = Number(registered);
  const valid = await verifySignature(objectTokenMessage(key, hash, exp, upstream, registered), token, env.JWT_SECRET);
  if (!valid) return objectError('Invalid token', 403);

  // The upstream is bound by the token, but check it anyway (defense in
  // depth if the secret ever leaks): allowlisted https host, and a path that
  // names the token's key.
  const check = isAllowedFetchUrl(upstream, env.ALLOWED_FETCH_HOSTS);
  if (!check.ok) return objectError(`Upstream not allowed: ${check.reason}`, 403);
  if (!upstreamPathMatchesKey(upstream, key)) {
    return objectError('Upstream does not name the token key', 403);
  }

  const range = request.headers.get('Range');
  const cache = getCache();
  const cacheKey = objectCacheKey(url.origin, key, hash);

  if (cache) {
    // The Cache API answers a Range request from a stored full object as 206.
    const hit = await cache.match(new Request(cacheKey, range ? { headers: { Range: range } } : {}));
    if (hit) return serve(hit, key, 'HIT', request);
  }

  // Presigned GET: the SigV4 signature covers the method, so always GET
  // upstream (a HEAD is answered from the GET's headers, body cancelled).
  const up = await fetch(upstream, {
    redirect: 'manual',
    headers: range ? { Range: range } : undefined,
  });
  if ((up.type as string) === 'opaqueredirect' || (up.status >= 300 && up.status < 400)) {
    up.body?.cancel().catch(() => {});
    return objectError(`Upstream redirect refused (${up.status})`, 502);
  }
  if (up.status === 404) {
    up.body?.cancel().catch(() => {});
    return objectError('Not found', 404);
  }
  if (up.status === 416) {
    // Not an object answer: keep it uncached and unlabelled, like the 404.
    up.body?.cancel().catch(() => {});
    return objectError('Range not satisfiable', 416);
  }
  if (!(up.status === 200 || up.status === 206) || !up.body) {
    up.body?.cancel().catch(() => {});
    return objectError(`Upstream fetch failed (${up.status})`, 502);
  }

  // Partial answers are streamed through, never stored: only a complete 200
  // may fill the (key, hash) slot — so a Range miss fills it with its own
  // full-object GET in the background. A HEAD miss already holds a full 200
  // (SigV4 forces the GET) and falls through to the tee below, where serve()
  // cancels the client leg and the store leg fills the slot: one upstream
  // fetch, not two.
  if (range || up.status !== 200 || !cache) {
    if (cache && ctx) ctx.waitUntil(fillInBackground(cache, cacheKey, key, upstream, registeredAt));
    return serve(up, key, 'MISS', request);
  }

  // An upstream newer than the token's registration is an in-place
  // overwrite: serve it (the product's current bytes), never store it under
  // the old hash.
  if (!isRegisteredBytes(up.headers, registeredAt)) {
    console.warn('not caching', key, ': upstream is newer than its registration');
    return serve(up, key, 'MISS', request);
  }

  const [toClient, toStore] = up.body.tee();
  const stored = new Response(toStore, { status: 200, headers: storedHeaders(up.headers, key) });
  const put = cache.put(new Request(cacheKey), stored).catch((err) => {
    console.error('cache.put failed for', key, err);
  });
  if (ctx) ctx.waitUntil(put);
  return serve(new Response(toClient, { status: 200, headers: up.headers }), key, 'MISS', request);
}
