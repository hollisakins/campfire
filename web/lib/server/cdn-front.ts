// Delivery front for immutable products (perf T2-D1, #507; decision D-D).
//
// A route that has authorized a read mints a url on the Cloudflare Worker's
// `/o/<key>` endpoint instead of streaming the bytes through the Vercel
// function. The url carries the object's registry content_hash (the cache
// identity — products are immutable per hash, not per path), a window-stable
// presigned upstream url the Worker fetches on a miss, and an HMAC token over
// all of it that the Worker verifies on every serve. See
// workers/download-worker/src/object.ts for the serving side.
//
// Off unless CDN_FRONT_URL (the Worker origin) and WORKER_JWT_SECRET are both
// set: callers get null and fall back to serving bytes themselves, so the app
// can ship before the Worker is deployed and local dev needs no Worker.
//
// The upstream path is mutable (re-deploys overwrite in place and register a
// new hash), so the token also carries the row's registration time (`r`):
// the Worker refuses to cache an upstream whose Last-Modified is newer than
// that, which is exactly an overwrite a still-valid older url would
// otherwise store under the old hash.
import 'server-only';

import { resolveObjectBackends, presignResolvedStable } from '@/lib/r2';
import { hmacBase64Url } from './worker-token';

const TOKEN_VERSION = 'campfire-o-v2';

/** The Worker origin, or null when the front is not configured. */
export function cdnFrontBase(): string | null {
  const base = (process.env.CDN_FRONT_URL || '').trim().replace(/\/+$/, '');
  const secret = process.env.WORKER_JWT_SECRET;
  if (!base || !secret) return null;
  return base;
}

/** Mirror of the Worker's objectTokenMessage — the two must stay identical. */
export function objectTokenMessage(
  key: string,
  hash: string,
  exp: number,
  upstream: string,
  registeredAt: number,
): string {
  return [TOKEN_VERSION, key, hash, String(exp), upstream, String(registeredAt)].join('\n');
}

function encodeKeyPath(key: string): string {
  return key.split('/').map(encodeURIComponent).join('/');
}

export function buildFrontUrl(
  base: string,
  key: string,
  hash: string,
  exp: number,
  upstream: string,
  token: string,
  registeredAt: number,
): string {
  return (
    `${base}/o/${encodeKeyPath(key)}` +
    `?h=${encodeURIComponent(hash)}&e=${exp}&r=${registeredAt}&t=${token}&u=${encodeURIComponent(upstream)}`
  );
}

/**
 * Front urls for a batch of storage keys (one registry resolution, presigns
 * in parallel). A key maps to null when the front is off, the key has no
 * active registry row (no content identity to cache on), or its presign
 * failed — callers serve those themselves. Authorization is the CALLER's:
 * this mints for whatever it is given.
 */
export async function frontUrlsFor(keys: string[]): Promise<Map<string, string | null>> {
  const out = new Map<string, string | null>(keys.map((k) => [k, null]));
  const base = cdnFrontBase();
  if (!base || keys.length === 0) return out;
  const secret = process.env.WORKER_JWT_SECRET as string;

  const resolved = await resolveObjectBackends(keys);
  await Promise.all(
    resolved.map(async (o, i) => {
      if (!o.contentHash) return;
      try {
        // A row without a registration time (should not happen: the column
        // is NOT NULL) mints with r=0: servable, never cacheable.
        const registeredAt = o.registeredAt ?? 0;
        const { url, exp } = await presignResolvedStable(o);
        const token = await hmacBase64Url(objectTokenMessage(o.key, o.contentHash, exp, url, registeredAt), secret);
        out.set(keys[i], buildFrontUrl(base, o.key, o.contentHash, exp, url, token, registeredAt));
      } catch (err) {
        console.error(`cdn-front: failed to mint a front url for ${keys[i]}:`, err);
      }
    }),
  );
  return out;
}

/** Front url for one key, or null (see frontUrlsFor). */
export async function frontUrlFor(key: string): Promise<string | null> {
  return (await frontUrlsFor([key])).get(key) ?? null;
}
