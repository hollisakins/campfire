# CAMPFIRE Download Worker

Cloudflare Worker: a credential-free CORS proxy for presigned download URLs. It
lets the browser read FITS bytes from R2 **or** OSN so they can be zipped
client-side.

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
  HMAC-sign (app) and verify (Worker) each presigned URL.

## Files

- `src/index.ts` — request handler + `isAllowedFetchUrl` host guard
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
