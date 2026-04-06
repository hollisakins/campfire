# Design: Object Lists & Deploy-Side Objects Rebuild

**Status:** Implemented
**Date:** 2026-03-30 (design) / 2026-04-06 (implementation)
**Branch:** `feat/objects-table`

## Problem

The `objects` table groups targets across programs by sky position, but it's
currently populated by a standalone script (`scripts/populate_objects.py`) that
wipes and rebuilds from scratch. This is fine today because objects carry no
user-editable state — but it blocks two things:

1. **Deploy integration.** Objects should be rebuilt automatically as part of
   `cfdeploy`, not as a manual post-deploy step.
2. **User-editable object metadata.** We want users to create curated lists of
   objects (e.g., "EELGs", "AGN candidates") that survive object rebuilds. The
   current `object_flags` bitmask on `targets` is the precursor to this — it
   needs to migrate to a system that lives at the object level and persists
   through rebuilds.

## Design Principles

- **Objects are a computed artifact.** The `objects` table has no user-editable
  state. Deploy can wipe and rebuild it per-field freely.
- **Coordinates are the durable key.** User metadata (list memberships) is
  anchored to sky positions, not to database row IDs. When objects are rebuilt
  and IDs change, metadata re-attaches via coordinate matching.
- **Deploy-side Python, not server-side SQL.** The clustering logic and re-link
  pass run in `cfdeploy`, not as Postgres functions. The Supabase client is the
  only database access path (no direct psycopg2).
- **Lists replace bitmask flags.** The `object_flags` column on `targets` is
  migrated to system-seeded lists on `object_lists`. Per-target flag assignment
  in the inspection flow is replaced by per-object list assignment on the object
  detail page.

---

## Part 1: Deploy-Side Objects Rebuild

### Current State

`scripts/populate_objects.py` runs standalone:
1. Fetches all targets (optionally per-field) via psycopg2
2. Friends-of-friends clustering via `astropy.coordinates.search_around_sky`
   with 0.2" radius and Union-Find
3. Builds object records (centroid, IAU name, aggregates)
4. Wipes `objects` table, inserts new rows, sets `targets.object_id` FKs

### New Flow

The clustering logic moves into `deploy/campfire_deploy/objects.py` as a module,
using the Supabase client instead of psycopg2. It runs as a step within
`deploy_observation()` and is also available as a standalone subcommand.

#### Integration into `deploy_observation()`

After the existing steps (upsert targets, upsert spectra, propagate
crossmatches, refresh materialized views), add:

```
# Rebuild objects for this observation's field
rebuild_field_objects(sb, field)
```

This step:
1. Fetches all targets for the field via Supabase client
2. Fetches spectra metadata for aggregate computation
3. Runs friends-of-friends clustering (same algorithm as `populate_objects.py`)
4. Wipes existing objects for the field (`DELETE FROM objects WHERE field = ?`)
5. Nulls `targets.object_id` for the field
6. Inserts new objects, sets target FKs
7. Runs the re-link pass for list members (see Part 2)
8. Recomputes `best_redshift`/`best_redshift_quality` via trigger (fires
   automatically when `object_id` FK is set on targets)
9. Refreshes materialized views

#### Standalone Subcommand

```bash
# Rebuild objects for all fields (full rebuild)
cfdeploy objects --all

# Rebuild objects for a single field
cfdeploy objects --field cosmos

# Dry run
cfdeploy objects --field cosmos --dry-run
```

This replaces `scripts/populate_objects.py`. The standalone script can be
removed once the deploy integration is verified.

#### Module Structure

```
deploy/campfire_deploy/objects.py
```

Key functions:
- `rebuild_field_objects(client, field, radius=0.2)` — full per-field rebuild
- `cluster_targets(targets, radius)` — friends-of-friends (reused from
  `populate_objects.py`)
- `build_objects(targets, groups, spectra_map)` — aggregate computation
- `relink_list_members(client, field, radius=0.2)` — re-link pass (see Part 2)

