# Epic: NIRCam deploy overhaul — mosaics & intermediates on OSN, FITS-native inspection UI, headless reduction loop

**Status:** draft for review
**Context:** follow-on to epic #210 (Intermediate products & cloud-as-source-of-truth, R2 → OSN).
Related: [intermediate-products design](design-intermediate-products.md),
[NIRCam exposure-major design](design-nircam-exposure-major.md).
**Driver:** Epic #210 deliberately **omitted NIRCam intermediate products and mosaics** from
the R2 → OSN migration, because the NIRCam deploy path needs an overhaul rather than a
bolt-on. This epic does that overhaul: brings NIRCam to deploy parity with NIRSpec, moves
mosaic distribution off CANDIDE and onto OSN, replaces preview-PNG inspection with in-browser
FITS rendering, and closes a headless `process → deploy → inspect → pull → combine → deploy`
reduction loop.

---

## 1. Where NIRCam sits today (the starting line)

Epic #210 built a full cloud-as-source-of-truth stack, but NIRCam is only half-wired into it.
Concretely, **as of `main`**:

| Product | Deployed today? | Where | Registered? | Lifecycle? |
|---|---|---|---|---|
| Exposure **preview PNG** (`*_preview.png`, thumbnail) | ✅ yes | **R2** (legacy key `nircam/exposures/…`) | ✅ `nircam_exposure_preview` | ❌ none |
| Exposure **full PNG** (`*_full.png`, masking canvas) | ✅ yes | **R2** (legacy key) | ✅ `nircam_exposure_full` | ❌ none |
| Exposure **canonical FITS** (`<rootname>.fits` + `CFP_*` state) | ❌ **no** | local only | (enum exists: `nircam_exposure`) | ❌ |
| Exposure **expmap / variance / DQ** | ❌ no | local only | (enum: `nircam_expmap`) | ❌ |
| **Mosaic** i2d (`mosaic_nircam_<filter>_<field>_<scale>_<version>_<tile>_i2d.fits`) | ⚠️ out-of-band | **CANDIDE** (`exchg.calet.org/hakins/…`) | ❌ | ❌ |
| Mosaic **RGB / thumb** | ⚠️ out-of-band | CANDIDE | ❌ (enum: `nircam_rgb`) | ❌ |
| **Masks** (`.reg` ↔ `nircam_exposures.mask_regions` JSONB) | ✅ round-trip | DB + local `reference/` | ❌ not registered | n/a |
| Map **tiles** (pyramid) | ✅ yes | R2/CDN | (aggregated) | n/a |

Key facts that shape this epic:

- **`nircam_exposures`** (admin triage table) has *no* `deploy_status` column — NIRCam exposures
  are implicitly always-visible-to-admins; there is no `draft` tier for them.
- **`nircam_images`** (the mosaic index) is populated by an **archived, out-of-band script**
  (`scripts/archive/deploy_nircam.py::upsert_nircam_images`), its `file_path` points at CANDIDE,
  and it has **no `deploy_status`, no OSN key, and no `storage_objects` row.** The public NIRCam
  page (`NircamTable.tsx`) and the curl-script generator hardcode
  `https://exchg.calet.org/hakins/data/data/nircam`.
- The inspection UI (`MaskEditor.tsx`) renders the **`*_full.png`** via the admin-only
  `/api/nircam-preview` proxy and draws SVG polygons in **DS9 image pixel space** (1-indexed,
  `origin='lower'`). **No FITS is parsed in the browser anywhere in the codebase**, and there is
  no FITS/WebGL imaging dependency in `web/`.
- Whole-exposure exclusion is a **pipeline config concern** today: `field.py` applies field-wide
  `skip` globs at glob time. The web UI's `review_status='excluded'` is recorded but **not consumed
  by the pipeline** — there is no path from "flag this exposure in the UI" to "combine drops it."
- `apply_masks` (combine phase) rasterizes `.reg` files (pulled from the DB) into the exposure `DQ`;
  outlier + resample honor `DQ`. Masks are per-pixel, in the exposure frame.

## 2. What epic #210 already gives us (build on, don't rebuild)

