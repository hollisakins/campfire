# Design: Unified Object Page for NIRSpec Review

**Status:** Proposed
**Date:** 2026-04-12

## Problem

The NIRSpec review UX splits closely related tasks across two page types:

- **Object detail page** (`/nirspec/objects/[id]`): Multi-spectrum comparison,
  member target listing, tagging — but no inspection capability.
- **Target detail page** (`/nirspec/targets/[id]`): Per-grating analysis,
  redshift fits, photometry, inspection panel, comments — but no tagging, no
  sibling comparison.

This creates a "two-page shuffle" for the most common scientific workflow:
forming an assessment of a sky position. A user sees the multi-target comparison
on the object page, forms an opinion, then must click into each member target
individually to record that opinion (set redshift quality, flag features, leave
comments). They cannot tag an object from the target page, and they cannot
inspect a target from the object page.

The unit of scientific judgment is the **object** (unique sky position), not the
**target** (individual observation). The UX should reflect this.

### Specific Pain Points

1. **No inspection on the object page.** Users must navigate to each member
   target individually to set redshift quality, spectral features, and DQ flags.
   For a 4-target object, this means 4 page loads with back-button clicks.

2. **No tagging on the target page.** After inspecting a target, users must
   navigate up to the object page to tag it.

3. **Comments are target-scoped.** Scientific notes about a sky position are
   fragmented across N per-target comment threads. There is no object-level
   discussion.

4. **Fullscreen inspection mode is target-only.** The inspection queue steps
   through targets without showing sibling targets from the same object for
   comparison context.

5. **Redshift consistency is invisible.** The object page shows
   `best_redshift`; each target has its own redshift. No UI shows whether
   members agree or disagree at a glance.

6. **Context switching cost.** Each target page load is a full navigation +
   data fetch. Inspecting a multi-target object requires multiple round trips.

---

## Design

### Layout: Sidebar + Main Panel

Replace the separate object and target detail pages with a single unified object
page. The page has three zones:

```
┌─────────────────────────────────────────────────────────────┐
│  Object Header                                              │
│  ID · Field · RA/Dec · Tags · Metrics                       │
├──────────────┬──────────────────────────────────────────────┤
│  Sidebar     │  Main Panel                                  │
│              │                                              │
│  [Overview]  │  (content depends on selected sidebar tab)   │
│  ─────────── │                                              │
│  [Target 1]  │                                              │
│  [Target 2]  │                                              │
│  [Target 3]  │                                              │
│              │                                              │
└──────────────┴──────────────────────────────────────────────┘
```

**Header** (persistent across all tabs):
- Object ID, field, RA/Dec, map link
- RGB cutout thumbnail
- Tags (existing `ObjectListsSection` UI)
- Summary metrics: max S/N, best redshift, best quality, grating count
- Download / copy link actions
- Prev/next object navigation

**Sidebar** (persistent, vertical tab list):
- **Overview** tab (always first)
- One tab per member target, each showing:
  - `target_id` (truncated if needed)
  - Program name
  - Available gratings (compact badges)
  - Current redshift value
  - Quality icon (color-coded dot)

The sidebar serves double duty: navigation and at-a-glance status. A user can
see which members are uninspected (gray dots) without clicking into them.

### Overview Tab

The overview tab contains what currently lives on the object detail page:

- Multi-spectrum comparison viewer (all members overlaid)
- Program and grating filter toggles
- Member targets summary table (read-only, links to target tabs)
- Object-level discussion / comments
- Nearby objects / context

This is the "big picture" view — comparison and context.

### Target Tabs

Each target tab contains what currently lives on the target detail page:

- **Sub-tabs** along the top of the main panel:
  - One per available grating (PRISM, G140H, G235H, etc.)
  - REDSHIFT (fit summary + per-grating fit plots)
  - PHOTOMETRY (SED plot, if available)
- **Grating sub-tab content**: spectrum plot (interactive) + grating metadata
  (wavelength range, resolution, S/N per order)
- **Redshift sub-tab content**: fit summary table + per-grating fit plot images
- **Photometry sub-tab content**: SED plot viewer

- **Sticky inspection bar** (bottom of the main panel, always visible on target
  tabs):
  - Current redshift display (auto or inspected, with strikethrough if excluded)
  - Redshift override input
  - Quality dropdown (color-coded, keyboard shortcut 1-4)
  - Spectral features multi-select
  - DQ flags multi-select
  - Save button / Save & Next Target button
  - Per-target comment thread (collapsible)

The sticky inspection bar ensures that the user can set quality and flags while
looking at any grating plot or redshift fit — no need to switch to a separate
"inspect" sub-tab.

### Layout Sketch

