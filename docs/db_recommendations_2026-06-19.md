# CAMPFIRE DB — Consolidated Recommendations (2026-06-19)

Prod-validated, read-only investigation. Project `puyczxwyuzpnqvpachip`.
Scale: 33,965 objects / 46,060 targets / 49,600 spectra / 195,175 shutters.
**Nothing has been applied to prod.** All EXPLAINs run against prod with real stats
(local seed is ~100 rows and its planner is unrepresentative).

This doc supersedes the severity calibration in `db_audit_2026-06-19.md` for the
objects-search path: the original audit rated it medium/low because it measured the
*default render* as a *privileged role* (no JWT). Re-measured under a real
authenticated admin session, it is the **#1 user-facing problem**.

---

## 0. Headline: the objects-table search timeout

### Root cause (measured, under a simulated authenticated admin session)

`authenticated` role has `statement_timeout = 8s` (`anon` = 3s). `pg_stat_statements`
shows `get_filtered_objects_paginated` (22,288 calls) with **max = 7,977 ms** — the
fingerprint of the timeout firing. Reproduced and decomposed:

| Path (you = admin, RLS live, warm) | Time | Buffers |
|---|---|---|
| Default render (no search) | 144 ms | — |
| Sort by `max_snr` (no index → full sort) | 205 ms | — |
| **Text search, current `OR/EXISTS` + `ORDER BY object_id LIMIT 50`** | **9,578 ms** | **282,613** |
| Search rewritten as uncorrelated `UNION` (same conditions) | 133 ms | 13,954 |
| Search as a single trgm-style column (`object_id ILIKE`, proxy) | 92 ms | 2,495 |

**Mechanism.** The search predicate
`object_id ILIKE '%x%' OR EXISTS(SELECT 1 FROM targets t WHERE t.object_id=o.id AND t.target_id ILIKE '%x%')`
is a cross-table disjunction. The planner can't estimate it well (est. 7,787 rows;
actual 9), so with `ORDER BY object_id LIMIT 50` it chooses to **walk
`objects_object_id_key` in order and apply the search as a per-row filter**, expecting
to fill 50 quickly. Because only ~9 of 34k rows match, it scans the *entire* table.
For a non-admin, the `programs &&` gate is selective and shrinks the scan; **for an
admin (all 29 programs) it scans everything → 9.6 s → 8 s timeout.** That is why *you*
hit it and most users don't.

(Self-correction from the live session: I initially attributed the cost to 34k per-row
`accessible_program_slugs()` calls. Measured, the function is `STABLE` and largely
cached; the real cost is the full 34k-row ordered index+heap walk. The RLS-function
wrapping (§3) is hygiene, **not** the fix.)

### Why it's *also* "often slow" without searching

Baseline 144 ms (no search) decomposes into:
- **Exact `COUNT(*)` over all 33,965 rows on every render** — ~54 ms (§2.1).
- **Sorting by `max_snr`/`n_spectra`/`n_targets`/`max_exposure_time`/`photo_z`** —
  full `top-N heapsort`, no index — ~58 ms (§2.2).
- Per-page `member_targets` + `lists` correlated subqueries + JSONB assembly.

---

## 1. The fix: a `search_text` column on `objects` (recommended)

A single denormalized, trgm-indexed text column. Validated: a single-column predicate
escapes the ordered-scan trap (92 ms worst case vs 9,578 ms) **and** unifies the search
surface you described (object ID, target ID, program, observation).

### 1a. What goes in it

