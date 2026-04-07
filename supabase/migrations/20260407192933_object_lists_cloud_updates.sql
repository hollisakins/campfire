drop function if exists "public"."get_adjacent_objects"(p_current_object_id text, p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export_objects"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_filtered_object_ids"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision);

drop function if exists "public"."get_filtered_objects_paginated"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer);

drop function if exists "public"."get_csv_export"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export_objects"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_search text, p_inspected_only boolean, p_list_ids integer[], p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export_spectra"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_lists_for_sync()
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE
AS $function$
BEGIN
  RETURN COALESCE(
    (SELECT jsonb_agg(jsonb_build_object(
      'id', ol.id,
      'slug', ol.slug,
      'name', ol.name,
      'description', ol.description,
      'visibility', ol.visibility,
      'is_system', ol.is_system,
      'created_by', ol.created_by,
      'created_at', ol.created_at,
      'updated_at', ol.updated_at,
      'member_count', (SELECT COUNT(*) FROM object_list_members olm WHERE olm.list_id = ol.id)
    ) ORDER BY ol.is_system DESC, ol.name)
    FROM object_lists ol),
    '[]'::jsonb
  );
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_csv_export(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text)
 RETURNS TABLE(target_id text, field text, ra double precision, "dec" double precision, redshift numeric, redshift_quality integer, max_snr double precision, max_exposure_time double precision, num_gratings integer, program_slug text, program_name text, last_inspected_at timestamp with time zone, last_inspected_by text, distance double precision, spectral_features integer, dq_flags integer, lists text)
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
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH filtered_targets AS (
    SELECT t.target_id, t.field, t.ra, t.dec, t.redshift, t.redshift_quality,
      t.max_snr, t.max_exposure_time,
      (SELECT COUNT(*)::INTEGER FROM spectra s WHERE s.target_id = t.target_id) AS num_gratings,
      t.program_slug, t.observation, t.last_inspected_at, t.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.dq_flags, 0) AS dq_flags,
      (SELECT string_agg(ol.slug, ';' ORDER BY ol.slug)
       FROM object_list_members olm
       JOIN object_lists ol ON ol.id = olm.list_id
       WHERE olm.object_id = t.object_id) AS lists
    FROM targets t
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)))
        OR (v_gratings_mode = 'all' AND (SELECT COUNT(DISTINCT gs.grating) FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)) = array_length(p_gratings, 1))
        OR (v_gratings_mode = 'none' AND NOT EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings))))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min) AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR t.object_id IN (
          SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND t.redshift_quality > 0) OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT ft.* FROM filtered_targets ft WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees)
  SELECT df.target_id, df.field, df.ra, df.dec, df.redshift, df.redshift_quality,
    df.max_snr, df.max_exposure_time, df.num_gratings, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.spectral_features, df.dq_flags, df.lists
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
    df.target_id ASC;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_csv_export_objects(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_list_ids integer[] DEFAULT NULL::integer[], p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'object_id'::text, p_sort_direction text DEFAULT 'asc'::text)
 RETURNS TABLE(object_id text, field text, ra double precision, "dec" double precision, best_redshift double precision, best_redshift_quality integer, n_targets integer, n_spectra integer, programs text, gratings text, max_snr double precision, max_exposure_time double precision, member_target_ids text, distance double precision, lists text)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN (
    'object_id', 'field', 'ra', 'dec', 'best_redshift', 'best_redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH filtered_objects AS (
    SELECT o.object_id, o.field, o.ra, o.dec,
      o.best_redshift, o.best_redshift_quality,
      o.n_targets, o.n_spectra,
      array_to_string(o.programs, ';') AS programs,
      array_to_string(o.gratings, ';') AS gratings,
      o.max_snr, o.max_exposure_time,
      (SELECT array_to_string(ARRAY(
        SELECT t.target_id FROM targets t
        WHERE t.object_id = o.id AND t.program_slug = ANY(v_filtered_program_slugs)
        ORDER BY t.target_id
      ), ';')) AS member_target_ids,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      (SELECT string_agg(ol.slug, ';' ORDER BY ol.slug)
       FROM object_list_members olm
       JOIN object_lists ol ON ol.id = olm.list_id
       WHERE olm.object_id = o.id) AS lists
    FROM objects o
    WHERE o.programs && v_filtered_program_slugs
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.best_redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.best_redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.best_redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.best_redshift_quality > 0) OR (p_inspected_only = FALSE AND o.best_redshift_quality = 0))
      AND (NOT v_coord_search_active OR (
        o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR o.id IN (
          SELECT olm.object_id FROM object_list_members olm
          WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
  ),
  distance_filtered AS (SELECT fo.* FROM filtered_objects fo WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees)
  SELECT df.object_id, df.field, df.ra, df.dec,
    df.best_redshift, df.best_redshift_quality,
    df.n_targets, df.n_spectra,
    df.programs, df.gratings,
    df.max_snr, df.max_exposure_time,
    df.member_target_ids, df.distance, df.lists
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
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'best_redshift' AND p_sort_direction = 'asc' THEN df.best_redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'best_redshift' AND p_sort_direction = 'desc' THEN df.best_redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'best_redshift_quality' AND p_sort_direction = 'asc' THEN df.best_redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'best_redshift_quality' AND p_sort_direction = 'desc' THEN df.best_redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN df.n_targets END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN df.n_targets END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN df.n_spectra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN df.n_spectra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
    df.object_id ASC;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text)
 RETURNS TABLE(target_id text, grating text, field text, ra double precision, "dec" double precision, redshift numeric, redshift_quality integer, signal_to_noise double precision, exposure_time double precision, fits_path text, program_slug text, program_name text, last_inspected_at timestamp with time zone, last_inspected_by text, distance double precision, spectral_features integer, dq_flags integer, lists text)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT t.target_id, s.grating, t.field, t.ra, t.dec, t.redshift, t.redshift_quality,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      t.last_inspected_at, t.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.dq_flags, 0) AS dq_flags,
      (SELECT string_agg(ol.slug, ';' ORDER BY ol.slug)
       FROM object_list_members olm
       JOIN object_lists ol ON ol.id = olm.list_id
       WHERE olm.object_id = t.object_id) AS lists
    FROM targets t JOIN spectra s ON s.target_id = t.target_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min) AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR t.object_id IN (
          SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND t.redshift_quality > 0) OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.target_id, df.grating, df.field, df.ra, df.dec, df.redshift, df.redshift_quality,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.spectral_features, df.dq_flags, df.lists
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY
    CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN df.signal_to_noise END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN df.signal_to_noise END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN df.exposure_time END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN df.exposure_time END DESC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN df.grating END ASC NULLS LAST,
    CASE WHEN NOT v_coord_search_active AND p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN df.grating END DESC NULLS LAST,
    df.target_id ASC, df.grating ASC;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_filtered_objects_paginated(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_list_ids integer[] DEFAULT NULL::integer[], p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'object_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT 1, p_page_size integer DEFAULT 50)
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
    'object_id', 'field', 'ra', 'dec', 'best_redshift', 'best_redshift_quality',
    'n_targets', 'n_spectra', 'max_snr', 'max_exposure_time'
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
    AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
    AND (
      NOT v_grating_filter_active
      OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
      OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
      OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
    )
    AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.best_redshift_quality = ANY(p_redshift_quality))
    AND (p_redshift_min IS NULL OR o.best_redshift >= p_redshift_min)
    AND (p_redshift_max IS NULL OR o.best_redshift <= p_redshift_max)
    AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
    AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND o.best_redshift_quality > 0)
      OR (p_inspected_only = FALSE AND o.best_redshift_quality = 0)
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
    ));

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
      o.best_redshift,
      o.best_redshift_quality,
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
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings)
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings)
        OR (v_gratings_mode = 'none' AND NOT o.gratings && p_gratings)
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.best_redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.best_redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.best_redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%'
      OR EXISTS (SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.target_id ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.best_redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.best_redshift_quality = 0)
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
      CASE WHEN p_sort_column = 'best_redshift' AND p_sort_direction = 'asc' THEN o.best_redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift' AND p_sort_direction = 'desc' THEN o.best_redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift_quality' AND p_sort_direction = 'asc' THEN o.best_redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'best_redshift_quality' AND p_sort_direction = 'desc' THEN o.best_redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'asc' THEN o.n_targets END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_targets' AND p_sort_direction = 'desc' THEN o.n_targets END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'asc' THEN o.n_spectra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'n_spectra' AND p_sort_direction = 'desc' THEN o.n_spectra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN o.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN o.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN o.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN o.max_exposure_time END DESC NULLS LAST,
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
        'best_redshift', fo.best_redshift,
        'best_redshift_quality', fo.best_redshift_quality,
        'created_at', fo.created_at,
        'distance', fo.distance,
        'member_targets', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'target_id', t.target_id,
              'program_slug', t.program_slug,
              'observation', t.observation,
              'redshift', t.redshift,
              'redshift_quality', t.redshift_quality
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