**Overview tab selected:**
```
┌──────────────────────────────────────────────────────────────┐
│  CAMPFIRE-J100023+021845   Field: cosmos   150.096  2.312    │
│  Tags: [AGN] [z>3] [+ Add]   Max S/N: 24   z: 3.21  ●Sec   │
├──────────────┬───────────────────────────────────────────────┤
│              │                                               │
│  ◉ Overview  │  Multi-Spectrum Comparison Viewer             │
│              │  ┌───────────────────────────────────────┐    │
│  ─────────── │  │  [all member spectra overlaid]        │    │
│              │  │  [interactive zoom/pan]                │    │
│  cosmos_     │  └───────────────────────────────────────┘    │
│  ddt_66964   │  Programs: [RUBIES] [AURORA]                  │
│  RUBIES      │  Gratings: [PRISM] [G140H] [G235H] [G395H]   │
│  PRISM G235H │                                               │
│  z=3.21 ●    │  Discussion (3 comments)                      │
│              │  ┌───────────────────────────────────────┐    │
│  cosmos_     │  │ Alice: Interesting broad Lya wing...  │    │
│  ddt_71002   │  │ Bob: Confirmed — see G395H continuum  │    │
│  AURORA      │  └───────────────────────────────────────┘    │
│  PRISM G395H │                                               │
│  z=3.19 ●    │  Nearby Objects                               │
│              │  ┌───────────────────────────────────────┐    │
│  cosmos_     │  │ [context / neighbor list]             │    │
│  ddt_80331   │  └───────────────────────────────────────┘    │
│  RUBIES      │                                               │
│  G140H       │                                               │
│  z=— ⚪      │                                               │
│              │                                               │
└──────────────┴───────────────────────────────────────────────┘
```

**Target tab selected (cosmos_ddt_66964):**
```
┌──────────────────────────────────────────────────────────────┐
│  CAMPFIRE-J100023+021845   Field: cosmos   150.096  2.312    │
│  Tags: [AGN] [z>3] [+ Add]   Max S/N: 24   z: 3.21  ●Sec   │
├──────────────┬───────────────────────────────────────────────┤
│              │  PRISM | G235H | REDSHIFT | PHOTOMETRY        │
│  Overview    │  ─────────────────────────────────────────    │
│              │                                               │
│  ─────────── │  ┌───────────────────────────────────────┐    │
│              │  │                                       │    │
│  ◉ cosmos_   │  │  Spectrum Plot (PRISM)                │    │
│  ddt_66964   │  │  [interactive, emission lines at z]   │    │
│  RUBIES      │  │                                       │    │
│  PRISM G235H │  └───────────────────────────────────────┘    │
│  z=3.21 ●    │                                               │
│              │  Grating Details                               │
│  cosmos_     │  Wavelength: 0.6 – 5.3 μm                    │
│  ddt_71002   │  Resolution: R ~ 100                          │
│  AURORA      │  S/N: 24.1 (max across orders)                │
│  PRISM G395H │                                               │
│  z=3.19 ●    │                                               │
│              │                                               │
│  cosmos_     ├───────────────────────────────────────────────┤
│  ddt_80331   │  z: 3.2100  override: [______]  Q: [●Secure] │
│  RUBIES      │  Features: [Lyα] [Multi Em.]   DQ: [——]      │
│  G140H       │  💬 2 comments              [Save] [Save+Next]│
│  z=— ⚪      │                                               │
└──────────────┴───────────────────────────────────────────────┘
```

---

## URL Structure

```
/nirspec/objects/[id]                   → Overview tab (default)
/nirspec/objects/[id]?tab=overview      → Overview tab (explicit)
/nirspec/objects/[id]?tab=[target_id]   → Target tab for that member
```

The selected sub-tab (grating, redshift, photometry) can optionally be encoded
as a second parameter:

```
/nirspec/objects/[id]?tab=[target_id]&grating=g235h
```

All URLs are shareable and bookmarkable. Filter state from the main list is
preserved via existing URL parameter encoding.

---

## Target Page Redirect

`/nirspec/targets/[id]` becomes a redirect:

1. Look up the target's `object_id`.
2. Redirect to `/nirspec/objects/[object_id]?tab=[target_id]`.

This preserves existing bookmarks, shared links, and external references. The
main spectra table continues to show target IDs as links — they just resolve
through the redirect.

### No Orphan Targets

This design assumes all targets have a parent object. The deployment pipeline
(`campfire deploy objects`) already creates singleton objects for isolated
targets. To make this a hard guarantee:

1. **Deploy pipeline**: Ensure `rebuild_field_objects()` runs as part of every
   `deploy` invocation, not just when explicitly requested. Currently it runs
   in `deploy_observation()` — verify it covers all code paths.

2. **Database constraint**: Consider adding a `NOT NULL` constraint on
   `targets.object_id` once all existing targets have been assigned objects.
   This can be done as a migration after a one-time backfill.

3. **Validation**: Add a deploy-time check that warns if any targets in the
   field remain without an `object_id` after the objects rebuild step.

Until the `NOT NULL` constraint is in place, the redirect should handle orphan
targets gracefully — either by rendering a singleton-style object page with
just that target, or by showing an error with a link to run the objects rebuild.

---

## Fullscreen Inspection Mode

The existing fullscreen inspection mode (`/inspect`) should become
object-aware:

- **Queue unit**: Objects, not targets. The queue is a list of object IDs
  matching the current filters.
- **Per-object view**: Same sidebar + main panel layout as the object page, but
  fullscreen. The user reviews all member targets for an object before advancing.
