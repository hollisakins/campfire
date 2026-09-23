# Design (brainstorm): data releases, snapshots & provenance

**Status:** brainstorm for discussion — nothing here is decided. 2026-09-23.
**Driver:** CAMPFIRE serves one mutable set of products: whatever was reduced last. That
was the right call for an internal platform in a fast-iteration phase, and it still is for
the team's day-to-day work. But the products are now used in papers and will eventually be
public, and "we used the CAMPFIRE archive" does not name anything: the bytes and catalog
values behind it change with every deploy, and the old ones are not kept.
**Related:** [intermediate products](design-intermediate-products.md) (lifecycle,
`storage_objects`), [NIRCam deploy overhaul](design-nircam-deploy-overhaul.md) (D1/D3:
latest-only retention, no mosaic version axis; D14 put "DR/version retention" explicitly
out of scope), [public mirror](design-public-mirror.md) (share links, sharing drafts),
[objects migration](design-objects-migration.md) (persistent objects, staleness).

---

## TL;DR

- Keep **live** exactly as it is: continuously deployed, the default for the team,
  clearly labelled as something that changes.
- Add one primitive, an immutable **manifest**: a named list of
  `(storage key, content_hash)` plus a frozen export of the catalog rows and their
  provenance. The three things asked for are all uses of it:
  - a **release** is a manifest of everything, cut periodically against release
    criteria (DR1, DR2, …);
  - a **snapshot** is a manifest of a selection, cut by any user, for one paper
    ("the 1,204 spectra in Smith+27");
  - a **bundle** is a manifest of products that are not in the live catalog at all
    (a one-off reduction for a colleague).
- Keep old bytes **only when a manifest pins them**: before deploy overwrites a key whose
  current hash is pinned, it copies the old object aside (copy-on-write). Storage then
  grows with churn in pinned products, not with the number of releases.
- Version what you actually re-reduce: **observation versions** (NIRSpec) and **field
  versions** (NIRCam). A release is a lockfile over unit versions plus a snapshot of the
  catalog layer (objects, inspected redshifts, line fits, photometry). A release then
  doesn't need one monolithic re-reduction; it needs every unit to meet the release
  criteria, and tooling can show which units are behind.
- Access stays dynamic: a manifest records everything, and what you see of it is
  *manifest ∩ your access*. A **public DR** is the public subset frozen at release time;
  that is the part that gets a DOI.
- "Needs review" should fire on a change to *what was reviewed* (redshift, membership,
  a new grating), not on a changed file hash.

---

## 1. Where we are today (the starting line)

Every product has exactly one current version, and nothing older is kept.