The `UnionFind` class and `generate_iau_name()` helper move here too.

#### Data Flow

```
Supabase (targets for field)
    ↓ fetch via client
Python (friends-of-friends clustering)
    ↓
Python (build object records + aggregates)
    ↓
Supabase (DELETE objects WHERE field, NULL target FKs)
    ↓
Supabase (INSERT objects, UPDATE target FKs)
    ↓
Python (re-link list members)  ← Part 2
    ↓
Supabase (UPDATE list_members.object_id)
```

#### Scale Considerations

For the largest field (COSMOS), this means pulling ~thousands of targets into
Python for clustering. This is fine — `search_around_sky` is vectorized and
the UnionFind is O(n α(n)). The bottleneck is the Supabase round-trips for
batch inserts/updates, which are already batched at 500 rows.

---

## Part 2: Object Lists

### Schema

Two new tables:

```sql
CREATE TABLE IF NOT EXISTS public.object_lists (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    description TEXT,
    visibility  TEXT NOT NULL DEFAULT 'private'
                CHECK (visibility IN ('private', 'public_read', 'public_edit')),
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    created_by  UUID REFERENCES auth.users(id),
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE public.object_lists IS
    'User-created or system-seeded lists of objects. Visibility controls '
    'who can see and edit the list. System lists (is_system=true) are '
    'seeded at migration time and cannot be deleted by users.';

CREATE TABLE IF NOT EXISTS public.object_list_members (
    id          SERIAL PRIMARY KEY,
    list_id     INTEGER NOT NULL REFERENCES object_lists(id) ON DELETE CASCADE,
    object_id   INTEGER REFERENCES objects(id) ON DELETE SET NULL,
    ra          DOUBLE PRECISION NOT NULL,
    dec         DOUBLE PRECISION NOT NULL,
    notes       TEXT,
    added_by    UUID REFERENCES auth.users(id),
    added_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(list_id, ra, dec)
);

COMMENT ON TABLE public.object_list_members IS
    'Members of object lists. Coordinates (ra, dec) are the durable '
    'positional key; object_id is a fast query key that gets refreshed '
    'after each objects rebuild via coordinate cross-matching.';
```

#### Key Design Decisions

- **`slug`** on `object_lists`: URL-friendly identifier, auto-generated from
  name (e.g., "EELGs" → "eelgs"). System lists use canonical slugs matching the
  old flag keys (e.g., "lrd", "broad-line").
- **`is_system`**: System lists are seeded at migration time from the current
  `OBJECT_FLAGS` definitions. They cannot be deleted by users. They have
  `visibility = 'public_edit'` and `created_by = NULL`.
- **`ON DELETE SET NULL`** on `object_list_members.object_id`: When objects are
  wiped during rebuild, list members go NULL temporarily. The re-link pass
  restores them.
- **`UNIQUE(list_id, ra, dec)`**: Prevents duplicate positions within the same
  list. The uniqueness is positional, not object-based — two objects at the same
  position would conflict, but this shouldn't happen in practice.

### Indexes

```sql
CREATE INDEX idx_list_members_object_id ON object_list_members(object_id)
    WHERE object_id IS NOT NULL;
CREATE INDEX idx_list_members_list_id ON object_list_members(list_id);
CREATE INDEX idx_list_members_coords ON object_list_members(ra, dec);
CREATE INDEX idx_object_lists_created_by ON object_lists(created_by);
CREATE INDEX idx_object_lists_visibility ON object_lists(visibility);
```

### RLS Policies