- **Navigation**: "Next" moves to the next object. "Next target" steps through
  members within the current object. Keyboard shortcuts:
  - `n` / `→`: next member target (or next object if on last member)
  - `p` / `←`: previous member target (or previous object if on first member)
  - `N` / `Shift+→`: next object (skip remaining members)
  - `1-4`: set quality for current target
  - `s`: save current target
  - `S`: save all members and advance to next object
- **Progress display**: "Object 3/47 · Target 2/4"

This aligns the inspection workflow with the scientific workflow: assess an
object (all its member targets), then move to the next one.

---

## Data Loading

### Object Page Load

The existing `getObjectById` action already returns all member targets with
their full `spectra[]` arrays. This is sufficient for the overview tab and
sidebar metadata.

For target tabs, additional data needs to be fetched on tab selection (lazy):

- **Redshift fit plots**: Fetched via `/api/redshift-fit/` per grating (already
  exists, currently used by the target detail page).
- **SED/photometry plot**: Fetched via `/api/sed-plot/` (already exists).
- **Comments**: Fetched per target via Supabase client (already exists).
- **Grating details**: Already included in the `spectra[]` array from
  `getObjectById`.

Lazy loading per-tab keeps the initial page load fast. Prefetching the next
member's data (like the current inspection mode does) would make tab switching
feel instant.

### Inspection Saves

The existing `updateInspection` server action works per-target and does not
need to change. "Save & Next Target" calls it for the current target, then
advances the sidebar selection.

---

## Discussion / Comments

### Object-Level Comments

Add a new comment scope: object-level discussion. This lives on the overview
tab alongside the comparison viewer. Object-level comments are for observations
about the sky position as a whole ("redshifts agree across programs," "likely
blend," etc.).

Implementation options:
- **New column**: Add `object_id` to the `comments` table (nullable, alongside
  existing `target_id`). Comments with `object_id` set and `target_id` null are
  object-level. Comments with both set are target-level but visible from the
  object context.
- **Separate table**: A new `object_comments` table. Simpler schema, but
  fragments the comment system.

Recommendation: extend the existing `comments` table with a nullable
`object_id` column. This keeps the comment system unified and allows queries
like "all comments related to this object" (either object-level or on any
member target).

### Per-Target Comments

Per-target comments remain on target tabs, in the sticky inspection bar
(collapsible). These are for target-specific notes ("PRISM has chip gap at
2.1μm," "G395H contaminated").

---

## Migration Plan

### Phase 1: Ensure No Orphan Targets
1. Run `campfire deploy objects --all` to backfill objects for all fields.
2. Verify no targets have `object_id = NULL`.
3. Update deploy pipeline to guarantee objects rebuild on every deploy.
4. Add `NOT NULL` constraint on `targets.object_id` (migration).

### Phase 2: Unified Object Page
1. Build the sidebar + main panel layout component.
2. Implement the overview tab (migrate current object detail page content).
3. Implement target tabs (migrate current target detail page content).
4. Add the sticky inspection bar to target tabs.
5. Wire up URL state (`?tab=` parameter).
6. Add object-level comments (extend `comments` table).
7. Test with single-target objects (should feel like the current target page
   with a minimal sidebar).

### Phase 3: Target Page Redirect
1. Convert `/nirspec/targets/[id]` to a redirect page that looks up
   `object_id` and redirects to the unified object page.
2. Update the main spectra table links to point to the object page directly
   (avoid the redirect hop where possible).
3. Update fullscreen inspection mode entry to use object URLs.

### Phase 4: Object-Aware Inspection Mode
1. Refactor the inspection queue to operate on objects.
2. Update the fullscreen overlay to use the sidebar + main panel layout.
3. Add per-object navigation (next/prev object) alongside per-member navigation.
4. Update keyboard shortcuts for the two-level navigation.

---

## Open Questions

1. **Sidebar on small screens.** On narrow viewports, the sidebar could become
   a horizontal tab bar or a collapsible drawer. Need to decide the responsive
   breakpoint behavior.

2. **Single-target objects.** These are the majority of objects. Should the
   sidebar still show for singletons, or should it collapse/hide when there's
   only one member? Showing it keeps the UI consistent; hiding it reduces noise.

3. **Object-level inspection fields.** Currently all inspection fields
   (redshift, quality, features, DQ) are per-target. Should any move to the
   object level? The `best_redshift` and `best_redshift_quality` on the
   `objects` table are computed from member targets, not directly editable.
   This seems correct — keep inspection per-target, keep aggregates computed.

4. **Main table default view mode.** With the target page becoming a redirect,
   should the main table default to "objects" view mode instead of "targets"?
   Users would primarily browse objects, clicking into the unified page. The
   "targets" view mode could remain as an advanced/power-user option.

5. **Comment migration.** Existing per-target comments should remain accessible
   on the unified page. No data migration needed for comments — they already
   have `target_id` and will show on the correct target tab. Object-level
   comments are a new feature with no existing data to migrate.

6. **Inspection mode queue filters.** If the queue operates on objects, how do
   target-level filters (e.g., "only show targets with S/N > 10") translate?
   Options: show the object if *any* member matches; show the object if *all*
   members match; show the object and highlight matching members.
