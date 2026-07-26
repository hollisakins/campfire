# Design: Public mirror — shareable, scope-limited links for unauthenticated viewers

**Status:** investigation / design proposal, not yet implemented.
**Date:** 2026-07-26 (rev. 2 — dropped the program-scoped phasing, see §3.1)
**Driver:** Sharing one NIRCam field or one NIRSpec observation with a collaborator
who has no CAMPFIRE account currently has no answer short of "make them an account"
or "email a tarball". We want an admin-minted URL that exposes exactly one scope,
hides the rest of the portal, and — as a stretch — lets a one-off reduction be
shared without ever appearing in the main catalog.
**Related:** [intermediate products / deploy lifecycle](design-intermediate-products.md)
(the `draft → published → revoked` machinery this leans on, issues #217/#218),
[NIRCam deploy overhaul](design-nircam-deploy-overhaul.md) (field-scoped deployments, #261).

---

## 1. What we want

1. Admin mints a URL scoped to **one NIRCam field or one NIRSpec observation**.
2. The URL carries an unguessable token; no login, no account, no email.
3. A visitor with the URL sees the normal CAMPFIRE pages for that scope —
   tables, object pages, spectra plots, the map, FITS downloads — and nothing else.
4. Portal chrome (nav, sign-out, admin links, cross-scope links) is suppressed so
   the visitor is not staring at buttons that render empty for them.
5. Admin can revoke a link, and see which links exist.
6. **Stretch:** a scope can be shared *without being published* — a one-off
   reduction for one colleague that never pollutes the catalog, the map, the
   counts, or anyone else's search results.

Non-goals: public write access (comments, inspection, lists), API-key/CLI access
for link holders, search-engine-indexable pages, per-object sharing.

### 1.1 A link is scoped to a scope, not to a deployment

This distinction drives the schema. A field or observation is deployed **many
times**, and any single deployment may be *narrower* than the scope — a
`source_ids_filter` subset, a few filters, a re-reduction of one grating.

So a share link references `observations(name)` / `fields(name)`, never
`deployments(id)`, and it shows **the scope's current state**, not a snapshot at
mint time. Redeploying the scope updates what the link shows, with no admin
action. Publishing or revoking an individual deployment likewise flows through.

That is almost certainly the behaviour you want ("I re-reduced it, hit refresh"),
but it has a consequence worth stating: a link can come to show data the admin
never looked at when they minted it. Revocation and expiry are the controls for
that, which is why both are in the schema from the start.

---

## 2. The one fact that determines the whole design

Every access decision in CAMPFIRE bottoms out in Postgres RLS keyed on
`auth.uid()`, through a small set of helpers in `supabase/schemas/functions.sql`:
`is_admin()`, `accessible_program_slugs()`, `can_comment()`, `can_inspect()`.

There is **no Next.js middleware** (`web/middleware.ts` does not exist) and no
route-level auth guard. Pages render for anyone; they come back empty because
every RLS `SELECT` policy is `TO authenticated` and `anon` matches nothing. The
`/api/v1/*` routes are the one exception: they use `validateAuth()` +
`getAccessiblePrograms(userId)` + a **service-role** client, re-deriving the same
scope explicitly in TypeScript (`web/lib/api-helpers.ts`).

The consequence: **anything that authenticates as a real Supabase user gets the
entire existing data plane for free** — server actions, the filter/search RPCs,
presigned FITS URLs, the download Worker, `/api/v1/cutout`, map tiles. Anything
that does *not* needs a parallel data plane built by hand.

That asymmetry is worth roughly an order of magnitude in implementation cost and
drives the recommendation below.

---

## 3. Options considered

### A. Anonymous RLS + token claim

Add `TO anon` policies that read a share token out of a custom JWT claim or a
`set_config` GUC. Rejected: every gated `SELECT` policy needs an `anon` twin,
doubling the policy surface — the exact surface where a mistake is a data leak.
You also still have to mint a signed JWT, so you have not avoided the
session-minting work, only the user row.

### B. Service-role public API + hand-written `/public/[token]` pages

Bypass RLS entirely, re-derive scope in TypeScript per route (the `/api/v1`
pattern extended to pages). Rejected: this is a second, parallel data plane. The
NIRCam field page alone pulls `getNircamImages`, `getNircamExpmaps`,
`getNircamFieldSummary`, `getNircamFieldImages`, `getFitsglDatasets`; the object
page pulls `getObjectById` plus photometry, spectra, comments, lists. Every one
needs a service-role twin with hand-rolled authz — the highest-leak, most-code
option, and it has to be built twice over because NIRSpec and NIRCam share almost
no readers.

### C. Link accounts — a synthetic authenticated principal per share link ✅

A share link is backed by a real `auth.users` row that cannot be logged into
normally. Visiting the link trades the token for a normal Supabase cookie session.
From there **every existing code path works unchanged**, because the visitor
genuinely is an authenticated user — just one whose scope is exactly the shared
field or observation.

All the work moves into *narrowing* what that principal sees: a finite, auditable
list of policy edits (§5) rather than a parallel implementation.

### D. Static snapshot export

`campfire export --obs X` → a self-contained static site or tarball. Cheapest by
far and genuinely useful for "here are my reductions" emails, but a different
product: no map, no live plots, no re-deploy freshness. Worth doing someday; does
not answer this request.

**Recommendation: C.**

### 3.1 Correction to rev. 1: there is no useful program-scoped MVP

Rev. 1 proposed shipping a first phase that scoped links by *program*, deferring
the sub-program (observation/field) axis. That was wrong, for the reason you gave:

**NIRCam mosaics are not program-scoped at all.** `nircam_images` is gated purely
on `deploy_status`, and `storage_objects` has an explicit branch for it —
"a field spans multiple programs, so there is no per-program scope — a published
field deployment is public to everyone". So a program-scoped link cannot express
"one field" *even in principle*; the entire NIRCam half of the request falls out.
And on the NIRSpec side it only works when the observation happens to be the sole
occupant of its program, which is the easy case, not the motivating one.

So the program axis is not a smaller version of this feature — it is a different
feature that happens to be cheaper. The build below is single-phase: the
observation/field axis is the feature, and draft support rides the same policies,
so it lands at the same time rather than as a follow-on.

---

## 4. Architecture

### 4.1 Data model

```sql
CREATE TABLE public.share_links (
  token          text PRIMARY KEY,          -- 32 url-safe random chars (~190 bits)
  label          text NOT NULL,             -- admin-facing, e.g. "Naidu — cosmos-web"
  observation    text REFERENCES observations(name),
  field          text REFERENCES fields(name),
  link_user_id   uuid NOT NULL REFERENCES auth.users(id),
  include_drafts boolean NOT NULL DEFAULT false,   -- §6
  allow_download boolean NOT NULL DEFAULT true,    -- §11
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
scoped along the same axis a deployment is, without being tied to one (§1.1).

`share_links` is admin-only under RLS. The token is the primary key and is never
exposed to a non-admin except by whoever holds the URL.

### 4.2 The link account

At mint time, a service-role call creates:

- an `auth.users` row via `auth.admin.createUser` with a synthetic, non-routable
  email (`link+<token>@shared.invalid`), `email_confirm: true`, and a random
  32-byte password stored server-side only and never shown to anyone;
- a `user_profiles` row with `is_admin=false`, `can_comment=false`,
  `can_inspect=false`, and a **new** `is_link_account=true` flag.

No `user_program_access` row is created. The link's entire scope lives in exactly
one place — its `share_links` row — and `accessible_program_slugs()` derives the
program from it (§5.1). One source of truth, nothing to drift.

`is_link_account` is the discriminator every narrowing rule keys on. It sits on
`user_profiles` next to `is_group_account`, the existing precedent for "a
principal with reduced affordances".

### 4.3 Session minting

`GET /s/<token>` (a Next.js route handler):

1. Look up the token with the service client. Reject if missing, revoked, expired.
2. `signInWithPassword` with the stored link credentials against the **cookie**
   client (`@supabase/ssr`), setting the session cookie exactly as a normal login.
3. Bump `view_count` / `last_seen_at`.
4. `redirect()` to the scope's canonical page — `/nircam/<field>` or
   `/nirspec?observations=<obs>`.

No new auth primitive, no custom JWT signing, no session format to keep in sync
with Supabase. It reuses the cookie plumbing `web/lib/supabase/server.ts` already
implements.

The token is a **path segment that immediately redirects**, not a query parameter,
so it does not persist in the address bar of the pages the visitor then browses,
and does not ride along in `Referer` headers to third parties. Rate-limit
sign-in per token at the route; with ~190 bits guessing is not a real threat, but
the counter is the abuse signal an admin wants.

---

## 5. Narrowing — the actual work

This is where the honest effort lives, and rev. 1 understated it (§3.1). I
audited all 47 `SELECT` policies in `supabase/schemas/policies.sql`.

### 5.1 One helper, evaluated once per query

```sql
-- TRUE for every ordinary user. For link accounts, TRUE only when the row is
-- inside the link's scope. Callers pass NULL for the axis their table lacks.
public.is_link_account()      RETURNS boolean          -- STABLE, SECURITY DEFINER
public.link_observation()     RETURNS text             -- NULL unless obs-scoped link
public.link_field()           RETURNS text             -- NULL unless field-scoped link
public.link_sees_drafts()     RETURNS boolean          -- §6
```

The predicate ANDed into policies is written in the schema's existing idiom —
argument-free helpers wrapped in `(SELECT ...)` so Postgres evaluates them **once
per query** rather than once per row:

```sql
AND ((SELECT NOT public.is_link_account())
     OR t.observation = (SELECT public.link_observation()))
```

This matters: `targets` and `spectra` back the big paginated table queries, and a
row-varying function call in their policy would be a per-row penalty on the
hottest path in the portal. The `(SELECT public.is_admin())` wrappers already
littering `policies.sql` exist for exactly this reason — follow them.

`accessible_program_slugs()` also changes, in two ways: a link account gets **only**
the program of its scoped observation (derived from `share_links`, no
`is_public` union, no admin union), and a field-scoped link gets `'{}'`. That one
edit is what keeps a NIRSpec link out of every public program in the archive.

### 5.2 Observation axis — 11 policies

`targets`, `spectra`, `objects` (`observations[]`, array-overlap form),
`observations`, `object_photometry`, `comments`, `flag_audit_log`,
`object_list_members`, `storage_objects` (spectrum→target and deployment
branches), **`shutters`**, **`slit_regions`**.

The last two are the ones rev. 1 missed: their policies are
`is_admin() OR NOT EXISTS (unpublished object)` — open to any authenticated
principal, no program gate at all.

Most of these are one added conjunct inside an existing inline subquery
(`object_photometry`, `comments`, `flag_audit_log`, `object_list_members` all
already re-derive the program check inline rather than relying on inheritance —
follow that convention rather than assuming transitive RLS).

### 5.3 Field axis — 5 policies

`nircam_images` (`field`), `fields` (`name`), `map_layers` (`field`),
`fitsgl_datasets` (`field`), `storage_objects` (the `d.field IS NOT NULL` branch).

Simpler than the observation axis precisely *because* NIRCam has no program
gating to interact with — the narrowing is a plain field-name equality.

### 5.4 `USING (true)` carve-outs — 4 policies

These leak to any authenticated principal and each needs an
`AND (SELECT NOT public.is_link_account())` or a narrowed predicate:

| Table | Policy | What a link account would otherwise see |
|---|---|---|
| `user_profiles` | `authenticated_select_profiles` | every CAMPFIRE user's name + username |
| `deployments` | `authenticated_select_deployments` | full deploy provenance for every scope |
| `object_lists` | `select_lists` | every `public_read` / `public_edit` list |
| `programs` | `accessible_programs_select` | metadata for every public program |
| `flag_definitions` | `authenticated_select_flags` | harmless — leave as is |

`user_profiles` is the sharpest edge and must land in the same commit that creates
link accounts. `deployments` needs a narrowed (not blanket-denied) predicate if
the provenance block of §11.4 is ever built — a link holder should see the
provenance *of their own scope*.

**~20 policy edits total**, each one added conjunct against a single helper. Not
six, as rev. 1 claimed.

### 5.5 Writes, and the test that gates all of this

Link accounts get `can_comment=false` / `can_inspect=false`, so every write policy
already refuses them; no write-side edits are needed.

A `supabase/tests/` case that opens a session as a link account and asserts **zero
rows** from each table in §5.2–5.4, plus zero rows for an out-of-scope observation
and an out-of-scope field, is the deliverable that makes this safe — not a
nice-to-have. A missed policy here is a silent successful read, not an error, and
20 hand-edited conjuncts is exactly the kind of change where one gets forgotten.
Parameterize it over both a field-scoped and an observation-scoped link.

---

## 6. Sharing without publishing

This falls out of the same edits, which is the main reason §3.1 folds it into the
one build rather than deferring it.

The `draft → published → revoked` lifecycle from #217/#218 already exists end to end:

- `campfire deploy --in-prep` writes `deployments.status='draft'` and
  `spectra.deploy_status='draft'` (`python/campfire/deploy/deploy.py`)
- draft rows are invisible to every non-admin — `select_spectra_by_access` gates on
  `deploy_status='published' OR is_admin()`, and `has_published_spectrum` on
  `targets`/`objects` keeps drafts out of the map, the SED endpoints, and every
  target-derived reader
- `nircam_images.deploy_status` does the same for mosaics
- `storage_objects` gates FITS downloads on the same flag
- `/admin/deployments` already renders the lifecycle with publish/revoke controls

So "reduce it, share it, don't pollute the catalog" becomes:

```
campfire deploy --obs one_off_thing --in-prep      # draft: nobody sees it
# admin panel → mint share link for that observation, "include drafts" checked
```

The change is relaxing the draft gate from `is_admin()` to
`(SELECT public.is_admin()) OR (SELECT public.link_sees_drafts())` — and because
`link_sees_drafts()` is only true inside the link's scope by construction, it
touches exactly the policies §5.2/§5.3 already touch.

Because a link tracks the scope and not a deployment (§1.1), `include_drafts`
means "every draft row currently in this scope", which may span several
deployments — including a re-reduction staged after the link was minted. That is
the intended semantic, and the reason it is a per-link opt-in rather than the
default.

Publishing later is a no-op for the link: the colleague's URL keeps working, the
data simply also becomes visible to everyone else.

**Caveat worth writing into the policy comments:** today `draft` means exactly
"admins only", an invariant that is easy to state and easy to audit. Afterwards it
means "admins, plus link accounts scoped to that row". Still narrow, but it is the
first exception to an absolute rule, and the next person reading those policies
deserves to be told.

---

## 7. UI

### Chrome suppression

`web/app/layout.tsx` renders `<Navigation />` and `<ConditionalFooter />`
unconditionally. `ConditionalFooter` already demonstrates the pattern (it hides on
`/map` via `usePathname`). Mirror it:

- `AuthContext` exposes `isLinkAccount`, read off the profile it already fetches
- `Navigation` renders a stripped bar when set — wordmark, scope name, theme
  toggle, and a short "You're viewing a shared view of *cosmos-web*" note with a
  "request an account" link. No nav links, no profile menu, no admin entry.
- Read the profile in the root layout's server component too, or accept a flash of
  the full nav on first paint.

Deep links out of scope (a *Show on map* button, a program breadcrumb) will render
empty rather than 403. The stripped nav removes most of them; the rest are worth a
pass once the feature is real, guided by what link holders actually click.

### Admin panel

Because a link is scoped to a field/observation and not to a deployment (§1.1),
the mint UI is **not** a per-row action on `/admin/deployments` — that would imply
a snapshot it does not take. Instead:

- `/admin/share-links` is the home: an `AdminTable` of active links (scope, label,
  creator, drafts?, downloads?, view count, last seen, expiry) with mint and
  revoke. Mint is a modal with an observation/field picker, mirroring the scope
  pickers the deploy admin pages already use.
- A secondary "Share" affordance can sit on the NIRCam field page and the NIRSpec
  observation view, prefilling the scope — convenience, same underlying action.

Revoke sets `revoked_at` **and** deletes the link's `auth.users` row, so any live
cookie session dies at its next token refresh rather than lingering for an hour.

---

## 8. What still works, unchanged

The payoff for choosing option C:

- **FITS downloads.** `lib/actions/download.ts` presigns server-side after the RLS
  read, then hands the client a Worker proxy URL. A link account's presign
  requests only ever cover rows it can see.
- **`/api/v1/storage/presign`.** Runs `filter_accessible_storage_keys` with the
  caller's program list — link accounts get their narrowed list from §5.1.
- **Cutouts, SED plots, spectra plots, the map, FitsGL** — all read through the
  same cookie client.
- **Map tiles** are already served from a public CDN base URL
  (`map_layers.tile_base_url`) with no auth; narrowing `map_layers` controls which
  layers are *discoverable*, which is the same protection existing users have.

---

## 9. Indexability (open question 4, answered)

"Indexability" = whether a shared view can end up in Google. Two things make this
non-theoretical even though the token is unguessable:

1. A link holder pastes the URL into a public issue, a shared doc, a mailing-list
   archive, or a Slack workspace with a crawler-visible export. The token stops
   being secret the moment it is published anywhere a crawler reaches, and search
   engines follow links they find.
2. Next.js `generateMetadata` already emits OpenGraph tags with real object
   metadata and an `/api/og-image/...` thumbnail (see
   `app/nirspec/objects/[id]/page.tsx`). Anything that unfurls the link — Slack,
   iMessage, Twitter — fetches those. That is *desirable* for sharing, but it means
   scope metadata leaves the perimeter as soon as the URL is pasted anywhere.

Mitigation, cheap and worth doing in the same build: emit `X-Robots-Tag: noindex,
nofollow` on `/s/*` and on any page rendered for a link account, and set
`robots: { index: false, follow: false }` in the metadata for those routes. This
does not stop a determined human with the URL — nothing does, that is the model —
but it keeps an accidental paste from becoming a permanent public index entry.

The reason this matters beyond hygiene: if a shared view should ever be *citable*
in a paper, the requirements invert — you want a stable, indexable,
non-expiring URL, which is a different feature (a DOI-ish published data view)
with a different security posture. Worth knowing which one you are building.
This design assumes not-citable.

---

## 10. Risks

- **A link account is a real user.** The blast radius of a missed policy is a real
  read, not an error. §5.5's test is the mitigation, and it is not optional.
- **~20 hand-edited policy conjuncts** is the kind of change where one gets
  forgotten, and the forgotten one is invisible until someone notices data they
  should not have. Prefer a single helper over bespoke predicates so the audit is
  a grep.
- **Security through obscurity is the actual security model.** That is a
  deliberate and appropriate choice (it is how every share-by-link product works),
  but it means expiry and revocation must be easy, and §9 applies.
- **Draft sharing widens a currently absolute invariant** (§6, caveat).
- **A link tracks the scope, not a snapshot** (§1.1) — a redeploy changes what the
  colleague sees without the admin doing anything.
- **One principal per link.** `download_log` and any future analytics attribute all
  activity to the link, not a person. Fine for the use case; worth not pretending
  otherwise.
- **Per-row policy cost** on `targets`/`spectra` if the helpers are written with
  row-varying arguments instead of the `(SELECT ...)` idiom (§5.1).

---

## 11. Build order

Single build — the ordering below is dependency, not shipping gates.

1. **Schema + link accounts.** `share_links`, `is_link_account`, the four helpers,
   mint/revoke service-role functions.
2. **Narrowing + the leak test** (§5). Land `user_profiles` in the same commit as
   step 1. This is the security core; everything else is UI on top of it.
3. **Draft support** (§6) — same policies as step 2, so it is a conjunct, not a
   second pass.
4. **`/s/<token>` route** + `noindex` headers (§9).
5. **Stripped chrome** (§7).
6. **Admin panel** — `/admin/share-links` plus the prefilled affordances.

Deferred, by your call: the **scope metadata / provenance block** (program, PI,
`cfpipe_version`, CRDS context, coverage). NIRCam field pages already have one and
the NIRSpec observation view is the model for the other half — building a single
component that serves both the regular portal and shared views is the right shape,
and it is a better standalone piece of work than a bolt-on here. Note that it
needs the narrowed-not-denied `deployments` predicate from §5.4.

---

## 12. Remaining open questions

1. **Expiry default.** Is a link forever-by-default with optional expiry, or
   30-days-by-default with optional forever? Given §1.1 (links track live scope
   state) and §9, I would default to an expiry and let it be cleared explicitly.
2. **Does revocation need to be per-scope as well as per-link?** "Un-share this
   observation from everyone" is a plausible panic button and is trivial now
   (`UPDATE share_links SET revoked_at = now() WHERE observation = ...`), awkward
   to retrofit into the UI later.
3. **What does a link holder see if the scope is later fully revoked?** Currently
   an empty page. An explicit "this data has been withdrawn" state would be kinder
   and is a small addition to the stripped-chrome work.

**Settled:** scope granularity is one field / one observation, tracking the scope
rather than a deployment (§1.1). Link holders can download FITS (`allow_download`
in the schema, defaulting true, so per-link opt-out exists without being in the
way). Indexability answered in §9. Metadata block deferred (§11).
