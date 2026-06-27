drop function if exists "public"."get_database_overview"();

drop function if exists "public"."get_observation_manifest"(p_obs_name text, p_program_slugs text[]);

drop function if exists "public"."get_observation_stats"(p_program_slugs text[]);

drop function if exists "public"."get_observations_overview"(p_program_slugs text[]);

drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."nircam_reduction_progress";

drop view if exists "public"."spectrum_flag_summary";

-- Collapse the dual pipeline-version columns into one. reduction_version held
-- the authoritative per-row CMPFRVER (config-aware, so [pipeline].version
-- overrides were correct there); cfpipe_version was the redundant package
-- __version__ derivation that ignored overrides. Carry the good value across
-- BEFORE dropping reduction_version so the surviving cfpipe_version is correct
-- for every existing row. (reduced_at is intentionally NOT backfilled — old
-- rows get it on the next re-deploy, sourced from the FITS CMPFRTIM header.)
UPDATE "public"."spectra" SET "cfpipe_version" = "reduction_version"
  WHERE "reduction_version" IS NOT NULL;
UPDATE "public"."deployments" SET "cfpipe_version" = "reduction_version"
  WHERE "reduction_version" IS NOT NULL;

alter table "public"."deployments" drop column "reduction_version";

alter table "public"."spectra" drop column "reduction_version";

