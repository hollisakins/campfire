# Design: Object-Centric Inspection Migration

**Status:** Proposed
**Date:** 2026-04-16
**GitHub:** #95
**Branch:** TBD

## Summary

Promote objects to persistent, first-class entities that own inspection state.
This simplifies the data model by eliminating the double aggregation
(targets → objects) and aligns inspection with how astronomers actually work —
assessing a sky position, not a per-program observation.

**Core changes:**
1. **Objects become persistent** — replace wipe-and-rebuild with incremental reconciliation
2. **Inspection state moves from `targets` → `objects`** — `redshift_inspected`, `redshift_quality`, `last_inspected_at`, `last_inspected_by`
3. **`redshift_auto` moves from `targets` → `spectra`** — per-grating auto-fit redshifts, with `objects.redshift_auto` computed from the best member spectrum
4. **DQ flags move from `targets` → `spectra`** — where they belong (read-only at object level; per-spectrum editing only)
5. **Deprecate `spectral_features`** — not widely used, remove rather than migrate
6. **New staleness tracking** — `last_data_change_at` vs `last_inspected_at` with machine-readable `staleness_reason`
7. **Optimistic locking** — `version` column on objects for concurrent edit protection
8. **Remove `propagate_crossmatch_inspection`** — redundant artifact of the old model (cross-object propagation at 0.1" is impossible when objects are defined at 0.2" FoF)
9. **Deprecate the targets list view** — with inspection state lifted to objects, targets become stateless provenance. The `?view=targets` mode on the list page and the supporting RPCs (`get_filtered_targets_paginated`, `get_targets_for_sync`, `count_distinct_inspected_targets`, `get_adjacent_targets`) go away. Listing/filtering happens at the objects and spectra levels only.
10. **Single-page object detail redesign** — ships in the same PR as the data migration (intentional: fewer surfaces changing for users in production)

## Motivation

The current three-level hierarchy (objects → targets → spectra) stores
inspection state at the target level, which creates several problems:

- **Double aggregation.** The pipeline computes a per-target consensus redshift
  across gratings, then the deploy process picks a "best" redshift across
  targets for the object. Both steps are doing the same thing.
- **Stale per-target state.** When a new program observes the same position,
  old target-level assessments become misleading. The
  `propagate_crossmatch_inspection` function is a workaround for this —
  evidence that the system wants object-level coherence.
