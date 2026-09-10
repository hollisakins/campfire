-- Sync: tombstones in the incremental streams, a finals-only storage mirror,
-- and the statement_timeout backstop restored on the /api/v1/sync/* RPCs.
--
-- Background: `campfire sync` was failing with "canceling statement due to
-- statement timeout" on /sync/spectra. T2-F (#536, 2026-09-05) dropped the sync
-- RPCs' `SET statement_timeout = '120s'` as an OFFSET-era relic, so they fell
-- back to the role default: the routes call them as service_role, which has no
-- timeout of its own and inherits authenticator's 8 s. The exemption never only
-- covered deep OFFSET pages -- every stream's first page runs catalog-wide
-- COUNTs, and the client fires all five first pages concurrently.
--
-- What changes:
--
--   * get_objects_for_sync / get_spectra_for_sync / get_storage_objects_for_sync
--     RETURN a fourth column, deleted_ids: on the first incremental page (a
--     p_updated_since, no cursor) the integer ids of in-scope rows that changed
--     since the cursor and are no longer visible to the caller -- soft-deleted
--     objects, un-published spectra (and members of a soft-deleted object),
--     registry rows that left the active state or whose spectrum was
--     un-published. The client deletes them locally, so an incremental sync no
--     longer leaves ghosts that force `campfire pull` into a full resync. Only
--     ids travel; nothing about an unpublished row is disclosed. A return-type
--     change is not a CREATE OR REPLACE, so the three are dropped and recreated
--     (grants re-issued below).
--
--   * get_storage_objects_for_sync gains p_product_types / p_observations /
--     p_fields (NULL = unfiltered): the client mirrors only the product kinds it
--     can download (finals by default) and refreshes intermediates per
--     observation / field when `pull --intermediate` asks, instead of paging the
--     sidecar and intermediate rows that make up most of the registry.
--
--   * get_spectra_for_sync: revoked spectra never sync (they are the soft-deleted
--     state, tombstoned instead; admins opting in still get drafts); program
--     scope is tested on the row-local spectra.program_slug (trigger-owned copy
--     of the parent target's, #504) so the keyset walk needs no targets lookup
--     per rejected row; the two three-table count joins collapse into one pass
--     over spectra.
--
--   * bump_spectra_updated_at_trigger fires on deploy_status too: a publish must
--     enter a non-admin client's incremental delta (a draft inserted before the
--     client's cursor was otherwise never seen), and a revoke is what the
--     spectra / storage tombstones key on.
--
--   * recompute_has_published_spectrum writes only objects whose flag flips and
--     stamps updated_at on them (the object tombstone for non-admins keys on
--     it). Its objects_updated count is therefore flips, not members.
--
--   * All five sync RPCs (the three above plus get_photometry_for_sync and
--     get_line_fits_for_sync): SET statement_timeout = '120s' restored.
--
-- Hand-authored (no Docker on the authoring machine): every definition below is
-- copied verbatim from supabase/schemas/functions.sql and triggers.sql (source
-- of truth). Verified on a throwaway PostgreSQL 16: applied on top of a database
-- built from main's schema files it yields the same pg_get_functiondef /
-- pg_get_triggerdef as a database built from the branch's schema files, and the
-- tombstone scenarios (deactivate an object, revoke a spectrum, un-publish an
-- object's last spectrum, scope filters) behave as described on a ~96k-spectrum
-- synthetic catalog.

-- Return type / signature changes: drop before create.
DROP FUNCTION IF EXISTS public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT);
DROP FUNCTION IF EXISTS public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT);
DROP FUNCTION IF EXISTS public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, BIGINT);

-- ---------------------------------------------------------------------------
-- get_objects_for_sync
-- ---------------------------------------------------------------------------
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
  p_after_object_id TEXT DEFAULT NULL
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
    ORDER BY o.object_id
    LIMIT p_limit
  ),
  member_targets_agg AS (
    SELECT t.object_id,
           jsonb_agg(t.target_id ORDER BY t.target_id) AS target_ids
    FROM targets t
    WHERE t.object_id IN (SELECT id FROM matched)
      AND t.program_slug = ANY(p_program_slugs)
    GROUP BY t.object_id
  ),
  -- Phase D: per-spectrum payload (per design doc) so the Python client
  -- can render redshift_auto and dq_flags per grating without a second
  -- round-trip.
  spectra_agg AS (
    SELECT t.object_id,
           jsonb_agg(jsonb_build_object(
             'id', s.id,
             'target_id', s.target_id,
             'grating', s.grating,
             'signal_to_noise', s.signal_to_noise,
             'exposure_time', s.exposure_time,
             'redshift_auto', s.redshift_auto,
             'dq_flags', s.dq_flags
           ) ORDER BY s.target_id, s.grating) AS spectra
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    WHERE t.object_id IN (SELECT id FROM matched)
      AND t.program_slug = ANY(p_program_slugs)
      AND (p_include_unpublished OR s.deploy_status = 'published')
    GROUP BY t.object_id
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
  -- reconcile_objects stamps updated_at) or, for non-admins, left without a
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
        -- into the Python catalog. See object_scoped_aggregates().
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
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
  LEFT JOIN LATERAL public.object_scoped_aggregates(m.id, p_program_slugs, p_include_unpublished) sa ON true;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;

-- ---------------------------------------------------------------------------
-- get_spectra_for_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(
  p_program_slugs TEXT[],
  p_user_id UUID DEFAULT NULL,
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Keyset cursor (#103): the spectrum_id of the last row of the previous page,
  -- seeked via the idx_spectra_spectrum_id UNIQUE btree. See
  -- get_objects_for_sync for the design; keyset-only since T2-F (#511).
  p_after_spectrum_id TEXT DEFAULT NULL
)
RETURNS TABLE(spectra JSONB, total_count BIGINT, total_accessible_count BIGINT,
              deleted_ids INTEGER[])
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Timeout backstop for the first-page counts; see get_objects_for_sync.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.file_size,
           s.signal_to_noise, s.exposure_time,
           s.cfpipe_version, s.crds_context, s.jwst_version, s.date_obs, s.reduced_at,
           s.redshift_auto, s.dq_flags,
           -- program_slug / observation are the trigger-owned row-local copies
           -- of the parent target's (perf T2-A, #504), so they read off the
           -- spectra row; targets is joined only for field and the object link.
           s.program_slug, s.observation, t.field,
           s.created_at, s.updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    -- Program scope on the row-local column: the keyset walk down
    -- idx_spectra_spectrum_id rejects out-of-scope rows without fetching the
    -- target first (a user with a few programs used to pay a targets lookup
    -- for every spectrum walked past).
    WHERE s.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      -- B1: fail-closed publish gate (this RPC always bypasses RLS). Drafts
      -- sync only for admins opting in; revoked never syncs -- it is the
      -- soft-deleted state and is tombstoned below instead.
      AND (s.deploy_status = 'published'
           OR (p_include_unpublished AND s.deploy_status = 'draft'))
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: a spectrum tombstoned as
      -- a member of a soft-deleted object comes back when the object is
      -- reactivated (which stamps objects.updated_at) even though the
      -- spectrum row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      -- Keyset (#103): spectrum_id is UNIQUE (idx_spectra_spectrum_id), so a
      -- strict > needs no tiebreaker; keep the ordering column UNIQUE.
      AND (p_after_spectrum_id IS NULL OR s.spectrum_id > p_after_spectrum_id)
    ORDER BY s.spectrum_id
    LIMIT p_limit
  ),
  -- First-page counts, gated on p_include_counts (when FALSE the planner
  -- collapses the CTE to One-Time Filter: false). One pass over spectra
  -- yields both numbers: total_count (the incremental window) is a FILTER on
  -- the scan that produces total_accessible_count, and the soft-deleted-
  -- object exclusion is an anti-join against the (tiny, partial-indexed) set
  -- of inactive objects. Previously two spectra x targets x objects hash
  -- joins ran back to back on every first page.
  counts AS (
    SELECT COUNT(*) FILTER (WHERE p_updated_since IS NULL
                              OR s.updated_at > p_updated_since
                              -- same object clause as matched; the recently
                              -- changed objects are a small hashed set
                              OR s.target_id IN (
                                SELECT t.target_id
                                FROM targets t
                                JOIN objects o ON o.id = t.object_id
                                WHERE o.updated_at > p_updated_since)) AS total_cnt,
           COUNT(*) AS accessible_cnt
    FROM spectra s
    WHERE p_include_counts
      AND s.program_slug = ANY(p_program_slugs)
      AND (s.deploy_status = 'published'
           OR (p_include_unpublished AND s.deploy_status = 'draft'))
      AND NOT EXISTS (
        SELECT 1
        FROM targets t
        JOIN objects o ON o.id = t.object_id
        WHERE t.target_id = s.target_id
          AND o.is_active = false)
  ),
  -- Tombstones (see get_objects_for_sync): first incremental page only.
  deleted AS (
    SELECT COALESCE(array_agg(x.id ORDER BY x.id), '{}'::INTEGER[]) AS ids
    FROM (
      -- Un-published since the cursor: revoked for everyone, plus a draft an
      -- admin's mirror may carry. set_spectra_deploy_status's write reaches
      -- here because deploy_status is in bump_spectra_updated_at_trigger's
      -- column list.
      SELECT s.id
      FROM spectra s
      WHERE p_updated_since IS NOT NULL
        AND p_after_spectrum_id IS NULL
        AND s.program_slug = ANY(p_program_slugs)
        AND s.updated_at > p_updated_since
        AND NOT (s.deploy_status = 'published'
                 OR (p_include_unpublished AND s.deploy_status = 'draft'))
      UNION
      -- Members of an object soft-deleted since the cursor: the spectrum row
      -- itself did not change, so its own updated_at says nothing.
      SELECT s.id
      FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      JOIN objects o ON o.id = t.object_id
      WHERE p_updated_since IS NOT NULL
        AND p_after_spectrum_id IS NULL
        AND s.program_slug = ANY(p_program_slugs)
        AND o.is_active = false
        AND o.updated_at > p_updated_since
    ) x
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'spectrum_id', m.spectrum_id,
        'target_id', m.target_id,
        'object_id', m.object_id,
        'grating', m.grating,
        'fits_path', m.fits_path,
        'file_hash', m.file_hash,
        'file_size', m.file_size,
        'signal_to_noise', m.signal_to_noise,
        'exposure_time', m.exposure_time,
        'cfpipe_version', m.cfpipe_version,
        'crds_context', m.crds_context,
        'jwst_version', m.jwst_version,
        'date_obs', m.date_obs,
        'reduced_at', m.reduced_at,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be spectrum_id-ordered (client cursors on
      -- the last element). matched has no post-ORDER joins, but pin it anyway.
      ORDER BY m.spectrum_id
    ), '[]'::jsonb),
    COALESCE((SELECT c.total_cnt FROM counts c), 0)::BIGINT,
    COALESCE((SELECT c.accessible_cnt FROM counts c), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;

-- ---------------------------------------------------------------------------
-- get_photometry_for_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_photometry_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_unpublished BOOLEAN DEFAULT false,
  -- Count gating (#103): only the keyset first page needs the count; skip the
  -- COUNT(*) scan on every subsequent page, matching the other /sync/* RPCs.
  p_include_counts BOOLEAN DEFAULT TRUE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the object_photometry PK btree. Keyset-only since T2-F (#511).
  p_after_id INTEGER DEFAULT NULL
)
RETURNS TABLE(photometry_records JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Timeout backstop for the first-page counts; see get_objects_for_sync.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT op.id, o.object_id, op.field, op.catalog_name, op.catalog_id,
           op.match_distance_arcsec, op.photometry, op.photo_z,
           op.photo_z_err_lo, op.photo_z_err_hi, op.has_pz,
           op.created_at, op.updated_at
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE o.programs && p_program_slugs
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR o.has_published_spectrum)
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: photometry deleted along
      -- with a tombstoned object (un-published, or soft-deleted) comes back
      -- when the object does -- both flips stamp objects.updated_at -- even
      -- though the photometry row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR op.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      -- Keyset (#103): op.id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR op.id > p_after_id)
    ORDER BY op.id
    LIMIT p_limit
  ),
  -- Count CTE gated on p_include_counts; when FALSE the planner collapses it to
  -- One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM object_photometry op
    JOIN objects o ON o.id = op.object_id
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND (p_include_unpublished OR o.has_published_spectrum)
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: photometry deleted along
      -- with a tombstoned object (un-published, or soft-deleted) comes back
      -- when the object does -- both flips stamp objects.updated_at -- even
      -- though the photometry row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR op.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'catalog_name', m.catalog_name,
        'catalog_id', m.catalog_id,
        'match_distance_arcsec', m.match_distance_arcsec,
        'photometry', m.photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'has_pz', m.has_pz,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO service_role;

-- ---------------------------------------------------------------------------
-- get_line_fits_for_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_line_fits_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_unpublished BOOLEAN DEFAULT false,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_after_id INTEGER DEFAULT NULL
)
RETURNS TABLE(line_fit_records JSONB, total_count BIGINT)
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Timeout backstop for the first-page counts; see get_objects_for_sync.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation,
           s.spectrum_id AS spectrum_name, t.field, o.object_id AS current_object_id,
           f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version,
           f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source,
           f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.lines,
           f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.fitted_at,
           f.created_at, f.updated_at,
           -- The record's sync timestamp: the latest of the fit row and the two
           -- parent rows its staleness flags derive from, so the client's
           -- incremental cursor (max local updated_at) advances past a
           -- re-inspection or redeploy instead of re-sending the row forever.
           GREATEST(f.updated_at, s.updated_at, o.updated_at) AS effective_updated_at,
           -- staleness vs the live inspection state (see spectrum_line_fits_status)
           (o.id IS NOT NULL AND (
              o.version IS DISTINCT FROM f.object_version
              OR o.redshift_quality IS DISTINCT FROM f.z_quality
              OR o.redshift IS NULL
              OR abs((o.redshift)::double precision - f.z_used) > 1e-5)) AS stale_redshift,
           -- both sides are 'sha256:<hex>'; strip the scheme defensively so a
           -- bare digest from an older product still compares.
           (regexp_replace(s.file_hash, '^sha256:', '')
              IS DISTINCT FROM regexp_replace(f.spectrum_hash, '^sha256:', '')) AS stale_spectrum
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE f.program_slug = ANY(p_program_slugs)
      -- Hide fits whose parent object was soft-deleted (same guard as
      -- get_spectra_for_sync), so the line catalog never carries a row the
      -- spectra / objects streams hide.
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      -- Incremental: stale_redshift / stale_spectrum are derived from the
      -- parent rows, so a re-inspected object or a redeployed spectrum must
      -- re-send the fit even though the fit row itself did not change.
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      AND (p_after_id IS NULL OR f.spectrum_id > p_after_id)
    ORDER BY f.spectrum_id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM spectrum_line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND f.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'spectrum_id', m.spectrum_id,
        'spectrum_name', m.spectrum_name,
        'target_id', m.target_id,
        'grating', m.grating,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'current_object_id', m.current_object_id,
        'z_used', m.z_used,
        'z_source', m.z_source,
        'z_quality', m.z_quality,
        'object_id', m.object_id,
        'object_version', m.object_version,
        'z_fit', m.z_fit,
        'z_fit_err', m.z_fit_err,
        'dv', m.dv,
        'dv_err', m.dv_err,
        'sigma_v', m.sigma_v,
        'sigma_v_err', m.sigma_v_err,
        'kin_source', m.kin_source,
        'n_lines', m.n_lines,
        'n_detected', m.n_detected,
        'n_broad', m.n_broad,
        'chi2', m.chi2,
        'dof', m.dof,
        'lines', m.lines,
        'fit_version', m.fit_version,
        'cfpipe_version', m.cfpipe_version,
        'f_lsf', m.f_lsf,
        'spectrum_hash', m.spectrum_hash,
        'fitted_at', m.fitted_at,
        'stale_redshift', m.stale_redshift,
        'stale_spectrum', m.stale_spectrum,
        'created_at', m.created_at,
        'fit_updated_at', m.updated_at,
        'updated_at', m.effective_updated_at
      ) ORDER BY m.spectrum_id
    ), '[]'::jsonb) AS line_fit_records,
    (SELECT cnt FROM total) AS total_count
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_line_fits_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_line_fits_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO service_role;

-- ---------------------------------------------------------------------------
-- get_storage_objects_for_sync
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(
  p_program_slugs TEXT[],
  p_updated_since TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 1000,
  p_include_counts BOOLEAN DEFAULT TRUE,
  p_include_unpublished BOOLEAN DEFAULT FALSE,
  -- Keyset cursor (#103): the id of the last row of the previous page, seeked
  -- via the storage_objects_pkey btree. storage_key is NOT usable as a cursor
  -- (only UNIQUE as (backend, bucket, storage_key)), and sync order is
  -- irrelevant to the client (it upserts by key), so this orders by the PK.
  -- Keyset-only since T2-F (#511): the scope predicate (published EXISTS
  -- checks) is evaluated on only ~p_limit rows past the seek.
  p_after_id BIGINT DEFAULT NULL,
  -- Mirror slimming: the client mirrors only the product kinds it can
  -- download (finals by default) and refreshes intermediates per observation /
  -- field when `pull --intermediate` asks for them, so it never pages through
  -- the sidecar and intermediate rows that make up most of the registry.
  -- NULL = unfiltered (the admin full mirror). observation / field scope is a
  -- union, mirroring the client's pending-object query.
  p_product_types TEXT[] DEFAULT NULL,
  p_observations TEXT[] DEFAULT NULL,
  p_fields TEXT[] DEFAULT NULL
)
RETURNS TABLE(objects JSONB, total_count BIGINT, total_accessible_count BIGINT,
              deleted_ids BIGINT[])
LANGUAGE plpgsql STABLE
SET plan_cache_mode = 'force_custom_plan'
-- Timeout backstop for the first-page counts; see get_objects_for_sync.
SET statement_timeout = '120s'
AS $$
BEGIN
  RETURN QUERY
  -- `scoped` carries the full published-scope filter and feeds the count CTEs
  -- only (gated on p_include_counts, so it is skipped entirely on keyset pages
  -- 2+). `matched` re-states the SAME scope inline against the base table so its
  -- keyset seek uses the PK index instead of reading a materialized full scan.
  -- The two copies must stay in sync (same house pattern as get_objects_for_sync's
  -- matched vs. total/accessible).
  WITH scoped AS (
    SELECT so.updated_at, so.spectrum_id
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
      AND ((p_observations IS NULL AND p_fields IS NULL)
           OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
           OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                -- when published. NIRSpec obs deploy stays program-scoped.
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
  ),
  matched AS MATERIALIZED (
    SELECT so.*
    FROM storage_objects so
    WHERE so.status = 'active'
      AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
      AND ((p_observations IS NULL AND p_fields IS NULL)
           OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
           OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      AND (
        p_include_unpublished
        OR (so.spectrum_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM spectra s
              JOIN targets t ON t.target_id = s.target_id
              WHERE s.spectrum_id = so.spectrum_id
                AND s.deploy_status = 'published'
                AND t.program_slug = ANY(p_program_slugs)))
        OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM deployments d
              LEFT JOIN observations o ON o.name = d.observation
              WHERE d.id = so.deployment_id
                AND d.status = 'published'
                AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
      )
      -- Incremental: the row itself changed, or its spectrum did. The
      -- spectrum clause is the resurrection path for non-admins: rows
      -- tombstoned when their spectrum was un-published come back on
      -- republish (which stamps spectra.updated_at) even though the registry
      -- row did not change. A semi-join, not a per-row EXISTS: the recently
      -- changed spectra are a small hashed set. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR so.updated_at > p_updated_since
           OR so.spectrum_id IN (SELECT s2.spectrum_id FROM spectra s2
                                 WHERE s2.updated_at > p_updated_since))
      -- Keyset (#103): id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR so.id > p_after_id)
    ORDER BY so.id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL
           OR scoped.updated_at > p_updated_since
           OR scoped.spectrum_id IN (SELECT s2.spectrum_id FROM spectra s2
                                     WHERE s2.updated_at > p_updated_since))
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  ),
  -- Tombstones (see get_objects_for_sync): first incremental page only. Rows
  -- that left the active state, and active rows whose spectrum was
  -- un-published (invisible to non-admins from now on; the spectrum's
  -- updated_at moves on a deploy_status change). Program-scoped like the
  -- sibling CTEs: a non-admin's tombstones are drawn from rows whose
  -- observation (or spectrum) is in an accessible program, plus field-only
  -- products, which the main scope shows to everyone when published. Only
  -- integer ids travel, and an id the client never mirrored deletes nothing.
  -- Hard deletes (deploy remove) still need a full sync to clear.
  deleted AS (
    SELECT COALESCE(array_agg(x.id ORDER BY x.id), '{}'::BIGINT[]) AS ids
    FROM (
      SELECT so.id
      FROM storage_objects so
      WHERE p_updated_since IS NOT NULL
        AND p_after_id IS NULL
        AND so.status <> 'active'
        AND so.updated_at > p_updated_since
        AND (p_include_unpublished
             OR (so.observation IS NULL AND so.field IS NOT NULL)
             OR EXISTS (SELECT 1 FROM observations ob
                        WHERE ob.name = so.observation
                          AND ob.program_slug = ANY(p_program_slugs)))
        AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
        AND ((p_observations IS NULL AND p_fields IS NULL)
             OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
             OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
      UNION
      SELECT so.id
      FROM storage_objects so
      JOIN spectra s ON s.spectrum_id = so.spectrum_id
      WHERE p_updated_since IS NOT NULL
        AND p_after_id IS NULL
        AND NOT p_include_unpublished
        AND so.status = 'active'
        AND s.program_slug = ANY(p_program_slugs)
        AND s.deploy_status <> 'published'
        AND s.updated_at > p_updated_since
        AND (p_product_types IS NULL OR so.product_type = ANY(p_product_types))
        AND ((p_observations IS NULL AND p_fields IS NULL)
             OR so.observation = ANY(COALESCE(p_observations, '{}'::TEXT[]))
             OR so.field = ANY(COALESCE(p_fields, '{}'::TEXT[])))
    ) x
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'sci_dq_hash', m.sci_dq_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'filter', m.filter,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, BIGINT, TEXT[], TEXT[], TEXT[]) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, BOOLEAN, BOOLEAN, BIGINT, TEXT[], TEXT[], TEXT[]) TO service_role;

-- ---------------------------------------------------------------------------
-- recompute_has_published_spectrum
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.recompute_has_published_spectrum(
  p_target_ids text[] DEFAULT NULL,
  p_field text DEFAULT NULL
)
RETURNS json
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_is_admin boolean;
  v_targets text[];
  v_n_targets int := 0;
  v_n_objects int := 0;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_target_ids IS NOT NULL THEN
    v_targets := p_target_ids;
  ELSIF p_field IS NOT NULL THEN
    SELECT array_agg(t.target_id) INTO v_targets FROM targets t WHERE t.field = p_field;
  ELSE
    RAISE EXCEPTION 'recompute_has_published_spectrum requires p_target_ids or p_field';
  END IF;

  IF v_targets IS NULL OR array_length(v_targets, 1) IS NULL THEN
    RETURN json_build_object('targets_updated', 0, 'objects_updated', 0);
  END IF;

  UPDATE targets t
  SET has_published_spectrum = EXISTS (
        SELECT 1 FROM spectra s
        WHERE s.target_id = t.target_id AND s.deploy_status = 'published'
      )
  WHERE t.target_id = ANY(v_targets);
  GET DIAGNOSTICS v_n_targets = ROW_COUNT;

  -- Only rows whose flag actually flips are written, and the write stamps
  -- updated_at: the flip changes what a non-admin sync client may see, and
  -- get_objects_for_sync's incremental delta (and its tombstones) keys on
  -- updated_at. objects_updated therefore counts flips, not members.
  UPDATE objects o
  SET has_published_spectrum = v.pub,
      updated_at = now()
  FROM (
    SELECT o2.id,
           EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o2.id AND t.has_published_spectrum) AS pub
    FROM objects o2
    WHERE o2.id IN (
      SELECT DISTINCT t2.object_id FROM targets t2
      WHERE t2.target_id = ANY(v_targets) AND t2.object_id IS NOT NULL
    )
  ) v
  WHERE o.id = v.id
    AND o.has_published_spectrum IS DISTINCT FROM v.pub;
  GET DIAGNOSTICS v_n_objects = ROW_COUNT;

  RETURN json_build_object('targets_updated', v_n_targets, 'objects_updated', v_n_objects);
END;
$$;

GRANT EXECUTE ON FUNCTION public.recompute_has_published_spectrum(text[], text) TO authenticated, service_role;

-- ---------------------------------------------------------------------------
-- bump_spectra_updated_at_trigger: + deploy_status
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS bump_spectra_updated_at_trigger ON public.spectra;
CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE OF
    dq_flags,
    redshift_auto,
    signal_to_noise,
    thumbnail_svg_fnu,
    thumbnail_svg_flambda,
    fits_path,
    file_hash,
    -- zfit scalars on the row (perf T2-D2, #508): written by deploy and the
    -- backfill; a sync client showing the fit summary must see them change.
    chi2_min,
    confidence,
    -- Publication state: a publish must enter a non-admin client's incremental
    -- delta (a draft inserted before its sync cursor was otherwise never
    -- seen), and a revoke is what get_spectra_for_sync tombstones.
    deploy_status
  ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();
