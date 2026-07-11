-- Keyset pagination for the /sync/* catalog RPCs (issue #103).
--
-- OFFSET pagination is linear in offset: each page scans+orders offset+limit
-- rows, so deep --full-sync pages grew until they tipped past the service_role
-- statement timeout (~page 29 of a 30k-object catalog). These four RPCs scan
-- forward only, so each gains a keyset cursor (p_after_*) on its unique,
-- btree-indexed ordering column, making each page O(log N + limit):
--   get_objects_for_sync         -> objects.object_id      (objects_object_id_key)
--   get_spectra_for_sync         -> spectra.spectrum_id    (idx_spectra_spectrum_id)
--   get_photometry_for_sync      -> object_photometry.id   (pkey)  [+ p_include_counts gating]
--   get_storage_objects_for_sync -> storage_objects.id     (pkey; storage_key is not
--                                   uniquely constrained alone, and its scope is
--                                   restated inline so the page seeks the index)
--
-- Each final jsonb_agg is ORDER BY'd on the cursor column so the returned page
-- is cursor-ordered (the client cursors on the last element).
--
-- p_offset and the 120s statement_timeout are retained so old offset-based
-- clients keep working; both can be dropped in a follow-up once keyset clients
-- are the norm. Old function signatures are dropped so no stale overload lingers.

drop function if exists "public"."get_objects_for_sync"(p_program_slugs text[], p_user_id uuid, p_updated_since timestamp with time zone, p_limit integer, p_offset integer, p_include_counts boolean, p_include_unpublished boolean);

drop function if exists "public"."get_photometry_for_sync"(p_program_slugs text[], p_updated_since timestamp with time zone, p_limit integer, p_offset integer, p_include_unpublished boolean);

drop function if exists "public"."get_spectra_for_sync"(p_program_slugs text[], p_user_id uuid, p_updated_since timestamp with time zone, p_limit integer, p_offset integer, p_include_counts boolean, p_include_unpublished boolean);

drop function if exists "public"."get_storage_objects_for_sync"(p_program_slugs text[], p_updated_since timestamp with time zone, p_limit integer, p_offset integer, p_include_counts boolean, p_include_unpublished boolean);
set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true, p_include_unpublished boolean DEFAULT false, p_after_object_id text DEFAULT NULL::text)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
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
    LIMIT p_limit OFFSET p_offset
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
           OR ol.visibility IN ('public_read', 'public_edit'))
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
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id
  LEFT JOIN LATERAL public.object_scoped_aggregates(m.id, p_program_slugs, p_include_unpublished) sa ON true;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_photometry_for_sync(p_program_slugs text[], p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_unpublished boolean DEFAULT false, p_include_counts boolean DEFAULT true, p_after_id integer DEFAULT NULL::integer)
 RETURNS TABLE(photometry_records jsonb, total_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
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
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
      -- Keyset (#103): op.id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR op.id > p_after_id)
    ORDER BY op.id
    LIMIT p_limit OFFSET p_offset
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
      AND (p_updated_since IS NULL OR op.updated_at > p_updated_since)
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
$function$
;

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true, p_include_unpublished boolean DEFAULT false, p_after_spectrum_id text DEFAULT NULL::text)
 RETURNS TABLE(spectra jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
BEGIN
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.file_size,
           s.signal_to_noise, s.exposure_time,
           s.cfpipe_version, s.crds_context, s.jwst_version, s.date_obs, s.reduced_at,
           s.redshift_auto, s.dq_flags,
           t.program_slug, t.observation, t.field,
           s.created_at, s.updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
      -- Keyset (#103): spectrum_id is UNIQUE (idx_spectra_spectrum_id), so a
      -- strict > needs no tiebreaker; keep the ordering column UNIQUE.
      AND (p_after_spectrum_id IS NULL OR s.spectrum_id > p_after_spectrum_id)
    ORDER BY s.spectrum_id
    LIMIT p_limit OFFSET p_offset
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
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
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(p_program_slugs text[], p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true, p_include_unpublished boolean DEFAULT false, p_after_id bigint DEFAULT NULL::bigint)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
BEGIN
  RETURN QUERY
  -- `scoped` carries the full published-scope filter and feeds the count CTEs
  -- only (gated on p_include_counts, so it is skipped entirely on keyset pages
  -- 2+). `matched` re-states the SAME scope inline against the base table so its
  -- keyset seek uses the PK index instead of reading a materialized full scan.
  -- The two copies must stay in sync (same house pattern as get_objects_for_sync's
  -- matched vs. total/accessible).
  WITH scoped AS (
    SELECT so.updated_at
    FROM storage_objects so
    WHERE so.status = 'active'
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
      AND (p_updated_since IS NULL OR so.updated_at > p_updated_since)
      -- Keyset (#103): id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR so.id > p_after_id)
    ORDER BY so.id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
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
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

-- Grants for the new signatures (the dropped overloads did not carry them
-- forward). Redundant with public default privileges but stated explicitly to
-- mirror supabase/schemas/functions.sql.
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(TEXT[], UUID, TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_photometry_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, BIGINT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_storage_objects_for_sync(TEXT[], TIMESTAMPTZ, INTEGER, INTEGER, BOOLEAN, BOOLEAN, BIGINT) TO service_role;
