# web/ — portal and REST API

Next.js (App Router) + Supabase + Tailwind + TanStack Query. Setup and env vars
are in `README.md`.

```bash
npm install && npm run dev                       # needs .env.local
npm run typecheck && npm test && npm run build   # what CI's `web` check runs (+ npm run budget)
```

## Where things are

| Path | What |
|---|---|
| `app/` | Pages and route handlers; `app/api/v1/` is the bearer-token REST API the Python client uses |
| `lib/actions/` | Server actions (`"use server"`), for mutations only |
| `lib/server/` | Server-only shared logic (`import 'server-only'`) used by routes and actions |
| `lib/auth/` | Identity resolution (`identity.ts`) and the per-user access memo (`access-context.ts`) |
| `lib/types.ts`, `lib/actions/spectra-types.ts` | DB row types; sort columns |
| `lib/flags.ts` | Bitmask flags (spectral features, object flags, DQ) and the quality enum |
| `lib/catalog-query/` | The catalog predicate tree and its filter vocabulary |
| `components/filters/` | Filter bar, chips, Advanced Filters modal |
| `components/spectra/plotting-utils.ts` | Spectrum plotting helpers |
| `lib/contexts/` | `useAuth()`, `useTheme()`, `usePreferences()` |
| `workers/download-worker/` | Cloudflare Worker that serves immutable products (`/o/<key>`) |
| `lib/docs/content/`, `lib/updates/content/` | User docs and the Updates feed, as Markdown (`/new-update` writes the latter) |

## Rules

**Copy.** Pages carry as little text as possible: avoid explanatory microcopy,
backend or implementation details (hashes, formats, snapshot semantics), and CLI
hints (those can live under `/admin`). A header gets one short line; sections show
numbers in cards or badges; longer detail goes behind a collapsible or in `/docs`.
Write in American English.

**Reads go through GET routes; server actions are mutations only.** Next
serializes server-action POSTs per client, so a read written as an action queues
behind every other action on the page and can't be aborted or cached. The pattern:
- The route under `app/api/` resolves identity with `getRequestIdentity()` /
  `getRequestPrincipal()`, queries under RLS, and exports its response type.
- It sets `Cache-Control: private, …`, never `public`. Add `Vary: Cookie` if the
  response is browser-cached longer than a session, because sign-out doesn't clear the HTTP cache.
- The client calls it with `fetchJson()` (`lib/fetch-json.ts`) inside a
  `useQuery`. The query key names *what* is fetched, never the viewer; the
  QueryClient is cleared on sign-out.
- Examples: `/api/shutters`, `/api/objects/adjacent`, `/api/filter-options`,
  `/api/metadata/*`, `/api/lists/membership`.

**Auth.**
- Server: resolve identity once per request with `getRequestIdentity()` /
  `getRequestPrincipal()` / `requireAdmin()` from `lib/auth/identity.ts` (the cookie
  session is verified locally against `SUPABASE_JWT_SECRET`; `middleware.ts`
  refreshes it), and `getAccessContext(userId)` for the admin flag and accessible programs.
- `/api/v1` bearer routes use `validateAuth()` / `authenticateApiRequest()` from `lib/api-auth.ts`.
- Never call `supabase.auth.getUser()` or read `SUPABASE_SERVICE_ROLE_KEY`
  directly; ESLint rejects both. Use `createServiceClient()` from
  `lib/supabase/service.ts`, and only where the code scopes rows itself.
- Client: `useAuth()`.

**Two hostnames, one portal.** `campfire.hollisakins.com` serves the same
container as `campfire-jwst.org`, because some networks block one domain or the
other. A visitor on an alias must never be sent to the primary host, so every
absolute URL the browser follows goes through `lib/host-aliases.ts`:
- `browserUrl()` in client code;
- `localizeUrl(url, request.headers.get('host'))` in route handlers;
- `urlForRequest()` (`lib/server/request-host.ts`) elsewhere on the server.

Server-to-server URLs and canonical ones (`metadataBase`, citations) stay
primary. Prefer relative links. The alias map is the build-time `NEXT_PUBLIC_HOST_ALIASES`.

**Bulk bytes don't stream through the app.** Sidecar JSON and NIRCam PNG/FITS
are served by the Cloudflare Worker. A route authorizes the read, then mints a
content-addressed URL with `frontUrlFor()` / `frontUrlsFor()`
(`lib/server/cdn-front.ts`), and falls back to streaming when that returns null
(the CDN front isn't configured). The header of `cdn-front.ts` explains the token and cache rules.

**Catalog filters are a predicate tree.** The filter state is `conditions:
TreeNode[]`, carried in the URL as `?q=` JSON and compiled in SQL by
`catalog_tree_sql`. Every server boundary calls `normalizeFilters(filters, root)`,
then `needsTreePath` to choose between the legacy list RPCs and the tree path.
A new filter dimension is a whitelist entry in the database plus a vocabulary
entry in `lib/catalog-query/vocabulary.ts`, not a new RPC parameter. Read the
headers of `lib/catalog-query/tree.ts` and `vocabulary.ts` before changing anything
in `lib/catalog-query/` or `components/filters/`.

**Line and band filters send parameters only while set.** `buildFilterParams`
sends `p_line*` / `p_band*` only when a line or band is chosen. Measurement
columns (`p_line_columns` / `p_band_columns`) are derived from the tree with
`columnKeys`, never stored separately.

**API pagination is keyset.** `/api/v1/objects` and `/api/v1/spectra/list` return
an opaque cursor (`lib/api-cursor.ts`, fingerprinted against the filter + sort). The
`/api/v1/sync/*` routes reject `offset>0`. Don't add offset paging. Keep RPC
parameters that deployed clients still send until the client floor
(`/api/v1/version`) moves past them, because migrations and the web image don't
land atomically.

## Generated files

Don't hand-edit these; regenerate them (CI fails on drift):
- `lib/linelist.json`: from `pipeline/.../nirspec/linelist.py` via `scripts/sync_linelist.py`.
- `lib/layout-products.json`: from `layout/` via `scripts/sync_layout_products.py`.
- `public/etc/models/`: a verbatim copy of `etc/campfire_etc/models/` via `npm run etc-models`.
- `components/plot/plotly-loadable-ids.json`: hand-maintained, but `npm run budget`
  fails if its ids no longer exist in the build's loadable manifest.

## NIRSpec ETC page

`/nirspec/etc` is unlisted on purpose: don't add it to the navbar. `lib/etc/model.ts`
ports `etc/campfire_etc/model.py`, and `model.test.ts` pins the same reference
numbers as `etc/tests/test_model.py`. The tables and caveats are generated from
the model file, so a new model version needs no prose edits.
