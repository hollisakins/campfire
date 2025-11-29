# Spectra Pagination & Server-Side Filtering Refactor

## Problem Statement

The spectra table currently shows "1,000 spectra found" even when there are more objects in the database. This is because:

1. **Supabase default row limit**: Supabase limits SELECT queries to 1000 rows by default
2. **Client-side filtering**: Some filters (gratings, bitmask flags) are applied after fetching data
3. **No true pagination**: The current implementation fetches all matching rows at once

### Current Architecture (`lib/actions/spectra.ts`)

```
getSpectra(filters)
    │
    ├── 1. Get user's accessible program IDs
    │
    ├── 2. Query objects with nested spectra (LIMITED TO 1000 by default)
    │       SELECT *, programs:program_id(program_name), spectra(*)
    │       FROM objects
    │       WHERE program_id IN (accessible_ids)
    │       + server-side filters (field, redshift_quality, redshift range, search)
    │
    ├── 3. Transform data to SpectrumObject format
    │       - Apply grating filter to nested spectra (CLIENT-SIDE)
    │       - Calculate max S/N
    │
    ├── 4. Apply client-side filters
    │       - Grating: exclude objects with no matching spectra (CLIENT-SIDE)
    │       - Bitmask filters: spectral_features, object_flags, dq_flags (CLIENT-SIDE)
    │
    └── 5. Return { spectra: filtered, total: filtered.length }
                                              ^^^^^^^^^^^^^^^^
                                              Max 1000 due to default limit
```

### Why Simple Fixes Don't Work

| Approach | Problem |
|----------|---------|
| Just increase limit to 10000 | Still caps out; fetches too much data for large datasets |
| Separate count query | Can't count client-side filtered items accurately |
| `{ count: 'exact' }` option | Returns total matching server filters, but we only show 1000 rows - confusing |

## Solution: Full Server-Side Filtering with Pagination

Move all filtering to the database and implement true pagination.

### Target Architecture

```
getSpectra(filters, page, pageSize)
    │
    ├── 1. Get user's accessible program IDs (unchanged)
    │
    ├── 2. If grating filter active:
    │       Query spectra table to get object_ids with matching gratings
    │
    ├── 3. Count query (with ALL filters server-side)
    │       SELECT count(*) FROM objects WHERE <all filters>
    │
    ├── 4. Data query (with pagination)
    │       SELECT *, programs:program_id(program_name), spectra(*)
    │       FROM objects
    │       WHERE <all filters>
    │       ORDER BY object_id
    │       LIMIT pageSize OFFSET (page-1)*pageSize
    │
    └── 5. Return { spectra, total: count, page, pageSize }
```

## Implementation Plan

### Step 1: Move Bitmask Filters to Server-Side (~15 min)

**File:** `lib/actions/spectra.ts`

**Current (client-side):**
```typescript
// Around line 192-205
if (filters?.spectral_features && filters.spectral_features.length > 0) {
  const combinedMask = filters.spectral_features.reduce((acc, val) => acc | val, 0);
  filtered = filtered.filter(obj => (obj.spectral_features & combinedMask) !== 0);
}
// Same for object_flags and dq_flags
```

**New (server-side):**
```typescript
// Add after other server-side filters (around line 128)
if (filters?.spectral_features && filters.spectral_features.length > 0) {
  const combinedMask = filters.spectral_features.reduce((acc, val) => acc | val, 0);
  // Postgres bitwise AND: (spectral_features & mask) > 0
  query = query.gt(`spectral_features.band.${combinedMask}`, 0);
  // Note: May need raw SQL or RPC if Supabase client doesn't support this syntax
  // Alternative: query = query.filter('spectral_features', 'band', combinedMask).gt(0);
}

if (filters?.object_flags && filters.object_flags.length > 0) {
  const combinedMask = filters.object_flags.reduce((acc, val) => acc | val, 0);
  query = query.gt(`object_flags.band.${combinedMask}`, 0);
}

if (filters?.dq_flags && filters.dq_flags.length > 0) {
  const combinedMask = filters.dq_flags.reduce((acc, val) => acc | val, 0);
  query = query.gt(`dq_flags.band.${combinedMask}`, 0);
}
```

**If Supabase client doesn't support bitwise operators directly**, create an RPC function:

```sql
-- Run in Supabase SQL Editor
CREATE OR REPLACE FUNCTION filter_objects_by_bitmask(
  p_spectral_features INTEGER DEFAULT NULL,
  p_object_flags INTEGER DEFAULT NULL,
  p_dq_flags INTEGER DEFAULT NULL
)
RETURNS SETOF objects AS $$
BEGIN
  RETURN QUERY
  SELECT * FROM objects
  WHERE (p_spectral_features IS NULL OR (spectral_features & p_spectral_features) > 0)
    AND (p_object_flags IS NULL OR (object_flags & p_object_flags) > 0)
    AND (p_dq_flags IS NULL OR (dq_flags & p_dq_flags) > 0);
END;
$$ LANGUAGE plpgsql;
```

### Step 2: Move Grating Filter to Server-Side (~30-45 min)

**File:** `lib/actions/spectra.ts`

**Current approach:**
- Fetch all spectra for each object
- Filter spectra by grating client-side
- Exclude objects with no remaining spectra

**New approach:**
```typescript
// Before the main query, if grating filter is active:
let gratingFilteredObjectIds: number[] | null = null;

if (filters?.gratings && filters.gratings.length > 0) {
  // Get object IDs that have at least one spectrum matching the grating filter
  const { data: matchingSpectra } = await supabase
    .from('spectra')
    .select('object_id')
    .in('grating', filters.gratings);

  if (matchingSpectra && matchingSpectra.length > 0) {
    gratingFilteredObjectIds = [...new Set(matchingSpectra.map(s => s.object_id))];
  } else {
    // No matching spectra, return empty result
    return { spectra: [], total: 0, page, pageSize, isAuthenticated: true };
  }
}

// In the main query, add:
if (gratingFilteredObjectIds !== null) {
  query = query.in('id', gratingFilteredObjectIds);
}
```