alter table "public"."spectra" add column "reduced_at" timestamp with time zone;

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.enforce_spectra_dq_user_update_scope()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    -- Service role (no JWT) and admins can write any column.
    IF auth.uid() IS NULL OR public.is_admin() THEN
        RETURN NEW;
    END IF;

    -- Non-admin users may only change dq_flags. updated_at is maintained
    -- by bump_spectra_updated_at; allow it through.
    IF OLD.grating IS DISTINCT FROM NEW.grating
       OR OLD.fits_path IS DISTINCT FROM NEW.fits_path
       OR OLD.signal_to_noise IS DISTINCT FROM NEW.signal_to_noise
       OR OLD.target_id IS DISTINCT FROM NEW.target_id
       OR OLD.thumbnail_svg_fnu IS DISTINCT FROM NEW.thumbnail_svg_fnu
       OR OLD.thumbnail_svg_flambda IS DISTINCT FROM NEW.thumbnail_svg_flambda
       OR OLD.file_hash IS DISTINCT FROM NEW.file_hash
       OR OLD.file_size IS DISTINCT FROM NEW.file_size
       OR OLD.exposure_time IS DISTINCT FROM NEW.exposure_time
       OR OLD.crds_context IS DISTINCT FROM NEW.crds_context
       OR OLD.jwst_version IS DISTINCT FROM NEW.jwst_version
       OR OLD.cfpipe_version IS DISTINCT FROM NEW.cfpipe_version
       OR OLD.date_obs IS DISTINCT FROM NEW.date_obs
       OR OLD.redshift_auto IS DISTINCT FROM NEW.redshift_auto
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
    THEN
        RAISE EXCEPTION 'Non-admin updates to spectra may only change dq_flags'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_csv_export_objects(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_needs_review boolean DEFAULT NULL::boolean, p_list_ids integer[] DEFAULT NULL::integer[], p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_has_photometry boolean DEFAULT NULL::boolean, p_photo_z_min double precision DEFAULT NULL::double precision, p_photo_z_max double precision DEFAULT NULL::double precision, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_sort_column text DEFAULT 'object_id'::text, p_sort_direction text DEFAULT 'asc'::text)
 RETURNS TABLE(object_id text, field text, ra double precision, "dec" double precision, redshift numeric, redshift_quality integer, redshift_inspected numeric, redshift_auto double precision, last_inspected_at timestamp with time zone, last_inspected_by text, last_data_change_at timestamp with time zone, staleness_reason text, version integer, n_targets integer, n_spectra integer, programs text, gratings text, max_snr double precision, max_exposure_time double precision, member_target_ids text, distance double precision, lists text, has_photometry boolean, photo_z double precision, photo_z_err_lo double precision, photo_z_err_hi double precision, photometry jsonb)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time', 'photo_z'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH member_targets AS (
    SELECT t.object_id, string_agg(t.target_id, ';' ORDER BY t.target_id) AS member_target_ids
    FROM targets t
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
    GROUP BY t.object_id
  ),
  visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
    GROUP BY olm.object_id
  ),
  filtered_objects AS (
    SELECT o.object_id, o.field, o.ra, o.dec,
      o.redshift, o.redshift_quality,
      o.redshift_inspected, o.redshift_auto,
      o.last_inspected_at, up.full_name AS last_inspected_by,
      o.last_data_change_at, o.staleness_reason, o.version,
      -- Aggregates scoped to accessible (+ filtered) programs so mixed-program
      -- objects don't export proprietary member metadata. See
      -- object_scoped_aggregates().
      sa.n_targets, sa.n_spectra,
      array_to_string(sa.programs, ';') AS programs,
      array_to_string(sa.gratings, ';') AS gratings,
      sa.max_snr, sa.max_exposure_time,
      mt.member_target_ids,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      vl.lists,
      o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
      phot.photometry
    FROM objects o
    LEFT JOIN member_targets mt ON mt.object_id = o.id
    LEFT JOIN visible_lists vl ON vl.object_id = o.id
    LEFT JOIN user_profiles up ON up.user_id = o.last_inspected_by
    LEFT JOIN LATERAL public.object_scoped_aggregates(o.id, v_filtered_program_slugs) sa ON true
    LEFT JOIN LATERAL (
      SELECT op.photometry FROM object_photometry op
      WHERE op.object_id = o.id ORDER BY op.updated_at DESC LIMIT 1
    ) phot ON true
    WHERE o.programs && v_filtered_program_slugs
      AND o.is_active = true
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
      AND (p_needs_review IS NULL
        OR (p_needs_review = TRUE
            AND o.staleness_reason IS NOT NULL
            AND o.last_inspected_at IS NOT NULL
            AND (o.last_data_change_at IS NULL OR o.last_data_change_at > o.last_inspected_at))
        OR (p_needs_review = FALSE
            AND (o.staleness_reason IS NULL
                 OR o.last_inspected_at IS NULL
                 OR (o.last_data_change_at IS NOT NULL AND o.last_data_change_at <= o.last_inspected_at))))
      AND (NOT v_coord_search_active OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
  ),
  distance_filtered AS (SELECT fo.* FROM filtered_objects fo WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees)
  SELECT df.object_id, df.field, df.ra, df.dec,
    df.redshift, df.redshift_quality,
    df.redshift_inspected, df.redshift_auto,
    df.last_inspected_at, df.last_inspected_by,
    df.last_data_change_at, df.staleness_reason, df.version,
    df.n_targets, df.n_spectra,
    df.programs, df.gratings,
    df.max_snr, df.max_exposure_time,
    df.member_target_ids, df.distance, df.lists,
    df.has_photometry, df.photo_z, df.photo_z_err_lo, df.photo_z_err_hi,
    df.photometry
  FROM distance_filtered df
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN df.n_targets END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN df.n_targets END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN df.n_spectra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN df.n_spectra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'photo_z' AND p_sort_direction = 'asc' THEN df.photo_z END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'photo_z' AND p_sort_direction = 'desc' THEN df.photo_z END DESC NULLS LAST,
    df.object_id ASC;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_database_overview()
 RETURNS TABLE(n_programs bigint, n_observations bigint, n_pointings bigint, n_targets bigint, n_spectra bigint, total_size_bytes bigint, latest_deployed_at timestamp with time zone, latest_cfpipe_version text)
 LANGUAGE sql
 STABLE
AS $function$
  WITH latest AS (
    SELECT d.deployed_at, d.cfpipe_version
    FROM public.deployments d
    WHERE d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  )
  SELECT
    (SELECT COUNT(*)::bigint FROM public.programs) AS n_programs,
    (SELECT COUNT(*)::bigint FROM public.observations) AS n_observations,
    (SELECT COALESCE(SUM(jsonb_array_length(pointings)), 0)::bigint
       FROM public.observations
       WHERE pointings IS NOT NULL) AS n_pointings,
    (SELECT COUNT(*)::bigint FROM public.targets) AS n_targets,
    (SELECT COUNT(*)::bigint FROM public.spectra) AS n_spectra,
    (SELECT COALESCE(SUM(file_size), 0)::bigint FROM public.spectra) AS total_size_bytes,
    (SELECT deployed_at FROM latest) AS latest_deployed_at,
    (SELECT cfpipe_version FROM latest) AS latest_cfpipe_version;
$function$
;

CREATE OR REPLACE FUNCTION public.get_field_shutters(p_field text)
 RETURNS TABLE(object_id text, source_id integer, center_ra double precision, center_dec double precision, position_angle double precision, shutter_idx smallint, dither_id smallint, shutter_state text, observation text, aperture_name text, aperture_width_arcsec double precision, aperture_height_arcsec double precision)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  FROM public.shutters s
  WHERE s.field = p_field
  ORDER BY s.object_id;
$function$
;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_needs_review boolean DEFAULT NULL::boolean, p_list_ids integer[] DEFAULT NULL::integer[], p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_has_photometry boolean DEFAULT NULL::boolean, p_photo_z_min double precision DEFAULT NULL::double precision, p_photo_z_max double precision DEFAULT NULL::double precision, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_sort_column text DEFAULT 'object_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT 1, p_page_size integer DEFAULT 50)
 RETURNS TABLE(targets jsonb, total_count bigint, page integer, page_size integer)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_offset INTEGER;
  v_total_count BIGINT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
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
    AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
    AND (
      NOT v_comment_search_active
      -- Uncorrelated semijoin: collect the object_ids that have a matching
      -- comment ONCE (object-level comments directly + target-level comments
      -- mapped through their parent object), then probe o.id IN (...). The old
      -- correlated EXISTS-inside-OR re-ran a per-object targets subquery for
      -- every (object x matching-comment) pair -> 271k subplan executions /
      -- ~870ms here, multi-second on broad terms or cold cache.
      OR o.id IN (
        SELECT c.object_id FROM comments c
        WHERE c.object_id IS NOT NULL
          AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (
            p_comment_search_scope = 'everyone'
            OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
          )
        UNION
        SELECT t.object_id FROM comments c
        JOIN targets t ON t.id = c.target_id
        WHERE c.target_id IS NOT NULL
          AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (
            p_comment_search_scope = 'everyone'
            OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
          )
      )
    );

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
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
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
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see the count query above for the rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
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
        -- Aggregates scoped to the viewer's accessible (+ filtered) programs so
        -- mixed-program objects don't leak proprietary member metadata. Filter
        -- and sort above still run on the global o.* columns (display-only
        -- scoping); the substitution happens only on the paginated result set.
        'n_targets', sa.n_targets,
        'n_spectra', sa.n_spectra,
        'programs', sa.programs,
        'gratings', sa.gratings,
        'max_snr', sa.max_snr,
        'max_exposure_time', sa.max_exposure_time,
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
    LEFT JOIN LATERAL public.object_scoped_aggregates(fo.id, v_filtered_program_slugs) sa ON true
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

CREATE OR REPLACE FUNCTION public.get_nearby_shutters(p_ra double precision, p_dec double precision, p_radius_arcsec double precision DEFAULT 5.0, p_field text DEFAULT NULL::text)
 RETURNS TABLE(object_id text, source_id integer, center_ra double precision, center_dec double precision, position_angle double precision, shutter_idx smallint, dither_id smallint, shutter_state text, observation text, aperture_name text, aperture_width_arcsec double precision, aperture_height_arcsec double precision)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation,
         s.aperture_name, s.aperture_width_arcsec, s.aperture_height_arcsec
  FROM shutters s
  WHERE (p_field IS NULL OR s.field = p_field)
    AND s.center_ra BETWEEN p_ra - p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
                        AND p_ra + p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
    AND s.center_dec BETWEEN p_dec - p_radius_arcsec / 3600.0
                         AND p_dec + p_radius_arcsec / 3600.0;
$function$
;

CREATE OR REPLACE FUNCTION public.get_observation_manifest(p_obs_name text, p_program_slugs text[])
 RETURNS TABLE(spectra_id integer, spectrum_id text, target_id text, grating text, fits_path text, file_hash text, file_size bigint, signal_to_noise double precision, cfpipe_version text)
 LANGUAGE plpgsql
 STABLE
AS $function$
BEGIN
  RETURN QUERY
  SELECT s.id, s.spectrum_id, s.target_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.cfpipe_version
  FROM spectra s
  JOIN targets t ON t.target_id = s.target_id
  WHERE t.observation = p_obs_name AND t.program_slug = ANY(p_program_slugs)
  ORDER BY s.spectrum_id;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_observation_stats(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, target_count bigint, spectrum_count bigint, total_size_bytes bigint, pointings jsonb, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, deployed_by_username text, deployed_by_full_name text, n_patches_since_full integer, last_patch_at timestamp with time zone)
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
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM stats s
  LEFT JOIN observations o ON o.name = s.observation
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
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

CREATE OR REPLACE FUNCTION public.get_observations_overview(p_program_slugs text[])
 RETURNS TABLE(observation text, program_slug text, program_name text, field text, cycle integer, gratings text[], pointing_count integer, pointings jsonb, target_count bigint, spectrum_count bigint, total_size_bytes bigint, crds_context text, cfpipe_version text, jwst_version text, reduced_at timestamp with time zone, deployed_at timestamp with time zone, deployed_by_username text, deployed_by_full_name text, n_patches_since_full integer, last_patch_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  WITH stats AS (
    SELECT t.observation, t.program_slug,
      COUNT(DISTINCT t.target_id) AS target_count,
      COUNT(s.id) AS spectrum_count,
      COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes,
      ARRAY_AGG(DISTINCT s.grating ORDER BY s.grating)
        FILTER (WHERE s.grating IS NOT NULL) AS gratings
    FROM public.targets t
    LEFT JOIN public.spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(p_program_slugs)
    GROUP BY t.observation, t.program_slug
  )
  SELECT
    o.name AS observation,
    o.program_slug,
    p.program_name,
    o.field,
    p.cycle,
    CASE
      WHEN COALESCE(array_length(s.gratings, 1), 0) > 0 THEN s.gratings
      ELSE COALESCE(o.gratings, ARRAY[]::text[])
    END AS gratings,
    COALESCE(jsonb_array_length(o.pointings), 0) AS pointing_count,
    o.pointings,
    COALESCE(s.target_count, 0)::bigint AS target_count,
    COALESCE(s.spectrum_count, 0)::bigint AS spectrum_count,
    COALESCE(s.total_size_bytes, 0)::bigint AS total_size_bytes,
    full_dep.crds_context,
    full_dep.cfpipe_version, full_dep.jwst_version,
    full_dep.reduced_at, full_dep.deployed_at,
    full_dep.deployed_by_username, full_dep.deployed_by_full_name,
    COALESCE(patches.n_patches, 0)::integer AS n_patches_since_full,
    patches.last_patch_at
  FROM public.observations o
  JOIN public.programs p ON p.slug = o.program_slug
  LEFT JOIN stats s ON s.observation = o.name AND s.program_slug = o.program_slug
  LEFT JOIN LATERAL (
    SELECT d.crds_context, d.cfpipe_version, d.jwst_version,
           d.reduced_at, d.deployed_at,
           up.username AS deployed_by_username,
           up.full_name AS deployed_by_full_name
    FROM public.deployments d
    LEFT JOIN public.user_profiles up ON up.user_id = d.deployed_by
    WHERE d.observation = o.name AND d.source_ids_filter IS NULL
    ORDER BY d.deployed_at DESC
    LIMIT 1
  ) full_dep ON true
  LEFT JOIN LATERAL (
    SELECT COUNT(*)::integer AS n_patches, MAX(d.deployed_at) AS last_patch_at
    FROM public.deployments d
    WHERE d.observation = o.name
      AND d.source_ids_filter IS NOT NULL
      AND (full_dep.deployed_at IS NULL OR d.deployed_at > full_dep.deployed_at)
  ) patches ON true
  WHERE o.program_slug = ANY(p_program_slugs)
  ORDER BY o.program_slug, o.name;
$function$
;

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync(p_program_slugs text[], p_user_id uuid DEFAULT NULL::uuid, p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true)
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
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
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


create or replace view "public"."spectrum_flag_summary" as  SELECT s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((s.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.spectra s
     CROSS JOIN public.flag_definitions fd)
  GROUP BY s.id, s.target_id, s.grating;