CREATE OR REPLACE FUNCTION public.get_objects_for_sync(p_program_slugs text[], p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.best_redshift, o.best_redshift_quality,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
    ORDER BY o.object_id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE o.programs && p_program_slugs
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE o.programs && p_program_slugs
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
        'best_redshift', m.best_redshift,
        'best_redshift_quality', m.best_redshift_quality,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(
          (SELECT jsonb_agg(t.target_id ORDER BY t.target_id)
           FROM targets t
           WHERE t.object_id = m.id
             AND t.program_slug = ANY(p_program_slugs)),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(ol.slug ORDER BY ol.slug)
           FROM object_list_members olm
           JOIN object_lists ol ON ol.id = olm.list_id
           WHERE olm.object_id = m.id),
          '[]'::jsonb
        )
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_targets_for_sync(p_program_slugs text[], p_updated_since timestamp without time zone DEFAULT NULL::timestamp without time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0)
 RETURNS TABLE(targets jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT t.id, t.target_id, t.program_slug, t.field, t.observation,
           t.ra, t.dec, t.redshift, t.redshift_auto, t.redshift_inspected,
           t.redshift_quality, t.spectral_features, t.dq_flags,
           t.max_snr, t.max_exposure_time,
           t.last_inspected_at, t.last_inspected_by, t.created_at, t.updated_at,
           t.object_id
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
    ORDER BY t.target_id
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'target_id', m.target_id,
        'program_slug', m.program_slug,
        'program_name', pr.program_name,
        'field', m.field,
        'observation', m.observation,
        'ra', m.ra,
        'dec', m.dec,
        'redshift', m.redshift,
        'redshift_auto', m.redshift_auto,
        'redshift_inspected', m.redshift_inspected,
        'redshift_quality', m.redshift_quality,
        'spectral_features', m.spectral_features,
        'dq_flags', m.dq_flags,
        'max_snr', m.max_snr,
        'max_exposure_time', m.max_exposure_time,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'spectra', COALESCE(
          (SELECT jsonb_agg(jsonb_build_object(
            'id', s.id,
            'target_id', s.target_id,
            'grating', s.grating,
            'fits_path', s.fits_path,
            'file_hash', s.file_hash,
            'file_size', s.file_size,
            'signal_to_noise', s.signal_to_noise,
            'exposure_time', s.exposure_time,
            'reduction_version', s.reduction_version
          )) FROM spectra s WHERE s.target_id = m.target_id),
          '[]'::jsonb
        ),
        'lists', COALESCE(
          (SELECT jsonb_agg(ol.slug ORDER BY ol.slug)
           FROM object_list_members olm
           JOIN object_lists ol ON ol.id = olm.list_id
           WHERE olm.object_id = m.object_id),
          '[]'::jsonb
        )
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m
  LEFT JOIN programs pr ON m.program_slug = pr.slug;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.log_list_membership_change()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (NEW.list_id, NEW.object_id, auth.uid(), 'add', NEW.ra, NEW.dec);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (OLD.list_id, OLD.object_id, auth.uid(), 'remove', OLD.ra, OLD.dec);
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.relink_list_members_for_field(p_field text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  n_relinked INTEGER := 0;
  n_orphaned INTEGER := 0;
  v_orphaned_details JSONB := '[]'::JSONB;
  v_field_ra_min DOUBLE PRECISION;
  v_field_ra_max DOUBLE PRECISION;
  v_field_dec_min DOUBLE PRECISION;
  v_field_dec_max DOUBLE PRECISION;
  v_tolerance_deg DOUBLE PRECISION := 0.3 / 3600.0;  -- 0.3 arcsec in degrees
BEGIN
  -- Get bounding box of objects in this field (with padding)
  SELECT MIN(o.ra) - v_tolerance_deg, MAX(o.ra) + v_tolerance_deg,
         MIN(o.dec) - v_tolerance_deg, MAX(o.dec) + v_tolerance_deg
  INTO v_field_ra_min, v_field_ra_max, v_field_dec_min, v_field_dec_max
  FROM objects o WHERE o.field = p_field;

  IF v_field_ra_min IS NULL THEN
    -- No objects in field, nothing to re-link
    RETURN jsonb_build_object('relinked', 0, 'orphaned', 0, 'orphaned_details', '[]'::jsonb);
  END IF;

  -- Re-link: for each unlinked member whose coords fall in this field,
  -- find the nearest object within 0.3 arcsec tolerance.
  WITH candidates AS (
    SELECT olm.id AS member_id,
           olm.ra AS member_ra,
           olm.dec AS member_dec,
           olm.list_id,
           o.id AS obj_id,
           -- Angular distance approximation (sufficient for sub-arcsec)
           SQRT(
             POWER((olm.ra - o.ra) * COS(RADIANS(olm.dec)), 2) +
             POWER(olm.dec - o.dec, 2)
           ) AS dist_deg,
           ROW_NUMBER() OVER (
             PARTITION BY olm.id
             ORDER BY SQRT(
               POWER((olm.ra - o.ra) * COS(RADIANS(olm.dec)), 2) +
               POWER(olm.dec - o.dec, 2)
             ) ASC
           ) AS rn
    FROM object_list_members olm
    CROSS JOIN LATERAL (
      SELECT o.id, o.ra, o.dec
      FROM objects o
      WHERE o.field = p_field
        AND o.ra BETWEEN olm.ra - v_tolerance_deg AND olm.ra + v_tolerance_deg
        AND o.dec BETWEEN olm.dec - v_tolerance_deg AND olm.dec + v_tolerance_deg
    ) o
    WHERE olm.object_id IS NULL
      AND olm.ra BETWEEN v_field_ra_min AND v_field_ra_max
      AND olm.dec BETWEEN v_field_dec_min AND v_field_dec_max
  ),
  best_match AS (
    SELECT member_id, obj_id, dist_deg
    FROM candidates
    WHERE rn = 1 AND dist_deg <= v_tolerance_deg
  ),
  updated AS (
    UPDATE object_list_members olm
    SET object_id = bm.obj_id
    FROM best_match bm
    WHERE olm.id = bm.member_id
    RETURNING olm.id
  )
  SELECT COUNT(*) INTO n_relinked FROM updated;

  -- Count orphaned members (still NULL after re-link, coords in field bbox)
  SELECT COUNT(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'list_slug', ol.slug,
           'list_name', ol.name,
           'ra', olm.ra,
           'dec', olm.dec
         )), '[]'::jsonb)
  INTO n_orphaned, v_orphaned_details
  FROM object_list_members olm
  JOIN object_lists ol ON ol.id = olm.list_id
  WHERE olm.object_id IS NULL
    AND olm.ra BETWEEN v_field_ra_min AND v_field_ra_max
    AND olm.dec BETWEEN v_field_dec_min AND v_field_dec_max;

  RETURN jsonb_build_object(
    'relinked', n_relinked,
    'orphaned', n_orphaned,
    'orphaned_details', v_orphaned_details
  );
END;
$function$
;


