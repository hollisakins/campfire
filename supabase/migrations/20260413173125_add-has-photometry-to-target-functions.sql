drop function if exists "public"."get_adjacent_targets"(p_current_target_id text, p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features integer, p_dq_flags integer, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_csv_export_spectra"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text);

drop function if exists "public"."get_filtered_spectra_paginated"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer, p_include_thumbnails boolean);

drop function if exists "public"."get_filtered_target_ids"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer, p_updated_since timestamp without time zone);

drop function if exists "public"."get_filtered_targets_paginated"(p_program_slugs text[], p_filter_programs text[], p_fields text[], p_gratings text[], p_gratings_mode text, p_observations text[], p_redshift_quality integer[], p_redshift_min double precision, p_redshift_max double precision, p_max_snr_min double precision, p_max_snr_max double precision, p_max_exposure_time_min double precision, p_max_exposure_time_max double precision, p_spectral_features integer, p_dq_flags integer, p_spectral_features_include_any integer, p_spectral_features_include_all integer, p_spectral_features_exclude integer, p_dq_flags_include_any integer, p_dq_flags_include_all integer, p_dq_flags_exclude integer, p_list_ids integer[], p_search text, p_inspected_only boolean, p_comment_search text, p_comment_search_scope text, p_comment_user_id uuid, p_coord_ra double precision, p_coord_dec double precision, p_radius_degrees double precision, p_sort_column text, p_sort_direction text, p_page integer, p_page_size integer, p_include_thumbnails boolean, p_updated_since timestamp without time zone);

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_adjacent_targets(p_current_target_id text, p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features integer DEFAULT NULL::integer, p_dq_flags integer DEFAULT NULL::integer, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_has_photometry boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text)
 RETURNS TABLE(prev_target_id text, next_target_id text, current_index bigint, total_count bigint)
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
  v_sort_is_text BOOLEAN;
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_dq_include_any INTEGER;
  v_dq_include_all INTEGER;
  v_dq_exclude INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  IF p_sort_direction NOT IN ('asc', 'desc') THEN p_sort_direction := 'asc'; END IF;
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF v_coord_search_active THEN p_sort_column := 'distance'; p_sort_direction := 'asc'; END IF;
  v_sort_is_text := p_sort_column IN ('target_id', 'field', 'observation');
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs))
    INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  RETURN QUERY
  WITH filtered_targets AS MATERIALIZED (
    SELECT
      t.target_id,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
          POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
        )))
      ELSE NULL END AS distance,
      t.field, t.observation, t.ra, t.dec, t.redshift, t.redshift_quality, t.max_snr, t.max_exposure_time
    FROM targets t
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode = 'any' AND EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)))
        OR (v_gratings_mode = 'all' AND (SELECT COUNT(DISTINCT gs.grating) FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)) = array_length(p_gratings, 1))
        OR (v_gratings_mode = 'none' AND NOT EXISTS (SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)))
      )
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
      AND (v_sf_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & v_sf_exclude) = 0)
      AND (v_dq_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_exclude) = 0)
      AND (p_list_ids IS NULL OR array_length(p_list_ids, 1) IS NULL OR t.object_id IN (
          SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
      ))
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
      AND (p_has_photometry IS NULL OR EXISTS (SELECT 1 FROM objects o WHERE o.id = t.object_id AND o.has_photometry = p_has_photometry))
      AND (NOT v_comment_search_active OR EXISTS (
        SELECT 1 FROM comments c WHERE c.target_id = t.id AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
      ))
      AND (NOT v_coord_search_active OR (
        t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
      ))
  ),
  distance_filtered AS MATERIALIZED (
    SELECT
      ft.*,
      CASE p_sort_column
        WHEN 'target_id' THEN ft.target_id WHEN 'field' THEN ft.field WHEN 'observation' THEN ft.observation ELSE NULL
      END AS sort_text,
      CASE p_sort_column
        WHEN 'ra' THEN ft.ra WHEN 'dec' THEN ft.dec WHEN 'redshift' THEN ft.redshift::DOUBLE PRECISION
        WHEN 'redshift_quality' THEN ft.redshift_quality::DOUBLE PRECISION
        WHEN 'max_snr' THEN ft.max_snr WHEN 'max_exposure_time' THEN ft.max_exposure_time
        WHEN 'distance' THEN ft.distance ELSE NULL
      END AS sort_num
    FROM filtered_targets ft
    WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees
  ),
  current_tgt AS (
    SELECT df.sort_text, df.sort_num, df.target_id FROM distance_filtered df WHERE df.target_id = p_current_target_id
  )
  SELECT
    (SELECT df.target_id FROM distance_filtered df, current_tgt c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.target_id < c.target_id)
       OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
       OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.target_id < c.target_id)
       OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
     END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END DESC NULLS FIRST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END ASC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END DESC NULLS FIRST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END ASC NULLS FIRST,
       df.target_id DESC
     LIMIT 1
    ) AS prev_target_id,
    (SELECT df.target_id FROM distance_filtered df, current_tgt c
     WHERE CASE WHEN v_sort_is_text THEN
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text > c.sort_text ELSE df.sort_text < c.sort_text END)
       OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.target_id > c.target_id)
       OR (c.sort_text IS NOT NULL AND df.sort_text IS NULL)
     ELSE
       (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num > c.sort_num ELSE df.sort_num < c.sort_num END)
       OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.target_id > c.target_id)
       OR (c.sort_num IS NOT NULL AND df.sort_num IS NULL)
     END
     ORDER BY
       CASE WHEN v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_text END ASC NULLS LAST,
       CASE WHEN v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_text END DESC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'asc' THEN df.sort_num END ASC NULLS LAST,
       CASE WHEN NOT v_sort_is_text AND p_sort_direction = 'desc' THEN df.sort_num END DESC NULLS LAST,
       df.target_id ASC
     LIMIT 1
    ) AS next_target_id,
    CASE WHEN EXISTS (SELECT 1 FROM current_tgt) THEN (
      SELECT COUNT(*) + 1
      FROM distance_filtered df, current_tgt c
      WHERE CASE WHEN v_sort_is_text THEN
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_text < c.sort_text ELSE df.sort_text > c.sort_text END)
        OR (df.sort_text IS NOT DISTINCT FROM c.sort_text AND df.target_id < c.target_id)
        OR (df.sort_text IS NOT NULL AND c.sort_text IS NULL)
      ELSE
        (CASE WHEN p_sort_direction = 'asc' THEN df.sort_num < c.sort_num ELSE df.sort_num > c.sort_num END)
        OR (df.sort_num IS NOT DISTINCT FROM c.sort_num AND df.target_id < c.target_id)
        OR (df.sort_num IS NOT NULL AND c.sort_num IS NULL)
      END
    )::BIGINT ELSE 0::BIGINT END AS current_index,
    (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_csv_export(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_has_photometry boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text)
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
       WHERE olm.object_id = t.object_id
         AND (ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit'))) AS lists
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
      AND (p_has_photometry IS NULL OR EXISTS (SELECT 1 FROM objects o WHERE o.id = t.object_id AND o.has_photometry = p_has_photometry))
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

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_has_photometry boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text)
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
       WHERE olm.object_id = t.object_id
         AND (ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit'))) AS lists
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
      AND (p_has_photometry IS NULL OR EXISTS (SELECT 1 FROM objects o WHERE o.id = t.object_id AND o.has_photometry = p_has_photometry))
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

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_has_photometry boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT 1, p_page_size integer DEFAULT 50, p_include_thumbnails boolean DEFAULT false)
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
    'target_id', 'field', 'observation', 'ra', 'dec', 'redshift',
    'redshift_quality', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;

  IF v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  v_offset := (COALESCE(p_page, 1) - 1) * COALESCE(p_page_size, 50);

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

  -- Single-pass CTE: filtered_spectra is referenced by both distance_filtered
  -- and the count subquery, so PostgreSQL materializes it once.
  RETURN QUERY
  WITH filtered_spectra AS (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      t.redshift,
      t.redshift_auto,
      t.redshift_inspected,
      t.redshift_quality,
      COALESCE(t.spectral_features, 0) AS spectral_features,
      COALESCE(t.dq_flags, 0) AS dq_flags,
      t.max_snr,
      t.max_exposure_time,
      t.last_inspected_at,
      t.last_inspected_by,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_id,
      s.grating,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.file_hash,
      s.file_size,
      s.thumbnail_svg_fnu,
      s.thumbnail_svg_flambda,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM targets t
    JOIN spectra s ON s.target_id = t.target_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
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
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
      )
      AND (p_has_photometry IS NULL OR EXISTS (SELECT 1 FROM objects o WHERE o.id = t.object_id AND o.has_photometry = p_has_photometry))
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.target_id = t.id
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      AND (
        NOT v_coord_search_active
        OR (
          t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
  ),
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM distance_filtered
      ORDER BY
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN distance END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN distance END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN target_id END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN target_id END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN field END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN field END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN observation END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN observation END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN ra END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN ra END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN "dec" END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN "dec" END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN redshift END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN redshift END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN redshift_quality END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN redshift_quality END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'asc' THEN signal_to_noise END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'signal_to_noise' AND p_sort_direction = 'desc' THEN signal_to_noise END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'asc' THEN exposure_time END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'exposure_time' AND p_sort_direction = 'desc' THEN exposure_time END DESC NULLS LAST,
        CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'asc' THEN grating END ASC NULLS LAST,
        CASE WHEN p_sort_column = 'grating' AND p_sort_direction = 'desc' THEN grating END DESC NULLS LAST,
        target_id ASC, grating ASC
      LIMIT p_page_size OFFSET v_offset
    ) sorted_page
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      'redshift', r.redshift,
      'redshift_auto', r.redshift_auto,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'spectral_features', r.spectral_features,
      'dq_flags', r.dq_flags,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_fnu ELSE NULL END,
        'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN r.thumbnail_svg_flambda ELSE NULL END
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    (SELECT COUNT(*) FROM distance_filtered),
    p_page,
    p_page_size
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_filtered_target_ids(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_has_photometry boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT NULL::integer, p_page_size integer DEFAULT NULL::integer, p_updated_since timestamp without time zone DEFAULT NULL::timestamp without time zone)
 RETURNS TABLE(target_id text, distance double precision, row_num bigint, total_count bigint)
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
  v_paginate BOOLEAN;
  v_offset INTEGER;
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
  IF NOT (p_sort_column IN ('target_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr', 'max_exposure_time')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'target_id';
  END IF;
  IF v_coord_search_active AND p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;
  v_paginate := (p_page IS NOT NULL AND p_page_size IS NOT NULL);
  IF v_paginate THEN
    v_offset := (p_page - 1) * p_page_size;
  END IF;
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_slugs;
  ELSE
    v_filtered_program_slugs := p_program_slugs;
  END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN
    RETURN;
  END IF;

  -- === Paginated path ===
  IF v_paginate THEN
    RETURN QUERY
    WITH filtered_targets AS (
      SELECT
        t.target_id,
        CASE WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL END AS distance,
        t.field, t.observation, t.ra, t.dec, t.redshift, t.redshift_quality, t.max_snr, t.max_exposure_time
      FROM targets t
      WHERE
        t.program_slug = ANY(v_filtered_program_slugs)
        AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
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
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
        )
        AND (p_has_photometry IS NULL OR EXISTS (SELECT 1 FROM objects o WHERE o.id = t.object_id AND o.has_photometry = p_has_photometry))
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.target_id = t.id AND c.is_deleted = false
              AND c.content ILIKE '%' || p_comment_search || '%'
              AND (p_comment_search_scope = 'everyone'
                   OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
          )
        )
        AND (
          NOT v_coord_search_active
          OR (t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
              AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees))
        )
    ),
    distance_filtered AS (
      SELECT ft.* FROM filtered_targets ft
      WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees
    )
    SELECT
      df.target_id, df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.target_id ASC
      ) AS row_num,
      (SELECT COUNT(*) FROM distance_filtered)::BIGINT AS total_count
    FROM distance_filtered df
    ORDER BY
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
      df.target_id ASC
    LIMIT p_page_size OFFSET v_offset;

  -- === Full path (all rows) ===
  ELSE
    RETURN QUERY
    WITH filtered_targets AS (
      SELECT
        t.target_id,
        CASE WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL END AS distance,
        t.field, t.observation, t.ra, t.dec, t.redshift, t.redshift_quality, t.max_snr, t.max_exposure_time
      FROM targets t
      WHERE
        t.program_slug = ANY(v_filtered_program_slugs)
        AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
        AND (
          NOT v_grating_filter_active
          OR (v_gratings_mode = 'any' AND EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
          OR (v_gratings_mode = 'all' AND (
            SELECT COUNT(DISTINCT gs.grating) FROM spectra gs
            WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ) = array_length(p_gratings, 1))
          OR (v_gratings_mode = 'none' AND NOT EXISTS (
            SELECT 1 FROM spectra gs WHERE gs.target_id = t.target_id AND gs.grating = ANY(p_gratings)
          ))
        )
        AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
        AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
        AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR t.redshift_quality = ANY(p_redshift_quality))
        AND (p_redshift_min IS NULL OR t.redshift >= p_redshift_min)
        AND (p_redshift_max IS NULL OR t.redshift <= p_redshift_max)
        AND (p_max_snr_min IS NULL OR t.max_snr >= p_max_snr_min)
        AND (p_max_snr_max IS NULL OR t.max_snr <= p_max_snr_max)
        AND (p_max_exposure_time_min IS NULL OR t.max_exposure_time >= p_max_exposure_time_min)
        AND (p_max_exposure_time_max IS NULL OR t.max_exposure_time <= p_max_exposure_time_max)
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
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
        )
        AND (p_has_photometry IS NULL OR EXISTS (SELECT 1 FROM objects o WHERE o.id = t.object_id AND o.has_photometry = p_has_photometry))
        AND (
          NOT v_comment_search_active
          OR EXISTS (
            SELECT 1 FROM comments c
            WHERE c.target_id = t.id AND c.is_deleted = false
              AND c.content ILIKE '%' || p_comment_search || '%'
              AND (p_comment_search_scope = 'everyone'
                   OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))
          )
        )
        AND (
          NOT v_coord_search_active
          OR (t.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
              AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees))
        )
    ),
    distance_filtered AS (
      SELECT ft.* FROM filtered_targets ft
      WHERE NOT v_coord_search_active OR ft.distance <= p_radius_degrees
    )
    SELECT
      df.target_id, df.distance,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'asc' THEN df.distance END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'distance' AND p_sort_direction = 'desc' THEN df.distance END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'asc' THEN df.target_id END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'target_id' AND p_sort_direction = 'desc' THEN df.target_id END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN df.field END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN df.field END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN df.observation END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN df.observation END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN df.ra END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN df.ra END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN df.dec END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN df.dec END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN df.redshift END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN df.redshift END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN df.redshift_quality END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN df.redshift_quality END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN df.max_snr END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN df.max_snr END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'asc' THEN df.max_exposure_time END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'max_exposure_time' AND p_sort_direction = 'desc' THEN df.max_exposure_time END DESC NULLS LAST,
          df.target_id ASC
      ) AS row_num,
      COUNT(*) OVER () AS total_count
    FROM distance_filtered df;
  END IF;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_filtered_targets_paginated(p_program_slugs text[], p_filter_programs text[] DEFAULT NULL::text[], p_fields text[] DEFAULT NULL::text[], p_gratings text[] DEFAULT NULL::text[], p_gratings_mode text DEFAULT 'any'::text, p_observations text[] DEFAULT NULL::text[], p_redshift_quality integer[] DEFAULT NULL::integer[], p_redshift_min double precision DEFAULT NULL::double precision, p_redshift_max double precision DEFAULT NULL::double precision, p_max_snr_min double precision DEFAULT NULL::double precision, p_max_snr_max double precision DEFAULT NULL::double precision, p_max_exposure_time_min double precision DEFAULT NULL::double precision, p_max_exposure_time_max double precision DEFAULT NULL::double precision, p_spectral_features integer DEFAULT NULL::integer, p_dq_flags integer DEFAULT NULL::integer, p_spectral_features_include_any integer DEFAULT NULL::integer, p_spectral_features_include_all integer DEFAULT NULL::integer, p_spectral_features_exclude integer DEFAULT NULL::integer, p_dq_flags_include_any integer DEFAULT NULL::integer, p_dq_flags_include_all integer DEFAULT NULL::integer, p_dq_flags_exclude integer DEFAULT NULL::integer, p_list_ids integer[] DEFAULT NULL::integer[], p_search text DEFAULT NULL::text, p_inspected_only boolean DEFAULT NULL::boolean, p_has_photometry boolean DEFAULT NULL::boolean, p_comment_search text DEFAULT NULL::text, p_comment_search_scope text DEFAULT NULL::text, p_comment_user_id uuid DEFAULT NULL::uuid, p_coord_ra double precision DEFAULT NULL::double precision, p_coord_dec double precision DEFAULT NULL::double precision, p_radius_degrees double precision DEFAULT NULL::double precision, p_sort_column text DEFAULT 'target_id'::text, p_sort_direction text DEFAULT 'asc'::text, p_page integer DEFAULT 1, p_page_size integer DEFAULT 50, p_include_thumbnails boolean DEFAULT false, p_updated_since timestamp without time zone DEFAULT NULL::timestamp without time zone)
 RETURNS TABLE(targets jsonb, total_count bigint, page integer, page_size integer)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