```sql
ALTER TABLE object_lists ENABLE ROW LEVEL SECURITY;

-- Users can see: their own lists + public lists + public_edit lists
CREATE POLICY "select_lists" ON object_lists FOR SELECT TO authenticated
    USING (
        created_by = auth.uid()
        OR visibility IN ('public_read', 'public_edit')
    );

-- Users can create lists (owned by them)
CREATE POLICY "insert_lists" ON object_lists FOR INSERT TO authenticated
    WITH CHECK (
        created_by = auth.uid()
        AND is_system = false
    );

-- Owners can update their own lists (but not system lists)
CREATE POLICY "update_own_lists" ON object_lists FOR UPDATE TO authenticated
    USING (created_by = auth.uid() AND is_system = false)
    WITH CHECK (created_by = auth.uid() AND is_system = false);

-- Owners can delete their own lists (but not system lists)
CREATE POLICY "delete_own_lists" ON object_lists FOR DELETE TO authenticated
    USING (created_by = auth.uid() AND is_system = false);

-- Admins can manage all lists including system lists
CREATE POLICY "admin_manage_lists" ON object_lists
    USING (public.is_admin());
```

```sql
ALTER TABLE object_list_members ENABLE ROW LEVEL SECURITY;

-- Members visible if:
--   1. The list is visible to the user, AND
--   2. The matched object (if any) has at least one accessible program
-- Members with NULL object_id (orphaned) are visible to the list owner only.
CREATE POLICY "select_list_members" ON object_list_members FOR SELECT TO authenticated
    USING (
        list_id IN (
            SELECT id FROM object_lists
            WHERE created_by = auth.uid()
               OR visibility IN ('public_read', 'public_edit')
        )
        AND (
            object_id IS NULL AND list_id IN (
                SELECT id FROM object_lists WHERE created_by = auth.uid()
            )
            OR object_id IN (
                SELECT o.id FROM objects o
                WHERE o.programs && public.accessible_program_slugs()
            )
        )
    );

-- can_comment users can add members to:
--   - Their own lists
--   - public_edit lists
CREATE POLICY "insert_list_members" ON object_list_members FOR INSERT TO authenticated
    WITH CHECK (
        public.can_comment()
        AND list_id IN (
            SELECT id FROM object_lists
            WHERE created_by = auth.uid()
               OR visibility = 'public_edit'
        )
    );

-- can_comment users can remove members from:
--   - Their own lists
--   - public_edit lists
CREATE POLICY "delete_list_members" ON object_list_members FOR DELETE TO authenticated
    USING (
        public.can_comment()
        AND list_id IN (
            SELECT id FROM object_lists
            WHERE created_by = auth.uid()
               OR visibility = 'public_edit'
        )
    );
```

### Dual-Key Linkage