Everything below is merged and live (only #216, the OSN cutover / streaming-zip proxy, is still open):

- **`storage_objects` registry** — the shadow index. Its `product_type` CHECK **already enumerates
  the NIRCam types we need**: `nircam_exposure`, `nircam_exposure_preview`, `nircam_exposure_full`,
  `nircam_mosaic`, `nircam_rgb`, `nircam_expmap`, `nircam_mask`, `nircam_astrom_cat`,
  `nircam_bad_pixel`, `nircam_flat`, `nircam_wisp`. Partial `UNIQUE (product_type, exposure_ref)
  WHERE status='active'`; typed scope columns (`field`, `exposure_ref`, `deployment_id`).
- **Backend abstraction** (`deploy/backend.py`, `web/lib/r2.ts`) — per-purpose factory routing
  **data → OSN / tiles → R2** by config, no hardcoded endpoints.
- **`campfire-layout`** (`layout/`, mirrored `web/lib/layout.ts`) — the single authority for
  `(instrument, scope, product_type, filename) → (path, storage key)` with a reversible bijection
  and a per-tree lifecycle class. All of `products/` and all of `reference/` are classified
  cloud-backed. **Any new NIRCam key/path must be added here, not hand-built.**
- **Lifecycle** — `deploy_status ∈ {draft, published, revoked}` (note: the shipped enum is
  **`draft`**, not the design doc's `in_prep`) on `spectra` + intermediate tables; `deployments`
  gets `status`/`published_at`/`revoked_at`; **RLS + explicit predicates in every reader** (incl.
  service-role sync RPCs, gated by `p_include_in_prep = is_admin()`); a security test harness proves
  non-admins get zero draft/revoked rows from every RPC and route; `deploy_events` audit log;
  admin publish/revoke UI cloned from the `/admin/nircam` triage pattern.
- **Register protocol** — presigned PUT with `x-amz-checksum-sha256` → authenticated `/register`
  (HEAD/verify) → row-on-match (write-after-verify).
- **OSN copy/verify/dual-read** (#215) — data bucket copied R2 → OSN, hash-verified, dual-read live.
- **`download --intermediate` / `delete-local` / multi-reducer safety** (#220) — the resume set
  *already lists* "NIRCam canonical exposures," and the verified-in-cloud `delete-local` interlock
  exists. But because NIRCam canonical exposures were never deployed, **this path is inert/untested
  for NIRCam.**

**Net:** the schema, keys, lifecycle, registry, and download machinery are in place. This epic is
mostly *wiring NIRCam into them* + one genuinely new capability (FITS-in-browser) + one migration
(CANDIDE → OSN).

---

## 3. Target end-state

1. **Deploy parity with NIRSpec.** `campfire deploy` uploads NIRCam **canonical exposures**
   (+ expmaps) and **mosaics** to OSN under canonical `campfire-layout` keys, registered in
   `storage_objects`, with the `draft → published → revoked` lifecycle — the same flow NIRSpec
   `deploy_observation` uses for spectra + spectrum-exposures.
2. **Mosaics distributed via OSN, not CANDIDE.** The public NIRCam page serves final mosaics from
   OSN (presigned / streaming-zip), gated by `deploy_status='published'`. Existing CANDIDE mosaics
   are migrated + registry-backfilled; CANDIDE is retired after a bake window.
3. **FITS-native inspection.** The admin exposure-inspection/masking UI renders the **canonical
   exposure FITS directly in the browser** (SCI + DQ overlay, in-browser stretch) instead of a
   baked `*_full.png`. Masking + "flag corrupted / reject exposure" are first-class and honored by
   `combine`.
4. **Headless reduction loop.** On a cluster: `cfpipe nircam process` → `campfire deploy` (implicit
   **draft**) → inspect/mask/flag in the web UI → `campfire … pull` (masks **and** exclusions) →
   `cfpipe nircam combine` → `campfire deploy` (mosaics, publish). Multi-reducer-safe; the CLI can
   restore a re-reducible workspace via `download --intermediate`.

---

## 4. What's missing from the one-paragraph vision (open questions to pin down)

These are the decisions the four bullets don't yet answer. Each should be resolved in the design
sub-issue (§7, N0) before its consumer is built.

### 4.1 The deploy *unit* and storage budget (biggest lever)
NIRCam dominates the 20 TB OSN budget: a COSMOS-Web-scale field is ~5,000 exposures × ~150 MB
≈ **750 GB for a single state**. Epic #210's design already decided the unit = **the canonical
exposure file + its `CFP_*` state vector, one object per exposure, re-uploaded only when its
`sha256(SCI+DQ)` content hash changes** (`manifest.py` already computes this). We must confirm and
make explicit:
- Do we deploy **only the latest canonical state** per exposure (recommended — never per-`CFP_*`
  snapshots), and rely on the `CFP_*` header to say how far it's reduced?
- Which arrays ship: SCI+DQ+ERR+VAR_* in one FITS (the on-disk canonical file, as-is) — yes.
  Expmaps: per-exposure or mosaic-level only?
- **Retention / GC policy** for superseded exposures and old mosaic versions (the budget RPC +
  `deploy gc` from #210 exist; NIRCam needs a policy that uses them).
- Mosaic size: multi-GB i2d per (filter × tile × pixel_scale × version × extension). What's the
  registry scope key for a mosaic (`nircam_mosaic` has no natural `exposure_ref`)? Proposed:
  `exposure_ref = <field>/<tile>/<filter>/<scale>/<version>/<extension>` mirroring the
  `nircam_images_unique` constraint.

### 4.2 Mosaic serving & download UX
- Full mosaics are too large to hand a browser as a single GET. Serving "on the nircam page" means
  **presigned single-file GET** per mosaic + **streaming-zip** for multi-file/bulk pulls — which is
  exactly the still-open **#216** (storage-agnostic streaming-zip proxy). **N3 depends on #216.**
- Fate of the existing **map-tile pyramid** (already on R2/CDN): keep as-is for the interactive map;
  the OSN move is about the *downloadable science mosaics*, not the tiles. State this boundary.
- `CurlScriptGenerator.tsx` and `NircamTable.tsx` hardcode CANDIDE — both need reworking to
  OSN/presigned URLs.

### 4.3 FITS-in-browser: which products, and how big
- **Which products render?** Definitely the per-exposure canonical FITS (2040×2040 ×~4 float
  arrays ≈ 130 MB uncompressed). Mosaics too, or only exposures? Rendering a multi-GB mosaic in the
  browser is not viable without cutouts/byte-range/pyramids.
- **Compression & transport:** canonical exposures may be tile-compressed/gzipped; the renderer
  must handle the actual on-disk encoding, and we likely want **HTTP byte-range** requests so the
  UI pulls headers + the SCI extension without downloading VAR_*/sidecars.
- **Stretch/scale in-browser** (zscale/asinh/log), **DQ overlay** (so reviewers see what's already
  flagged), pan/zoom on large arrays → **WebGL vs canvas**, memory budget. Library choice
  (fitsjs / custom WebGL / Aladin-Lite-style) is a spike.
- **Do preview PNGs survive?** Likely keep the small **thumbnail** (`*_preview.png`) for the fast
  grid list, and **retire `*_full.png`** (replaced by the FITS render). That's a pipeline change
  (`preview` step), a deploy change, and a schema change (`full_png_path`). Decide the transition
  (feature-flag both paths during cutover).

### 4.4 "Flag corrupted data" is a new concept, distinct from masking
The vision lists "draw masks / flag corrupted data" as one action, but they are two mechanisms:
- **Masks** = per-pixel `.reg` polygons → `DQ` (exists).
- **Reject/corrupt** = drop the *whole exposure* from combine. Today that's a hand-edited `field.py`
  `skip` glob; the UI's `review_status='excluded'` is **not** wired to it. We need: a first-class
  exclusion flag on `nircam_exposures`, and a **pull path that translates UI exclusions into
  something `combine` honors** (a generated skip-list / per-exposure DQ / config fragment). This is
  a real missing link, not a UI tweak.

### 4.5 The public-visibility enforcement gap for mosaics
"Implicit draft deployment" means draft mosaics must be **invisible on the public nircam page** until
published. `nircam_images` was **never part of #217's enforcement pass** (it had no lifecycle). So
this epic must add `deploy_status` + **RLS + reader predicates + the security test** for
`nircam_images`, mirroring what #217 did for `spectra`/`objects`. A draft mosaic leaking to the
public page is the same silent-leak failure mode #210 guarded against — treat it with the same rigor.

### 4.6 The headless loop's human-in-the-loop barrier
`process (cluster) → deploy → inspect (human, browser) → pull → combine (cluster)` is inherently
asynchronous and multi-machine. Undefined in the vision:
- **How does the cluster job know masking is "done" and it's safe to `pull` + `combine`?** Options:
  a field-level "ready to combine" gate an admin flips in the UI; per-exposure `review_status`
  reaching a quorum; or the reducer simply re-runs `pull` on a schedule. Recommend an explicit
  field-level gate.
- **Multi-reducer locking** (#220's per-`(field)` optimistic version / lease) applied to the NIRCam
  field scope, so two reducers don't clobber a deploy or a combine.

### 4.7 Smaller but real
- **CANDIDE decommission plan:** dual-serve (CANDIDE + OSN) window, migration + hash-verify of the
  existing corpus, then flip + retire.
- **Mask registration:** per #210, all of `reference/` is cloud-backed — NIRCam masks/astrom-cats/
  bad-pixels should be **registered** (`nircam_mask`, `nircam_astrom_cat`, `nircam_bad_pixel` enums
  exist) so a reduction is reproducible and `download --intermediate` can restore them. `pull-masks`
  today only does the DB↔`.reg` round-trip and registers nothing.
- **Re-combine → new mosaic version → registry supersede → republish** churn: define how a mask edit
  bumps a mosaic `version`, supersedes the old `storage_objects` row, and keeps the public page on
  the latest *published* version (manifest staleness detection already exists).
- **Provenance/version gate:** deploying intermediates almost always carries a `.dev`/non-release
  `CMPFRVER` → the CLAUDE.md warn-and-confirm gate fires. Recommend (per #210) `--draft` auto-confirms;
  `publish` re-checks. Decide whether intermediate/mosaic deploys need a CHANGELOG entry.
- **MIRI / photometry:** explicitly out of scope (note it so nobody assumes coverage).

---

## 5. Challenges & risks

1. **FITS-in-browser is the one genuinely new capability** and the largest unknown: transport
   (byte-range on possibly-compressed FITS), rendering large arrays performantly (WebGL), stretch,
   memory, DQ overlay, and correct pixel/coordinate handling. There's no precedent in the repo.
   *Mitigation:* a bounded renderer spike (N4) with a go/no-go before committing the UI migration
   (N5); keep the PNG path behind a flag until FITS render is proven.
2. **The "render FITS directly" vs "don't ship GB to the browser" tension**, especially for mosaics.
   May force a server-side cutout/downsample service — i.e., you don't fully escape a
   derived representation. *Mitigation:* scope FITS render to per-exposure first; treat mosaic
   in-browser render as a stretch goal behind cutouts.
3. **Storage budget.** Even one-state-per-exposure NIRCam is ~0.75 TB/field; mosaics add multi-GB ×
   versions. *Mitigation:* content-hash dedup (already in `manifest.py`), latest-state-only policy,
   budget RPC + alerts + `deploy gc`, explicit retention.
4. **Silent public leak of draft mosaics** if `nircam_images` visibility isn't enforced in *every*
   reader (RLS + the public list action + any REST route). *Mitigation:* reuse #217's exact
   two-surface enforcement + security test, extended to `nircam_images`.
5. **Coordinate-frame correctness** across the PNG→FITS UI change. Masks are 1-indexed DS9 image,
   `origin='lower'`, y-flipped relative to the PNG; `apply_masks` depends on this exactly.
   *Mitigation:* golden round-trip test (draw in UI → JSONB → `.reg`/skip → `DQ`) that must be
   byte-stable across the migration; the FITS renderer must reproduce the current pixel convention.
6. **Dependency on the still-open #216.** Mosaic serving + bulk download need the streaming-zip
   proxy and the OSN data-backend flip. *Mitigation:* sequence N3 after #216; N1/N2/N4 don't need it.
7. **Async multi-machine loop coordination** (barrier + locking) is workflow, not just CLI, and is
   easy to get subtly wrong (races, clobbered deploys). *Mitigation:* explicit field-level gate +
   #220's optimistic-version lock; make every step idempotent.
8. **CANDIDE migration correctness** (hash-verify the existing corpus; no gap in availability).
   *Mitigation:* dual-serve + verify-before-flip, reusing #215's copy/verify pattern.
9. **Re-deploy/version churn** creating registry drift or a public page pointing at a stale/mixed
   version. *Mitigation:* manifest-driven staleness + `superseded` tombstones + "latest published"
   resolution, tested.

---

## 6. Deploy parity gap (NIRCam vs NIRSpec) — checklist

What NIRSpec's `deploy_observation` has that NIRCam's `deploy_nircam` lacks (target: close each):

- [ ] Canonical **intermediate FITS** upload to OSN canonical keys (NIRCam previews are still R2
      *legacy* keys) — N1.
- [ ] `draft → published → revoked` **lifecycle** on the exposure/mosaic rows — N1 (exposures), N3
      (mosaics).
- [ ] **Mosaic** (final science product) deploy — N2 (no analogue exists; CANDIDE today).
- [ ] **Registry rows** for exposures/mosaics/masks under canonical keys — N1/N2/N6.
- [ ] **publish / revoke** verbs reaching NIRCam rows — N3 (+ admin UI).
- [ ] **`download --intermediate` / `delete-local`** validated for NIRCam (inert today) — N6.
- [ ] Multi-field / filter **batching & filtering** flags on the NIRCam command — N1.
- [ ] `nircam` deploy is a **flat command**, not a group; adding `mosaics`/`intermediate`/`pull`
      subcommands suggests refactoring to a `deploy nircam <sub>` group — N1.

---

## 7. Proposed work breakdown (natural units → sub-issues)

Grouped into a shared design unit, then four tracks. Two tracks (deploy/serving and inspection-UI)
are largely parallel; the loop track integrates them.

```
N0 Design + budget + unit decisions ──┬──────────────────────────────────────────────┐
                                       │                                              │
Track D (deploy → OSN, main-inert):    │  Track U (inspection UI, parallel):          │
  N1 exposures+expmaps on OSN + draft ──┤    N4 FITS-in-browser renderer (spike→build) │
  N2 mosaic deploy + versioning ────────┤    N5 migrate masking UI PNG→FITS + reject   │
  N3 public serving from OSN (needs #216)│                                             │
        + nircam_images lifecycle/RLS    │                                             │
        + CANDIDE migration/retire       │                                             │
                                       Track L (loop): N6 pull masks+exclusions,        │
                                       ready-to-combine gate, multi-reducer, restore ───┘
```

### N0 — Design spec: deploy unit, budget/retention, and open decisions
Resolve §4 (unit & budget, mosaic registry key, PNG fate, exclusion mechanism, ready-to-combine
signal, CANDIDE plan). Add any missing `campfire-layout` `PRODUCTS` entries / paths. **No code beyond
the layout contract + a budget estimate.** Blocks the rest. *(mirrors #210's PR-2 "contract first".)*

### N1 — NIRCam exposures on OSN, registered, draft-aware  *(deps: N0)*
- Deploy the **canonical exposure FITS** (+ expmap) to OSN under `campfire-layout` canonical keys,
  content-hash-versioned; move preview/full PNG registration onto **canonical OSN keys** too.
- Add `deploy_status` to `nircam_exposures`; register `storage_objects` rows (`nircam_exposure`,
  `nircam_expmap`); wire NIRCam into the `--draft`/publish path.
- Refactor `deploy nircam` into a subgroup; add field/filter batching + `--dry-run` parity.
- **Merges inert** (draft default = admin-only; no public surface touched).
- *Accept:* a processed field deploys canonical exposures as `draft`, registered, hash-dedup on
  re-deploy; security test shows non-admins see zero draft exposures.

### N2 — Mosaic deploy + versioning  *(deps: N1)*
- `campfire deploy nircam mosaics` (or fold into field deploy): upload i2d per
  (filter × tile × scale × version × extension) to OSN, register (`nircam_mosaic`, `nircam_rgb`),
  upsert `nircam_images` with the OSN key + `deployment_id`; replace the archived
  `scripts/archive/deploy_nircam.py` path.
- Version/supersede on re-combine (manifest staleness → new `version` → tombstone old registry row).
- *Accept:* a combined field deploys mosaics to OSN, registered + indexed, re-deploy supersedes
  cleanly; `download` fetches a mosaic by registry key.

### N3 — Serve mosaics from OSN on the public NIRCam page  *(deps: N2, #216)*
- Add `deploy_status` + **RLS + reader predicates + security test** to `nircam_images` (the #217
  enforcement pass it never got); public page filters `published`.
- Swap CANDIDE URLs (`NircamTable.tsx`, `CurlScriptGenerator.tsx`) for OSN presigned / streaming-zip
  (via #216); public downloads run through the storage-agnostic proxy.
- **One-time migration** of existing CANDIDE mosaics → OSN + registry backfill + hash-verify;
  dual-serve window; retire CANDIDE.
- *Accept:* public users download published mosaics from OSN; draft mosaics are invisible; curl
  scripts point at OSN; CANDIDE decommissioned after bake.

### N4 — FITS-in-browser renderer  *(deps: N1; parallel to N2/N3)*
- Spike → build: fetch canonical exposure FITS (byte-range, handling on-disk compression) via an
  admin proxy; render SCI with in-browser stretch (zscale/asinh) + DQ overlay; pan/zoom (WebGL).
- Reproduce the exact current pixel convention (1-indexed, `origin='lower'`) so masks stay valid.
- Go/no-go gate on perf/memory before N5.
- *Accept:* an admin opens an exposure and sees the live FITS SCI (not a PNG) with stretch + DQ
  overlay, at interactive framerate on a full-frame detector.

### N5 — Migrate masking/inspection UI to FITS + first-class reject  *(deps: N4)*
- Port `MaskEditor` from `<img>` PNG to the N4 FITS canvas; preserve the mask JSONB coordinate
  contract; keep the golden round-trip test green.
- Add **"flag corrupted / reject exposure"** as a first-class action (schema flag + UI), distinct
  from per-pixel masks.
- Retire `*_full.png` from the pipeline `preview` step + deploy + schema (keep the thumbnail);
  feature-flag the transition.
- *Accept:* masking works entirely on FITS render; a rejected exposure is recorded distinctly;
  `_full.png` is no longer produced/needed.

### N6 — Close the headless loop  *(deps: N1, N2, N5)*
- Extend the pull path: `pull` materializes **masks and exclusions** into what `combine` honors
  (exclusions → generated skip-list / DQ; masks → `.reg` as today); register masks as cloud-backed
  `reference/` products.
- Add a **field-level "ready to combine"** gate and per-`(field)` multi-reducer lock (#220 pattern).
- Validate `download --intermediate` / `delete-local` **for NIRCam** (restore a re-reducible field =
  `products/` canonical exposures + cloud-backed `reference/` inputs).
- *Accept:* end-to-end on a cluster: `process → deploy(draft) → inspect/mask/flag → pull → combine
  → deploy(publish)`, with a clean-local-then-restore round-trip and no clobbering under two
  reducers.

### Sequencing & merge safety
- **N0 first.** Then Track D (N1→N2→N3) and Track U (N4→N5) run in parallel; N6 integrates.
- Every step **merges inert to `main`**: draft default keeps new NIRCam products admin-only; the
  public page changes (N3) ship behind the published-filter + only after mosaics exist on OSN.
- **One schema-changing PR to `main` at a time** (sequential migrations off the squashed baseline);
  **regenerate `seed.sql`** on any `nircam_exposures` / `nircam_images` column add or preview
  branches go red.
- Pipeline-side changes (retiring `*_full.png`; any exclusion-honoring in `combine`) ride the
  **`pipeline-vX.Y.Z`** tag cadence with a CHANGELOG entry, decoupled from the web deploys.

---

## 8. Definition of done
- `campfire deploy` pushes NIRCam **canonical exposures + mosaics** to OSN, registered, with the
  `draft/published/revoked` lifecycle — parity with NIRSpec.
- The public NIRCam page serves **published mosaics from OSN**; CANDIDE is retired; draft mosaics are
  provably invisible to non-admins.
- The admin inspection UI renders **canonical exposure FITS in-browser** (SCI + DQ + stretch);
  masking + reject are first-class and honored by `combine`; `*_full.png` retired.
- The headless `process → deploy → inspect → pull → combine → deploy` loop runs end-to-end,
  multi-reducer-safe, with clean-local + restore.