### Step 3: Add Pagination Parameters (~20 min)

**File:** `lib/actions/spectra.ts`

**Update function signature:**
```typescript
export interface PaginatedSpectraResult {
  spectra: SpectrumObject[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  error?: string;
  isAuthenticated: boolean;
}

export async function getSpectra(
  filters?: Partial<FilterOptions>,
  page: number = 1,
  pageSize: number = 50
): Promise<PaginatedSpectraResult> {
```

**Add count query (after building all filters but before fetching data):**
```typescript
// Clone the query for counting (or build filter conditions separately)
const { count: totalCount, error: countError } = await supabase
  .from('objects')
  .select('*', { count: 'exact', head: true })
  .in('program_id', accessibleProgramIds)
  // ... apply all the same filters ...
  ;

if (countError) {
  console.error('Error counting objects:', countError);
}
```

**Add pagination to data query:**
```typescript
// Calculate offset
const offset = (page - 1) * pageSize;

// Add to query
query = query
  .order('object_id', { ascending: true })
  .range(offset, offset + pageSize - 1);
```

**Update return statement:**
```typescript
return {
  spectra: spectraObjects, // No longer need client-side filtering
  total: totalCount || 0,
  page,
  pageSize,
  totalPages: Math.ceil((totalCount || 0) / pageSize),
  isAuthenticated: true,
};
```

### Step 4: Update Spectra Page to Use Server-Side Pagination (~15 min)

**File:** `app/spectra/page.tsx`

**Add page state to URL params:**
```typescript
// In parseFiltersFromURL, add:
const page = parseInt(searchParams.get('page') || '1', 10);
const pageSize = parseInt(searchParams.get('pageSize') || '50', 10);

// In filtersToURLParams, add:
if (page > 1) params.set('page', page.toString());
if (pageSize !== 50) params.set('pageSize', pageSize.toString());
```

**Update data fetching:**
```typescript
const { spectra, total, page, pageSize, totalPages, error, isAuthenticated } =
  await getSpectra(filters, currentPage, currentPageSize);
```

**Pass pagination props to SpectraTable:**
```typescript
<SpectraTable
  spectra={spectra}
  total={total}
  page={page}
  pageSize={pageSize}
  totalPages={totalPages}
  onPageChange={(newPage) => updateURL({ page: newPage })}
  onPageSizeChange={(newSize) => updateURL({ pageSize: newSize, page: 1 })}
/>
```

### Step 5: Update SpectraTable for Server-Side Pagination (~15 min)

**File:** `components/spectra/SpectraTable.tsx`

Currently uses TanStack Table's client-side pagination. Need to switch to manual/server-side pagination:

```typescript
interface SpectraTableProps {
  spectra: SpectrumObject[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  // ... existing props
}

// In useReactTable config:
const table = useReactTable({
  data: spectra,
  columns,
  manualPagination: true, // <-- Key change
  pageCount: totalPages,
  state: {
    pagination: {
      pageIndex: page - 1,
      pageSize,
    },
    sorting,
  },
  onPaginationChange: (updater) => {
    const newState = typeof updater === 'function'
      ? updater({ pageIndex: page - 1, pageSize })
      : updater;
    onPageChange(newState.pageIndex + 1);
    if (newState.pageSize !== pageSize) {
      onPageSizeChange(newState.pageSize);
    }
  },
  // ... rest of config
});
```

### Step 6: Remove Client-Side Filtering Code (~5 min)

**File:** `lib/actions/spectra.ts`

Remove the following sections (approximately lines 183-205):
- Grating filter on spectraObjects
- spectral_features bitmask filter
- object_flags bitmask filter
- dq_flags bitmask filter

These are now handled server-side.

## Files Changed Summary

| File | Changes |
|------|---------|
| `lib/actions/spectra.ts` | Major refactor - server-side filters, pagination, count query |
| `app/spectra/page.tsx` | Add page/pageSize to URL state, pass to getSpectra |
| `components/spectra/SpectraTable.tsx` | Switch to manual pagination mode |

## Optional: Database Index for Performance

If queries become slow with large datasets, add an index:

```sql
-- Run in Supabase SQL Editor
CREATE INDEX IF NOT EXISTS idx_objects_program_field ON objects(program_id, field);
CREATE INDEX IF NOT EXISTS idx_spectra_object_grating ON spectra(object_id, grating);
```

## Testing Checklist

- [ ] Verify total count is accurate (not capped at 1000)
- [ ] Test pagination: page 1, middle page, last page
- [ ] Test all filters work correctly:
  - [ ] Program filter
  - [ ] Field filter
  - [ ] Grating filter
  - [ ] Redshift quality filter
  - [ ] Redshift range filter
  - [ ] Spectral features bitmask filter
  - [ ] Object flags bitmask filter
  - [ ] DQ flags bitmask filter
  - [ ] Inspected only filter
  - [ ] Search filter
- [ ] Test filter combinations
- [ ] Test URL state persistence (refresh page, share link)
- [ ] Test sorting still works
- [ ] Performance check with large dataset

## Estimated Time

| Task | Time |
|------|------|
| Step 1: Bitmask filters | 15 min |
| Step 2: Grating filter | 30-45 min |
| Step 3: Pagination params | 20 min |
| Step 4: Update page.tsx | 15 min |
| Step 5: Update SpectraTable | 15 min |
| Step 6: Remove old code | 5 min |
| Testing | 20 min |
| **Total** | **~2 hours** |