Real data shows the tokens you want are spread across columns and **not always
substrings of each other** — e.g. program `egs_bubbles` has target_ids like
`mason_egs_p3_62859` (the slug isn't in the target_id). So concatenate all four sources:

```
search_text = lower( object_id
                     ⧺ every member target_id          -- e.g. castellano3073_p10_16115
                     ⧺ every distinct program_slug      -- e.g. egs_bubbles
                     ⧺ every distinct observation )      -- e.g. castellano3073_p10
```

This makes all of your cases substring-matchable in one indexed predicate:
search `J141934` (object id) · `castellano3073` (program) · `_p10` (observation/pointing)
· `16115` (source id) · `1772` (partial). Size: avg 63.6 chars, max 364,
total ~2.1 MB over 34k objects → trgm GIN ≈ 20–30 MB.

**Comment text is intentionally excluded** (see §1e).

### 1b. Schema + index (`supabase/schemas/tables.sql` + `indexes.sql`)

```sql
-- tables.sql, objects table body
ALTER TABLE public.objects ADD COLUMN search_text text;

-- indexes.sql, -- objects section
CREATE INDEX IF NOT EXISTS idx_objects_search_text_trgm
    ON public.objects USING gin (search_text gin_trgm_ops);
```

`db diff` emits the same. **Lock:** `objects` is a hot-write table (340k upd) — build
the index with `CREATE INDEX CONCURRENTLY` out-of-band. ⚠️ The Supabase CLI wraps each
migration file in a transaction (documented in migration `20260417184613`), so
`CONCURRENTLY` cannot run inside a normal migration — apply it via a manual
`supabase db push` step or a split migration. At 34k rows a plain `CREATE INDEX` is
sub-second if you prefer a brief lock.

### 1c. Population — in the reconcile pipeline, like every other object aggregate

Object aggregates (`programs`, `observations`, `n_targets`, `max_snr`) are built in
**Python during reconcile** (`reconcile.py:258-263`, `deploy/objects.py:234-239`) and
batch-upserted — there are no SQL triggers for them. `search_text` belongs in exactly
the same place (and changes only when membership changes, i.e. at reconcile — no
trigger needed, no comments → no real-time freshness requirement):

```python
# reconcile.py _aggregate(...) and deploy/objects.py, alongside programs/observations
search_text = " ".join(filter(None, [
    object_id,
    *sorted({m["target_id"]    for m in members}),
    *programs,                       # already computed
    *observations,                   # already computed
])).lower()
# add 'search_text': search_text to the upsert payload (objects.py ~321)
```

Ensure `target_id` is in the member projection used for aggregation (`program_slug`
and `observation` already are).

### 1d. One-time backfill for the 34k existing objects (reconcile only runs on deploy)

```sql
UPDATE public.objects o SET search_text = sub.txt
FROM (
  SELECT o2.id, lower(concat_ws(' ',
           o2.object_id,
           (SELECT string_agg(DISTINCT t.target_id,   ' ') FROM public.targets t WHERE t.object_id=o2.id),
           (SELECT string_agg(DISTINCT t.program_slug, ' ') FROM public.targets t WHERE t.object_id=o2.id),
           (SELECT string_agg(DISTINCT t.observation,  ' ') FROM public.targets t WHERE t.object_id=o2.id)
         )) AS txt
  FROM public.objects o2
) sub
WHERE sub.id = o.id;
```

Writes all 34k rows once. Runs as the migration role (bypasses the
`enforce_object_user_update_scope` trigger, which only restricts non-admins). Build the
trgm index *after* the backfill.

### 1e. RPC change (`functions.sql`) — replaces the `OR/EXISTS` (and the §1-of-audit `UNION` patch)

In `get_filtered_objects_paginated` (page CTE ~1037 **and** the count step ~926) and
`get_filtered_object_ids`, replace the `OR/EXISTS` block with:

```sql
AND (p_search IS NULL OR o.search_text ILIKE '%' || p_search || '%')
```

`db diff` emits full-body `CREATE OR REPLACE FUNCTION`s.

### 1f. Comment search stays separate — and that's correct

The UI already separates the two (`filter-params.ts`: `p_search` is labeled
`targetIdSearch`; `p_comment_search` is its own scoped control). Comment search must
stay separate because it is (1) **user-scoped** (`just_me` vs `everyone` — can't be
denormalized into a global column), (2) **mutable in real time** (would need triggers
on `comments`), (3) on a tiny table (284 rows). If comment search is ever slow, fix it
*in place* with the same materialize-the-id-set trick — `comments.content` already has
`idx_comments_content_trgm`. Don't fold it into `search_text`.

### 1g. Residual risk to verify post-deploy

The proxy test used a *constant* pattern; the RPC uses `'%' || p_search || '%'`
(parameterized). The RPC already sets `plan_cache_mode='force_custom_plan'`, which
re-plans per call with the actual value, so it should behave like the 92 ms test. After
deploy, `EXPLAIN ANALYZE` the RPC with a real `p_search` and confirm a `Bitmap`/`Sort`
plan (not an `Index Scan using objects_object_id_key` that removes ~34k rows by filter).
If `force_custom_plan` misfires, the fallback is dynamic SQL interpolating the pattern
as a literal (`format(... ILIKE %L ...)`) so trigram extraction is guaranteed.
Short searches (1–2 chars) can't use trgm — accept the scan or require ≥3 chars in the UI.

### 1h. Ship sequence

1. (Optional bleed-stopper, no schema change) the uncorrelated `UNION` rewrite of the
   `OR/EXISTS` predicate — measured 9,578 ms → 133 ms. Use it only if you need relief
   before the `search_text` work lands; `search_text` makes it obsolete.
2. `search_text`: add column → backfill → build trgm index (CONCURRENTLY, out-of-band)
   → swap the RPC predicate → add population to reconcile. Apply the predicate swap to
   `get_filtered_objects_paginated`, `get_filtered_object_ids`. The spectra RPC
   (`get_filtered_spectra_paginated`, `p_search` on `target_id`/`spectrum_id`, max 7,989 ms)
   has the **same pattern** — give it the same treatment as a fast follow.

---

## 2. Other "now" findings (current-size, user-facing)

### 2.1 Gate the exact `COUNT(*)` on the list RPCs — MEDIUM
`get_filtered_objects_paginated` and `get_filtered_spectra_paginated` recompute an exact
full-set `COUNT(*)` on every render (~54 ms objects; ~384 ms spectra, where a CTE is
materialized twice). Add `p_include_count boolean DEFAULT true`; when false, skip the
count (objects) / drop the second CTE reference (spectra — returning `-1` alone is not
enough). The pattern already exists on the sync RPCs. Web must cache the count across
page changes (count is filter-stable) and fetch it only on page 1 / filter change.

### 2.2 Index the objects sort columns — MEDIUM (but lower-value than it looks)
`max_snr`, `n_spectra`, `n_targets`, `max_exposure_time`, `photo_z` have no btree, so
sorting by them does a full `top-N heapsort` (~58 ms at 34k). **Caveat:** for admin the
`programs &&` gate is non-selective, so a btree on these may not be chosen (same gating
as search); for non-admins the selective `programs &&` keeps the sort small. Net: fine
today, a scaling concern. The analogous **spectra** sort columns `signal_to_noise` /
`exposure_time` (audit #2) are higher-value — no such gating there:
```sql
CREATE INDEX IF NOT EXISTS idx_spectra_signal_to_noise ON public.spectra (signal_to_noise DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_spectra_exposure_time   ON public.spectra (exposure_time   DESC NULLS LAST);
```

### 2.3 `shutters(observation)` FK-covering index — MEDIUM
Deploy delete/replace seq-scans 195k rows; `pg_stat_statements` shows ~71 s cumulative
on `DELETE … WHERE observation=$1`. `CREATE INDEX idx_shutters_observation ON shutters(observation);`
(`slit_regions` has the identical uncovered pattern — file alongside.) CONCURRENTLY caveat applies.

### 2.4 `objects.observations` GIN — MEDIUM
All four objects RPCs apply `o.observations && p_observations`; no GIN → admin/all-programs
seq-scans. `CREATE INDEX idx_objects_observations ON objects USING gin(observations);`

### 2.5 `timestamptz` conversion (12 naive `timestamp` columns) — MEDIUM, coupled
JS readers parse naive timestamps as local time (wrong on non-UTC clients). Convert the
12 event/audit columns to `timestamptz … AT TIME ZONE 'UTC'`. **Must ship with** the web
fix in `admin/activity/page.tsx` (its `${ts}Z` workaround breaks once values serialize
`+00:00`). See `db_audit_2026-06-19.md` §#6 for the column list and ALTERs.

### 2.6 Constraint hygiene — LOW
`targets.object_id` FK → `ON DELETE SET NULL` (matches the relink model; lone outlier);
`objects_redshift_quality_check (0–4)`; drop the duplicate `nircam_images_unique`. All
via `NOT VALID` + `VALIDATE`. Details in `db_audit_2026-06-19.md` §#7–#10.
**Do NOT** add `snr >= 0` checks (prod holds 20 negative-SNR spectra / 14 objects — open
science question: legitimate flux-weighted values or dirty data?).

---

## 3. At-scale (10×–100×, not urgent today)

- **Wrap RLS functions:** 9 policies call bare `accessible_program_slugs()` / `auth.uid()`;
  wrap as `(select …)` to hoist `STABLE SECURITY DEFINER` calls to a once-per-query
  InitPlan (audit #23 + Supabase advisor). Measured *incidental* to the timeout, but
  cheap insurance that reduces per-row RLS overhead on every gated read at scale.
- **The admin RLS full-scan:** the objects SELECT policy is `programs && accessible_program_slugs()`;
  for admins this returns all slugs → the `programs` GIN always bitmaps the whole catalog.
  Harmless now; at 10–100× consider an `is_admin() OR (programs && (select accessible_program_slugs()))`
  short-circuit so admins skip the overlap. (Interacts with browse-vs-search index choice — measure first.)
- **Deep OFFSET pagination** in the sync/CSV RPCs → keyset cursors (audit #24).
- **int4 PK sequences** on churn tables — safe to ~100×; document the headroom (audit #27).
- **`spectra.date_obs` text → date** (audit #28).

---

## 4. Consolidated priority

| # | Item | Lens | Sev | Effort | Where |
|---|------|------|-----|--------|-------|
| 1 | `search_text` column + trgm GIN → fix objects search timeout | now | **high** | M | §1 |
| 2 | Gate exact `COUNT(*)` on both list RPCs (+ client count cache) | now | med | M | §2.1 |
| 3 | `shutters(observation)` + `slit_regions(observation)` indexes | now | med | S | §2.3 |
| 4 | `objects.observations` GIN | now | med | S | §2.4 |
| 5 | spectra `signal_to_noise` / `exposure_time` sort indexes | now | med | S | §2.2 |
| 6 | `timestamptz` conversion + coupled web fix | now | med | M | §2.5 |
| 7 | objects sort-column indexes (max_snr, n_spectra, …) | now/scale | low | S | §2.2 |
| 8 | Constraint hygiene (FK SET NULL, redshift_quality check, dup unique) | now | low | S | §2.6 |
| 9 | Wrap bare `auth.uid()`/`accessible_program_slugs()` in 9 policies | scale | med | M | §3 |
| 10 | Keyset pagination on sync/CSV RPCs | scale | med | M | §3 |
| — | DO NOT: `snr>=0` check · `COUNT(*) OVER()` · correlated-EXISTS for lists · drop trgm/grating "unused" indexes | — | — | — | audit §7 |

### Schema cleanliness / drift
No meaningful drift between prod and `supabase/schemas/` (68 migrations applied,
29 tables / 136 indexes / 57 functions / 93 policies match). Coordinates are all
`double precision`; natural-key uniqueness is fully enforced; RLS covers all 29 tables.

### Suggested PR grouping
- **PR A — objects search** (`search_text`): column + backfill migration + trgm index
  (manual `db push` for CONCURRENTLY) + RPC predicate swap + reconcile population. Highest impact.
- **PR B — index pack**: shutters/slit_regions(observation), objects.observations GIN,
  spectra sort indexes. Pure adds, low risk.
- **PR C — count gating** (RPCs + web count caching).
- **PR D — timestamptz** (ALTERs + `admin/activity` web fix), low-traffic window.
- **PR E — constraint hygiene** + **PR F — RLS `(select …)` wrap** (advisor-driven), independent.

### Verification gaps (honest)
- No `hypopg` on prod (install denied) → index *adoption* reasoned from selectivity +
  proxied with existing same-shape indexes, not measured hypothetically.
- The `search_text` RPC predicate was validated via a constant-pattern proxy on the
  existing `object_id` trgm index; the parameterized-in-plpgsql case needs a post-deploy
  `EXPLAIN` (§1g).
- Per-row RLS penalties (§3) are structural (InitPlan vs inline), not benchmarked under
  concurrency; 10–100× projections are reasoned, not load-tested.