- **Fragile objects.** Objects are wiped and rebuilt every deploy, with state
  surviving only through lossy coordinate re-linking (0.3" tolerance). This is
  fine when objects carry no user-editable state, but blocks any future
  enhancement.
- **Inspection is conceptually per-object.** When an astronomer inspects,
  they're answering "what's the redshift of this sky position?" — an
  object-level question. The per-target UI forces users to pick a tab before
  they can inspect, even for singleton objects.
- **Per-grating auto-z values are available but discarded.** The pipeline
  computes `redshift_auto` per grating via zfit, but
  `deploy/summary.py:get_unique_objects()` collapses them to a single consensus
  value per target. The per-grating values are scientifically useful for
  inspection context.

After this migration, each level has a clean role:

- **Objects** — the entity users interact with (inspect, tag, comment on, add
  to lists). Owns `redshift_inspected`, `redshift_quality`, and
  `redshift_auto` (best member spectrum's auto-z).
- **Targets** — stateless provenance (these spectra came from this program's
  observation).
- **Spectra** — individual data products with per-file quality flags
  (`dq_flags`) and per-grating auto-fit redshifts (`redshift_auto`).

## Inspection model

The key insight: with per-spectrum `redshift_auto` values, the inspection
workflow becomes **"Do I agree with the automatic assessment?"** The object
detail page shows the full landscape of auto-fit redshifts across all member
spectra, making the inspector's job clear:

```
J100028.34+021234.5 | redshift = 1.5123

4 spectra across 3 observations:
ember_cosmos_p1_12345  | PRISM | z_auto = 1.5000 | SNR = 10
ember_cosmos_p1_12345  | G395M | z_auto = 1.4989 | SNR = 4
capers_cosmos_p2_11111 | PRISM | z_auto = 1.4999 | SNR = 50
gto_wide_cosmos_p1_567 | G395H | z_auto = 1.5123 | SNR = 25  ← selected
```

The "selected" indicator marks which spectrum's `redshift_auto` was promoted
to `objects.redshift_auto` (highest SNR). The inspector sees all per-grating
fits, understands why one was selected, and either accepts or overrides with
`redshift_inspected`.

For DQ flags: displayed as a read-only union of member spectra flags on the
object page. Per-spectrum DQ editing happens inline on each spectrum card. No
object-level DQ write — this avoids broadcasting a single DQ value to all
spectra (which would mark clean data as corrupted).

## UI redesign: single-page object detail

Bundled into the same PR as the data migration. Rationale: fewer changes
for users in production, one "flip day" rather than a schema change followed
by a UX change. Risk tradeoff accepted.

### Problem with today's layout

The current page is organized around **targets** — sidebar lists targets,
clicking one opens a target tab, inspection happens per-target. The new model
makes targets stateless provenance. Users complained that per-spectrum info
(2D spectra, exposure times) felt hidden behind sidebar clicks that didn't
signal their payoff. The tab system (overview → target → grating sub-tab) is
two layers of navigation before content.

### Single-page, no tabs

The object detail page becomes a single scrollable page with all information
visible in a clear hierarchy.

#### Header

```
J100028.34+021234.5
Field: cosmos  ·  3 observations  ·  4 spectra
Coordinates: 150.1181 +2.2096  (10:00:28.34 +02:12:34.5)   Show on map
[MAX S/N: 50.3]  [REDSHIFT: 1.5123]  [QUALITY: Secure]  [4 GRATINGS]
```

- `redshift` and `quality` read from the object (not aggregated from targets).
- **"Needs Review" badge** when `last_data_change_at > last_inspected_at`, with
  tooltip showing `staleness_reason` (e.g., "New target added since last
  inspection").

#### Section 1: Spectrum Comparison

`MultiSpectrumViewer` promoted to primary content. Same grating/program filter
pills as today. New:

- Traces labeled directly on the plot (e.g., `PRISM (EMBER)`, `G395H (GTO)`),
  not just color-coded to sidebar.
- Click a trace to scroll down to that spectrum's detail card.

#### Section 2: Spectra Detail Cards (replaces target tabs)

Each spectrum gets an expandable card in a vertical stack, ordered by sidebar
drag order:

```
┌──────────────────────────────────────────────────────────────────┐
│ ▼  PRISM · ember_cosmos_p1 · ember_cosmos_p1_12345               │
│    z_auto = 1.5000 · SNR = 10.2 · t_exp = 3.2h · DQ: — · ← selected │
├──────────────────────────────────────────────────────────────────┤
│  [2D S/N heatmap + cross-dispersion profile]                     │
│  [1D spectrum (SpectrumPlot)]                                    │
│  [GratingDetails: reduction version, FITS path, download]        │
└──────────────────────────────────────────────────────────────────┘
```

- **Collapsed state** (default for all except highest-SNR spectrum): header
  row only — grating, observation, target_id, z_auto, SNR, exposure time, DQ
  flag pills.
- **Expanded state**: full `SpectrumPlot` (2D + 1D + profile), `GratingDetails`,
  download button.
- **"← selected"** indicator on the spectrum whose `redshift_auto` matches
  `objects.redshift_auto`.
- **DQ flags**: clickable pills on the card header row. Toggling auto-saves to
  `PATCH /api/spectra/[id]/dq` immediately, independent of the object-level
  inspection save.
- **Collapse all / expand all** toggle. Keyboard shortcuts `J` / `K` for
  stepping through cards.
- **"Needs Review" auto-scroll**: when `staleness_reason = 'new_target'`, the
  page auto-scrolls to the new spectrum card on load and briefly highlights it.

#### Section 3: Redshift Fits

`RedshiftFitSummary` at the object level — one row per spectrum (grating,
observation, z_auto, χ²_min, confidence, "selected" checkmark). Clicking a row
scrolls to and expands that spectrum's card. Collapsible χ² curve plots below.

#### Section 4: Photometry + SED

Same as today. Interactive SED + P(z). Static PDF viewer as collapsible
subsection.

#### Section 5: Discussion

`ObjectComments` at the object level **plus** per-target comment threads (both
retained). Per-target attribution preserved for historical comments. Target
comments are useful for program-specific notes.

#### Section 6: Nearby Objects

Same, but links go to object pages (not target pages).

#### Sidebar (simplified)

No longer a navigation device — becomes a control panel:

- Cutout with shutter overlay (color-keyed by target).
- Spectrum visibility checkboxes with color dots and drag-to-reorder (controls
  comparison plot z-order AND card order in Section 2).
- No "Overview" button (no tabs to switch).

#### Floating Inspection Panel (simplified)

- **Left**: Object list tags (same).
- **Right**: Redshift display, override input, quality selector, Save button.
- **No spectral_features** (deprecated).
- **No DQ flags** here (per-spectrum, on each card header).
- **Always active** — no "select a target to inspect" disabled state.

### Inspection Mode Overlay

Navigates through objects instead of targets (decided).

- **Main plot area**: grating tab switcher, but tabs now span all spectra
  across all member targets. Grouped by observation when there are multiple:
  `PRISM (EMBER) | G395M (EMBER) | PRISM (CAPERS) | G395H (GTO)`.
- **Right sidebar** (`DashboardPanel`):
  - Member spectra summary table at top (z_auto, SNR, DQ per spectrum) —
    compact version of Section 2 headers.
  - Cutout, redshift section, quality section, save buttons (same as today
    minus spectral_features).
- **Queue**: fetches uninspected object IDs (via `get_filtered_object_ids`).
  Save & Next saves the object, advances to next uninspected object.
- **Keyboard**: `G` cycles through all spectra across all member targets. All
  other shortcuts unchanged.

### Edge cases

- **Objects with 10+ spectra** (rare but real — e.g., LRD in 5 programs):
  collapse all / expand all toggle, J/K keyboard nav.
- **Old bookmarks**: `/nirspec/targets/[id]` redirects to object page, landing
  with that target's spectra cards auto-expanded.
- **Mobile**: sections stack naturally without tabs; sidebar becomes a
  collapsible header.
- **`staleness_reason = 'migration_conflict'`**: tooltip shows the conflicting
  inspected redshift values from different targets.
- **409 Conflict response** (see Phase D.4a): simple error banner "Inspection
  state has been changed, please refresh" — no merge UI, user reloads.

---

## Phased implementation

Code can land in stages; the production flip (Phase D) is the only atomic
event. Phases A–C can merge weeks in advance.

### Phase A — Additive schema (safe to ship alone)

Columns sit empty; no behavior change.

Add to `objects` (`supabase/schemas/tables.sql`):

```sql
-- Inspection state (moved from targets)
redshift_auto double precision,
redshift_inspected numeric(10,6),
redshift numeric(10,6) GENERATED ALWAYS AS (
    CASE WHEN redshift_quality = 1 THEN NULL
         ELSE COALESCE(redshift_inspected, redshift_auto)
    END
) STORED,
redshift_quality integer NOT NULL DEFAULT 0,
last_inspected_at timestamptz,
last_inspected_by uuid REFERENCES auth.users,

-- Staleness & concurrency
last_data_change_at timestamptz,
staleness_reason text,  -- 'new_target' | 'reprocessed' | 'membership_changed' | 'migration_conflict'
version integer NOT NULL DEFAULT 1,

-- Lifecycle
is_active boolean NOT NULL DEFAULT true
```

Add to `spectra`:

```sql
redshift_auto double precision,
dq_flags integer NOT NULL DEFAULT 0
```

Indexes:

```sql
CREATE INDEX idx_objects_redshift_quality ON objects (redshift_quality);
CREATE INDEX idx_objects_redshift ON objects (redshift);
CREATE INDEX idx_spectra_dq_flags ON spectra (dq_flags) WHERE dq_flags != 0;
CREATE INDEX idx_objects_is_active ON objects (is_active) WHERE is_active = false;
```

RLS: non-admins currently can't write to objects. Add an inspection-specific
update RPC (service-role, field-restricted) or a scoped RLS policy.

Note: `spectral_features` is **not** migrated to objects — deprecated (Phase E).

### Phase B — Deploy pipeline writes new columns (safe to ship alone)

Fills in new columns on the next production deploy; old UI unaffected.

- `deploy/summary.py:get_spectra_records()` carries per-grating `redshift_auto`
  from the pipeline ECSV column of the same name. NaN values (failed zfit)
  become NULL. Always written when the column exists in the ECSV — a re-fit
  producing NULL clears the previous value rather than silently keeping it.
- `deploy/supabase.py:batch_upsert_spectra()` needs no code change: it
  passes whatever keys are in the dict to PostgREST, which only updates the
  columns present (so existing `dq_flags` stays untouched).
- **`spectra.dq_flags` is NOT written from the deploy pipeline.** The pipeline
  doesn't produce per-spectrum DQ. New spectra get the column default `0`;
  existing spectra keep whatever the per-spectrum API endpoint set. The
  initial population (target → spectra copy) happens once in Phase D.1c.
- Target-level `redshift_auto` and `dq_flags` keep being populated by
  `batch_upsert_objects()` (back-compat). Nothing changes there.

Validate on local Supabase + a dry-run against one field before running
against production.

### Phase C — Persistent objects reconciliation (merged, not yet run)

Replaces `rebuild_field_objects()` (wipe-and-rebuild) with
`reconcile_field_objects()` (incremental). Gated behind a config flag or
unreleased subcommand until Phase D completes; running it beforehand = nothing
to preserve.

Algorithm:

1. Cluster targets (same FoF algorithm as today).
2. Match clusters to existing objects by position (greedy, distance-sorted,
   0.3" tolerance).
3. **Matched objects**: update aggregates only (n_targets, n_spectra, programs,
   gratings, max_snr, etc.), preserve inspection state. Set
   `last_data_change_at` and `staleness_reason` per the staleness detection
   rules below.
4. **New clusters**: insert new objects (no inspection state).
5. **Orphaned objects**: set `is_active = false` (don't delete).
6. **Splits/merges**: detected and surfaced as an interactive confirmation
   step in the CLI — **not automatic**. See split/merge policy below.

**Delete entirely**: `_clear_field_objects()`, `_relink_list_members()`,
`_relink_comments()`, `_relink_photometry()`, `_save_comment_coords()`. These
are all artifacts of wipe-and-rebuild.

#### Staleness detection

`staleness_reason` is set on a matched object when one of these conditions
holds during reconciliation. Values are mutually exclusive — if multiple
apply, the priority order below resolves it. `last_data_change_at` is set to
`NOW()` whenever `staleness_reason` is set; both fields are left untouched
when reconciliation finds nothing has changed.

1. **`'membership_changed'`** — the set of member targets changed in any way
   other than a single addition (i.e., a target was removed, or two-plus
   targets were added at once). Highest priority because the underlying
   composition shifted.
2. **`'new_target'`** — exactly one new target was added to the cluster, no
   targets removed. Distinguished from the general case so the UI can
   auto-scroll to the new spectrum card on load.
3. **`'reprocessed'`** — membership unchanged, but at least one member
   spectrum's `file_hash` differs from the snapshot taken at the previous
   reconciliation. Detected by comparing pre/post `spectra.file_hash` for the
   object's member spectra inside the reconcile transaction. This replaces
   the old "drift detection" in `batch_upsert_objects()` (which silently
   reset `redshift_quality → 0` on `|Δredshift_auto| > 0.03`); we now mark
   stale via badge instead of resetting quality.
4. **`'migration_conflict'`** — set only by the Phase D.1a data migration
   when multiple member targets carried conflicting `quality=4` redshifts.
   Never set by ordinary reconciliation.

#### `is_active = false` semantics

Orphaned objects are soft-deleted (set `is_active = false`), not removed.
This preserves their inspection state, comments, list memberships, and
photometry so they can be reactivated if a future deploy reintroduces
overlapping targets at the same sky position.

Visibility:
- Hidden from list views, map markers, the inspection queue, and CSV exports.
- Reachable via direct URL with a "This object is inactive" banner — and via
  the admin endpoint at `/api/admin/objects/inactive` (D.4d) for listing,
  permanent delete, or reactivation.
- Comments, list memberships, and photometry remain attached. Reactivation
  brings them all back automatically.
- Search by `object_id` (IAU name) still returns inactive matches with the
  banner — astronomers may legitimately need to look up an old name.

#### Split/merge policy

When FoF produces two clusters where one object existed before (split), or
one cluster where two objects existed before (merge):

- **Split inheritance rule**: the daughter object with the highest `max_snr`
  among members that overlap the old object's members by ≥50% inherits the
  inspection state. The other daughter is created with no inspection state.
- **Merge inheritance rule**: the surviving object inherits state from the
  pre-merge object with the higher `redshift_quality` (ties broken by
  `last_inspected_at` descending, then `max_snr` descending). The other
  pre-merge object is set `is_active = false` with
  `staleness_reason = 'membership_changed'`.

**Associated state inheritance** (applies to splits and merges alike):

- **Comments** (`comments.object_id`): follow the inspection inheritance.
  On split, all comments move to the inheriting daughter. On merge, both
  pre-merge objects' comment threads concatenate onto the survivor (no
  dedup; user-attributed history is preserved).
- **List membership** (`object_list_members.object_id`): follow the
  inspection inheritance. On split, list memberships move to the inheriting
  daughter. On merge, the union of both objects' list memberships attaches
  to the survivor (deduped by list_id).
- **Photometry** (`object_photometry.object_id`): position-keyed, not
  membership-keyed. On split, the daughter whose centroid is closest to the
  original photometry coordinates inherits the row. On merge, both pre-merge
  photometry rows roll up to the survivor as separate rows (deduplication
  is a Phase E concern).

**CLI UX**: `campfire deploy objects` and any deploy that triggers
reconciliation must log each proposed split/merge with:

- The object ID(s) involved, sky positions, member target counts before/after.
- The proposed inheritance decision and the reason (e.g., "daughter A gets
  inspection state: overlaps old members 4/5, max_snr 50.3 vs daughter B's 10.1").
- Require interactive confirmation before committing, unless `--yes` is passed.

Also add `campfire deploy objects merge` / `campfire deploy objects split`
for manual resolution after the fact.

#### CLI surface

The `campfire deploy objects` subcommand splits into two:

- **`campfire deploy objects reconcile`** *(default subcommand for the bare
  `campfire deploy objects` form)* — runs `reconcile_field_objects()`. Called
  automatically from `deploy_observation()` after spectra upsert. Manual
  invocation supports `--field <name>`, `--all`, `--dry-run`, and `--yes` to
  skip split/merge confirmation prompts.
- **`campfire deploy objects rebuild --force`** — escape hatch. Runs the
  legacy wipe-and-rebuild path. `rebuild_field_objects()` is retained
  *solely* for this purpose and only callable through the `--force` flag,
  which triggers a hard interactive confirmation ("this will wipe inspection
  state for N objects, type DESTROY to continue"). For use only when
  reconciliation produces structurally wrong results that warrant starting
  over.

#### `objects.redshift_auto` computation

After reconciliation: for each object, find the member spectrum with the
highest `signal_to_noise` and set `objects.redshift_auto` to that spectrum's
`redshift_auto`. Document the selection rule as a `COMMENT ON COLUMN`. This
replaces the old two-hop path (pipeline → target → object trigger) with a
direct one-hop path (spectrum → object at reconciliation time).

#### Deploy orchestration

Change flow in `deploy.py` from:

```
upsert targets → upsert spectra → rebuild_field_objects()
```

To:

```
upsert targets → upsert spectra (with redshift_auto)
  → reconcile_field_objects()
  → compute_object_redshift_auto()
```

Also in `supabase.py`:

- Remove inspection fields from target INSERT/UPDATE (keep `redshift_auto` on
  targets during transition but it's read-only/derived).
- Remove drift detection logic from targets (staleness mechanism on objects
  handles this).
- Remove `propagate_crossmatches()` call.

### Phase D — Migration day (atomic)

Bundled into one PR + one `supabase db push` + one Vercel deploy. Includes the
full UI redesign.

#### D.1 — Data migration SQL (one transaction)

**D.1a. Populate object inspection state from best member target:**

```sql
WITH best_targets AS (
    SELECT DISTINCT ON (t.object_id)
        t.object_id,
        t.redshift_inspected,
        t.redshift_quality,
        t.last_inspected_at,
        t.last_inspected_by
    FROM targets t
    WHERE t.object_id IS NOT NULL
      AND t.redshift_quality > 0
    ORDER BY t.object_id,
             t.redshift_quality DESC NULLS LAST,
             t.max_snr DESC NULLS LAST
)
UPDATE objects o SET
    redshift_inspected = bt.redshift_inspected,
    redshift_quality = bt.redshift_quality,
    last_inspected_at = bt.last_inspected_at,
    last_inspected_by = bt.last_inspected_by
FROM best_targets bt
WHERE o.id = bt.object_id;
```

Then flag migration conflicts (multiple member targets with `quality = 4` and
differing `redshift_inspected`):

```sql
WITH conflict_objects AS (
    SELECT t.object_id
    FROM targets t
    WHERE t.object_id IS NOT NULL AND t.redshift_quality = 4
    GROUP BY t.object_id
    HAVING COUNT(DISTINCT ROUND(t.redshift_inspected::numeric, 2)) > 1
)
UPDATE objects o SET
    last_data_change_at = NOW(),
    staleness_reason = 'migration_conflict'
FROM conflict_objects c
WHERE o.id = c.object_id;
```

**D.1b. Backfill `spectra.redshift_auto` (placeholder, overwritten on next deploy):**

```sql
UPDATE spectra s SET redshift_auto = t.redshift_auto
FROM targets t
WHERE s.target_id = t.target_id;
```

Set `objects.redshift_auto` from best member spectrum:

```sql
WITH best_spectrum AS (
    SELECT DISTINCT ON (t.object_id)
        t.object_id, s.redshift_auto
    FROM spectra s
    JOIN targets t ON s.target_id = t.target_id
    WHERE t.object_id IS NOT NULL AND s.redshift_auto IS NOT NULL
    ORDER BY t.object_id, s.signal_to_noise DESC NULLS LAST
)
UPDATE objects o SET redshift_auto = bs.redshift_auto
FROM best_spectrum bs WHERE o.id = bs.object_id;
```

**D.1c. Copy DQ flags from targets to spectra:**

```sql
UPDATE spectra s SET dq_flags = t.dq_flags
FROM targets t
WHERE s.target_id = t.target_id AND t.dq_flags != 0;
```

Conservative: copies the full target bitmask to all its spectra. Users refine
per-spectrum on re-inspection.

**D.1d. Initial staleness timestamps:**

```sql
UPDATE objects SET last_data_change_at = updated_at
WHERE last_inspected_at IS NOT NULL;
```

#### D.2 — Database functions, triggers, views

- **Remove** `update_object_best_redshift` trigger (objects own their state).
- **Remove** `propagate_crossmatch_inspection` function entirely.
- **Remove entirely** (target list view deprecated):
  - `get_filtered_targets_paginated`
  - `get_targets_for_sync`
  - `get_adjacent_targets`
  - `count_distinct_inspected_targets`
  - `get_csv_export` (target-CSV variant; the object/spectra variants stay)
- **Retarget** `log_flag_changes`: split into two triggers — one on `objects`
  (logs `redshift_quality` changes), one on `spectra` (logs `dq_flags`
  changes). Update `flag_audit_log` to add nullable `object_id` and
  `spectrum_id`, make `target_id` nullable. Existing rows keep their
  `target_id`; no backfill.
- **Add** optimistic-locking trigger: auto-increment `objects.version` only
  when `redshift_inspected` or `redshift_quality` changes (not on aggregate
  column updates from reconciliation).
- **Rewrite** filter/sort RPCs:
  - `get_filtered_spectra_paginated` — `redshift_quality` filter joins to
    objects via targets; `dq_flags` filter reads directly from `spectra`;
    return `s.redshift_auto` in output.
  - `get_filtered_objects_paginated` — reads inspection state directly from
    objects; filters out `is_active = false`; returns per-spectrum
    `redshift_auto` in embedded spectra.
  - `get_csv_export_objects` — updated field sources, includes new object
    inspection fields.
  - **New**: `get_adjacent_objects` — replaces `get_adjacent_targets` for
    prev/next navigation.
  - **New**: `count_distinct_inspected_objects` — replaces
    `count_distinct_inspected_targets`.
- **Sync RPC:**
  - `get_objects_for_sync`: add `redshift_inspected`, `redshift_quality`,
    `redshift_auto`, `last_inspected_at`, `last_inspected_by`,
    `last_data_change_at`, `staleness_reason`, `version`, `is_active`.
    Per-spectrum embedded output includes `redshift_auto` and `dq_flags`.
- **Views:**
  - `target_flag_summary` → `spectrum_flag_summary` (dq_flags only, from spectra).
  - `targets_with_flags` → drop entirely (no consumers after target view removal).
  - Remove `spectral_features` from any remaining views.

#### D.3 — Deploy pipeline (cutover)

Phase C code is already merged. Migration day:

- Flip the config flag / release the subcommand.
- First post-migration deploy runs `reconcile_field_objects()` against
  production; migration-day objects already carry inspection state from D.1a,
  so reconciliation preserves it.

#### D.4 — Web API

**D.4a. `PATCH /api/objects/[id]/inspect`** — replaces `PATCH /api/targets/[id]`
for inspection writes. Accepts:

```json
{
    "redshift_inspected": 2.1,
    "redshift_quality": 4,
    "expected_version": 5
}
```

No `dq_flags` or `spectral_features` in this endpoint.

Returns `409 Conflict` if `expected_version` doesn't match. **Response policy
(decided):** simple error; the UI surfaces "Inspection state has been changed,
please refresh." No merge UI, no diff, no auto-reload.

**D.4b. `PATCH /api/spectra/[id]/dq`** — writes `dq_flags` to individual
spectra. Preserves per-spectrum granularity.

**D.4c.** `/api/admin/activity/route.ts`: update `flag_audit_log` query to
join on `object_id` / `spectrum_id` instead of only `target_id`.

**D.4d.** Admin endpoint for `is_active = false` objects — count, list,
permanently delete or reactivate.

#### D.5 — Web frontend (types + components)

**Types (`web/lib/types.ts`, `web/lib/actions/spectra-types.ts`):**

- `ObjectDetail`: add `redshift_auto`, `redshift_inspected`, `redshift_quality`,
  `last_inspected_at`, `last_inspected_by`, `last_data_change_at`,
  `staleness_reason`, `version`. Remove `best_redshift` / `best_redshift_quality`.
- `ObjectMemberTarget`: remove inspection fields (keep `redshift_auto` on
  targets during transition for display).
- `Spectrum`: add `redshift_auto`, `dq_flags`.
- Remove `spectral_features` from all types.
- `spectra-types.ts`: update object sort columns (`best_redshift` → `redshift`).

**Components:** the full single-page redesign (see UI redesign section
above) — header, Section 1 (MultiSpectrumViewer promoted), Section 2
(spectra detail cards replacing target tabs), Section 3 (redshift fits),
Section 4 (photometry/SED), Section 5 (discussion — both object + target
comments), Section 6 (nearby objects), simplified sidebar, simplified
floating inspection panel, inspection mode overlay navigating by object.

**Hooks:**

- `useInspectionState.save()` POSTs to `/api/objects/{objectId}/inspect`;
  takes `objectId` and `version`. Handles 409 by showing the simple refresh
  error banner.
- `useInspectionQueue` navigates uninspected *objects* (not targets).

**Tables / list page:**

- `SpectraTable`: **remove the `targets` view mode entirely**. The list page
  toggle becomes Objects | Spectra (no Targets option). Default landing view
  switches to Objects.
- Objects mode: rename `best_redshift` → `redshift`, add a "Needs Review"
  staleness indicator column (badge, sortable by `last_data_change_at >
  last_inspected_at`).
- Spectra mode: add `redshift_auto`, `dq_flags` columns; the quality column
  reads through `targets → objects` join (server-side via
  `get_filtered_spectra_paginated`).
- Remove the `spectral_features` column from all modes.
- Update `web/lib/navigation-cache.ts`: drop the targets cache; keep object
  and spectrum caches.
- `web/lib/actions/spectra-types.ts`: drop the targets sort-column type;
  keep object and spectrum sort-column types (with `best_redshift → redshift`
  rename in objects).
- Routing: any link to `/nirspec?view=targets` collapses to `/nirspec`
  (objects view); old bookmarks redirect server-side.

#### D.6 — Migration-day sequence

1. Announce maintenance window.
2. `supabase db push --linked` — single transaction (schema + data migration + function updates).
3. `git push` to main — Vercel auto-deploys new frontend.
4. Verify: browse objects, inspect one, run `campfire deploy --dry-run` against one field.
5. Run `campfire deploy` on one production field to confirm reconciliation works end-to-end.
6. Announce complete.

#### D.7 — Rollback plan

If the web deploy has issues, revert the Vercel deployment. Old code still
works because old target columns haven't been dropped — they still hold their
pre-migration values. New object columns are additive. Losses during rollback
window: any inspection writes made against the new object endpoint won't show
up in the old UI (which reads from target columns). Acceptable for the narrow
rollback window.

### Phase E — Cleanup (weeks after Phase D)

Gate: two successful production deploys post-D, no rollback calls, admin
activity endpoint confirmed writing to new audit rows.

**E.1. Deprecate `spectral_features`:**

- Drop from `targets` table.
- Never add to `objects` table.
- Remove `SPECTRAL_FEATURES` from `web/lib/flags.ts`.
- Remove inspection UI checkboxes.
- Remove `SpectralFeatures` IntFlag from Python client `flags.py`.
- Remove from all RPCs that filter/return it.

**E.2. Drop old columns:**

- From `targets`: `redshift_inspected`, `redshift_quality`, `dq_flags`,
  `last_inspected_at`, `last_inspected_by`, `spectral_features`, `max_snr`
  (aggregate moved to objects).
- Eventually from `targets`: `redshift_auto` (once all consumers read from
  spectra).
- From `objects`: `best_redshift`, `best_redshift_quality`.
- Drop old triggers; remove back-compat code.

**E.3. Python client:**

- `client.py`: `query_objects()` returns full inspection state.
  `query_targets()` is either collapsed to a thin provenance lookup (just
  `target_id → program_slug, observation, object_id`) or removed entirely if
  no callers remain. Decision deferred to Phase E — grep callers at that
  point.
- `sync.py`: local DuckDB/SQLite schema — schema version bump triggers full
  rebuild. Drop target inspection columns from local schema.
- `db/store.py`: add `redshift_auto` and `dq_flags` to spectra DDL; remove
  inspection columns from targets DDL.
- `flags.py`: remove `SpectralFeatures`.

---

## Open questions (resolved)

1. **Split/merge policy.** Resolved: highest `max_snr` among daughters with
   ≥50% member overlap inherits; deploy CLI logs the decision and requires
   interactive confirmation (`--yes` to skip). See Phase C.
2. **Target-level comments.** Resolved: keep both object + target comment
   threads.
3. **`redshift_auto` recomputation timing.** Resolved: recompute
   `objects.redshift_auto` after `reconcile_field_objects()` (which may change
   membership), not after target upsert.
4. **Inspection mode overlay navigation unit.** Resolved: navigate by object.
   A multi-target object shows all member spectra at once; `G` cycles through
   them.
5. **409 Conflict UI.** Resolved: simple error banner "Inspection state has
   been changed, please refresh." No merge UI.
6. **Targets list view.** Resolved: deprecated entirely. Targets become
   stateless provenance; the list page toggle is Objects | Spectra only.
   Affected RPCs (`get_filtered_targets_paginated`, `get_targets_for_sync`,
   `get_adjacent_targets`, `count_distinct_inspected_targets`, target CSV
   export) drop in Phase D.2.
7. **Reprocessing detection.** Resolved: `staleness_reason='reprocessed'` is
   set when any member spectrum's `file_hash` changes between reconciliations.
   This replaces the legacy auto-reset of `redshift_quality` on
   `|Δredshift_auto| > 0.03`; we surface staleness via badge instead of
   silently mutating user state.
8. **Associated state on split/merge.** Resolved: comments and list
   memberships follow inspection inheritance; photometry follows position
   (closest centroid on split, both rows roll up on merge). See Phase C
   "Associated state inheritance".
9. **`is_active = false` visibility.** Resolved: hidden from list/map/queue/
   CSV; reachable via direct URL with banner; admin endpoint manages
   reactivation/permanent delete; comments/lists/photometry stay attached.
10. **CLI escape hatch.** Resolved: `campfire deploy objects reconcile`
    (default) for incremental, `campfire deploy objects rebuild --force` for
    legacy wipe-and-rebuild with hard interactive confirmation. See Phase C
    "CLI surface".

## Pre-flight checklist (before migration day)

- [ ] Phase A schema merged to production, columns visible in Supabase dashboard.
- [ ] Phase B deploy pipeline merged; at least one production deploy has
      populated `spectra.redshift_auto` and `spectra.dq_flags`.
- [ ] Phase C reconciliation code merged behind flag; tested on local Supabase
      with production-like data (via `generate_seed.py --objects-per-program 20`).
- [ ] Dry-run the data migration SQL on a local DB reset from production seed;
      confirm conflict count is reasonable (spot-check a few).
- [ ] New frontend tested end-to-end against local Supabase.
- [ ] Rollback plan rehearsed (revert Vercel, confirm old UI still functional
      reading from frozen target columns).
- [ ] Split/merge CLI confirmation UX tested on a synthetic split case.
- [ ] Announce maintenance window.
