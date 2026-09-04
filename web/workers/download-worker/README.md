# CAMPFIRE Delivery Worker

Cloudflare Worker with two credential-free endpoints:

- **`/o/<key>`** — the content-addressed delivery front for immutable products
  (spectrum / zfit / P(z) sidecars, NIRCam PNG and FITS, …): token-gated,
  Cache API-backed, `Range`-aware. Perf T2-D1 (#507), decision D-D of the
  2026-09 audit. See "Delivery front" below.
- **`/proxy`** — the per-file CORS proxy for the bulk FITS-ZIP download (#255).
  It lets the browser read FITS bytes from R2 **or** OSN so they can be zipped
  client-side.

## Delivery front (`/o/`)

```
Next.js route (RLS authorizes the read; lib/server/cdn-front.ts mints the url)
    |  resolves the registry row (backend + content_hash, 60 s memo),
    |  presigns upstream on a 6 h window (stable url), HMAC-signs the token
Browser  ->  GET /o/<key>?h=<content_hash>&e=<exp>&r=<registered>&t=<token>&u=<presigned upstream>
    |  Worker verifies t = HMAC(JWT_SECRET, "campfire-o-v2\n<key>\n<h>\n<e>\n<u>\n<r>")
    |  (r = the registry row's updated_at; an upstream whose Last-Modified is
    |   newer was overwritten in place and is served but never cached)
    |  on EVERY serve; checks u is https, host-allowlisted, and names <key>
    |  Cache API lookup keyed by (key, content_hash) — Range answered as 206
    |  miss: fetch u (Range forwarded, redirects refused), stream back with the
    |        upstream status; a full 200 is stored under (key, content_hash)
Browser reads the bytes (CORS `*` — the token is the authorization)
```

Why the hash: products are immutable per `storage_objects.content_hash`, not
per path — a re-deploy overwrites in place and registers a new hash, so the
hash in the cache key is what keeps a stale copy from ever being served. The
presigned upstream url churns (SigV4 date + expiry), so the app signs it on a
fixed 6 h window: every mint within a window is byte-identical and the browser
cache hits too (`private, max-age=86400, immutable`).

The app side is gated on `CDN_FRONT_URL` (this Worker's origin) +
`WORKER_JWT_SECRET`; unset, the routes stream bytes themselves — so the app
can deploy before or after the Worker. Consumers today: `/api/nircam-png` and
`/api/nircam-fits` (`resolve=1` answers the front url), and the exposure
`<img>` presigns (`presignExposurePngs`).

## Bulk download proxy (`/proxy`)

## Why it exists

Object bytes live in a private bucket on R2 or, after the epic #210 migration, on
OSN (Open Storage Network). The browser can't `fetch()` those bytes directly:
neither bucket sends CORS headers (and OSN's are not ours to set). This Worker
sits on an origin we control, fetches the presigned URL server-to-server, and
re-serves the bytes with `Access-Control-Allow-Origin` so the browser can read
and zip them.

It holds **no** object-store credentials and **no** R2 binding. All authorization
and presigning happen in the Next.js app; the Worker only proxies URLs the app
signed. That also keeps it on the **free** Workers plan: one subrequest and ~0
CPU per request (a plain passthrough), versus the 1000-subrequest / high-CPU
profile that server-side zipping would need.

## Architecture

```
Next.js server action (RLS authorizes the key set)
    |  presigns each key via dual-read (R2 or OSN) + HMAC-signs the URL
Browser  ->  GET /proxy?url=<presigned>&sig=<hmac>   (one request per file)
    |  Worker verifies the HMAC, host-allowlists the URL, fetches it
R2 / OSN
    |  Worker streams the bytes back with CORS headers
Browser zips the results client-side (fflate) -> single .zip download
```

## Request

`GET /proxy?url=<url-encoded presigned URL>&sig=<base64url HMAC-SHA256>`

The Worker:
1. verifies `sig == HMAC-SHA256(JWT_SECRET, url)` — so it only fetches URLs the
   app signed (not an open relay);
2. checks the URL is `https`, has no embedded credentials, and its host is in
   `ALLOWED_FETCH_HOSTS` (SSRF defense-in-depth);
3. `fetch(url, { redirect: 'error' })` and streams the body back with CORS.

## Configuration

`wrangler.toml`:
- `ALLOWED_ORIGINS` — browser origins allowed to read responses (CORS).
- `ALLOWED_FETCH_HOSTS` — object-store hosts the proxy may fetch (SSRF guard).
  Includes both the OSN host (migrated FITS) and the R2 account host (un-migrated
  / NIRCam), so the proxy is storage-agnostic. Subdomains match, so R2
  virtual-hosted URLs are covered. A host not in the list fails loudly (403).

Secret (via `wrangler secret put`):
- `JWT_SECRET` — shared with the Next.js app (`WORKER_JWT_SECRET`); used to
  HMAC-sign (app) and verify (Worker) each presigned URL and object token.

`ALLOWED_ORIGINS` applies to `/proxy` only; `/o/` answers `*` because a fetch
that reached it through a cross-origin redirect carries `Origin: null`, and its
token is the authorization.

## Files

- `src/index.ts` — request router + the `/proxy` handler
- `src/object.ts` — the `/o/` delivery front
- `src/guards.ts` — `isAllowedFetchUrl` host guard (both endpoints)
- `src/auth.ts` — `verifyUrlSignature` (HMAC-SHA256, Web Crypto)
- `src/proxy.test.ts` — unit tests for the host guard and signature verify
- `wrangler.toml` — configuration

## Develop / deploy

```bash
npm install
wrangler dev        # local, http://localhost:8787
wrangler deploy     # production, campfire-download.hollisakins.com
wrangler tail       # logs
npm test            # runs the unit tests via the web app's vitest
```

Tests live in `src/proxy.test.ts` and run under the web app's vitest (they are
also picked up by `npm test` in `web/`, so CI covers them).

Runs on the free Workers plan; no paid features are used.

## Security

- The Worker holds no object-store credentials; presigned URLs (SigV4-scoped to a
  single object with a TTL) are the only capability, and it fetches one only if
  the app's HMAC over that exact URL verifies.
- `ALLOWED_FETCH_HOSTS` + https-only + no-embedded-credentials + `redirect: error`
  bound the fetch target to our object stores even if the secret leaks.
- CORS reflection is limited to `ALLOWED_ORIGINS`, so only our portal's JS can
  read proxied responses.