List members store both:
- **`(ra, dec)`** — the durable positional key, set when the member is added
  (from the object's centroid at that time)
- **`object_id`** — the fast query FK, used for all normal reads

Normal reads join on `object_id` (instant, indexed). After an objects rebuild,
the re-link pass restores `object_id` by coordinate matching:

```python
def relink_list_members(client, field, radius_arcsec=0.2):
    """Re-link orphaned list members to rebuilt objects."""

    # Fetch orphaned members (object_id IS NULL or object was in this field)
    # Fetch all objects for the field
    # For each orphaned member, find the nearest object within radius
    # Update object_id FK

    # Report conflicts:
    #   - orphaned: member coords don't match any object (> radius)
    #   - ambiguous: member coords match multiple objects (pick nearest)
```

The re-link pass runs as part of `rebuild_field_objects()`. After completion:
- **Matched**: `object_id` restored, member is live
- **Orphaned** (no match within 0.2"): `object_id` stays NULL, reported to
  deployer
- **Ambiguous** (multiple matches): nearest centroid wins, reported to deployer

The deployer sees a summary like:
```
Re-linking list members...
  142 members re-linked
  3 orphaned (no object within 0.2"):
    - List "EELGs": J095942.31+021234.5
    - List "LRDs": J100012.88+022056.1
    - List "LRDs": J100023.44+021845.7
  0 ambiguous
Continue? [Y/n]:
```

If the deployer aborts, the objects rebuild has already happened but the list
members remain orphaned (NULL object_id). They can be investigated and the
rebuild re-run.

### Adding an Object to a List (Frontend Flow)

On the **object detail page**, a new "Lists" section shows:
- Tags/badges for each list the object belongs to (public lists visible to user)
- An "Add to list" button that opens a dropdown/modal showing:
  - System lists (the migrated flags)
  - User's own lists
  - Public_edit lists
- Adding/removing toggles the membership via server action

The server action:
1. `INSERT INTO object_list_members (list_id, object_id, ra, dec, added_by)`
   using the object's current centroid coordinates
2. Or `DELETE FROM object_list_members WHERE list_id = ? AND object_id = ?`

### Querying Lists for an Object

For the object detail page, fetch all list memberships:

```sql
SELECT ol.id, ol.name, ol.slug, ol.visibility, ol.is_system,
       olm.notes, olm.added_by, olm.added_at
FROM object_list_members olm
JOIN object_lists ol ON ol.id = olm.list_id
WHERE olm.object_id = ?
```

This is a simple indexed join on `object_id` — no spatial query needed for
normal reads.

For filtering the objects list view by list membership:

```sql
-- "Show me all objects in list X"
SELECT o.* FROM objects o
JOIN object_list_members olm ON olm.object_id = o.id
WHERE olm.list_id = ?
```

This could be added as a filter parameter to `get_filtered_objects_paginated`.

---

## Part 3: Migration from `object_flags`

### Phase 1: Add Tables + Seed System Lists

1. Add `object_lists` and `object_list_members` tables (schema above)
2. Seed system lists from current `OBJECT_FLAGS`:

```sql
INSERT INTO object_lists (name, slug, description, visibility, is_system)
VALUES
    ('Little Red Dots', 'lrd', 'Little red dot candidates', 'public_edit', true),
    ('Broad Line AGN', 'broad-line', 'Broad emission line sources', 'public_edit', true),
    ('Lya Emitters', 'lya-emitter', 'Strong Lyman-alpha emitters', 'public_edit', true),
    ('Balmer Break Galaxies', 'balmer-break-galaxy', 'Strong Balmer break', 'public_edit', true),
    ('[OIII] Emitters', 'oiii-emitter', 'Strong [OIII] emitters', 'public_edit', true),
    ('Ha Emitters', 'ha-emitter', 'Strong H-alpha emitters', 'public_edit', true),
    ('Quiescent Galaxies', 'passive', 'Little star formation', 'public_edit', true),
    ('Dusty Galaxies', 'dusty', 'Significant dust attenuation', 'public_edit', true),
    ('Stars', 'star', 'Stellar spectra', 'public_edit', true);
```

3. Migrate existing flag data: for each target with `object_flags != 0`, decode
   the bitmask and insert list members using the target's parent object's
   coordinates:

```sql
-- Example for LRD flag (bit 0, value 1):
INSERT INTO object_list_members (list_id, object_id, ra, dec, added_by)
SELECT
    (SELECT id FROM object_lists WHERE slug = 'lrd'),
    t.object_id,
    o.ra,
    o.dec,
    t.last_inspected_by
FROM targets t
JOIN objects o ON o.id = t.object_id
WHERE t.object_flags & 1 != 0
  AND t.object_id IS NOT NULL
ON CONFLICT (list_id, ra, dec) DO NOTHING;
```

This runs for each of the 9 flag bits. The `ON CONFLICT DO NOTHING` handles
cases where multiple targets in the same object have the same flag set.

### Phase 2: Frontend Migration

1. **Object detail page**: Add "Lists" section showing memberships as tags, with
   "Add to list" / "Remove from list" UI
2. **Objects list view**: Add list membership as a filter option
3. **Inspection flow**: Remove `object_flags` from `FlagsSection` and
   `InspectionPanel`. Keep `spectral_features` and `dq_flags` (these are
   per-target/per-spectrum properties, not object-level)
4. **`web/lib/flags.ts`**: Remove `OBJECT_FLAGS` export. Add list-related types.

### Phase 3: Cleanup

1. Drop `object_flags` column from `targets` table
2. Remove `object_flags` from deploy upsert logic (`batch_upsert_objects`)
3. Remove `object_flags` from all RPC functions and filter params
4. Remove bitmask filtering for object_flags from `get_filtered_target_ids`

### Migration Ordering

Phase 1 and Phase 2 can be deployed together — the system lists are populated
from existing data, and the old flags remain readable during the transition.
Phase 3 (column drop) should be a separate migration after verifying the lists
system works correctly.

---

## Part 4: `web/lib/flags.ts` Changes

After migration, `flags.ts` retains:
- `REDSHIFT_QUALITY` (ordinal enum, per-target) — unchanged
- `SPECTRAL_FEATURES` (bitmask, per-target) — unchanged
- `DQ_FLAGS` (bitmask, per-target) — unchanged
- `OBJECT_FLAGS` — **removed**

New types for lists (likely in a new file, e.g., `web/lib/types/lists.ts`):

```typescript
interface ObjectList {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  visibility: 'private' | 'public_read' | 'public_edit';
  is_system: boolean;
  created_by: string | null;
  created_at: string;
}

interface ObjectListMember {
  id: number;
  list_id: number;
  object_id: number | null;
  ra: number;
  dec: number;
  notes: string | null;
  added_by: string | null;
  added_at: string;
}
```

System lists could retain the color/icon metadata from the old `FlagDef` for
visual continuity. This could be stored as JSONB metadata on the `object_lists`
table, or hardcoded in the frontend for system lists.

---

## Implementation Plan

### Milestone 1: Deploy-Side Objects Rebuild
1. Create `deploy/campfire_deploy/objects.py` with clustering logic
2. Add `rebuild_field_objects()` to `deploy_observation()` flow
3. Add `cfdeploy objects` subcommand
4. Verify against local Supabase
5. Remove `scripts/populate_objects.py`

### Milestone 2: Object Lists Schema + Migration
1. Add `object_lists` and `object_list_members` to `supabase/schemas/tables.sql`
2. Add RLS policies to `policies.sql`
3. Add indexes to `indexes.sql`
4. Seed system lists in migration
5. Write data migration for existing `object_flags`
6. Generate migration via `supabase db diff`

### Milestone 3: Re-link Pass
1. Implement `relink_list_members()` in `objects.py`
2. Integrate into `rebuild_field_objects()`
3. Add conflict reporting + abort prompt

### Milestone 4: Frontend — Object Lists UI
1. Server actions for lists (CRUD, membership toggle)
2. Object detail page: lists section with tags + add/remove
3. Objects list view: filter by list membership
4. "My Lists" page (create, rename, delete, change visibility)

### Milestone 5: Flags Migration
1. Remove `object_flags` from inspection UI
2. Remove from deploy upsert logic
3. Remove from RPC filter functions
4. Drop column from `targets` (separate migration)

---

## Open Questions

1. **System list metadata.** Should system lists carry color/icon in a JSONB
   column, or should we hardcode the visual treatment for known slugs in the
   frontend? JSONB is more flexible; hardcoding is simpler and avoids a schema
   change if we want to tweak colors.

2. **List member notes.** The `notes` column on `object_list_members` allows
   per-membership annotations (e.g., "confirmed via HST imaging"). Is this
   wanted for the MVP, or should we add it later?

3. **My Lists page scope.** Should this show all visible lists (including
   others' public lists), or just the user's own? Probably a toggle.

4. **`spectral_features` and `dq_flags` future.** These remain per-target
   bitmasks for now since they describe the spectrum, not the physical object.
   Should they eventually get a similar treatment (lists or tags)?

5. **Audit logging.** The current flag changes are logged to `flag_audit_log`.
   Should list membership changes be logged similarly? If so, a new table or
   extend the existing one?