- **Storage keys carry no version.** Keys are `data/` + the local relpath
  (`layout/campfire_layout/keys.py`, mirrored in `web/lib/layout.ts`), and pipeline
  filenames are version-free (`{obs}_{grating}_{filter}_{source_id}_spec.fits`;
  NIRCam mosaic names explicitly "must NOT reintroduce" a version). A redeploy PUTs the
  new bytes to the same key ("pushing to the key of an already-published product replaces
  the served bytes immediately", `python/campfire/deploy/push.py`) and upserts the same
  `storage_objects` row. No bucket versioning is configured.
- **Tombstones exist in the schema but are never written.** `storage_objects.status`
  allows `superseded` / `revoked` and `deploy_events` reserves `supersede`, but no code
  path sets them. Retention is latest-only by decision (NIRCam overhaul D1, D3).
- **Catalog rows are one-per-slot.** `spectra` upserts on `(target_id, grating)`;
  `spectrum_id` derives from the FITS filename, so it is stable while the filename is. The
  code says it outright: "a single spectra row per (target, grating) can't hold a live +
  draft version at once" (`deploy.py`). `spectra` has no `deployment_id`. A redeploy does
  not delete spectra that the new reduction no longer produces; they linger.
- **Deploy history is kept, but only as a ledger.** Every deploy run inserts a
  `deployments` row (observation XOR field) that is never deleted, carrying
  `cfpipe_version`, `jwst_version`, `crds_context`, `config_snapshot` (NIRSpec; the NIRCam
  path leaves them NULL, admin audit B2). `observations.latest_deployment_id` /
  `fields.latest_deployment_id` point at the newest. `deploy_events` is append-only. This
  is the closest thing to "unit versions" we already have.
- **Per-product provenance is good for NIRSpec.** `spectra` carries `cfpipe_version`,
  `crds_context`, `jwst_version`, `reduced_at`, `date_obs`; deploy warns on non-release
  versions and on mixed CRDS contexts. Line fits carry `fit_version`, `z_used`,
  `object_version` and `spectrum_hash` and already report `stale_redshift` /
  `stale_spectrum`. That line-fit provenance is a small-scale version of what §8 proposes
  for reviews.
- **Objects:** the integer `objects.id` is stable across reconciles (splits and merges
  reuse it, orphans are soft-deleted). The IAU-style `object_id` string is regenerated
  from the centroid, so it can change (#150).
- **NIRCam:** mosaics have no version axis; a re-combine overwrites in place, and even a
  `--draft` redeploy overwrites the published bytes. The "imaging version" in the cutout
  store is a derived cache-busting hash (`web/lib/asset-version.ts`), not a data version.
- **Photometry:** a new `catalog_name` lands next to the old one; `--supersede`
  hard-deletes the others.
- **Sharing:** share links deliberately show a scope's *current* state, not a snapshot
  (design-public-mirror §1.1); a static "snapshot export" was considered there and
  deferred.
- **"Data release" exists as a label only:** `DATA_RELEASE = ''` in
  `web/lib/updates/versions.ts` ("set this when you cut a data release"), plus a `release`
  category on the Updates feed (#180, #348). There is no DOI, citation or snapshot code
  anywhere.

---

## 2. What "citable" has to mean

"I can cite it" bundles several requirements with very different costs:

| # | Requirement | What it needs | Cost |
|---|---|---|---|
| R1 | Name the data in one short string | an identifier: `CAMPFIRE DR1`, `campfire:snap/2027-smith-lae` | trivial |
| R2 | Get back the catalog values a paper used (z, quality, flags, line fluxes) | a frozen export of catalog rows | small (under 100k spectra rows is MBs) |
| R3 | Get back the bytes a paper used | old FITS retained | the expensive one |
| R4 | Say what changed between two states | diffable manifests + a deploy ledger with no gaps | moderate |
| R5 | Keep a bleeding edge | live stays | none |
| R6 | Users with different access | manifest ∩ access | moderate |
| R7 | Bounded cost | retention driven by pins, not by copies | design choice |

R2 carries most of the value: most papers quote catalog values and a table of IDs. R3
matters for anyone who refits spectra themselves. Re-running the pipeline is a weak
substitute for R3. A pipeline tag pins the CRDS context and the `jwst` minor version, but
not the dependency patch versions, the raw inputs (MAST reprocesses them), the per-obs
config overrides (mutable in the config plane), or the hand-made reference state (masks,
stuck shutters, background overrides). "Re-reduce at tag X" gets close to what someone
used, not exactly to it. Keeping the bytes is simpler than proving reproducibility.

---

## 3. The primitive: an immutable manifest

A manifest is:

- **identity:** id, kind (`release` | `snapshot` | `bundle`), name, created_at,
  created_by, description, parent (the manifest it is diffed against);
- **unit versions** (releases): `{obs_name → obs version, field → field version}`;
- **product list:** rows of `(storage_key, content_hash, product_type, size, spectrum_id |
  field/filter/tile, program)`, stored as Parquet;
- **catalog export:** the rows of `spectra`, `objects`, line fits, photometry matches, zfit
  scalars as they were at freeze, as Parquet and FITS (astronomers will want FITS);
- **provenance:** per unit, the pipeline version, CRDS context, `jwst` version and the
  config that produced it. `deployments.config_snapshot` already records the config per
  deploy, and the config plane hashes every observation/field definition, so this is
  mostly a matter of copying what exists;
- **access scope:** the program of every row, so *manifest ∩ access* is computable;
- **manifest hash:** a hash over all of the above, for anyone who wants to quote an exact
  state.

Storage: one row in a `manifests` table plus files under `manifests/<id>/` in the bucket.
Immutable once frozen. Diffs between manifests (new spectra, re-reduced units, changed
redshifts) become release notes almost for free.

The git analogy is close enough to be useful:

| git | CAMPFIRE |
|---|---|
| working tree | local `$CAMPFIRE_ROOT/products` |
| `main` HEAD | live |
| annotated tag | release |
| lightweight tag | snapshot |
| side branch never merged | bundle / variant reduction |
| object store keyed by hash | pinned blobs |
| `git log` / `git diff` | deployment ledger / manifest diff |

The repo already does this at small scale. ETC models are versioned data
(`etc/campfire_etc/models/nirspec-2026.09.json` plus a `manifest.json` naming `latest`);
a published model file is never edited, and every tool result carries `model_version`.
This proposal applies the same rule to the archive.

---

## 4. What gets a version: reduction units and the catalog layer

Two layers change at different rates, for different reasons.

1. **Reduction units** are what a reducer re-runs.
   - NIRSpec: an observation. Its version increments on each deploy that changes any
     final product's content hash.
   - NIRCam: a field. Its version increments when any mosaic's hash changes. Filter-level
     re-combines happen, but a per-field version with per-filter hashes in the manifest
     is enough; nobody cites "COSMOS F444W v3" separately from the field.
2. **The catalog layer** is what people and cross-unit steps produce: objects (FoF across
   observations), inspected redshifts and quality, flags, line fits, photometry
   cross-matches, zfit scalars. It changes continuously and is not tied to any one
   reduction. It is versioned only when a manifest snapshots it.

A release is then a lockfile, like a pinned conda environment: unit versions plus a
catalog-layer snapshot taken at freeze.

**The full re-reduction problem.** A DR does not have to be one re-reduction run. It needs
a **homogeneity policy** and a way to see who is behind:

- *strict*: every unit at the same pipeline MINOR. Under the bump policy that means
  calibration-equivalent, since the canonical CRDS context is pinned per pipeline release
  in `config_default.toml`;
- *mixed*: each unit carries its own versions, and the release tables say so per row.

Strict for NIRSpec releases seems right: #202 already flagged mixed CRDS contexts inside
one sample as a problem. NIRCam fields are independent products, so per-field versions
are natural. A `campfire release status <name>` command would list every unit with its
deployed version, pipeline/CRDS version, whether it meets the criteria, and its review
coverage. The re-reduction campaign becomes a checklist worked through over weeks, while
live keeps updating as each unit lands. The pipeline release drives the data release:
"DR1 = everything at 0.7.x".

---

## 5. Keeping old bytes without keeping copies

Today keys are path-derived and overwritten in place, `storage_objects` records a content
hash per key, and retention is latest-only. The options for keeping pinned bytes:

| Option | How | Storage cost | Change |
|---|---|---|---|
| A. Copy on release | server-side copy of every released object to `releases/<id>/…` at freeze | one full copy of the finals per release | small: a new prefix |
| B. Content-addressed blobs | physical key = hash; the registry maps logical key → hash; GC drops unreferenced blobs | churn only | large: key scheme, `campfire-layout` bijection, every reader |
| C. Pin-aware copy-on-write | keys unchanged; before push overwrites a key whose current hash is pinned by any manifest, server-side copy the old object to `pinned/<sha256>` and register it | churn in pinned products only | moderate: one check in the push engine, GC honours pins |
| D. Bucket versioning | let the object store keep every old version | all churn, pinned or not | unknown whether OSN offers it; would need asking |

**C looks like the sweet spot.** The push engine already knows when it is about to
overwrite (the content hash differs, so it uploads). The CDN front already treats a product
as immutable per `content_hash` (decision D-D), so a pinned blob can be served through the
same Worker path. And C does not rule out moving to B later. One wrinkle: in the default
`login` deploy mode, admin machines hold no object-store write keys (uploads go through
presigned PUTs), so the copy-aside has to be a server-side `CopyObject` issued by the web
tier, next to the existing presign route.

**What to pin:** finals (`nirspec_spec`, `nircam_mosaic`) and whatever sidecars are needed
to browse a release in the portal (the 1-D JSON at least). Intermediates are not pinned.
Mosaics dominate: a NIRCam field release costs its mosaics once, plus again on every
re-combine after the freeze. OSN egress is free and the allocation is expandable (D2 in
the NIRCam overhaul), so the budget is bytes at rest. Measure the finals' total size
before choosing.

**Catalog layer:** no temporal tables needed. At freeze, export the rows to
`manifests/<id>/catalog/` as Parquet + FITS. At under 100k spectra and tens of thousands
of objects this is MBs.

---

## 6. Access

The problem: DR1 as seen by a program member is not DR1 as seen by the public.

Proposal:

- A manifest records **everything**, regardless of access.
- What anyone sees of a manifest is **its rows ∩ their access, evaluated now**, through
  the same `accessible_program_slugs()` rules the portal already uses.
- A **public DR** is a separate frozen view: manifest ∩ programs public at freeze. That is
  what gets a DOI, a paper and a download page, and it never grows. When a program becomes
  public later, it enters the next release (or a point release), not DR1's public subset
  after the fact.
- Team papers cite `(release, programs)`: "CAMPFIRE release 2026.12, programs 1180, 3215,
  6585". A citation helper in the portal and CLI prints that statement for the current
  selection and can save the selection as a snapshot, so the statement is exact and
  re-derivable by anyone with the same access.

This is essentially the Research Data Alliance recommendation for citing dynamic data:
cite a stored query plus a timestamp plus a hash of the result, on a store that keeps
versions.

Facts that shape this:

- `programs.is_public` is a boolean with no dates, flipped by hand (admin UI or
  `programs.toml`). There is no exclusive-access-period logic, so "public at freeze" means
  "flagged public when the release was cut". Recording a public-from date per program
  would make that exact and would let the flip happen on schedule; it is a small,
  separate feature.
- Access is per program for NIRSpec, through `accessible_program_slugs()`, and is
  independent of the draft/published lifecycle.
- NIRCam is effectively not access-scoped: a published mosaic is visible to every
  signed-in user, and the map tiles sit in a public bucket. For NIRCam, *manifest ∩
  access* is simply the manifest, unless per-program field gating arrives later.

---

## 7. Live

- Live stays the default for the team: the map, filters and inspection all run on it.
- Label it. Every API response and client result carries `release: "live"` and an
  `as_of`; the portal shows a small "Live — changes as data is re-reduced; cite a release
  or snapshot" marker. The Updates page already has a `DATA_RELEASE` slot
  (`web/lib/updates/versions.ts`), empty today.
- Python: `Campfire()` is live; `Campfire(release="dr1")` or
  `Campfire(snapshot="…")` reads the manifest's frozen catalog and downloads pinned bytes.
  Locally, releases need their own root (`$CAMPFIRE_ROOT/releases/<id>/…`) next to
  `products/`: the `campfire-layout` key↔path bijection assumes one version per key, so
  this is a layout contract change, mirrored in `web/lib/layout.ts`.
- Browsing old releases in the portal (catalog tables, object pages) can come later. The
  first cut is a landing page, the catalog download and the file manifest.

---

## 8. "Needs review"

**How it works today.** "Needs review" is not stored as a flag. It is derived:
`objects.staleness_reason IS NOT NULL AND last_data_change_at > last_inspected_at`, in
`p_needs_review` and in `StalenessBadge.tsx`. Reconcile sets `staleness_reason` on deploy:

- `membership_changed` / `new_target` when an object's members change;
- `reprocessed` when **any member spectrum's `file_hash` changed**. That is the whole-file
  sha256, so a re-reduction that only restamps headers (a new `CMPFRVER`, a new reduction
  date) is enough;
- `reprocessed` also when `redshift_auto` moves by ≥ 0.01(1+z) on an object with
  quality ≥ 2 (#460). That one *is* a materiality test.

Any inspection write, or "Mark as reviewed", stamps `last_inspected_at` and clears the
badge; `staleness_reason` itself is never reset. Line fits have the same weakness:
`stale_spectrum` also compares `file_hash`.

**Why this matters for releases.** A release campaign re-reduces everything. Under the
file-hash rule, every inspected object gets a badge at once, and the badge stops meaning
anything.

**Proposal: flag a change to what was reviewed, not a change to the file.**

1. **Hash the science content, not the file.** A NIRSpec `sci_hash` over the
   wave/flux/err/DQ arrays, like NIRCam's `sci_dq_hash`. Header-only changes then stop
   counting, for review staleness and for `stale_spectrum` alike.
2. **Record the review basis at sign-off.** Store the member spectrum ids with their
   `sci_hash`es and the `redshift_auto` the reviewer saw. `pin_redshift_on_signoff`
   already pins the redshift; this extends the same idea to the inputs.
3. **Split the signal into two levels.**
   - **Needs review** (amber badge, in the queue): membership changed; a new grating or
     spectrum arrived; a reviewed spectrum disappeared; or the new `zfit` disagrees with
     the *inspected* redshift by ≥ 0.01(1+z) when the old one agreed.
   - **Reprocessed since review** (grey, informational, not in the queue): science bytes
     changed but none of the above.
4. **Releases record review coverage.** Release criteria can then say "no needs-review
   objects among quality ≥ 3", and the release tables can carry `reviewed_on` (the
   pipeline version the review was made against).

Open detail: whether large S/N changes (e.g. a re-reduction that doubles the S/N of a
quality-2 object) should also count as material. Probably yes for quality ≤ 2 only, since
that is where more data can change the verdict.

---

## 9. One-off distribution: bundles and variants

Two different needs hide under "distribute a custom reduction":

**(a) Files that should never enter the catalog** (a custom mosaic, an alternate reduction
with different settings, a stack): a **bundle**. Upload under `bundles/<id>/`, register
the files, write a manifest with a README and provenance, and share a link to a simple
landing page (file list, provenance, download all). It stays out of the catalog, the map
and the counts. It can be link-only or public, and can get a DOI later if a paper uses it.

**(b) A variant you want to browse in the portal** (plots, object pages): a **variant
scope**. Deploy it under a distinct scope name as a draft and share it with a link; the
public-mirror design (§6 there) already lets a link see drafts in its scope. The catch is
that today scope = observation or field name, so the fiducial and a variant of the same
data cannot coexist. A variant needs its own scope name and must stay out of objects
reconciliation. One caveat: a `--draft` deploy hides the *spectra*, but nothing in the
deploy or reconcile path sets `targets` / `objects.has_published_spectrum`, which default
to TRUE. The objects of a brand-new draft observation may therefore be visible before
publication. That needs fixing before "draft" can be relied on as private.

(a) is simpler and covers "send a colleague the files"; (b) can wait for demand.

---

## 10. Things likely missing from the list

1. **Stable identifiers.** Reconcile can rename an object's IAU `object_id` when its
   centroid moves (#150), and objects split and merge. A release catalog needs IDs stable
   within a release plus a cross-release map (old → new, with split/merge relations).
   The integer `objects.id` is already stable (splits and merges reuse it), so the simplest
   rule is a designation derived from it, or an IAU-style name assigned once at creation
   and never regenerated, with coordinates kept as ordinary columns.
2. **Data model version.** Flag bitmasks (`web/lib/flags.ts`), the quality enum, column
   meanings and units all evolve. A release must ship its data model as of freeze (column
   descriptions, flag definitions), versioned separately from the pipeline.
3. **Errata and point releases.** Never mutate a release. A bug means an errata page and a
   DR1.1. Withdrawal (a PI asks, data turn out to be wrong) is a tombstone in the next point
   release, plus a policy for when pinned bytes really must be deleted.
4. **Human-generated content.** Inspected redshifts are team labour. Decide which are
   released (quality ≥ 3?), how inspectors are credited, and that comments are never
   released.
5. **External inputs.** Photometry catalogs (UNICORN releases etc.), the CRDS context and
   reference files (masks, astrometric catalogs, flats) belong in provenance. Reference
   inputs are already registered (D9), so they can be pinned too.
6. **The deployment ledger has gaps.** The admin audit (2026-07-03, B2/B3) found NIRCam
   deployment rows with no provenance and several paths that mutate storage or catalog with
   no record. Unit versions and release notes depend on a gap-free ledger, so this comes
   first.
7. **A unit's product set has to be well defined.** A redeploy never deletes spectra (or
   their files in storage) that the new reduction no longer produces; they linger in live.
   A release frozen today would include those leftovers. Deploy should retire what the new
   reduction did not produce, which is what the unused `storage_objects` `superseded` /
   `revoked` states were reserved for.
8. **Long-term hosting.** CAMPFIRE's hosting depends on grants and the OSN allocation.
   Public releases should be exportable to an archive that outlives the project: MAST's
   High-Level Science Products (the usual home for JWST community products; they issue
   DOIs), and possibly Zenodo for the catalogs (check its per-record size limits). A
   manifest makes the export mechanical.
9. **License and acknowledgement text.** CC-BY 4.0 is typical, plus a standard "cite the
   CAMPFIRE paper and the JWST programs' papers and MAST DOIs" statement.
10. **API stability.** A release is a promise that reads keep working. Old-release endpoints
    need to survive API changes (versioned release read endpoints, or static files only).
11. **Release governance.** A release candidate (DR1-rc) with a validation window in which
    the team checks the frozen catalog, someone who signs off, and an announcement on the
    Updates page.
12. **Cutouts and tiles.** The map tiles and the cutout store are live-only. Paper figures
    made from a release mosaic would need `/api/v1/cutout?release=…`; later.
13. **Sync.** Releases are immutable, so a release download is a one-shot fetch of static
    files, with no incremental sync or tombstones needed.

---

## 11. Prior art (from memory — check details before quoting any of it)

- **SDSS**: numbered, cumulative DRs; every past DR stays online; each has a release
  paper.
- **DESI**: named internal spectroscopic productions (e.g. `fuji` behind the EDR, `iron`
  behind DR1) plus a continuously updated internal `daily` production. The closest
  analogue to "live + periodic releases".
- **Gaia**: each DR is a full reprocessing; source IDs can change between releases, and
  cross-release neighbourhood tables are published.
- **JWST surveys**: JADES releases per field and tier; the DAWN JWST Archive publishes
  versioned reductions of public data.
- **MAST HLSPs**: a DOI per HLSP.
- **RDA dynamic data citation**: cite a timestamped, stored query plus a hash of its
  result, on a versioned store.

---

## 12. A possible path (each step useful on its own)

- **P0, hygiene (cheap, now).** Gap-free deployment ledger (B2/B3); provenance on NIRCam
  deployments; redeploys retire products the new reduction didn't produce; the
  needs-review rework (§8); a never-renamed object designation; label live as live. None of this is "releases" yet, but everything later depends on it.
- **P1, snapshots.** A `manifests` table; `campfire snapshot create` from a query or list;
  catalog export to Parquet/FITS; pin-aware copy-on-write in the push engine; GC that
  honours pins; `Campfire(snapshot=…)`. This answers "how do I cite CAMPFIRE in my paper"
  before any DR exists.
- **P2, internal releases.** Unit versions; release criteria and `campfire release status`;
  `release freeze`; release notes from manifest diffs; a landing page. Cut the first
  internal release once a pipeline MINOR has propagated to every unit.
- **P3, public DR.** The public subset; the data-model document; license; DOI; MAST HLSP
  export; release paper.
- **P4, as demand shows.** Browsing releases in the portal; bundles; variant scopes.

---

## 13. Open questions

1. **Values or bytes?** When a team paper says "we used CAMPFIRE", does it need the
   catalog values back (R2, cheap) or the exact FITS (R3)? If R2 covers most papers, P1
   can ship without byte pinning and add it later.
2. **What makes a full NIRSpec re-reduction hard today:** wall-clock, manual per-obs
   overrides, review churn, or the deploy itself? That decides whether a strict-homogeneity
   release is realistic or whether releases should be mixed with per-row provenance.
3. **Cadence and trigger.** Tie internal releases to pipeline MINORs ("DR1 = everything at
   0.7.x"), to the calendar, or to papers?
4. **Naming.** One scheme or two? For example calendar tags for internal releases
   (`2026.12`, as the ETC models do) and `DR1`, `DR2` for public ones, with each DR
   declared as the public subset of a specific internal release (the DESI pattern).
5. **Portal browsing of releases.** Is a landing page plus catalog and file downloads
   enough, or do people need to open a DR1 object page as it was?
6. **What goes into a public DR:** which review quality threshold, how inspectors are
   credited, whether line fits and photometry matches are included.
7. **Long-term home.** Is a MAST HLSP the intended archival venue for public releases?
8. **Bundles:** is "files + landing page + link" enough for one-off reductions, or do
   colleagues need to browse a variant in the portal?