AS $function$
DECLARE
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_dq_include_any INTEGER;
  v_dq_include_all INTEGER;
  v_dq_exclude INTEGER;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_coord_search_active BOOLEAN;
BEGIN
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  RETURN QUERY
  WITH ids AS (
    SELECT *
    FROM public.get_filtered_target_ids(
      p_program_slugs, p_filter_programs, p_fields, p_gratings, p_gratings_mode,
      p_observations, p_redshift_quality, p_redshift_min, p_redshift_max,
      p_max_snr_min, p_max_snr_max, p_max_exposure_time_min, p_max_exposure_time_max,
      v_sf_include_any, v_sf_include_all, v_sf_exclude,
      v_dq_include_any, v_dq_include_all, v_dq_exclude,
      p_list_ids,
      p_search, p_inspected_only, p_has_photometry,
      p_comment_search, p_comment_search_scope, p_comment_user_id,
      p_coord_ra, p_coord_dec, p_radius_degrees,
      p_sort_column, p_sort_direction,
      p_page, p_page_size,
      p_updated_since
    )
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', t.id,
        'target_id', t.target_id,
        'program_slug', t.program_slug,
        'program_name', pr.program_name,
        'field', t.field,
        'observation', t.observation,
        'ra', t.ra,
        'dec', t.dec,
        'redshift', t.redshift,
        'redshift_auto', t.redshift_auto,
        'redshift_inspected', t.redshift_inspected,
        'redshift_quality', t.redshift_quality,
        'spectral_features', t.spectral_features,
        'dq_flags', t.dq_flags,
        'max_snr', t.max_snr,
        'max_exposure_time', t.max_exposure_time,
        'last_inspected_at', t.last_inspected_at,
        'last_inspected_by', t.last_inspected_by,
        'created_at', t.created_at,
        'updated_at', t.updated_at,
        'distance', CASE WHEN v_coord_search_active THEN ids.distance ELSE NULL END,
        'spectra', COALESCE(
          (SELECT jsonb_agg(
            jsonb_build_object(
              'id', s.id,
              'target_id', s.target_id,
              'grating', s.grating,
              'fits_path', s.fits_path,
              'signal_to_noise', s.signal_to_noise,
              'file_hash', s.file_hash,
              'file_size', s.file_size,
              'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
              'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
            )
          )
          FROM spectra s
          WHERE s.target_id = t.target_id
            AND (NOT v_grating_filter_active OR v_gratings_mode = 'none' OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        ),
        'row_num', ids.row_num
      ) AS obj_json,
      ids.total_count,
      ids.row_num
    FROM ids
    JOIN targets t ON t.target_id = ids.target_id
    LEFT JOIN programs pr ON t.program_slug = pr.slug
  )
  SELECT
    COALESCE(jsonb_agg(wr.obj_json ORDER BY wr.row_num), '[]'::jsonb),
    COALESCE(MAX(wr.total_count), 0)::BIGINT,
    p_page,
    p_page_size
  FROM with_relations wr;
END;
$function$
;


