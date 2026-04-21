drop function if exists "public"."get_objects_for_sync"(p_program_slugs text[], p_user_id uuid, p_updated_since timestamp with time zone, p_limit integer, p_offset integer);

drop function if exists "public"."get_spectra_for_sync"(p_program_slugs text[], p_user_id uuid, p_updated_since timestamp with time zone, p_limit integer, p_offset integer);

-- Plain CREATE INDEX (not CONCURRENTLY): the Supabase CLI's migration runner
-- wraps each file in a transaction it owns, so a COMMIT/BEGIN sandwich around
-- CONCURRENTLY breaks the seed-file bookkeeping that runs after migrations.
-- The `objects` table is small enough (~tens of thousands of rows) that the
-- brief exclusive lock from a non-concurrent btree build is acceptable; if
-- the table grows large this can be split into its own post-deploy migration.
CREATE INDEX IF NOT EXISTS idx_objects_updated_at
    ON public.objects USING btree (updated_at);

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
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
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
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
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        'n_targets', m.n_targets,
        'n_spectra', m.n_spectra,
        'programs', m.programs,
        'gratings', m.gratings,
        'max_snr', m.max_snr,
        'max_exposure_time', m.max_exposure_time,
        'redshift', m.redshift,
        'redshift_quality', m.redshift_quality,
        'redshift_inspected', m.redshift_inspected,
        'redshift_auto', m.redshift_auto,
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
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true)
 RETURNS TABLE(spectra jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
BEGIN
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.fits_path, s.file_hash, s.file_size,
           s.signal_to_noise, s.exposure_time, s.reduction_version,
           s.redshift_auto, s.dq_flags,
           t.program_slug, t.observation, t.field,
           s.created_at, s.updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_updated_since IS NULL OR s.updated_at > p_updated_since)
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
        'reduction_version', m.reduction_version,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

-- NOTE: `supabase db diff` also emitted spurious drop/recreate blocks for
-- the materialized views (mv_filter_options, mv_programs_overview) and the
-- spectrum_flag_summary view. These are known migra false positives for
-- materialized views (see CLAUDE.md) and have been stripped — the actual
-- definitions in supabase/schemas/views.sql are unchanged by this PR.

-- Grant execution to the authenticated/service roles on the new signatures.
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(text[], uuid, timestamptz, integer, integer, boolean) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_objects_for_sync(text[], uuid, timestamptz, integer, integer, boolean) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(text[], uuid, timestamptz, integer, integer, boolean) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_spectra_for_sync(text[], uuid, timestamptz, integer, integer, boolean) TO service_role;

