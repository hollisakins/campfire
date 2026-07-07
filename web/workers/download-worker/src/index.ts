/**
 * Cloudflare Worker for CAMPFIRE FITS file downloads.
 *
 * A credential-free, per-file CORS proxy. Vercel authorizes a download (RLS),
 * presigns each file against whichever backend homes it (dual-read: R2 or OSN),
 * signs the presigned URL with the shared secret, and hands the browser ready
 * `/proxy?url=…&sig=…` links. This Worker verifies the signature, checks the
 * target host against an allowlist, then streams the object back with CORS
 * headers so the browser can read the bytes and zip them client-side.
 *
 * It holds NO object-store credentials and NO R2 binding — that's what keeps it
 * on the free Workers plan (1 subrequest, ~0 CPU per request) and lets it serve
 * OSN, which the old R2-binding proxy could not.
 */

import { verifyUrlSignature } from './auth';

export interface Env {
  JWT_SECRET: string; // shared HMAC secret with the Next.js server action
  ALLOWED_ORIGINS: string; // comma-separated browser origins allowed to read responses
  ALLOWED_FETCH_HOSTS: string; // comma-separated object-store hosts the proxy may fetch
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return handleCORS(request, env);
    }

    if (request.method !== 'GET' || url.pathname !== '/proxy') {
      return new Response('Not found', { status: 404 });
    }

    try {
      // Fail loudly on a misconfigured proxy. Without the shared secret,
      // verifyUrlSignature's HMAC importKey gets a zero-length key and throws a
      // DataError, which would otherwise surface as an opaque 500 on every file.
      if (!env.JWT_SECRET) {
        return corsError('Proxy misconfigured: JWT_SECRET is not set', 503, request, env);
      }

      const target = url.searchParams.get('url');
      const sig = url.searchParams.get('sig');
      if (!target || !sig) {
        return corsError('Missing url or sig parameter', 400, request, env);
      }

      // Only fetch URLs our own server signed — the proxy is not an open relay.
      const valid = await verifyUrlSignature(target, sig, env.JWT_SECRET);
      if (!valid) {
        return corsError('Invalid signature', 403, request, env);
      }

      // Defense-in-depth: even a validly-signed URL must point at an allowlisted
      // object-store host over https (guards SSRF if the secret ever leaks).
      const check = isAllowedFetchUrl(target, env.ALLOWED_FETCH_HOSTS);
      if (!check.ok) {
        return corsError(`Target not allowed: ${check.reason}`, 403, request, env);
      }

      // redirect: 'error' — an allowlisted host must not be able to bounce us
      // to an arbitrary host after the allowlist check.
      const upstream = await fetch(target, { redirect: 'error' });
      if (!upstream.ok || !upstream.body) {
        return corsError(`Upstream fetch failed (${upstream.status})`, 502, request, env);
      }

      const headers = new Headers();
      headers.set('Content-Type', upstream.headers.get('Content-Type') || 'application/octet-stream');
      const len = upstream.headers.get('Content-Length');
      if (len) headers.set('Content-Length', len);
      headers.set('Access-Control-Allow-Origin', getAllowedOrigin(request, env));
      headers.set('Access-Control-Allow-Methods', 'GET, OPTIONS');
      headers.set('Vary', 'Origin');
      headers.set('Cache-Control', 'no-store');

      return new Response(upstream.body, { status: 200, headers });
    } catch (error) {
      console.error('Worker error:', error);
      return corsError('Internal server error', 500, request, env);
    }
  },
};

/**
 * Validate that a signed target URL is safe to fetch: https, no embedded
 * credentials, and a hostname that exactly matches (or is a subdomain of) one
 * of the allowlisted object-store hosts. Exported for unit testing.
 */
export function isAllowedFetchUrl(
  rawUrl: string,
  allowedHostsCsv: string
): { ok: true } | { ok: false; reason: string } {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return { ok: false, reason: 'unparseable url' };
  }
  if (parsed.protocol !== 'https:') {
    return { ok: false, reason: 'non-https scheme' };
  }
  if (parsed.username || parsed.password) {
    return { ok: false, reason: 'embedded credentials' };
  }
  const allowed = allowedHostsCsv
    .split(',')
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
  const host = parsed.hostname.toLowerCase();
  const hostOk = allowed.some((h) => host === h || host.endsWith('.' + h));
  if (!hostOk) {
    return { ok: false, reason: 'host not in allowlist' };
  }
  return { ok: true };
}

function handleCORS(request: Request, env: Env): Response {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Max-Age': '86400',
      'Vary': 'Origin',
    },
  });
}

function getAllowedOrigin(request: Request, env: Env): string {
  const origin = request.headers.get('Origin');
  const allowedOrigins = (env.ALLOWED_ORIGINS || '').split(',').map((o) => o.trim()).filter(Boolean);

  if (origin && allowedOrigins.includes(origin)) {
    return origin;
  }

  // Never fall back to '*': a misconfigured (empty) allowlist must DENY, not
  // open the proxy to every origin. 'null' is a valid Origin value no browser matches.
  return allowedOrigins[0] ?? 'null';
}

/** Error response that still carries CORS headers, so the browser can read the
 * status/message instead of collapsing every failure into an opaque network error. */
function corsError(message: string, status: number, request: Request, env: Env): Response {
  return new Response(message, {
    status,
    headers: {
      'Access-Control-Allow-Origin': getAllowedOrigin(request, env),
      'Vary': 'Origin',
    },
  });
}
