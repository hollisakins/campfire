drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."nircam_reduction_progress";

drop view if exists "public"."spectrum_flag_summary";

alter table "public"."nircam_exposures" add column "full_png_path" text;

alter table "public"."nircam_exposures" add column "image_height" integer;

alter table "public"."nircam_exposures" add column "image_width" integer;

alter table "public"."nircam_exposures" add column "mask_regions" jsonb;

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.enforce_object_user_update_scope()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    -- Service role (no JWT) and admins can write any column.
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    -- Non-admin users may only touch the inspection set:
    --   redshift_inspected, redshift_quality, last_inspected_at,
    --   last_inspected_by. version and updated_at are maintained by sibling
    --   triggers; we explicitly allow them to change so this trigger
    --   doesn't reject writes that went through the legitimate path.
    IF OLD.object_id IS DISTINCT FROM NEW.object_id
       OR OLD.field IS DISTINCT FROM NEW.field
       OR OLD.ra IS DISTINCT FROM NEW.ra
       OR OLD.dec IS DISTINCT FROM NEW.dec
       OR OLD.n_targets IS DISTINCT FROM NEW.n_targets
       OR OLD.n_spectra IS DISTINCT FROM NEW.n_spectra
       OR OLD.programs IS DISTINCT FROM NEW.programs
       OR OLD.gratings IS DISTINCT FROM NEW.gratings
       OR OLD.observations IS DISTINCT FROM NEW.observations
       OR OLD.max_snr IS DISTINCT FROM NEW.max_snr
       OR OLD.max_exposure_time IS DISTINCT FROM NEW.max_exposure_time
       OR OLD.photo_z IS DISTINCT FROM NEW.photo_z
       OR OLD.photo_z_err_lo IS DISTINCT FROM NEW.photo_z_err_lo
       OR OLD.photo_z_err_hi IS DISTINCT FROM NEW.photo_z_err_hi
       OR OLD.has_photometry IS DISTINCT FROM NEW.has_photometry
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.last_data_change_at IS DISTINCT FROM NEW.last_data_change_at
       OR OLD.staleness_reason IS DISTINCT FROM NEW.staleness_reason
       OR OLD.inspected_used_auto IS DISTINCT FROM NEW.inspected_used_auto
       OR OLD.is_active IS DISTINCT FROM NEW.is_active
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
        RAISE EXCEPTION 'Non-admin updates to objects may only change inspection fields (redshift_inspected, redshift_quality, last_inspected_at, last_inspected_by)'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_needs_review boolean DEFAULT NULL::boolean, p_list_ids integer[] DEFAULT NULL::integer[], p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_has_photometry boolean DEFAULT NULL::boolean, p_photo_z_min double precision DEFAULT NULL::double precision, p_photo_z_max double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'object_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT 1, p_page_size integer DEFAULT 50)
 RETURNS TABLE(targets jsonb, total_count bigint, page integer, page_size integer)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

  -- Intersect user-accessible programs with filter selection
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;

  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size;
    RETURN;
  END IF;

  -- Step 1: count
  SELECT COUNT(*) INTO v_total_count
  FROM objects o
  WHERE
    -- Access control: object must have at least one accessible program
    o.programs && v_filtered_program_slugs
    AND o.is_active = true
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
      OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
    )
    AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
    )
    AND (
      p_needs_review IS NULL
      OR (p_needs_review = TRUE
          AND o.staleness_reason IS NOT NULL
          AND o.last_inspected_at IS NOT NULL
          AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
      OR (p_needs_review = FALSE
          AND (o.staleness_reason IS NULL
               OR o.last_inspected_at IS NULL
               OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
    )
    AND (
      NOT v_coord_search_active
      OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    )
    AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
        SELECT olm.object_id FROM object_list_members olm
        WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
    ))
    AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
    AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
    AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max);

  -- Step 2: fetch page
  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.id,
      o.object_id,
      o.field,
      o.ra,
      o.dec,
      o.n_targets,
      o.n_spectra,
      o.programs,
      o.gratings,
      o.max_snr,
      o.max_exposure_time,
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.redshift_auto,
      o.inspected_used_auto,
      o.last_inspected_at,
      o.last_inspected_by,
      o.last_data_change_at,
      o.staleness_reason,
      o.version,
      o.is_active,
      o.photo_z,
      o.has_photometry,
      o.created_at,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM objects o
    WHERE
      o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observations && p_observations)
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at)))
      )
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
          AND 2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          ))) <= p_radius_degrees
        )
      )
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
          POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
        ))) END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN o.object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN o.object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN o.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN o.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN o.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN o.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN o.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN o.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN o.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN o.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN o.redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN o.redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN o.photo_z END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN o.photo_z END DESC NULLS LAST,
      o.object_id ASC
    LIMIT p_page_size OFFSET v_offset
  ),
  with_members AS (
    SELECT
      jsonb_build_object(
        'id', fo.id,
        'object_id', fo.object_id,
        'field', fo.field,
        'ra', fo.ra,
        'dec', fo.dec,
        'n_targets', fo.n_targets,
        'n_spectra', fo.n_spectra,
        'programs', fo.programs,
        'gratings', fo.gratings,
        'max_snr', fo.max_snr,
        'max_exposure_time', fo.max_exposure_time,
        'redshift', fo.redshift,
        'redshift_quality', fo.redshift_quality,
        'redshift_inspected', fo.redshift_inspected,
        'redshift_auto', fo.redshift_auto,
        'inspected_used_auto', fo.inspected_used_auto,
        'last_inspected_at', fo.last_inspected_at,
        'last_inspected_by', fo.last_inspected_by,
        'last_data_change_at', fo.last_data_change_at,
        'staleness_reason', fo.staleness_reason,
        'version', fo.version,
        'is_active', fo.is_active,
        'photo_z', fo.photo_z,
        'has_photometry', fo.has_photometry,
        'created_at', fo.created_at,
        'distance', fo.distance,
        -- Phase D: member_targets becomes provenance only (target_id, program,
        -- observation). Inspection state lives on the object now; redshift_auto
        -- on targets is retained for transitional UI display until Phase E.
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift_auto', t.redshift_auto
            )
          )
          FROM targets t
          WHERE t.object_id = fo.id
            AND t.program_slug = ANY(v_filtered_program_slugs)
          ),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', ol.id,
              'name', ol.name,
              'slug', ol.slug,
              'icon', ol.icon,
              'color', ol.color
            ) ORDER BY ol.name
          )
          FROM object_list_members olm
          JOIN object_lists ol ON ol.id = olm.list_id
          WHERE olm.object_id = fo.id),
          '[]'::jsonb
        )
      ) AS obj_json
    FROM filtered_objects fo
  )
  SELECT
    COALESCE(jsonb_agg(wm.obj_json), '[]'::jsonb),
    v_total_count,
    p_page,
    p_page_size
  FROM with_members wm;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true)
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

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, target_count bigint, spectrum_count bigint, total_size_bytes bigint, pointings jsonb, reduction_version text, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, deployed_by_username text, deployed_by_full_name text, n_patches_since_full integer, last_patch_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  WITH stats AS (
    SELECT t.observation, t.program_slug, p.program_name, t.field,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
    FROM targets t
    JOIN programs p ON p.slug = t.program_slug
    LEFT JOIN spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug, p.program_name, t.field
  )
  SELECT s.observation, s.program_slug, s.program_name, s.field,
    s.target_count, s.spectrum_count, s.total_size_bytes,
    o.pointings,
    full_dep.reduction_version, full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.reduction_version, d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = s.observation AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = s.observation
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  ORDER BY s.observation;
$function$
;

