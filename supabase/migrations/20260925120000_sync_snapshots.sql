-- Sync catalog snapshots: the nightly public-scope snapshot a first-time
-- `campfire sync` downloads instead of paging the five /sync/* streams.
--
-- A first full sync re-derived the whole catalog from Postgres for every new
-- user (on 2026-09-24 one stalled the Micro instance for ~70 min; #569-#572
-- made each walk ~15 s of DB time, but the cost still scales with users). A
-- Vercel cron route now walks the streams once a night for the public-program
-- scope and writes gzip JSONL files to the private data bucket; the client
-- loads them, fetches the rows a public snapshot cannot carry (an "extras"
-- walk), then catches up incrementally from the snapshot's start time.
--
--   * sync_snapshots, NEW: one row per build (started_at watermark,
--     created_at wall clock, public_programs, files, status), at most one
--     `building` at a time (idx_sync_snapshots_one_building). Service-role
--     only: RLS on, no policies, no grants to anon/authenticated.
--   * sync_snapshot_begin(p_format_version), NEW: inserts the `building` row
--     with the public programs and the catch-up watermark (start of the oldest
--     open transaction minus a margin; SECURITY DEFINER to read
--     pg_stat_activity). Service-role only.
--   * sync_deletions, NEW (service-role only) + journal_sync_deletions
--     statement triggers on objects / spectra / storage_objects /
--     object_photometry / spectrum_line_fits + get_sync_deletions(p_since):
--     hard deletes after a snapshot's watermark, which a bootstrapping client
--     applies after its catch-up (a hard delete leaves nothing for the
--     catch-up walk to return). Trimmed by the snapshot builder.
--   * get_objects_for_sync: gains p_filter_program_slugs (the extras walk --
--     objects touching the caller's non-snapshot programs, or in the caller's
--     own/shared non-public lists; the payload stays scoped to the full
--     p_program_slugs). New signature, so the old one is dropped and the
--     grants re-issued. With the filter NULL the output is unchanged
--     (verified: identical full keyset walks against the previous definition
--     for five caller shapes on a production-shaped copy), and a public-scope
--     snapshot overlaid with a caller's extras walk reproduces that caller's
--     full walk row for row.
--
-- Hand-authored (no local Docker for `supabase db diff`); definitions copied
-- verbatim from supabase/schemas/{tables,functions,policies}.sql, which remain
-- the source of truth. The /sync/objects route sends p_filter_program_slugs
-- only in extras mode, so a normal sync is unaffected if the web deploy lands
-- before this migration.

-- sync_snapshots: the nightly public-scope catalog snapshot a first-time
-- `campfire sync` downloads instead of paging the five /sync/* streams (the
-- 2026-09-24 sync outage). One row per build by the /api/cron/sync-snapshot
-- route; the files live in the private data bucket under
-- sync-snapshots/<id>/, served only through /api/v1/sync/snapshot. Service-role
-- only: RLS on with no policies, and no anon/authenticated grants.
CREATE TABLE IF NOT EXISTS "public"."sync_snapshots" (
    "id" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Catch-up watermark, taken before the first page: the start of the
    -- oldest transaction then open, minus a margin (sync_snapshot_begin), so
    -- a row changed while the build was walking -- or committed by a
    -- transaction already in flight when it began -- is re-fetched.
    "started_at" timestamp with time zone NOT NULL,
    "completed_at" timestamp with time zone,
    -- Wall-clock build start. started_at is a backdated watermark and must
    -- not be used to judge whether a build is still running.
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "format_version" integer NOT NULL,
    -- The program scope the snapshot was built for (the public programs at
    -- build time). A caller's extras walk is their accessible programs minus
    -- this set.
    "public_programs" "text"[] NOT NULL,
    -- [{stream, key, sha256, size, rows}], one entry per sync stream.
    "files" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'building'::"text" NOT NULL,
    "error" "text",
    CONSTRAINT "sync_snapshots_status_check" CHECK (("status" = ANY (ARRAY['building'::"text", 'ready'::"text", 'failed'::"text"])))
);


ALTER TABLE "public"."sync_snapshots" OWNER TO "postgres";


COMMENT ON TABLE "public"."sync_snapshots" IS 'Nightly public-scope catalog snapshots for first-time campfire sync (files in the private data bucket under sync-snapshots/<id>/). Written by the /api/cron/sync-snapshot route, read by /api/v1/sync/snapshot; service-role only.';

-- sync_snapshots is service-role only (the cron builder and the snapshot
-- endpoint); the default privileges would otherwise hand it to anon/authenticated.
REVOKE ALL ON TABLE "public"."sync_snapshots" FROM "anon";
REVOKE ALL ON TABLE "public"."sync_snapshots" FROM "authenticated";
GRANT ALL ON TABLE "public"."sync_snapshots" TO "service_role";

ALTER TABLE public.sync_snapshots ENABLE ROW LEVEL SECURITY;

-- At most one snapshot build in flight: sync_snapshot_begin's INSERT fails
-- with unique_violation while another row is `building` (see that function).
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_snapshots_one_building
    ON public.sync_snapshots USING btree ((true))
    WHERE status = 'building';

DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT);

-- Sync snapshot extras walk: gained p_filter_program_slugs (a new signature,
-- so the previous one is dropped before CREATE).
DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT, TEXT[]);

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Keyset cursor (#103): the object_id of the last row of the previous page.
  -- When non-NULL the scan seeks straight to the next id via the
  -- objects_object_id_key UNIQUE btree, so each page costs O(log N + limit).
  -- Keyset is the only pagination (T2-F, #511): OFFSET is refused at the
  -- route (client floor 0.5.0) and p_offset is gone from the signature.
  p_after_object_id TEXT DEFAULT NULL,
  -- Sync snapshot extras walk: when set, return only the objects a
  -- public-scope snapshot cannot carry for this caller -- objects touching
  -- these (the caller's non-snapshot) programs, whose scoped aggregates the
  -- snapshot computed over public programs alone, plus members of the
  -- caller's own or shared non-public lists, whose `lists` field the
  -- snapshot lacks. Everything in the payload stays scoped to the full
  -- p_program_slugs; this only narrows which objects are walked.
  p_filter_program_slugs TEXT[] DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT,
              deleted_ids INTEGER[])
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Statement-timeout backstop for the five /sync/* RPCs (this one,
-- get_spectra_for_sync, get_photometry_for_sync, get_storage_objects_for_sync,
-- get_line_fits_for_sync). T2-F (#536) dropped it as an OFFSET-era relic, but
-- it never only covered deep OFFSET pages: the first page of every stream
-- still runs catalog-wide COUNTs, and `campfire sync` fires all five first
-- pages concurrently. The routes call these as service_role, which has no
-- timeout of its own and inherits authenticator's 8 s, so on a busy or small
-- instance the counts were cancelled mid-sync ("canceling statement due to
-- statement timeout" on /sync/spectra). Keyset pages themselves stay cheap;
-- this only stops a slow first page from killing the whole sync.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  -- matched is MATERIALIZED so the three aggregate CTEs below each see the
  -- same ~p_limit-row set without re-evaluating the WHERE/ORDER/LIMIT.
  WITH matched AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.redshift, o.redshift_quality,
           o.redshift_inspected, o.redshift_auto,
           o.inspected_used_auto,
           o.last_inspected_at, o.last_inspected_by,
           o.last_data_change_at, o.staleness_reason,
           o.version, o.is_active,
           o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      -- Phase D: hide soft-deleted objects from sync. Reactivation rewrites
      -- updated_at, so re-activated rows get re-synced naturally on next pull.
      AND o.is_active = true
      -- B1: drop objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
      -- Keyset (#103): seek past the previous page's last object_id. object_id
      -- is UNIQUE, so a strict > needs no (sort_col, id) tiebreaker. Any future
      -- change to this ORDER BY must keep the ordering column UNIQUE (or switch
      -- to a row-value cursor) or keyset will skip/duplicate rows.
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND (p_filter_program_slugs IS NULL
           OR o.programs && p_filter_program_slugs
           OR o.id IN (
             SELECT olm.object_id
             FROM object_list_members olm
             JOIN object_lists ol ON ol.id = olm.list_id
             WHERE ol.visibility NOT IN ('public_read', 'public_edit')
               AND (ol.created_by = p_user_id
                    OR ol.id IN (SELECT list_id FROM object_list_shares
                                 WHERE user_id = p_user_id))))
    ORDER BY o.object_id
    LIMIT p_limit
  ),
  -- One pass over the page's members (#569 follow-up). Every target of every
  -- page object, flagged with the two scopes the aggregates below need; the
  -- spectra join rides on it. Replaces a per-row LATERAL
  -- object_scoped_aggregates() call (~24 buffer probes per object, ~1.6 s per
  -- 5000-row page on production) plus two separate targets x spectra scans.
  page_members AS MATERIALIZED (
    SELECT t.object_id, t.target_id, t.program_slug,
           -- member_target_ids / spectra payload scope: accessible programs.
           t.program_slug = ANY(p_program_slugs) AS in_programs,
           -- object_scoped_aggregates' recompute scope (its CTE m): accessible
           -- programs AND, B1, a target that contributes a published spectrum.
           (t.program_slug = ANY(p_program_slugs)
            AND (p_include_unpublished OR t.has_published_spectrum)) AS in_scope
    FROM targets t
    WHERE t.object_id IN (SELECT id FROM matched)
  ),
  member_spectra AS MATERIALIZED (
    SELECT pm.object_id, pm.in_programs, pm.in_scope,
           s.id, s.target_id, s.grating, s.signal_to_noise, s.exposure_time,
           s.redshift_auto, s.dq_flags, s.deploy_status,
           (p_include_unpublished OR s.deploy_status = 'published') AS visible
    FROM page_members pm
    JOIN spectra s ON s.target_id = pm.target_id
  ),
  member_targets_agg AS (
    SELECT pm.object_id,
           jsonb_agg(pm.target_id ORDER BY pm.target_id) AS target_ids
    FROM page_members pm
    WHERE pm.in_programs
    GROUP BY pm.object_id
  ),
  -- Phase D: per-spectrum payload (per design doc) so the Python client
  -- can render redshift_auto and dq_flags per grating without a second
  -- round-trip.
  spectra_agg AS (
    SELECT ms.object_id,
           jsonb_agg(jsonb_build_object(
             'id', ms.id,
             'target_id', ms.target_id,
             'grating', ms.grating,
             'signal_to_noise', ms.signal_to_noise,
             'exposure_time', ms.exposure_time,
             'redshift_auto', ms.redshift_auto,
             'dq_flags', ms.dq_flags
           ) ORDER BY ms.target_id, ms.grating) AS spectra
    FROM member_spectra ms
    WHERE ms.in_programs AND ms.visible
    GROUP BY ms.object_id
  ),
  -- Set-based object_scoped_aggregates() for the page: the same fast path and
  -- recompute, per object. Keep in step with that function.
  scope_targets AS (
    SELECT pm.object_id,
           array_agg(DISTINCT pm.program_slug ORDER BY pm.program_slug) AS programs,
           COUNT(*)::integer AS n_targets
    FROM page_members pm
    WHERE pm.in_scope
    GROUP BY pm.object_id
  ),
  scope_spectra AS (
    SELECT ms.object_id,
           -- Fast-path probes, over ALL member spectra (any program): no
           -- non-published spectrum, and a visible count matching stored
           -- n_spectra. This RPC runs RLS-free, so the count probe is
           -- trivially true here, as it is for the helper's RLS-free callers.
           bool_or(ms.deploy_status <> 'published') AS has_unpublished,
           COUNT(*)::integer AS n_all,
           -- Recompute (the helper's CTE sp): visible spectra of in-scope targets.
           array_agg(DISTINCT ms.grating ORDER BY ms.grating)
             FILTER (WHERE ms.in_scope AND ms.visible AND ms.grating IS NOT NULL) AS gratings,
           (COUNT(*) FILTER (WHERE ms.in_scope AND ms.visible))::integer AS n_spectra,
           MAX(ms.signal_to_noise) FILTER (WHERE ms.in_scope AND ms.visible) AS max_snr,
           MAX(ms.exposure_time) FILTER (WHERE ms.in_scope AND ms.visible) AS max_exposure_time
    FROM member_spectra ms
    GROUP BY ms.object_id
  ),
  scoped_aggs AS (
    SELECT m.id AS object_id,
           -- The helper's `stored` branch condition; a NULL (e.g. a NULL
           -- stored n_spectra) falls through to the recompute, as there.
           COALESCE(NOT p_include_unpublished
                    AND m.programs <@ p_program_slugs
                    AND NOT COALESCE(ss.has_unpublished, false)
                    AND m.n_spectra = COALESCE(ss.n_all, 0), false) AS use_stored,
           st.programs AS r_programs, st.n_targets AS r_n_targets,
           ss.gratings AS r_gratings, ss.n_spectra AS r_n_spectra,
           ss.max_snr AS r_max_snr, ss.max_exposure_time AS r_max_exposure_time
    FROM matched m
    LEFT JOIN scope_targets st ON st.object_id = m.id
    LEFT JOIN scope_spectra ss ON ss.object_id = m.id
  ),
  lists_agg AS (
    SELECT olm.object_id,
           jsonb_agg(ol.slug ORDER BY ol.slug) AS list_slugs
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT id FROM matched)
      AND (ol.created_by = p_user_id
           OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id))
    GROUP BY olm.object_id
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
  ),
  -- Tombstones: on the first incremental page (p_updated_since set, no
  -- cursor), the ids of in-scope objects that changed since the cursor and are
  -- no longer visible to this caller -- soft-deleted (is_active = false;
  -- apply_object_reconciliation stamps updated_at) or, for non-admins, left without a
  -- published spectrum (recompute_has_published_spectrum stamps updated_at on
  -- the flip). The client deletes them locally, so an incremental sync no
  -- longer leaves ghosts that force the next pull into a full resync. Only the
  -- integer ids travel: nothing about an unpublished object is disclosed, and
  -- an id the client never mirrored deletes nothing.
  deleted AS (
    SELECT COALESCE(array_agg(o.id ORDER BY o.id), '{}'::INTEGER[]) AS ids
    FROM objects o
    WHERE p_updated_since IS NOT NULL
      AND p_after_object_id IS NULL
      AND o.programs && p_program_slugs
      AND o.updated_at > p_updated_since
      AND NOT (o.is_active AND (p_include_unpublished OR o.has_published_spectrum))
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        -- Aggregates scoped to the caller's accessible programs so a sync that
        -- pulls a mixed-program object doesn't leak proprietary member metadata
        -- into the Python catalog. Same semantics as object_scoped_aggregates(),
        -- computed per page in scoped_aggs.
        'n_targets', CASE WHEN sa.use_stored THEN m.n_targets ELSE COALESCE(sa.r_n_targets, 0) END,
        'n_spectra', CASE WHEN sa.use_stored THEN m.n_spectra ELSE COALESCE(sa.r_n_spectra, 0) END,
        'programs', CASE WHEN sa.use_stored THEN m.programs ELSE COALESCE(sa.r_programs, '{}') END,
        'gratings', CASE WHEN sa.use_stored THEN m.gratings ELSE COALESCE(sa.r_gratings, '{}') END,
        'max_snr', CASE WHEN sa.use_stored THEN m.max_snr ELSE sa.r_max_snr END,
        'max_exposure_time', CASE WHEN sa.use_stored THEN m.max_exposure_time ELSE sa.r_max_exposure_time END,
        'redshift', m.redshift,
        'redshift_quality', m.redshift_quality,
        'redshift_inspected', m.redshift_inspected,
        'redshift_auto', m.redshift_auto,
        'inspected_used_auto', m.inspected_used_auto,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'last_data_change_at', m.last_data_change_at,
        'staleness_reason', m.staleness_reason,
        'version', m.version,
        'is_active', m.is_active,
        'has_photometry', m.has_photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(mt.target_ids, '[]'::jsonb),
        'spectra',           COALESCE(sp.spectra,    '[]'::jsonb),
        'lists',             COALESCE(la.list_slugs, '[]'::jsonb)
      )
      -- Keyset (#103): the client uses the LAST element's object_id as the next
      -- page's cursor, so the page array MUST be in object_id order. matched is
      -- ORDER BY object_id, but the LEFT JOINs below can reorder it, so pin the
      -- aggregate order explicitly.
      ORDER BY m.object_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id
  JOIN scoped_aggs              sa ON sa.object_id = m.id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT, TEXT[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT, TEXT[]) TO service_role;


-- =============================================================================
-- sync_snapshot_begin
-- (opens a nightly sync catalog snapshot build; /api/cron/sync-snapshot)
-- =============================================================================
-- Inserts the `building` row with the public programs the snapshot is built
-- for and its `started_at` watermark: the client's catch-up cursor after
-- loading the snapshot (`updated_at > started_at`), so every row the walk may
-- have read in an old state must have an updated_at after it.
--
-- now() alone is not that. Writers stamp updated_at with THEIR transaction's
-- start time, so a write whose transaction began before the build and
-- committed after the builder read its page is absent from the snapshot yet
-- has updated_at <= now(), and the catch-up would never return it. The
-- watermark is therefore the start of the oldest transaction still open
-- (read from pg_stat_activity, hence SECURITY DEFINER: postgres holds
-- pg_read_all_stats), minus a margin for transactions that start in the
-- instant between this read and the first page. A wider catch-up window only
-- re-sends rows the client already holds.
--
-- One build at a time: idx_sync_snapshots_one_building allows a single
-- `building` row, so a second concurrent call fails with unique_violation
-- (the cron route answers 409). A build still `building` 15 minutes after it
-- was created (by wall clock, created_at -- never the backdated started_at)
-- died without marking itself failed and is retired here first, so it cannot
-- block every later build.
CREATE OR REPLACE FUNCTION public.sync_snapshot_begin(p_format_version INTEGER)
RETURNS SETOF public.sync_snapshots
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
  UPDATE public.sync_snapshots
     SET status = 'failed', error = 'abandoned: still building after 15 minutes'
   WHERE status = 'building' AND created_at < now() - interval '15 minutes';

  INSERT INTO public.sync_snapshots (started_at, format_version, public_programs)
  SELECT LEAST(
           now(),
           COALESCE((SELECT min(a.xact_start)
                     FROM pg_stat_activity a
                     WHERE a.backend_type = 'client backend'
                       AND a.pid <> pg_backend_pid()
                       AND a.xact_start IS NOT NULL), now())
         ) - interval '5 minutes',
         p_format_version,
         COALESCE((SELECT array_agg(slug ORDER BY slug)
                   FROM public.programs WHERE is_public), '{}'::text[])
  RETURNING *;
$$;

REVOKE ALL ON FUNCTION public.sync_snapshot_begin(INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sync_snapshot_begin(INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.sync_snapshot_begin(INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.sync_snapshot_begin(INTEGER) TO service_role;


-- sync_deletions: journal of hard deletes from the five synced tables, so a
-- client that bootstrapped from a sync snapshot can drop rows deleted after
-- the snapshot was built (a hard delete leaves nothing for the catch-up walk
-- to return, and a photometry supersede or `deploy remove` deletes outright).
-- Filled by the journal_sync_deletions statement triggers, read through
-- get_sync_deletions, trimmed by the snapshot builder to the oldest snapshot
-- it keeps. Only integer ids: nothing about a deleted row is disclosed.
-- Service-role only, like sync_snapshots.
CREATE TABLE IF NOT EXISTS "public"."sync_deletions" (
    "id" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "stream" "text" NOT NULL,
    -- objects.id / spectra.id / storage_objects.id / object_photometry.id /
    -- spectrum_line_fits.spectrum_id -- the keys the client mirror uses.
    "row_id" bigint NOT NULL,
    -- The deleting transaction's now(): later than any snapshot watermark
    -- taken while it was open (see sync_snapshot_begin).
    "deleted_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "sync_deletions_stream_check" CHECK (("stream" = ANY (ARRAY['objects'::"text", 'spectra'::"text", 'storage'::"text", 'photometry'::"text", 'line_fits'::"text"])))
);


ALTER TABLE "public"."sync_deletions" OWNER TO "postgres";

REVOKE ALL ON TABLE "public"."sync_deletions" FROM "anon";
REVOKE ALL ON TABLE "public"."sync_deletions" FROM "authenticated";
GRANT ALL ON TABLE "public"."sync_deletions" TO "service_role";

ALTER TABLE public.sync_deletions ENABLE ROW LEVEL SECURITY;

-- get_sync_deletions reads "deleted since <watermark>"; the builder trims by age.
CREATE INDEX IF NOT EXISTS idx_sync_deletions_deleted_at
    ON public.sync_deletions USING btree (deleted_at);


-- =============================================================================
-- get_sync_deletions
-- (hard deletes since a sync snapshot's watermark; /api/v1/sync/deletions)
-- =============================================================================
-- The ids hard-deleted from each synced table after p_since, per stream,
-- minus any id that exists again now (a row re-created under the same id is
-- live, and the catch-up walk already brought it). Deliberately unscoped:
-- only integer ids travel, and an id the client never mirrored deletes
-- nothing -- the same contract as the sync RPCs' deleted_ids tombstones.
-- Service-role only.
CREATE OR REPLACE FUNCTION public.get_sync_deletions(p_since TIMESTAMPTZ)
RETURNS TABLE(stream TEXT, row_ids BIGINT[])
LANGUAGE sql STABLE
SET search_path = public, pg_catalog
AS $$
  SELECT d.stream, array_agg(DISTINCT d.row_id ORDER BY d.row_id)
  FROM public.sync_deletions d
  WHERE d.deleted_at > p_since
    AND NOT CASE d.stream
      WHEN 'objects' THEN EXISTS (SELECT 1 FROM public.objects o WHERE o.id = d.row_id)
      WHEN 'spectra' THEN EXISTS (SELECT 1 FROM public.spectra s WHERE s.id = d.row_id)
      WHEN 'storage' THEN EXISTS (SELECT 1 FROM public.storage_objects so WHERE so.id = d.row_id)
      WHEN 'photometry' THEN EXISTS (SELECT 1 FROM public.object_photometry p WHERE p.id = d.row_id)
      WHEN 'line_fits' THEN EXISTS (SELECT 1 FROM public.spectrum_line_fits f WHERE f.spectrum_id = d.row_id)
    END
  GROUP BY d.stream;
$$;

REVOKE ALL ON FUNCTION public.get_sync_deletions(TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_sync_deletions(TIMESTAMPTZ) FROM anon;
REVOKE ALL ON FUNCTION public.get_sync_deletions(TIMESTAMPTZ) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_sync_deletions(TIMESTAMPTZ) TO service_role;


-- =============================================================================
-- journal_sync_deletions: hard deletes from the synced tables -> sync_deletions
-- =============================================================================
-- Statement-level with a transition table, so a bulk delete (a photometry
-- supersede, `deploy remove`, a cascade from objects) journals in one INSERT.
-- TG_ARGV: (stream name, key column). SECURITY DEFINER: the deleting role may
-- be an admin session that cannot write the service-role-only journal.
CREATE OR REPLACE FUNCTION public.journal_sync_deletions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
BEGIN
    EXECUTE format(
        'INSERT INTO public.sync_deletions (stream, row_id) SELECT %L, %I FROM old_rows',
        TG_ARGV[0], TG_ARGV[1]);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.objects;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.objects REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('objects', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.spectra;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.spectra REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('spectra', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.storage_objects;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.storage_objects REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('storage', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.object_photometry;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.object_photometry REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('photometry', 'id');

DROP TRIGGER IF EXISTS journal_sync_deletions_trigger ON public.spectrum_line_fits;
CREATE TRIGGER journal_sync_deletions_trigger
  AFTER DELETE ON public.spectrum_line_fits REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.journal_sync_deletions('line_fits', 'spectrum_id');
