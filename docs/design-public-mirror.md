# Design: Public mirror — shareable, scope-limited links for unauthenticated viewers

**Status:** investigation / design proposal, not yet implemented.
**Date:** 2026-07-26
**Driver:** Sharing one NIRCam field or one NIRSpec observation with a collaborator
who has no CAMPFIRE account currently has no answer short of "make them an account"
or "email a tarball". We want an admin-minted URL that exposes exactly one deploy
scope, hides the rest of the portal, and — as a stretch — lets a one-off reduction
be shared without ever appearing in the main catalog.
**Related:** [intermediate products / deploy lifecycle](design-intermediate-products.md)
(the `draft → published → revoked` machinery this leans on, issues #217/#218),
[NIRCam deploy overhaul](design-nircam-deploy-overhaul.md) (field-scoped deployments, #261).

---

## 1. What we want

1. Admin mints a URL scoped to **one deploy scope** — a NIRCam field or a NIRSpec
   observation.
2. The URL carries an unguessable token; no login, no account, no email.
3. A visitor with the URL sees the normal CAMPFIRE pages for that scope —
   tables, object pages, spectra plots, the map, FITS downloads — and nothing else.
4. Portal chrome (nav, sign-out, admin links, cross-scope links) is suppressed so
   the visitor is not staring at buttons that 404 for them.
5. Admin can revoke a link, and see which links exist.
6. **Stretch:** a scope can be shared *without being published* — a one-off
   reduction for one colleague that never pollutes the catalog, the map, the
   counts, or anyone else's search results.

Non-goals: public write access (comments, inspection, lists), API-key/CLI access
for link holders, search-engine-indexable public pages, per-object sharing.

---

## 2. The one fact that determines the whole design

Every access decision in CAMPFIRE bottoms out in Postgres RLS keyed on
`auth.uid()`, through exactly three helpers in `supabase/schemas/functions.sql`:

- `public.is_admin()`
- `public.accessible_program_slugs()` — the single choke point for program-scoped
  reads; unions explicit `user_program_access` grants, all `is_public` programs,
  and (for admins) everything
- `public.can_comment()` / `public.can_inspect()` for writes

There is **no Next.js middleware** (`web/middleware.ts` does not exist) and no
route-level auth guard. Pages render for anyone; they come back empty because
every RLS `SELECT` policy is `TO authenticated` and `anon` matches nothing. The
`/api/v1/*` routes are the one exception: they use `validateAuth()` +
`getAccessiblePrograms(userId)` + a **service-role** client, re-deriving the same
scope explicitly in TypeScript (`web/lib/api-helpers.ts`).

The consequence: **anything that authenticates as a real Supabase user gets the
entire existing data plane for free** — server actions, the ~30 filter/search
RPCs, presigned FITS URLs, the download Worker, `/api/v1/cutout`, map tiles.
Anything that does *not* needs a parallel data plane built by hand.

That asymmetry is worth roughly an order of magnitude in implementation cost, and
it drives the recommendation below.

---

## 3. Options considered

### A. Anonymous RLS + token claim

Add `TO anon` policies that read a share token out of a custom JWT claim or a
`set_config` GUC. Rejected: every one of the ~20 gated `SELECT` policies would
need an `anon` twin, doubling the policy surface — the exact surface where a
mistake is a data leak. You also still have to mint a signed JWT, so you have not
avoided the session-minting work; you have only avoided having a user row.

### B. Service-role public API + hand-written `/public/[token]` pages

Bypass RLS entirely, re-derive scope in TypeScript per route (the `/api/v1`
pattern extended to pages). Rejected: this is a second, parallel data plane. The
NIRCam field page alone pulls from `getNircamImages`, `getNircamExpmaps`,
`getNircamFieldSummary`, `getNircamFieldImages`, `getFitsglDatasets`; the object
page pulls `getObjectById` plus photometry, spectra, comments, lists. Every one
would need a service-role twin with its own hand-rolled authz — the highest-leak,
highest-code option.

### C. Link accounts — a synthetic authenticated principal per share link ✅

A share link is backed by a real `auth.users` row that cannot be logged into
normally. Visiting the link trades the token for a normal Supabase cookie session.
From that point on **every existing code path works unchanged**, because the
visitor genuinely is an authenticated user — just one whose scope is exactly the
shared field or observation.

All the work moves into *narrowing* what that principal can see, which is a
finite, auditable list of policy edits (§5) rather than a parallel implementation.

### D. Static snapshot export

`campfire export --obs X` → a self-contained static site or tarball. Cheapest by
far and genuinely useful for "here are my reductions" emails, but it is a
different product: no map, no live plots, no re-deploy freshness. Worth doing
someday; does not answer this request.

**Recommendation: C.**

---

## 4. Architecture (option C)

### 4.1 Data model

```sql
CREATE TABLE public.share_links (
  token          text PRIMARY KEY,          -- 32 url-safe random chars
  label          text NOT NULL,             -- admin-facing, e.g. "Naidu — cosmos-web f444w"
  observation    text REFERENCES observations(name),
  field          text REFERENCES fields(name),
  link_user_id   uuid NOT NULL REFERENCES auth.users(id),
  include_drafts boolean NOT NULL DEFAULT false,   -- §6
  created_by     uuid NOT NULL REFERENCES auth.users(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz,               -- NULL = no expiry
  revoked_at     timestamptz,
  last_seen_at   timestamptz,
  view_count     integer NOT NULL DEFAULT 0,
  CONSTRAINT share_links_scope_check CHECK (num_nonnulls(observation, field) = 1)
);
```

The scope check deliberately mirrors `deployments_scope_check` — a share link is
scoped the same way a deployment is, which is what makes the admin UI natural
(§7) and the draft tie-in clean (§6).

`share_links` is admin-only under RLS. The token is the primary key and is never
exposed to a non-admin except by whoever holds the URL.

### 4.2 The link account

At mint time, a service-role call creates:

- an `auth.users` row via `auth.admin.createUser` with a synthetic, non-routable
  email (`link+<token>@shared.invalid`), `email_confirm: true`, and a random
  32-byte password that is stored server-side only (in `share_links`, or in a
  sibling secret column) and never shown to anyone;
- a `user_profiles` row with `is_admin=false`, `can_comment=false`,
  `can_inspect=false`, and a **new** `is_link_account=true` flag;
- a `user_program_access` grant for the scope's program (NIRSpec) — NIRCam field
  deployments are not program-scoped, so the field axis is handled by §5.2.

`is_link_account` is the discriminator every narrowing rule keys on. It goes on
`user_profiles` next to `is_group_account`, which is the existing precedent for
"a principal with reduced affordances".

### 4.3 Session minting

`GET /s/<token>` (a Next.js route handler):

1. Look up the token with the service client. Reject if missing, revoked, or expired.
2. `signInWithPassword` with the stored link credentials against the **cookie**
   client (`@supabase/ssr`), which sets the session cookie exactly as a normal
   login does.
3. Bump `view_count` / `last_seen_at`.
4. `redirect()` to the scope's canonical page — `/nircam/<field>` or
   `/nirspec?observations=<obs>` — with a `?shared=1` marker.

No new auth primitive, no custom JWT signing, no session format to keep in sync
with Supabase. It reuses the same cookie plumbing `web/lib/supabase/server.ts`
already implements.

Sign-in is rate-limited per token at the route to blunt token guessing; with 32
url-safe characters (~190 bits) guessing is not a realistic threat, but the
counter is also the abuse signal an admin wants to see.

---

## 5. Narrowing — the actual work

This is where the honest effort lives. A link account is `authenticated`, so it
inherits every policy written as "any logged-in user may read this". I audited
all 47 `SELECT` policies in `supabase/schemas/policies.sql`. Three groups:

### 5.1 Program axis — one function, ~10 lines

`accessible_program_slugs()` unions in every `is_public` program. A link account
must **not** get that union — otherwise a share link is a key to every public
program in the archive. Fix, inside the existing function:

```sql
-- Link accounts (share_links) see ONLY their explicit grants: no is_public
-- union, no admin union. The single choke point for the program axis.
SELECT CASE WHEN public.is_link_account() THEN <explicit grants only>
            ELSE <existing union> END
```

Because ~20 policies already route through this function, that one edit correctly
narrows targets, objects, spectra, photometry, comments, observations,
storage_objects, list members, and the audit logs.

### 5.2 Sub-program axis — a second helper, ~6 policies

`accessible_program_slugs()` cannot express "one observation out of a program that
has eight". Add:

```sql
-- NULL for ordinary users (no restriction); the link's scope for link accounts.
public.link_scope() RETURNS TABLE (observation text, field text)
```

and `AND` a `public.in_link_scope(...)` predicate into the six policies whose
tables actually carry the axis: `targets` (`observation`), `spectra` (via
`targets`), `objects` (`observations[]`), `observations` (`name`),
`nircam_images` (`field`), `storage_objects` (via spectrum → target, or via
`deployments`). Everything else inherits transitively.

**MVP shortcut worth taking:** if the shared scope is given its own program slug,
§5.2 disappears entirely and §5.1 alone is sufficient. For the one-off reductions
that motivate the stretch goal this is *already the natural shape* — a bespoke
reduction for a colleague wants its own program row anyway. I would ship §5.1
first, share by program, and add §5.2 when the first "share one obs out of a
shared program" request actually arrives.

### 5.3 The `USING (true)` carve-outs — 6 policies

These leak to any authenticated principal and each needs an explicit
`AND NOT is_link_account()` (or a narrowed predicate):

| Table | Policy | What a link account would otherwise see |
|---|---|---|
| `user_profiles` | `authenticated_select_profiles` | every CAMPFIRE user's name + username |
| `deployments` | `authenticated_select_deployments` | full deploy provenance for every scope |
| `map_layers` | `Authenticated users can read map layers` | tile layers for every field |
| `nircam_images` | `authenticated_select_nircam` | every published mosaic, all fields |
| `object_lists` | `select_lists` | every `public_read` / `public_edit` list |
| `programs` | `accessible_programs_select` | metadata for every public program |
| `flag_definitions` | `authenticated_select_flags` | harmless — leave as is |

`fields` and `fitsgl_datasets` narrow automatically once `nircam_images` does,
since their policies are defined in terms of it.

This table is the security core of the feature and the part that most deserves a
test. A `supabase/tests/` case that opens a session as a link account and asserts
zero rows from each of the above (plus zero rows for an out-of-scope observation)
is the right regression gate — a leak here is silent otherwise.

### 5.4 Writes

Link accounts get `can_comment=false` / `can_inspect=false`, so every write policy
already refuses them. The one gap is the `insert_lists` policy, which checks
`can_comment` — already false. Nothing further needed, but the test above should
assert it.

---

## 6. The stretch goal: sharing without publishing

This falls out almost for free, and it is the most interesting part of the whole
proposal.

The `draft → published → revoked` lifecycle from #217/#218 already exists and is
already wired end to end:

- `campfire deploy --in-prep` writes `deployments.status='draft'` and
  `spectra.deploy_status='draft'` (`python/campfire/deploy/deploy.py`)
- draft rows are **invisible to every non-admin** — `select_spectra_by_access`
  gates on `deploy_status='published' OR is_admin()`, and
  `targets.has_published_spectrum` / `objects.has_published_spectrum` keep drafts
  out of the map, the SED endpoints, and the target-derived readers
- `nircam_images.deploy_status` does the same for mosaics
- `storage_objects` gates FITS downloads on the same flag
- `/admin/deployments` already renders the lifecycle with publish/revoke controls

So "reduce it, share it, don't pollute the catalog" is exactly:

```
campfire deploy --obs one_off_thing --in-prep      # draft: nobody sees it
# admin panel → mint share link, "include drafts" checked
```

The only schema change is to relax the draft gate from `is_admin()` to
`is_admin() OR link_sees_draft(<row>)`, where `link_sees_draft` is true when the
row is inside the link's scope *and* the link was minted with
`include_drafts=true`. That is the same six policies as §5.2, so the two land
together.

Everything else already behaves correctly: draft data stays out of counts,
aggregates, the map, and search for every ordinary user, and `campfire deploy`
already knows how to produce it. The lifecycle was designed for admin-only
staging; this extends it by exactly one principal type.

A useful consequence: publishing later is a no-op for the link. The colleague's
URL keeps working; the data simply also becomes visible to everyone else.

---

## 7. UI

### Chrome suppression

`web/app/layout.tsx` renders `<Navigation />` and `<ConditionalFooter />`
unconditionally. `ConditionalFooter` already demonstrates the pattern (it hides
on `/map` via `usePathname`). Add the mirror of it:

- `AuthContext` exposes `isLinkAccount` (read off the profile it already fetches)
- `Navigation` renders a stripped bar when `isLinkAccount` — CAMPFIRE wordmark,
  the scope name, theme toggle, and a short "You're viewing a shared view of
  *cosmos-web*" note with a "request an account" link. No nav links, no profile
  menu, no admin entry.
- Server-render the flag too, or accept a brief flash of the full nav on first
  paint. Reading the profile in the root layout's server component avoids it.

Deep links out of scope (a `Show on map` button, a program breadcrumb) will land
on pages that render empty rather than 403. The stripped nav removes most of
them; the rest are worth a pass once the feature is real, guided by whatever a
link holder actually clicks.

### Admin panel

`/admin/deployments` is already keyed on the same `(observation | field)` scope, so
the natural placement is a **Share** action per deployment row, next to the
existing Publish/Revoke control, opening a modal for label / expiry /
include-drafts and returning the URL to copy. A `/admin/share-links` table lists
active links with scope, creator, view count, last seen, and a revoke button —
reusing `AdminTable` + `useAdminTableQuery` like every other admin page.

Revoke = set `revoked_at`, and delete the link's `auth.users` row so any live
cookie session dies at the next token refresh.

---

## 8. What still works, unchanged

Worth stating explicitly, because it is the payoff for choosing option C:

- **FITS downloads.** `lib/actions/download.ts` presigns server-side after the
  RLS read, then hands the client a Worker proxy URL. A link account's presign
  requests only ever cover rows it can see.
- **`/api/v1/storage/presign`.** Runs `filter_accessible_storage_keys` with the
  caller's program list — link accounts get their narrowed list from §5.1.
- **Cutouts, SED plots, spectra plots, the map, FitsGL.** All read through the
  same cookie client.
- **Map tiles** are already served from a public CDN base URL
  (`map_layers.tile_base_url`) with no auth — narrowing `map_layers` controls
  which layers are *discoverable*, which is the same protection existing users
  have.

---

## 9. Risks

- **A link account is a real user.** The blast radius of a missed policy is a
  real read, not an error. §5.3 must be complete and tested; the test is the
  deliverable, not a nice-to-have.
- **`user_profiles` leak is the sharpest edge.** `USING (true)` on that table
  means an unnarrowed link account can enumerate every CAMPFIRE user. Fix it in
  the same commit that creates link accounts, not after.
- **Security through obscurity is the actual security model.** That is a
  deliberate, appropriate choice here (it is how every "share by link" product
  works), but it means expiry and revocation need to be easy, and the token
  should never end up in a referrer or a server log the way a query parameter
  would — `/s/<token>` as a path segment that immediately redirects, rather than
  `?token=`, is why §4.3 is shaped that way.
- **Draft sharing widens the draft gate.** Today `draft` means exactly "admins
  only", which is easy to reason about. After §6 it means "admins, plus scoped
  link accounts". That is still narrow, but it is the first crack in a currently
  absolute invariant, and the policy comments should say so.
- **Anonymous-visitor semantics.** Every link holder shares one principal, so
  `download_log` and any future analytics attribute all activity to the link, not
  a person. Fine for the use case; worth not pretending otherwise.

---

## 10. Suggested phasing

**Phase 1 — program-scoped sharing (the MVP).**
`share_links` table + link accounts + `is_link_account` + §5.1 + §5.3 +
`/s/<token>` + stripped nav + admin mint/revoke UI + the leak test.
This already answers "share one field" and "share one observation" whenever the
scope has its own program slug.

**Phase 2 — sub-program scoping.** `link_scope()` / `in_link_scope()` ANDed into
the six axis-carrying policies. Needed the first time an observation must be
shared out of a multi-observation program.

**Phase 3 — draft sharing.** Relax the draft gate to scoped link accounts;
`include_drafts` on the mint form. Lands naturally with Phase 2 since it touches
the same policies. Delivers the stretch goal.

**Phase 4 — polish.** Expiry sweeps, per-link download budget, out-of-scope link
suppression in page bodies, an "invite to a real account" CTA on the shared view.

Phase 1 is the bulk of the value and is largely additive — one new table, one
function edit, six policy edits, one route, one layout branch, one admin screen.
Phases 2–3 are where the policy surface actually changes shape, and they are
better done together, after Phase 1 has proven the shape is right.

---

## 11. Open questions

1. **Scope granularity.** Is "share one field / one observation" enough, or will
   "share these five objects" be wanted? The latter argues for a scope expressed
   as an object list rather than a deploy scope — a different `share_links` shape,
   so worth deciding before Phase 2.
2. **Should link holders be able to download FITS at all,** or only browse? A
   per-link `allow_download` flag is trivial to add now and awkward to retrofit.
3. **Indexability.** `noindex` on shared views is assumed here. If any shared view
   should ever be citable in a paper, that changes the expiry story.
4. **Does a shared view need a citation/attribution block** (program, PI,
   `cfpipe_version`, CRDS context)? The provenance is all in `deployments`, and a
   colleague looking at someone else's reduction is exactly who needs it.