create materialized view "public"."mv_filter_options" as  SELECT 1 AS id,
    ARRAY( SELECT DISTINCT targets.field
           FROM public.targets
          ORDER BY targets.field) AS fields,
    ARRAY( SELECT DISTINCT targets.observation
           FROM public.targets
          WHERE (targets.observation IS NOT NULL)
          ORDER BY targets.observation) AS observations,
    ARRAY( SELECT DISTINCT spectra.grating
           FROM public.spectra
          ORDER BY spectra.grating) AS gratings;


create materialized view "public"."mv_programs_overview" as  SELECT p.slug,
    p.program_name,
    p.pi_name,
    p.description,
    p.is_public,
    p.cycle,
    COALESCE(stats.target_count, (0)::bigint) AS target_count,
    COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
    COALESCE(stats.observations, ARRAY[]::text[]) AS observations,
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids,
    COALESCE(pids.n_observations, (0)::bigint) AS n_observations,
    last_red.last_reduced_at
   FROM (((public.programs p
     LEFT JOIN ( SELECT t.program_slug,
            count(DISTINCT t.target_id) AS target_count,
            array_agg(DISTINCT s.grating ORDER BY s.grating) FILTER (WHERE (s.grating IS NOT NULL)) AS gratings,
            array_agg(DISTINCT t.field ORDER BY t.field) AS fields,
            array_agg(DISTINCT t.observation ORDER BY t.observation) AS observations
           FROM (public.targets t
             LEFT JOIN public.spectra s ON ((s.target_id = t.target_id)))
          GROUP BY t.program_slug) stats ON ((p.slug = stats.program_slug)))
     LEFT JOIN ( SELECT observations.program_slug,
            array_agg(DISTINCT observations.jwst_program_id ORDER BY observations.jwst_program_id) AS jwst_pids,
            count(*) AS n_observations
           FROM public.observations
          GROUP BY observations.program_slug) pids ON ((p.slug = pids.program_slug)))
     LEFT JOIN ( SELECT o.program_slug,
            max(d.reduced_at) AS last_reduced_at
           FROM (public.observations o
             JOIN public.deployments d ON ((d.observation = o.name)))
          WHERE (d.source_ids_filter IS NULL)
          GROUP BY o.program_slug) last_red ON ((p.slug = last_red.program_slug)));


create or replace view "public"."nircam_reduction_progress" as  SELECT field,
    filter,
    count(*) AS total,
    count(*) FILTER (WHERE (stage = 'uncal'::text)) AS at_uncal,
    count(*) FILTER (WHERE (stage = 'detector1'::text)) AS at_detector1,
    count(*) FILTER (WHERE (stage = 'persistence'::text)) AS at_persistence,
    count(*) FILTER (WHERE (stage = 'wisp'::text)) AS at_wisp,
    count(*) FILTER (WHERE (stage = 'striping'::text)) AS at_striping,
    count(*) FILTER (WHERE (stage = 'image2'::text)) AS at_image2,
    count(*) FILTER (WHERE (stage = 'edge'::text)) AS at_edge,
    count(*) FILTER (WHERE (stage = 'sky'::text)) AS at_sky,
    count(*) FILTER (WHERE (stage = 'diag_striping'::text)) AS at_diag_striping,
    count(*) FILTER (WHERE (stage = 'variance'::text)) AS at_variance,
    count(*) FILTER (WHERE (stage = 'wcs_shift'::text)) AS at_wcs_shift,
    count(*) FILTER (WHERE (stage = 'preview'::text)) AS at_preview,
    count(*) FILTER (WHERE (stage = 'jhat'::text)) AS at_jhat,
    count(*) FILTER (WHERE (stage = 'apply_mask'::text)) AS at_apply_mask,
    count(*) FILTER (WHERE (stage = 'bad_pixel'::text)) AS at_bad_pixel,
    count(*) FILTER (WHERE (stage = 'outlier'::text)) AS at_outlier,
    count(*) FILTER (WHERE (review_status = 'pending'::text)) AS pending_review,
    count(*) FILTER (WHERE (review_status = 'approved'::text)) AS approved,
    count(*) FILTER (WHERE (review_status = 'excluded'::text)) AS excluded,
    count(*) FILTER (WHERE (masking = 'needed'::text)) AS needs_masking,
    count(*) FILTER (WHERE (correction = 'needed'::text)) AS needs_correction
   FROM public.nircam_exposures
  GROUP BY field, filter;


CREATE OR REPLACE FUNCTION public.pin_redshift_on_signoff()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.redshift_quality >= 2 THEN
        IF NEW.redshift_inspected IS NULL AND NEW.redshift_auto IS NOT NULL THEN
            -- Implicit sign-off: pin to the current auto-fit so reprocessing
            -- can't silently move the displayed redshift.
            NEW.redshift_inspected := NEW.redshift_auto::numeric;
            NEW.inspected_used_auto := true;
        ELSIF TG_OP = 'UPDATE'
              AND NEW.redshift_inspected IS NOT NULL
              AND NEW.redshift_inspected IS DISTINCT FROM OLD.redshift_inspected THEN
            -- Explicit override (newly typed or changed): clear the auto flag.
            NEW.inspected_used_auto := false;
        ELSIF TG_OP = 'INSERT' AND NEW.redshift_inspected IS NOT NULL THEN
            -- Initial insert with an explicit override.
            NEW.inspected_used_auto := false;
        END IF;
    ELSE
        -- quality < 2: object is uninspected or Impossible. Drop the pin so
        -- redshift_inspected reflects "no user override" again. The generated
        -- redshift column handles Impossible (quality=1 → NULL) on its own.
        NEW.redshift_inspected := NULL;
        NEW.inspected_used_auto := false;
    END IF;
    RETURN NEW;
END;
$function$
;

create or replace view "public"."spectrum_flag_summary" as  SELECT s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((s.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.spectra s
     CROSS JOIN public.flag_definitions fd)
  GROUP BY s.id, s.target_id, s.grating;



