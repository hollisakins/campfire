


SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE EXTENSION IF NOT EXISTS "pg_graphql" WITH SCHEMA "graphql";






CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pg_trgm" WITH SCHEMA "public";






CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";






CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";






CREATE OR REPLACE FUNCTION "public"."accessible_program_slugs"() RETURNS "text"[]
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  SELECT COALESCE(array_agg(DISTINCT slug), '{}')
  FROM (
    SELECT program_slug AS slug
    FROM user_program_access
    WHERE user_id = auth.uid()
    UNION
    SELECT slug
    FROM programs
    WHERE is_public = true
  ) sub;
$$;


ALTER FUNCTION "public"."accessible_program_slugs"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET
    status = 'authorized',
    user_id = p_user_id,
    authorized_at = NOW()
  WHERE
    user_code = p_user_code
    AND status = 'pending'
    AND expires_at > NOW();

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;


ALTER FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."can_comment"() RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  SELECT COALESCE(
    (SELECT can_comment FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;


ALTER FUNCTION "public"."can_comment"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."check_device_code_status"("p_device_code" "text") RETURNS TABLE("status" "text", "user_id" "uuid", "is_expired" boolean)
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
  RETURN QUERY
  SELECT
    dc.status,
    dc.user_id,
    (dc.expires_at < NOW())::BOOLEAN AS is_expired
  FROM device_codes dc
  WHERE dc.device_code = p_device_code;
END;
$$;


ALTER FUNCTION "public"."check_device_code_status"("p_device_code" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."cleanup_expired_device_codes"() RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  DELETE FROM device_codes
  WHERE expires_at < NOW()
  RETURNING 1 INTO deleted_count;

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$;


ALTER FUNCTION "public"."cleanup_expired_device_codes"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."cleanup_expired_refresh_tokens"() RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  deleted_count INTEGER;
BEGIN
  -- Delete tokens that expired more than 30 days ago (keep recent for audit)
  DELETE FROM refresh_tokens
  WHERE expires_at < NOW() - INTERVAL '30 days';

  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$;


ALTER FUNCTION "public"."cleanup_expired_refresh_tokens"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."consume_device_code"("p_device_code" "text") RETURNS "uuid"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  v_user_id UUID;
BEGIN
  UPDATE device_codes
  SET status = 'consumed'
  WHERE
    device_code = p_device_code
    AND status = 'authorized'
    AND expires_at > NOW()
  RETURNING user_id INTO v_user_id;

  RETURN v_user_id;
END;
$$;


ALTER FUNCTION "public"."consume_device_code"("p_device_code" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."count_distinct_inspected_objects"("p_user_id" "uuid") RETURNS integer
    LANGUAGE "sql" STABLE SECURITY DEFINER
    AS $$
  SELECT COUNT(DISTINCT object_id)::INTEGER
  FROM flag_audit_log
  WHERE user_id = p_user_id;
$$;


ALTER FUNCTION "public"."count_distinct_inspected_objects"("p_user_id" "uuid") OWNER TO "postgres";


COMMENT ON FUNCTION "public"."count_distinct_inspected_objects"("p_user_id" "uuid") IS 'Returns the count of distinct objects a user has inspected (made flag changes to)';



CREATE OR REPLACE FUNCTION "public"."deny_device_code"("p_user_code" "text") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE device_codes
  SET status = 'denied'
  WHERE
    user_code = p_user_code
    AND status = 'pending';

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;


ALTER FUNCTION "public"."deny_device_code"("p_user_code" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_adjacent_targets"("p_current_target_id" "text", "p_program_slugs" "text"[], "p_filter_programs" "text"[] DEFAULT NULL::"text"[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_max_exposure_time_min" double precision DEFAULT NULL::double precision, "p_max_exposure_time_max" double precision DEFAULT NULL::double precision, "p_spectral_features" integer DEFAULT NULL::integer, "p_object_flags" integer DEFAULT NULL::integer, "p_dq_flags" integer DEFAULT NULL::integer, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'target_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text") RETURNS TABLE("prev_target_id" "text", "next_target_id" "text", "current_index" bigint, "total_count" bigint)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
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
  v_of_include_any INTEGER;
  v_of_include_all INTEGER;
  v_of_exclude INTEGER;
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
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
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
      AND (v_of_include_any IS NULL OR (COALESCE(t.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(t.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(t.object_flags, 0) & v_of_exclude) = 0)
      AND (v_dq_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & v_dq_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0))
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
$$;


ALTER FUNCTION "public"."get_adjacent_targets"("p_current_target_id" "text", "p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_csv_export"("p_program_slugs" "text"[], "p_filter_programs" "text"[] DEFAULT NULL::"text"[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_max_exposure_time_min" double precision DEFAULT NULL::double precision, "p_max_exposure_time_max" double precision DEFAULT NULL::double precision, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'target_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text") RETURNS TABLE("target_id" "text", "field" "text", "ra" double precision, "dec" double precision, "redshift" numeric, "redshift_quality" integer, "max_snr" double precision, "max_exposure_time" double precision, "num_gratings" integer, "program_slug" "text", "program_name" "text", "last_inspected_at" timestamp with time zone, "last_inspected_by" "text", "distance" double precision, "spectral_features" integer, "object_flags" integer, "dq_flags" integer)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
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
      COALESCE(t.object_flags, 0) AS object_flags,
      COALESCE(t.dq_flags, 0) AS dq_flags
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
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
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
    df.distance, df.spectral_features, df.object_flags, df.dq_flags
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
$$;


ALTER FUNCTION "public"."get_csv_export"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_csv_export_spectra"("p_program_slugs" "text"[], "p_filter_programs" "text"[] DEFAULT NULL::"text"[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_max_exposure_time_min" double precision DEFAULT NULL::double precision, "p_max_exposure_time_max" double precision DEFAULT NULL::double precision, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'target_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text") RETURNS TABLE("target_id" "text", "grating" "text", "field" "text", "ra" double precision, "dec" double precision, "redshift" numeric, "redshift_quality" integer, "signal_to_noise" double precision, "exposure_time" double precision, "fits_path" "text", "program_slug" "text", "program_name" "text", "last_inspected_at" timestamp with time zone, "last_inspected_by" "text", "distance" double precision, "spectral_features" integer, "object_flags" integer, "dq_flags" integer)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
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
      COALESCE(t.object_flags, 0) AS object_flags,
      COALESCE(t.dq_flags, 0) AS dq_flags
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
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
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
    df.distance, df.spectral_features, df.object_flags, df.dq_flags
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
$$;


ALTER FUNCTION "public"."get_csv_export_spectra"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_download_stats"("p_days" integer DEFAULT 30) RETURNS json
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  IF NOT is_admin THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT json_build_object(
    'total_downloads', (
      SELECT COUNT(*) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'unique_users', (
      SELECT COUNT(DISTINCT user_id) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'by_type', (
      SELECT json_object_agg(download_type, count)
      FROM (
        SELECT download_type, COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY download_type
      ) t
    ),
    'total_files', (
      SELECT COALESCE(SUM(file_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'total_targets', (
      SELECT COALESCE(SUM(target_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'recent_downloads', (
      SELECT json_agg(t)
      FROM (
        SELECT
          dl.id,
          dl.download_type,
          dl.target_count,
          dl.file_count,
          dl.requested_at,
          au.email,
          up.full_name
        FROM download_log dl
        LEFT JOIN auth.users au ON dl.user_id = au.id
        LEFT JOIN user_profiles up ON dl.user_id = up.user_id
        WHERE dl.requested_at >= NOW() - (p_days || ' days')::INTERVAL
        ORDER BY dl.requested_at DESC
        LIMIT 50
      ) t
    ),
    'most_downloaded_targets', (
      SELECT json_agg(t)
      FROM (
        SELECT
          target_id,
          COUNT(*) as download_count
        FROM download_log, unnest(target_ids) as target_id
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY target_id
        ORDER BY download_count DESC
        LIMIT 20
      ) t
    ),
    'downloads_by_day', (
      SELECT json_agg(t ORDER BY day)
      FROM (
        SELECT
          DATE(requested_at) as day,
          COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY DATE(requested_at)
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;


ALTER FUNCTION "public"."get_download_stats"("p_days" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_filtered_spectra_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[] DEFAULT NULL::"text"[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_max_exposure_time_min" double precision DEFAULT NULL::double precision, "p_max_exposure_time_max" double precision DEFAULT NULL::double precision, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'target_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text", "p_page" integer DEFAULT 1, "p_page_size" integer DEFAULT 50, "p_include_thumbnails" boolean DEFAULT false) RETURNS TABLE("targets" "jsonb", "total_count" bigint, "page" integer, "page_size" integer)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
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

  -- Step 1: compute total count separately (avoids window function on full result set)
  SELECT COUNT(*) INTO v_total_count
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
    -- Per-spectrum filtering (not target-level max)
    AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
    AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
    AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
    AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
    AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
    AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
    AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
    AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
    AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
    AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
    AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
    AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
    AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
    AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
    AND (
      p_inspected_only IS NULL
      OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
      OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
    )
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
        AND 2 * DEGREES(ASIN(SQRT(
          POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
          COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
          POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
        ))) <= p_radius_degrees
      )
    );

  -- Step 2: fetch just the page rows (sort + LIMIT without window function overhead)
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
      COALESCE(t.object_flags, 0) AS object_flags,
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
      -- Per-spectrum filtering (not target-level max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_spectral_features_include_any IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_any) != 0)
      AND (p_spectral_features_include_all IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_include_all) = p_spectral_features_include_all)
      AND (p_spectral_features_exclude IS NULL OR (COALESCE(t.spectral_features, 0) & p_spectral_features_exclude) = 0)
      AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
      AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
      AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
      )
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
      'object_flags', r.object_flags,
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
    v_total_count,
    p_page,
    p_page_size
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug;
END;
$$;


ALTER FUNCTION "public"."get_filtered_spectra_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_filtered_target_ids"("p_program_slugs" "text"[], "p_filter_programs" "text"[] DEFAULT NULL::"text"[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_max_exposure_time_min" double precision DEFAULT NULL::double precision, "p_max_exposure_time_max" double precision DEFAULT NULL::double precision, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'target_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text", "p_page" integer DEFAULT NULL::integer, "p_page_size" integer DEFAULT NULL::integer, "p_updated_since" timestamp without time zone DEFAULT NULL::timestamp without time zone) RETURNS TABLE("target_id" "text", "distance" double precision, "row_num" bigint, "total_count" bigint)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
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
        AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
        )
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
        AND (p_object_flags_include_any IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_any) != 0)
        AND (p_object_flags_include_all IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_include_all) = p_object_flags_include_all)
        AND (p_object_flags_exclude IS NULL OR (COALESCE(t.object_flags, 0) & p_object_flags_exclude) = 0)
        AND (p_dq_flags_include_any IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_any) != 0)
        AND (p_dq_flags_include_all IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
        AND (p_dq_flags_exclude IS NULL OR (COALESCE(t.dq_flags, 0) & p_dq_flags_exclude) = 0)
        AND (p_search IS NULL OR t.target_id ILIKE '%' || p_search || '%')
        AND (
          p_inspected_only IS NULL
          OR (p_inspected_only = TRUE AND t.redshift_quality > 0)
          OR (p_inspected_only = FALSE AND t.redshift_quality = 0)
        )
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
$$;


ALTER FUNCTION "public"."get_filtered_target_ids"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_updated_since" timestamp without time zone) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_filtered_targets_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[] DEFAULT NULL::"text"[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_max_exposure_time_min" double precision DEFAULT NULL::double precision, "p_max_exposure_time_max" double precision DEFAULT NULL::double precision, "p_spectral_features" integer DEFAULT NULL::integer, "p_object_flags" integer DEFAULT NULL::integer, "p_dq_flags" integer DEFAULT NULL::integer, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'target_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text", "p_page" integer DEFAULT 1, "p_page_size" integer DEFAULT 50, "p_include_thumbnails" boolean DEFAULT false, "p_updated_since" timestamp without time zone DEFAULT NULL::timestamp without time zone) RETURNS TABLE("targets" "jsonb", "total_count" bigint, "page" integer, "page_size" integer)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
DECLARE
  v_sf_include_any INTEGER;
  v_sf_include_all INTEGER;
  v_sf_exclude INTEGER;
  v_of_include_any INTEGER;
  v_of_include_all INTEGER;
  v_of_exclude INTEGER;
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
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
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
      v_of_include_any, v_of_include_all, v_of_exclude,
      v_dq_include_any, v_dq_include_all, v_dq_exclude,
      p_search, p_inspected_only, p_comment_search, p_comment_search_scope, p_comment_user_id,
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
        'object_flags', t.object_flags,
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
$$;


ALTER FUNCTION "public"."get_filtered_targets_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean, "p_updated_since" timestamp without time zone) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_nearby_shutters"("p_ra" double precision, "p_dec" double precision, "p_radius_arcsec" double precision DEFAULT 5.0, "p_field" "text" DEFAULT NULL::"text") RETURNS TABLE("object_id" "text", "source_id" integer, "center_ra" double precision, "center_dec" double precision, "position_angle" double precision, "shutter_idx" smallint, "dither_id" smallint, "shutter_state" "text", "observation" "text")
    LANGUAGE "sql" STABLE
    AS $$
  SELECT s.object_id, s.source_id, s.center_ra, s.center_dec,
         s.position_angle, s.shutter_idx, s.dither_id, s.shutter_state, s.observation
  FROM shutters s
  WHERE (p_field IS NULL OR s.field = p_field)
    AND s.center_ra BETWEEN p_ra - p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
                        AND p_ra + p_radius_arcsec / 3600.0 / COS(RADIANS(p_dec))
    AND s.center_dec BETWEEN p_dec - p_radius_arcsec / 3600.0
                         AND p_dec + p_radius_arcsec / 3600.0;
$$;


ALTER FUNCTION "public"."get_nearby_shutters"("p_ra" double precision, "p_dec" double precision, "p_radius_arcsec" double precision, "p_field" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_observation_manifest"("p_obs_name" "text", "p_program_slugs" "text"[]) RETURNS TABLE("spectra_id" integer, "target_id" "text", "grating" "text", "fits_path" "text", "file_hash" "text", "file_size" bigint, "signal_to_noise" double precision, "reduction_version" "text")
    LANGUAGE "plpgsql" STABLE
    AS $$
BEGIN
  RETURN QUERY
  SELECT s.id, s.target_id, s.grating, s.fits_path, s.file_hash, s.file_size,
         s.signal_to_noise, s.reduction_version
  FROM spectra s
  JOIN targets t ON t.target_id = s.target_id
  WHERE t.observation = p_obs_name AND t.program_slug = ANY(p_program_slugs)
  ORDER BY s.target_id, s.grating;
END;
$$;


ALTER FUNCTION "public"."get_observation_manifest"("p_obs_name" "text", "p_program_slugs" "text"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_observation_stats"("p_program_slugs" "text"[]) RETURNS TABLE("observation" "text", "program_slug" "text", "program_name" "text", "field" "text", "target_count" bigint, "spectrum_count" bigint, "total_size_bytes" bigint)
    LANGUAGE "sql" STABLE
    AS $$
  SELECT t.observation, t.program_slug, p.program_name, t.field,
    COUNT(DISTINCT t.target_id) AS target_count,
    COUNT(s.id) AS spectrum_count,
    COALESCE(SUM(s.file_size), 0)::bigint AS total_size_bytes
  FROM targets t
  JOIN programs p ON p.slug = t.program_slug
  LEFT JOIN spectra s ON s.target_id = t.target_id
  WHERE t.program_slug = ANY(p_program_slugs)
  GROUP BY t.observation, t.program_slug, p.program_name, t.field
  ORDER BY t.observation;
$$;


ALTER FUNCTION "public"."get_observation_stats"("p_program_slugs" "text"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_program_stats"() RETURNS TABLE("slug" "text", "target_count" bigint, "user_access_count" bigint)
    LANGUAGE "sql" STABLE SECURITY DEFINER
    AS $$
  SELECT p.slug,
    COALESCE(tc.cnt, 0) AS target_count,
    COALESCE(a.cnt, 0) AS user_access_count
  FROM programs p
  LEFT JOIN (SELECT program_slug, COUNT(*) AS cnt FROM targets GROUP BY program_slug) tc ON p.slug = tc.program_slug
  LEFT JOIN (SELECT program_slug, COUNT(*) AS cnt FROM user_program_access GROUP BY program_slug) a ON p.slug = a.program_slug;
$$;


ALTER FUNCTION "public"."get_program_stats"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_programs_overview"() RETURNS TABLE("slug" "text", "program_name" "text", "pi_name" "text", "description" "text", "is_public" boolean, "cycle" integer, "target_count" bigint, "gratings" "text"[], "fields" "text"[], "observations" "text"[], "jwst_pids" integer[])
    LANGUAGE "sql" STABLE
    AS $$
  SELECT mv.slug, mv.program_name, mv.pi_name, mv.description, mv.is_public, mv.cycle,
    mv.target_count, mv.gratings, mv.fields, mv.observations, mv.jwst_pids
  FROM public.mv_programs_overview mv ORDER BY mv.program_name;
$$;


ALTER FUNCTION "public"."get_programs_overview"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_targets_for_sync"("p_program_slugs" "text"[], "p_updated_since" timestamp without time zone DEFAULT NULL::timestamp without time zone, "p_limit" integer DEFAULT 1000, "p_offset" integer DEFAULT 0) RETURNS TABLE("targets" "jsonb", "total_count" bigint, "total_accessible_count" bigint)
    LANGUAGE "plpgsql" STABLE
    SET "plan_cache_mode" TO 'force_custom_plan'
    AS $$
BEGIN
  RETURN QUERY
  WITH matched AS (
    SELECT t.id, t.target_id, t.program_slug, t.field, t.observation,
           t.ra, t.dec, t.redshift, t.redshift_auto, t.redshift_inspected,
           t.redshift_quality, t.spectral_features, t.object_flags, t.dq_flags,
           t.max_snr, t.max_exposure_time, t.last_inspected_at, t.created_at, t.updated_at
    FROM targets t
    WHERE t.program_slug = ANY(p_program_slugs)
      AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)
    ORDER BY t.target_id LIMIT p_limit OFFSET p_offset
  ),
  total AS (SELECT COUNT(*) AS cnt FROM targets t WHERE t.program_slug = ANY(p_program_slugs) AND (p_updated_since IS NULL OR t.updated_at > p_updated_since)),
  accessible AS (SELECT COUNT(*) AS cnt FROM targets t WHERE t.program_slug = ANY(p_program_slugs))
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', m.id, 'target_id', m.target_id, 'program_slug', m.program_slug,
      'program_name', pr.program_name, 'field', m.field, 'observation', m.observation,
      'ra', m.ra, 'dec', m.dec, 'redshift', m.redshift,
      'redshift_auto', m.redshift_auto, 'redshift_inspected', m.redshift_inspected,
      'redshift_quality', m.redshift_quality, 'spectral_features', m.spectral_features,
      'object_flags', m.object_flags, 'dq_flags', m.dq_flags,
      'max_snr', m.max_snr, 'max_exposure_time', m.max_exposure_time,
      'last_inspected_at', m.last_inspected_at, 'created_at', m.created_at, 'updated_at', m.updated_at,
      'spectra', COALESCE(
        (SELECT jsonb_agg(jsonb_build_object(
          'id', s.id, 'target_id', s.target_id, 'grating', s.grating,
          'fits_path', s.fits_path, 'file_hash', s.file_hash, 'file_size', s.file_size,
          'signal_to_noise', s.signal_to_noise, 'exposure_time', s.exposure_time,
          'reduction_version', s.reduction_version
        )) FROM spectra s WHERE s.target_id = m.target_id),
        '[]'::jsonb)
    )), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m LEFT JOIN programs pr ON m.program_slug = pr.slug;
END;
$$;


ALTER FUNCTION "public"."get_targets_for_sync"("p_program_slugs" "text"[], "p_updated_since" timestamp without time zone, "p_limit" integer, "p_offset" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_targets_in_viewport"("p_ra_min" double precision, "p_ra_max" double precision, "p_dec_min" double precision, "p_dec_max" double precision, "p_field" "text" DEFAULT NULL::"text", "p_limit" integer DEFAULT 5000) RETURNS TABLE("target_id" "text", "ra" double precision, "dec" double precision, "redshift" double precision, "redshift_quality" integer, "field" "text", "program_slug" "text")
    LANGUAGE "plpgsql" STABLE
    AS $$
BEGIN
  RETURN QUERY
  SELECT t.target_id, t.ra, t.dec, t.redshift::double precision, t.redshift_quality, t.field, t.program_slug
  FROM public.targets t
  WHERE t.ra BETWEEN p_ra_min AND p_ra_max AND t.dec BETWEEN p_dec_min AND p_dec_max
    AND (p_field IS NULL OR t.field = p_field)
  ORDER BY t.ra LIMIT p_limit;
END;
$$;


ALTER FUNCTION "public"."get_targets_in_viewport"("p_ra_min" double precision, "p_ra_max" double precision, "p_dec_min" double precision, "p_dec_max" double precision, "p_field" "text", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") RETURNS json
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    AS $$
DECLARE
  result JSON;
  targets_inspected BIGINT;
  comments_posted BIGINT;
  last_comment_at TIMESTAMPTZ;
  last_inspection_at TIMESTAMPTZ;
  last_activity TIMESTAMPTZ;
BEGIN
  SELECT COUNT(DISTINCT target_id) INTO targets_inspected
  FROM flag_audit_log
  WHERE user_id = p_user_id;

  SELECT COUNT(*) INTO comments_posted
  FROM comments
  WHERE user_id = p_user_id AND is_deleted = false;

  SELECT created_at INTO last_comment_at
  FROM comments
  WHERE user_id = p_user_id
  ORDER BY created_at DESC
  LIMIT 1;

  SELECT changed_at INTO last_inspection_at
  FROM flag_audit_log
  WHERE user_id = p_user_id
  ORDER BY changed_at DESC
  LIMIT 1;

  last_activity := GREATEST(
    COALESCE(last_comment_at, '1970-01-01'::timestamptz),
    COALESCE(last_inspection_at, '1970-01-01'::timestamptz)
  );
  IF last_activity = '1970-01-01'::timestamptz THEN
    last_activity := NULL;
  END IF;

  result := json_build_object(
    'targets_inspected', COALESCE(targets_inspected, 0),
    'comments_posted', COALESCE(comments_posted, 0),
    'last_activity', last_activity
  );

  RETURN result;
END;
$$;


ALTER FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."increment_tile_version"("p_field" "text", "p_filter" "text") RETURNS "void"
    LANGUAGE "sql"
    AS $$
    UPDATE public.map_layers
    SET tile_version = tile_version + 1
    WHERE field = p_field AND filter = p_filter;
$$;


ALTER FUNCTION "public"."increment_tile_version"("p_field" "text", "p_filter" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."is_admin"() RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
  SELECT COALESCE(
    (SELECT is_admin FROM user_profiles WHERE user_id = auth.uid()),
    false
  );
$$;


ALTER FUNCTION "public"."is_admin"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."log_flag_changes"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    IF OLD.spectral_features IS DISTINCT FROM NEW.spectral_features THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'spectral_features', OLD.spectral_features, NEW.spectral_features);
    END IF;
    IF OLD.object_flags IS DISTINCT FROM NEW.object_flags THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'object_flags', OLD.object_flags, NEW.object_flags);
    END IF;
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (target_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."log_flag_changes"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."propagate_crossmatch_inspection"("p_target_id" integer, "p_radius_arcsec" double precision DEFAULT 0.1, "p_redshift_tolerance" double precision DEFAULT 0.01) RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
    v_source RECORD;
    v_radius_deg DOUBLE PRECISION;
    v_ra_radius_deg DOUBLE PRECISION;
    v_updated_count INTEGER := 0;
BEGIN
    SELECT id, ra, dec, redshift, redshift_auto, redshift_quality
    INTO v_source
    FROM targets
    WHERE id = p_target_id;

    IF NOT FOUND THEN
        RETURN 0;
    END IF;

    v_radius_deg := p_radius_arcsec / 3600.0;
    v_ra_radius_deg := v_radius_deg / COS(RADIANS(v_source.dec));

    IF v_source.redshift_quality = 4 AND v_source.redshift IS NOT NULL THEN
        WITH matches AS (
            SELECT t.id
            FROM targets t
            WHERE t.id != v_source.id
              AND t.redshift_quality = 0
              AND t.redshift_auto IS NOT NULL
              AND ABS(t.redshift_auto - v_source.redshift) < p_redshift_tolerance
              AND t.ra BETWEEN (v_source.ra - v_ra_radius_deg) AND (v_source.ra + v_ra_radius_deg)
              AND t.dec BETWEEN (v_source.dec - v_radius_deg) AND (v_source.dec + v_radius_deg)
              AND (2 * DEGREES(ASIN(SQRT(
                  POWER(SIN(RADIANS(t.dec - v_source.dec) / 2), 2) +
                  COS(RADIANS(v_source.dec)) * COS(RADIANS(t.dec)) *
                  POWER(SIN(RADIANS(t.ra - v_source.ra) / 2), 2)
              )))) <= v_radius_deg
        )
        UPDATE targets
        SET redshift_quality = 4,
            last_inspected_at = NOW()
        FROM matches
        WHERE targets.id = matches.id;

        GET DIAGNOSTICS v_updated_count = ROW_COUNT;

    ELSIF v_source.redshift_quality = 0 AND v_source.redshift_auto IS NOT NULL THEN
        PERFORM 1
        FROM targets t
        WHERE t.id != v_source.id
          AND t.redshift_quality = 4
          AND t.redshift IS NOT NULL
          AND ABS(v_source.redshift_auto - t.redshift) < p_redshift_tolerance
          AND t.ra BETWEEN (v_source.ra - v_ra_radius_deg) AND (v_source.ra + v_ra_radius_deg)
          AND t.dec BETWEEN (v_source.dec - v_radius_deg) AND (v_source.dec + v_radius_deg)
          AND (2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(t.dec - v_source.dec) / 2), 2) +
              COS(RADIANS(v_source.dec)) * COS(RADIANS(t.dec)) *
              POWER(SIN(RADIANS(t.ra - v_source.ra) / 2), 2)
          )))) <= v_radius_deg
        LIMIT 1;

        IF FOUND THEN
            UPDATE targets
            SET redshift_quality = 4,
                last_inspected_at = NOW()
            WHERE id = v_source.id;

            v_updated_count := 1;
        END IF;
    END IF;

    RETURN v_updated_count;
END;
$$;


ALTER FUNCTION "public"."propagate_crossmatch_inspection"("p_target_id" integer, "p_radius_arcsec" double precision, "p_redshift_tolerance" double precision) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."refresh_filter_options"() RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
  BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_filter_options;
  END;
  $$;


ALTER FUNCTION "public"."refresh_filter_options"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."refresh_programs_overview"() RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_programs_overview;
END;
$$;


ALTER FUNCTION "public"."refresh_programs_overview"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."revoke_all_user_refresh_tokens"("p_user_id" "uuid") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows;
END;
$$;


ALTER FUNCTION "public"."revoke_all_user_refresh_tokens"("p_user_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."revoke_refresh_token"("p_token_id" "uuid", "p_user_id" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  updated_rows INTEGER;
BEGIN
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW()
  WHERE
    id = p_token_id
    AND user_id = p_user_id
    AND is_revoked = FALSE;

  GET DIAGNOSTICS updated_rows = ROW_COUNT;
  RETURN updated_rows > 0;
END;
$$;


ALTER FUNCTION "public"."revoke_refresh_token"("p_token_id" "uuid", "p_user_id" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."rotate_refresh_token"("p_old_token_hash" "text", "p_new_token_hash" "text", "p_expires_at" timestamp with time zone, "p_client_ip" "text" DEFAULT NULL::"text", "p_user_agent" "text" DEFAULT NULL::"text") RETURNS TABLE("success" boolean, "user_id" "uuid", "new_token_id" "uuid")
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  v_user_id UUID;
  v_old_token_id UUID;
  v_new_token_id UUID;
  v_device_name TEXT;
BEGIN
  -- First, validate and get the old token info
  SELECT rt.user_id, rt.id, rt.device_name
  INTO v_user_id, v_old_token_id, v_device_name
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_old_token_hash
    AND rt.is_revoked = FALSE
    AND rt.expires_at > NOW();

  IF v_user_id IS NULL THEN
    -- Token not found or invalid
    RETURN QUERY SELECT FALSE, NULL::UUID, NULL::UUID;
    RETURN;
  END IF;

  -- Create new token
  INSERT INTO refresh_tokens (
    token_hash,
    user_id,
    device_name,
    expires_at,
    client_ip,
    user_agent
  ) VALUES (
    p_new_token_hash,
    v_user_id,
    v_device_name,
    p_expires_at,
    p_client_ip,
    p_user_agent
  )
  RETURNING id INTO v_new_token_id;

  -- Revoke old token and link to new one
  UPDATE refresh_tokens
  SET
    is_revoked = TRUE,
    revoked_at = NOW(),
    replaced_by = v_new_token_id
  WHERE id = v_old_token_id;

  RETURN QUERY SELECT TRUE, v_user_id, v_new_token_id;
END;
$$;


ALTER FUNCTION "public"."rotate_refresh_token"("p_old_token_hash" "text", "p_new_token_hash" "text", "p_expires_at" timestamp with time zone, "p_client_ip" "text", "p_user_agent" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."update_api_key_last_used"("key_hash_input" "text") RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
  UPDATE api_keys
  SET last_used_at = NOW()
  WHERE key_hash = key_hash_input;
END;
$$;


ALTER FUNCTION "public"."update_api_key_last_used"("key_hash_input" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."update_target_max_exposure_time"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
  v_target_id TEXT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_target_id := OLD.target_id;
  ELSE
    v_target_id := NEW.target_id;
  END IF;
  UPDATE targets
  SET max_exposure_time = (
    SELECT MAX(exposure_time)
    FROM spectra
    WHERE spectra.target_id = v_target_id
  )
  WHERE targets.target_id = v_target_id;
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."update_target_max_exposure_time"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."update_target_max_snr"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
  v_target_id TEXT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_target_id := OLD.target_id;
  ELSE
    v_target_id := NEW.target_id;
  END IF;
  UPDATE targets
  SET max_snr = (
    SELECT MAX(signal_to_noise)
    FROM spectra
    WHERE spectra.target_id = v_target_id
  )
  WHERE targets.target_id = v_target_id;
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."update_target_max_snr"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."validate_api_key"("key_hash_input" "text") RETURNS TABLE("user_id" "uuid", "is_valid" boolean)
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
  RETURN QUERY
  SELECT
    ak.user_id,
    (ak.is_active AND (ak.expires_at IS NULL OR ak.expires_at > NOW()))::BOOLEAN AS is_valid
  FROM api_keys ak
  WHERE ak.key_hash = key_hash_input;
END;
$$;


ALTER FUNCTION "public"."validate_api_key"("key_hash_input" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."validate_refresh_token"("p_token_hash" "text") RETURNS TABLE("is_valid" boolean, "user_id" "uuid", "token_id" "uuid")
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
  -- Also update last_used_at when validating
  UPDATE refresh_tokens
  SET last_used_at = NOW()
  WHERE token_hash = p_token_hash
    AND is_revoked = FALSE
    AND expires_at > NOW();

  RETURN QUERY
  SELECT
    (rt.is_revoked = FALSE AND rt.expires_at > NOW())::BOOLEAN AS is_valid,
    rt.user_id,
    rt.id AS token_id
  FROM refresh_tokens rt
  WHERE rt.token_hash = p_token_hash;
END;
$$;


ALTER FUNCTION "public"."validate_refresh_token"("p_token_hash" "text") OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."access_codes" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "code" "text" NOT NULL,
    "description" "text",
    "grants_all_programs" boolean DEFAULT false,
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "expires_at" timestamp with time zone,
    "max_uses" integer,
    "use_count" integer DEFAULT 0,
    "is_active" boolean DEFAULT true,
    "program_slugs" "text"[]
);


ALTER TABLE "public"."access_codes" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."account_requests" (
    "id" integer NOT NULL,
    "email" "text" NOT NULL,
    "full_name" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "is_admin" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "reviewed_at" timestamp with time zone,
    "reviewed_by" "uuid",
    "rejection_reason" "text",
    "program_slugs" "text"[],
    CONSTRAINT "account_requests_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'approved'::"text", 'rejected'::"text"])))
);


ALTER TABLE "public"."account_requests" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."account_requests_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."account_requests_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."account_requests_id_seq" OWNED BY "public"."account_requests"."id";



CREATE TABLE IF NOT EXISTS "public"."api_keys" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "key_hash" "text" NOT NULL,
    "key_prefix" "text" NOT NULL,
    "name" "text",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "last_used_at" timestamp with time zone,
    "expires_at" timestamp with time zone,
    "is_active" boolean DEFAULT true,
    "rate_limit_per_minute" integer DEFAULT 60
);


ALTER TABLE "public"."api_keys" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."code_redemptions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "code_id" "uuid",
    "user_id" "uuid",
    "redeemed_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."code_redemptions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."comments" (
    "id" integer NOT NULL,
    "target_id" integer NOT NULL,
    "user_id" "uuid" NOT NULL,
    "content" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "edited_at" timestamp without time zone,
    "is_deleted" boolean DEFAULT false
);


ALTER TABLE "public"."comments" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."comments_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."comments_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."comments_id_seq" OWNED BY "public"."comments"."id";



CREATE TABLE IF NOT EXISTS "public"."device_codes" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "device_code" "text" NOT NULL,
    "user_code" "text" NOT NULL,
    "user_id" "uuid",
    "verification_uri" "text" NOT NULL,
    "expires_at" timestamp with time zone NOT NULL,
    "interval_seconds" integer DEFAULT 5,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "authorized_at" timestamp with time zone,
    "client_ip" "text",
    "user_agent" "text"
);


ALTER TABLE "public"."device_codes" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."download_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "download_type" "text" NOT NULL,
    "target_count" integer,
    "file_count" integer,
    "target_ids" "text"[],
    "filter_snapshot" "jsonb",
    "ip_address" "text",
    "user_agent" "text",
    "requested_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "download_log_download_type_check" CHECK (("download_type" = ANY (ARRAY['fits_single'::"text", 'fits_object'::"text", 'fits_batch'::"text", 'fits_zip'::"text", 'csv'::"text", 'sed_plot'::"text", 'fits_sync'::"text"])))
);


ALTER TABLE "public"."download_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."flag_audit_log" (
    "id" integer NOT NULL,
    "target_id" integer NOT NULL,
    "user_id" "uuid",
    "field_name" "text" NOT NULL,
    "old_value" integer,
    "new_value" integer,
    "changed_at" timestamp without time zone DEFAULT "now"()
);


ALTER TABLE "public"."flag_audit_log" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."flag_audit_log_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."flag_audit_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."flag_audit_log_id_seq" OWNED BY "public"."flag_audit_log"."id";



CREATE TABLE IF NOT EXISTS "public"."flag_definitions" (
    "category" "text" NOT NULL,
    "bit_position" integer,
    "value" integer NOT NULL,
    "label" "text" NOT NULL,
    "short_label" "text",
    "icon" "text",
    "color" "text",
    "description" "text"
);


ALTER TABLE "public"."flag_definitions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."map_layers" (
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "filter" "text" NOT NULL,
    "tile_base_url" "text" NOT NULL,
    "min_zoom" integer NOT NULL,
    "max_zoom" integer NOT NULL,
    "tile_size" integer DEFAULT 256 NOT NULL,
    "ra_min" double precision NOT NULL,
    "ra_max" double precision NOT NULL,
    "dec_min" double precision NOT NULL,
    "dec_max" double precision NOT NULL,
    "wcs_params" "jsonb" NOT NULL,
    "image_width" integer NOT NULL,
    "image_height" integer NOT NULL,
    "total_tiles" integer,
    "total_size_bytes" bigint,
    "is_default" boolean DEFAULT false,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "tile_version" integer DEFAULT 1 NOT NULL
);


ALTER TABLE "public"."map_layers" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."map_layers_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."map_layers_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."map_layers_id_seq" OWNED BY "public"."map_layers"."id";



CREATE TABLE IF NOT EXISTS "public"."spectra" (
    "id" integer NOT NULL,
    "grating" "text" NOT NULL,
    "fits_path" "text" NOT NULL,
    "reduction_version" "text" DEFAULT 'v1.0'::"text",
    "signal_to_noise" double precision,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "target_id" "text" NOT NULL,
    "thumbnail_svg_fnu" "text",
    "thumbnail_svg_flambda" "text",
    "file_hash" "text",
    "file_size" bigint,
    "exposure_time" double precision
);


ALTER TABLE "public"."spectra" OWNER TO "postgres";


COMMENT ON COLUMN "public"."spectra"."thumbnail_svg_fnu" IS 'Pre-generated SVG sparkline thumbnail in f_nu units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';



COMMENT ON COLUMN "public"."spectra"."thumbnail_svg_flambda" IS 'Pre-generated SVG sparkline thumbnail in f_lambda units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';



COMMENT ON COLUMN "public"."spectra"."file_hash" IS 'SHA-256 hash of the FITS file in R2. Used for incremental sync to detect changed files.';



COMMENT ON COLUMN "public"."spectra"."file_size" IS 'Size of the FITS file in bytes. Used for download size estimation and verification.';



COMMENT ON COLUMN "public"."spectra"."exposure_time" IS 'Effective exposure time in seconds, from EFFEXPTM FITS header.';



CREATE TABLE IF NOT EXISTS "public"."targets" (
    "id" integer NOT NULL,
    "target_id" "text" NOT NULL,
    "field" "text" NOT NULL,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "redshift_auto" double precision,
    "redshift_quality" integer DEFAULT 0,
    "spectral_features" integer DEFAULT 0,
    "object_flags" integer DEFAULT 0,
    "dq_flags" integer DEFAULT 0,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "updated_at" timestamp without time zone DEFAULT "now"(),
    "redshift_inspected" numeric(10,6) DEFAULT NULL::numeric,
    "last_inspected_at" timestamp with time zone,
    "last_inspected_by" "uuid",
    "max_snr" double precision,
    "redshift" numeric(10,6) GENERATED ALWAYS AS (
CASE
    WHEN ("redshift_quality" = 1) THEN NULL::double precision
    ELSE COALESCE(("redshift_inspected")::double precision, "redshift_auto")
END) STORED,
    "has_sed_plot" boolean DEFAULT false NOT NULL,
    "max_exposure_time" double precision,
    "program_slug" "text" NOT NULL,
    "observation" "text" NOT NULL
);


ALTER TABLE "public"."targets" OWNER TO "postgres";


COMMENT ON COLUMN "public"."targets"."redshift" IS 'Generated column: NULL when redshift_quality = 1 (Impossible), otherwise COALESCE(redshift_inspected, redshift_auto). This allows "Impossible" objects to be excluded from redshift range filters.';



COMMENT ON COLUMN "public"."targets"."has_sed_plot" IS 'Indicates whether an SED plot PDF exists in R2. Set during deployment to avoid runtime R2 HeadObject calls.';



CREATE MATERIALIZED VIEW "public"."mv_filter_options" AS
 SELECT 1 AS "id",
    ARRAY( SELECT DISTINCT "targets"."field"
           FROM "public"."targets"
          ORDER BY "targets"."field") AS "fields",
    ARRAY( SELECT DISTINCT "targets"."observation"
           FROM "public"."targets"
          WHERE ("targets"."observation" IS NOT NULL)
          ORDER BY "targets"."observation") AS "observations",
    ARRAY( SELECT DISTINCT "spectra"."grating"
           FROM "public"."spectra"
          ORDER BY "spectra"."grating") AS "gratings"
  WITH NO DATA;


ALTER MATERIALIZED VIEW "public"."mv_filter_options" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."observations" (
    "name" "text" NOT NULL,
    "program_slug" "text" NOT NULL,
    "jwst_program_id" integer NOT NULL,
    "field" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."observations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."programs" (
    "slug" "text" NOT NULL,
    "program_name" "text" NOT NULL,
    "pi_name" "text",
    "description" "text",
    "cycle" integer,
    "is_public" boolean DEFAULT false,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."programs" OWNER TO "postgres";


CREATE MATERIALIZED VIEW "public"."mv_programs_overview" AS
 SELECT "p"."slug",
    "p"."program_name",
    "p"."pi_name",
    "p"."description",
    "p"."is_public",
    "p"."cycle",
    COALESCE("stats"."object_count", (0)::bigint) AS "target_count",
    COALESCE("stats"."gratings", ARRAY[]::"text"[]) AS "gratings",
    COALESCE("stats"."fields", ARRAY[]::"text"[]) AS "fields",
    COALESCE("stats"."observations", ARRAY[]::"text"[]) AS "observations",
    COALESCE("pids"."jwst_pids", ARRAY[]::integer[]) AS "jwst_pids"
   FROM (("public"."programs" "p"
     LEFT JOIN ( SELECT "o"."program_slug",
            "count"(DISTINCT "o"."target_id") AS "object_count",
            "array_agg"(DISTINCT "s"."grating" ORDER BY "s"."grating") FILTER (WHERE ("s"."grating" IS NOT NULL)) AS "gratings",
            "array_agg"(DISTINCT "o"."field" ORDER BY "o"."field") AS "fields",
            "array_agg"(DISTINCT "o"."observation" ORDER BY "o"."observation") AS "observations"
           FROM ("public"."targets" "o"
             LEFT JOIN "public"."spectra" "s" ON (("s"."target_id" = "o"."target_id")))
          GROUP BY "o"."program_slug") "stats" ON (("p"."slug" = "stats"."program_slug")))
     LEFT JOIN ( SELECT "observations"."program_slug",
            "array_agg"(DISTINCT "observations"."jwst_program_id" ORDER BY "observations"."jwst_program_id") AS "jwst_pids"
           FROM "public"."observations"
          GROUP BY "observations"."program_slug") "pids" ON (("p"."slug" = "pids"."program_slug")))
  WITH NO DATA;


ALTER MATERIALIZED VIEW "public"."mv_programs_overview" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."nircam_images" (
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "tile" "text" NOT NULL,
    "filter" "text" NOT NULL,
    "pixel_scale" "text" NOT NULL,
    "version" "text" NOT NULL,
    "extension" "text" NOT NULL,
    "file_path" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "file_size" bigint
);


ALTER TABLE "public"."nircam_images" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."nircam_images_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."nircam_images_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."nircam_images_id_seq" OWNED BY "public"."nircam_images"."id";



CREATE TABLE IF NOT EXISTS "public"."password_reset_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "reset_at" timestamp with time zone DEFAULT "now"(),
    "ip_address" "text",
    "user_agent" "text"
);


ALTER TABLE "public"."password_reset_log" OWNER TO "postgres";


COMMENT ON TABLE "public"."password_reset_log" IS 'Logs password reset attempts for security monitoring';



COMMENT ON COLUMN "public"."password_reset_log"."user_id" IS 'User who reset their password';



COMMENT ON COLUMN "public"."password_reset_log"."reset_at" IS 'When the password was reset';



COMMENT ON COLUMN "public"."password_reset_log"."ip_address" IS 'IP address from which the reset was performed';



COMMENT ON COLUMN "public"."password_reset_log"."user_agent" IS 'Browser user agent string';



CREATE TABLE IF NOT EXISTS "public"."pending_invites" (
    "id" integer NOT NULL,
    "email" "text" NOT NULL,
    "is_admin" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "invited_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "accepted_at" timestamp with time zone,
    "full_name" "text",
    "program_slugs" "text"[]
);


ALTER TABLE "public"."pending_invites" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."pending_invites_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."pending_invites_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."pending_invites_id_seq" OWNED BY "public"."pending_invites"."id";



CREATE TABLE IF NOT EXISTS "public"."refresh_tokens" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "token_hash" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "device_name" "text",
    "expires_at" timestamp with time zone NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "last_used_at" timestamp with time zone,
    "is_revoked" boolean DEFAULT false,
    "revoked_at" timestamp with time zone,
    "replaced_by" "uuid",
    "client_ip" "text",
    "user_agent" "text"
);


ALTER TABLE "public"."refresh_tokens" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."shutters" (
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "observation" "text" NOT NULL,
    "object_id" "text" NOT NULL,
    "source_id" integer NOT NULL,
    "center_ra" double precision NOT NULL,
    "center_dec" double precision NOT NULL,
    "position_angle" double precision NOT NULL,
    "shutter_idx" smallint NOT NULL,
    "dither_id" smallint DEFAULT 0 NOT NULL,
    "shutter_state" "text" DEFAULT 'open'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."shutters" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."shutters_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."shutters_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."shutters_id_seq" OWNED BY "public"."shutters"."id";



CREATE TABLE IF NOT EXISTS "public"."slit_regions" (
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "observation" "text" NOT NULL,
    "object_id" "text" NOT NULL,
    "grating" "text",
    "center_ra" double precision NOT NULL,
    "center_dec" double precision NOT NULL,
    "position_angle" double precision NOT NULL,
    "shutter_idx" smallint NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."slit_regions" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."slit_regions_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."slit_regions_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."slit_regions_id_seq" OWNED BY "public"."slit_regions"."id";



CREATE SEQUENCE IF NOT EXISTS "public"."spectra_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."spectra_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."spectra_id_seq" OWNED BY "public"."spectra"."id";



CREATE OR REPLACE VIEW "public"."target_flag_summary" AS
 SELECT "o"."id",
    "o"."target_id" AS "object_id",
    "array_agg"(DISTINCT "fd"."label") FILTER (WHERE (("fd"."category" = 'spectral_features'::"text") AND (("o"."spectral_features" & "fd"."value") > 0))) AS "spectral_features_labels",
    "array_agg"(DISTINCT "fd"."label") FILTER (WHERE (("fd"."category" = 'object_flags'::"text") AND (("o"."object_flags" & "fd"."value") > 0))) AS "object_flags_labels",
    "array_agg"(DISTINCT "fd"."label") FILTER (WHERE (("fd"."category" = 'dq_flags'::"text") AND (("o"."dq_flags" & "fd"."value") > 0))) AS "dq_flags_labels"
   FROM ("public"."targets" "o"
     CROSS JOIN "public"."flag_definitions" "fd")
  GROUP BY "o"."id", "o"."target_id";


ALTER VIEW "public"."target_flag_summary" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."targets_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."targets_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."targets_id_seq" OWNED BY "public"."targets"."id";



CREATE OR REPLACE VIEW "public"."targets_with_flags" AS
 SELECT "o"."id",
    "o"."target_id" AS "object_id",
    "o"."program_slug",
    "o"."field",
    "o"."ra",
    "o"."dec",
    "o"."redshift_auto" AS "redshift",
    "o"."redshift_quality",
    "o"."spectral_features",
    "o"."object_flags",
    "o"."dq_flags",
    "o"."created_at",
    "o"."updated_at",
    "rq"."label" AS "redshift_quality_label",
    "rq"."icon" AS "redshift_quality_icon",
    "rq"."color" AS "redshift_quality_color"
   FROM ("public"."targets" "o"
     LEFT JOIN "public"."flag_definitions" "rq" ON ((("rq"."category" = 'redshift_quality'::"text") AND ("rq"."value" = "o"."redshift_quality"))));


ALTER VIEW "public"."targets_with_flags" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_profiles" (
    "user_id" "uuid" NOT NULL,
    "full_name" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "is_group_account" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "is_admin" boolean DEFAULT false,
    "preferences" "jsonb" DEFAULT '{}'::"jsonb"
);


ALTER TABLE "public"."user_profiles" OWNER TO "postgres";


COMMENT ON COLUMN "public"."user_profiles"."preferences" IS 'User preferences including theme (light/dark/system) and spectrum viewer settings (flux unit, colorscale, SNR range, spectrum color)';



CREATE TABLE IF NOT EXISTS "public"."user_program_access" (
    "user_id" "uuid" NOT NULL,
    "granted_at" timestamp without time zone DEFAULT "now"(),
    "granted_by" "uuid",
    "program_slug" "text" NOT NULL
);


ALTER TABLE "public"."user_program_access" OWNER TO "postgres";


ALTER TABLE ONLY "public"."account_requests" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."account_requests_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."comments" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."comments_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."flag_audit_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."flag_audit_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."map_layers" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."map_layers_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."nircam_images" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nircam_images_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."pending_invites" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."pending_invites_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."shutters" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."shutters_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."slit_regions" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."slit_regions_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."spectra" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."spectra_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."targets" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."targets_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."access_codes"
    ADD CONSTRAINT "access_codes_code_key" UNIQUE ("code");



ALTER TABLE ONLY "public"."access_codes"
    ADD CONSTRAINT "access_codes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."account_requests"
    ADD CONSTRAINT "account_requests_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."account_requests"
    ADD CONSTRAINT "account_requests_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_key_hash_key" UNIQUE ("key_hash");



ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_code_id_user_id_key" UNIQUE ("code_id", "user_id");



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."device_codes"
    ADD CONSTRAINT "device_codes_device_code_key" UNIQUE ("device_code");



ALTER TABLE ONLY "public"."device_codes"
    ADD CONSTRAINT "device_codes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."device_codes"
    ADD CONSTRAINT "device_codes_user_code_key" UNIQUE ("user_code");



ALTER TABLE ONLY "public"."download_log"
    ADD CONSTRAINT "download_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."flag_definitions"
    ADD CONSTRAINT "flag_definitions_pkey" PRIMARY KEY ("category", "value");



ALTER TABLE ONLY "public"."map_layers"
    ADD CONSTRAINT "map_layers_field_filter_key" UNIQUE ("field", "filter");



ALTER TABLE ONLY "public"."map_layers"
    ADD CONSTRAINT "map_layers_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_field_tile_filter_pixel_scale_version_extensi_key" UNIQUE ("field", "tile", "filter", "pixel_scale", "version", "extension");



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_unique" UNIQUE ("field", "tile", "filter", "pixel_scale", "version", "extension");



ALTER TABLE ONLY "public"."observations"
    ADD CONSTRAINT "observations_pkey" PRIMARY KEY ("name");



ALTER TABLE ONLY "public"."password_reset_log"
    ADD CONSTRAINT "password_reset_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."programs"
    ADD CONSTRAINT "programs_pkey1" PRIMARY KEY ("slug");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_token_hash_key" UNIQUE ("token_hash");



ALTER TABLE ONLY "public"."shutters"
    ADD CONSTRAINT "shutters_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."slit_regions"
    ADD CONSTRAINT "slit_regions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."spectra"
    ADD CONSTRAINT "spectra_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "targets_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "targets_target_id_key" UNIQUE ("target_id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_pkey" PRIMARY KEY ("user_id", "program_slug");



CREATE INDEX "idx_access_codes_code" ON "public"."access_codes" USING "btree" ("code");



CREATE INDEX "idx_account_requests_created_at" ON "public"."account_requests" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_account_requests_email" ON "public"."account_requests" USING "btree" ("email");



CREATE INDEX "idx_account_requests_status" ON "public"."account_requests" USING "btree" ("status");



CREATE INDEX "idx_api_keys_is_active" ON "public"."api_keys" USING "btree" ("is_active") WHERE ("is_active" = true);



CREATE INDEX "idx_api_keys_key_hash" ON "public"."api_keys" USING "btree" ("key_hash");



CREATE INDEX "idx_api_keys_user_id" ON "public"."api_keys" USING "btree" ("user_id");



CREATE INDEX "idx_audit_target" ON "public"."flag_audit_log" USING "btree" ("target_id");



CREATE INDEX "idx_audit_time" ON "public"."flag_audit_log" USING "btree" ("changed_at" DESC);



CREATE INDEX "idx_audit_user" ON "public"."flag_audit_log" USING "btree" ("user_id");



CREATE INDEX "idx_code_redemptions_user" ON "public"."code_redemptions" USING "btree" ("user_id");



CREATE INDEX "idx_comments_content_trgm" ON "public"."comments" USING "gin" ("content" "public"."gin_trgm_ops");



CREATE INDEX "idx_comments_created" ON "public"."comments" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_comments_target" ON "public"."comments" USING "btree" ("target_id");



CREATE INDEX "idx_comments_user" ON "public"."comments" USING "btree" ("user_id");



CREATE INDEX "idx_device_codes_device_code" ON "public"."device_codes" USING "btree" ("device_code");



CREATE INDEX "idx_device_codes_expires_at" ON "public"."device_codes" USING "btree" ("expires_at");



CREATE INDEX "idx_device_codes_status" ON "public"."device_codes" USING "btree" ("status") WHERE ("status" = 'pending'::"text");



CREATE INDEX "idx_device_codes_user_code" ON "public"."device_codes" USING "btree" ("user_code");



CREATE INDEX "idx_download_log_download_type" ON "public"."download_log" USING "btree" ("download_type");



CREATE INDEX "idx_download_log_requested_at" ON "public"."download_log" USING "btree" ("requested_at" DESC);



CREATE INDEX "idx_download_log_target_ids" ON "public"."download_log" USING "gin" ("target_ids");



CREATE INDEX "idx_download_log_user_id" ON "public"."download_log" USING "btree" ("user_id");



CREATE INDEX "idx_flag_audit_log_target_id" ON "public"."flag_audit_log" USING "btree" ("target_id");



CREATE INDEX "idx_images_field" ON "public"."nircam_images" USING "btree" ("field");



CREATE INDEX "idx_images_filter" ON "public"."nircam_images" USING "btree" ("filter");



CREATE INDEX "idx_map_layers_field" ON "public"."map_layers" USING "btree" ("field");



CREATE INDEX "idx_objects_redshift_generated" ON "public"."targets" USING "btree" ("redshift") WHERE ("redshift" IS NOT NULL);



CREATE INDEX "idx_objects_redshift_quality" ON "public"."targets" USING "btree" ("redshift_quality");



CREATE INDEX "idx_observations_jwst_pid" ON "public"."observations" USING "btree" ("jwst_program_id");



CREATE INDEX "idx_observations_program_slug" ON "public"."observations" USING "btree" ("program_slug");



CREATE INDEX "idx_password_reset_log_reset_at" ON "public"."password_reset_log" USING "btree" ("reset_at" DESC);



CREATE INDEX "idx_password_reset_log_user_id" ON "public"."password_reset_log" USING "btree" ("user_id");



CREATE INDEX "idx_pending_invites_email" ON "public"."pending_invites" USING "btree" ("email");



CREATE INDEX "idx_refresh_tokens_active" ON "public"."refresh_tokens" USING "btree" ("user_id", "expires_at") WHERE ("is_revoked" = false);



CREATE INDEX "idx_refresh_tokens_token_hash" ON "public"."refresh_tokens" USING "btree" ("token_hash");



CREATE INDEX "idx_refresh_tokens_user_id" ON "public"."refresh_tokens" USING "btree" ("user_id");



CREATE INDEX "idx_shutters_field" ON "public"."shutters" USING "btree" ("field");



CREATE INDEX "idx_shutters_object_id" ON "public"."shutters" USING "btree" ("object_id");



CREATE INDEX "idx_shutters_observation" ON "public"."shutters" USING "btree" ("observation");



CREATE INDEX "idx_shutters_ra_dec" ON "public"."shutters" USING "btree" ("center_ra", "center_dec");



CREATE INDEX "idx_slit_regions_field" ON "public"."slit_regions" USING "btree" ("field");



CREATE INDEX "idx_spectra_file_hash" ON "public"."spectra" USING "btree" ("file_hash") WHERE ("file_hash" IS NOT NULL);



CREATE UNIQUE INDEX "idx_spectra_fits_path" ON "public"."spectra" USING "btree" ("fits_path");



CREATE INDEX "idx_spectra_grating" ON "public"."spectra" USING "btree" ("grating");



CREATE INDEX "idx_spectra_target_grating" ON "public"."spectra" USING "btree" ("target_id", "grating");



CREATE INDEX "idx_spectra_target_id" ON "public"."spectra" USING "btree" ("target_id") INCLUDE ("grating", "fits_path");



CREATE INDEX "idx_targets_coords" ON "public"."targets" USING "btree" ("ra", "dec");



CREATE INDEX "idx_targets_field" ON "public"."targets" USING "btree" ("field");



CREATE INDEX "idx_targets_field_observation" ON "public"."targets" USING "btree" ("field", "observation");



CREATE INDEX "idx_targets_has_sed_plot" ON "public"."targets" USING "btree" ("has_sed_plot") WHERE ("has_sed_plot" = true);



CREATE INDEX "idx_targets_max_exposure_time" ON "public"."targets" USING "btree" ("max_exposure_time") WHERE ("max_exposure_time" IS NOT NULL);



CREATE INDEX "idx_targets_max_snr" ON "public"."targets" USING "btree" ("max_snr") WHERE ("max_snr" IS NOT NULL);



CREATE INDEX "idx_targets_observation" ON "public"."targets" USING "btree" ("observation");



CREATE INDEX "idx_targets_program_slug" ON "public"."targets" USING "btree" ("program_slug");



CREATE INDEX "idx_targets_program_slug_field" ON "public"."targets" USING "btree" ("program_slug", "field");



CREATE INDEX "idx_targets_program_slug_quality" ON "public"."targets" USING "btree" ("program_slug", "redshift_quality");



CREATE INDEX "idx_targets_target_id_trgm" ON "public"."targets" USING "gin" ("target_id" "public"."gin_trgm_ops");



COMMENT ON INDEX "public"."idx_targets_target_id_trgm" IS 'Trigram index for fuzzy text search on object_id. Supports ILIKE with leading/trailing wildcards.
Example: WHERE object_id ILIKE ''%cosmos%'' will use this index.
Alternative: For prefix-only search, use text_pattern_ops index instead.';



CREATE INDEX "idx_targets_updated_at" ON "public"."targets" USING "btree" ("updated_at");



CREATE INDEX "idx_user_profiles_preferences" ON "public"."user_profiles" USING "gin" ("preferences");



CREATE UNIQUE INDEX "mv_filter_options_id" ON "public"."mv_filter_options" USING "btree" ("id");



CREATE UNIQUE INDEX "mv_programs_overview_slug" ON "public"."mv_programs_overview" USING "btree" ("slug");



CREATE OR REPLACE TRIGGER "track_flag_changes" BEFORE UPDATE ON "public"."targets" FOR EACH ROW EXECUTE FUNCTION "public"."log_flag_changes"();



CREATE OR REPLACE TRIGGER "update_max_exposure_time_trigger" AFTER INSERT OR DELETE OR UPDATE ON "public"."spectra" FOR EACH ROW EXECUTE FUNCTION "public"."update_target_max_exposure_time"();



CREATE OR REPLACE TRIGGER "update_max_snr_trigger" AFTER INSERT OR DELETE OR UPDATE ON "public"."spectra" FOR EACH ROW EXECUTE FUNCTION "public"."update_target_max_snr"();



ALTER TABLE ONLY "public"."access_codes"
    ADD CONSTRAINT "access_codes_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."account_requests"
    ADD CONSTRAINT "account_requests_reviewed_by_fkey" FOREIGN KEY ("reviewed_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_code_id_fkey" FOREIGN KEY ("code_id") REFERENCES "public"."access_codes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."device_codes"
    ADD CONSTRAINT "device_codes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."download_log"
    ADD CONSTRAINT "download_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."shutters"
    ADD CONSTRAINT "fk_shutters_observation" FOREIGN KEY ("observation") REFERENCES "public"."observations"("name");



ALTER TABLE ONLY "public"."slit_regions"
    ADD CONSTRAINT "fk_slit_regions_observation" FOREIGN KEY ("observation") REFERENCES "public"."observations"("name");



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "fk_targets_observation" FOREIGN KEY ("observation") REFERENCES "public"."observations"("name");



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "fk_targets_program" FOREIGN KEY ("program_slug") REFERENCES "public"."programs"("slug");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "fk_upa_program" FOREIGN KEY ("program_slug") REFERENCES "public"."programs"("slug");



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."observations"
    ADD CONSTRAINT "observations_program_slug_fkey" FOREIGN KEY ("program_slug") REFERENCES "public"."programs"("slug");



ALTER TABLE ONLY "public"."password_reset_log"
    ADD CONSTRAINT "password_reset_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_invited_by_fkey" FOREIGN KEY ("invited_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_replaced_by_fkey" FOREIGN KEY ("replaced_by") REFERENCES "public"."refresh_tokens"("id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."spectra"
    ADD CONSTRAINT "spectra_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("target_id");



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "targets_last_inspected_by_fkey" FOREIGN KEY ("last_inspected_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_granted_by_fkey" FOREIGN KEY ("granted_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



CREATE POLICY "Anyone can read active codes" ON "public"."access_codes" FOR SELECT USING (("is_active" = true));



CREATE POLICY "Anyone can submit requests" ON "public"."account_requests" FOR INSERT TO "authenticated", "anon" WITH CHECK (true);



CREATE POLICY "Authenticated users can read map layers" ON "public"."map_layers" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "Authenticated users can view shutters" ON "public"."shutters" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "Authenticated users can view slit regions" ON "public"."slit_regions" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "Service role full access" ON "public"."device_codes" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "Service role full access" ON "public"."refresh_tokens" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "Service role has full access to map layers" ON "public"."map_layers" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "Users can check own request status" ON "public"."account_requests" FOR SELECT TO "authenticated", "anon" USING (true);



CREATE POLICY "Users can create own API keys" ON "public"."api_keys" FOR INSERT TO "authenticated" WITH CHECK (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can delete own API keys" ON "public"."api_keys" FOR DELETE TO "authenticated" USING (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can read own invite by email" ON "public"."pending_invites" FOR SELECT TO "authenticated" USING (("email" = (( SELECT "users"."email"
   FROM "auth"."users"
  WHERE ("users"."id" = "auth"."uid"())))::"text"));



CREATE POLICY "Users can redeem codes" ON "public"."code_redemptions" FOR INSERT WITH CHECK (("user_id" = "auth"."uid"()));



CREATE POLICY "Users can see own redemptions" ON "public"."code_redemptions" FOR SELECT USING (("user_id" = "auth"."uid"()));



CREATE POLICY "Users can update own API keys" ON "public"."api_keys" FOR UPDATE TO "authenticated" USING (("auth"."uid"() = "user_id")) WITH CHECK (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can update own tokens" ON "public"."refresh_tokens" FOR UPDATE TO "authenticated" USING (("auth"."uid"() = "user_id")) WITH CHECK (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can view own API keys" ON "public"."api_keys" FOR SELECT TO "authenticated" USING (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can view own downloads" ON "public"."download_log" FOR SELECT TO "authenticated" USING (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can view own reset logs" ON "public"."password_reset_log" FOR SELECT USING (("auth"."uid"() = "user_id"));



CREATE POLICY "Users can view own tokens" ON "public"."refresh_tokens" FOR SELECT TO "authenticated" USING (("auth"."uid"() = "user_id"));



ALTER TABLE "public"."access_codes" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "accessible_observations_select" ON "public"."observations" FOR SELECT TO "authenticated" USING (("program_slug" = ANY ("public"."accessible_program_slugs"())));



CREATE POLICY "accessible_programs_select" ON "public"."programs" FOR SELECT TO "authenticated" USING ((("is_public" = true) OR ("slug" IN ( SELECT "user_program_access"."program_slug"
   FROM "public"."user_program_access"
  WHERE ("user_program_access"."user_id" = "auth"."uid"())))));



ALTER TABLE "public"."account_requests" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "admin_delete_access" ON "public"."user_program_access" FOR DELETE TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_delete_invites" ON "public"."pending_invites" FOR DELETE TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_delete_profile" ON "public"."user_profiles" FOR DELETE TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_insert_access" ON "public"."user_program_access" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_admin"());



CREATE POLICY "admin_insert_invites" ON "public"."pending_invites" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_admin"());



CREATE POLICY "admin_insert_profile" ON "public"."user_profiles" FOR INSERT TO "authenticated" WITH CHECK ("public"."is_admin"());



CREATE POLICY "admin_manage_codes" ON "public"."access_codes" USING ("public"."is_admin"());



CREATE POLICY "admin_programs_select" ON "public"."programs" FOR SELECT TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_programs_update" ON "public"."programs" FOR UPDATE TO "authenticated" USING ("public"."is_admin"()) WITH CHECK ("public"."is_admin"());



CREATE POLICY "admin_select_access" ON "public"."user_program_access" FOR SELECT TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_select_downloads" ON "public"."download_log" FOR SELECT TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_select_invites" ON "public"."pending_invites" FOR SELECT TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_select_redemptions" ON "public"."code_redemptions" FOR SELECT USING ("public"."is_admin"());



CREATE POLICY "admin_select_requests" ON "public"."account_requests" FOR SELECT TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_select_reset_logs" ON "public"."password_reset_log" FOR SELECT USING ("public"."is_admin"());



CREATE POLICY "admin_update_invites" ON "public"."pending_invites" FOR UPDATE TO "authenticated" USING ("public"."is_admin"());



CREATE POLICY "admin_update_profile" ON "public"."user_profiles" FOR UPDATE TO "authenticated" USING ("public"."is_admin"()) WITH CHECK ("public"."is_admin"());



CREATE POLICY "admin_update_requests" ON "public"."account_requests" FOR UPDATE TO "authenticated" USING ("public"."is_admin"());



ALTER TABLE "public"."api_keys" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "authenticated_select_flags" ON "public"."flag_definitions" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "authenticated_select_nircam" ON "public"."nircam_images" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "authenticated_select_profiles" ON "public"."user_profiles" FOR SELECT TO "authenticated" USING (true);



ALTER TABLE "public"."code_redemptions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."comments" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."device_codes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."download_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."flag_audit_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."flag_definitions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "insert_audit_by_access" ON "public"."flag_audit_log" FOR INSERT TO "authenticated" WITH CHECK (("target_id" IN ( SELECT "o"."id"
   FROM "public"."targets" "o"
  WHERE ("o"."program_slug" = ANY ("public"."accessible_program_slugs"())))));



CREATE POLICY "insert_comments_by_access" ON "public"."comments" FOR INSERT WITH CHECK ((("target_id" IN ( SELECT "o"."id"
   FROM "public"."targets" "o"
  WHERE ("o"."program_slug" = ANY ("public"."accessible_program_slugs"())))) AND "public"."can_comment"()));



ALTER TABLE "public"."map_layers" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."nircam_images" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."observations" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."password_reset_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."pending_invites" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."programs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."refresh_tokens" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "select_audit_by_access" ON "public"."flag_audit_log" FOR SELECT USING (("target_id" IN ( SELECT "o"."id"
   FROM "public"."targets" "o"
  WHERE ("o"."program_slug" = ANY ("public"."accessible_program_slugs"())))));



CREATE POLICY "select_comments_by_access" ON "public"."comments" FOR SELECT USING (("target_id" IN ( SELECT "o"."id"
   FROM "public"."targets" "o"
  WHERE ("o"."program_slug" = ANY ("public"."accessible_program_slugs"())))));



CREATE POLICY "select_spectra_by_access" ON "public"."spectra" FOR SELECT USING (("target_id" IN ( SELECT "o"."target_id" AS "object_id"
   FROM "public"."targets" "o"
  WHERE ("o"."program_slug" = ANY ("public"."accessible_program_slugs"())))));



CREATE POLICY "select_targets_by_access" ON "public"."targets" FOR SELECT USING (("program_slug" = ANY ("public"."accessible_program_slugs"())));



CREATE POLICY "self_select_access" ON "public"."user_program_access" FOR SELECT TO "authenticated" USING (("user_id" = "auth"."uid"()));



CREATE POLICY "self_update_profile" ON "public"."user_profiles" FOR UPDATE TO "authenticated" USING (("user_id" = "auth"."uid"())) WITH CHECK (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."shutters" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."slit_regions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."spectra" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."targets" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "update_targets_by_access" ON "public"."targets" FOR UPDATE USING ((("program_slug" = ANY ("public"."accessible_program_slugs"())) AND "public"."can_comment"()));



ALTER TABLE "public"."user_profiles" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."user_program_access" ENABLE ROW LEVEL SECURITY;




ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";


GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_in"("cstring") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_out"("public"."gtrgm") TO "service_role";

























































































































































GRANT ALL ON FUNCTION "public"."accessible_program_slugs"() TO "anon";
GRANT ALL ON FUNCTION "public"."accessible_program_slugs"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."accessible_program_slugs"() TO "service_role";



GRANT ALL ON FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."can_comment"() TO "anon";
GRANT ALL ON FUNCTION "public"."can_comment"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."can_comment"() TO "service_role";



GRANT ALL ON FUNCTION "public"."check_device_code_status"("p_device_code" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."check_device_code_status"("p_device_code" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."check_device_code_status"("p_device_code" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."cleanup_expired_device_codes"() TO "anon";
GRANT ALL ON FUNCTION "public"."cleanup_expired_device_codes"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."cleanup_expired_device_codes"() TO "service_role";



GRANT ALL ON FUNCTION "public"."cleanup_expired_refresh_tokens"() TO "anon";
GRANT ALL ON FUNCTION "public"."cleanup_expired_refresh_tokens"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."cleanup_expired_refresh_tokens"() TO "service_role";



GRANT ALL ON FUNCTION "public"."consume_device_code"("p_device_code" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."consume_device_code"("p_device_code" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."consume_device_code"("p_device_code" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."count_distinct_inspected_objects"("p_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."count_distinct_inspected_objects"("p_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."count_distinct_inspected_objects"("p_user_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."deny_device_code"("p_user_code" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."deny_device_code"("p_user_code" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."deny_device_code"("p_user_code" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_adjacent_targets"("p_current_target_id" "text", "p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."get_adjacent_targets"("p_current_target_id" "text", "p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_adjacent_targets"("p_current_target_id" "text", "p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_csv_export"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."get_csv_export"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_csv_export"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_csv_export_spectra"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."get_csv_export_spectra"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_csv_export_spectra"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_download_stats"("p_days" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."get_download_stats"("p_days" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_download_stats"("p_days" integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_filtered_spectra_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."get_filtered_spectra_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_filtered_spectra_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_filtered_target_ids"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_updated_since" timestamp without time zone) TO "anon";
GRANT ALL ON FUNCTION "public"."get_filtered_target_ids"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_updated_since" timestamp without time zone) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_filtered_target_ids"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_updated_since" timestamp without time zone) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_filtered_targets_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean, "p_updated_since" timestamp without time zone) TO "anon";
GRANT ALL ON FUNCTION "public"."get_filtered_targets_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean, "p_updated_since" timestamp without time zone) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_filtered_targets_paginated"("p_program_slugs" "text"[], "p_filter_programs" "text"[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_max_exposure_time_min" double precision, "p_max_exposure_time_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean, "p_updated_since" timestamp without time zone) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_nearby_shutters"("p_ra" double precision, "p_dec" double precision, "p_radius_arcsec" double precision, "p_field" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."get_nearby_shutters"("p_ra" double precision, "p_dec" double precision, "p_radius_arcsec" double precision, "p_field" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_nearby_shutters"("p_ra" double precision, "p_dec" double precision, "p_radius_arcsec" double precision, "p_field" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_observation_manifest"("p_obs_name" "text", "p_program_slugs" "text"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."get_observation_manifest"("p_obs_name" "text", "p_program_slugs" "text"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_observation_manifest"("p_obs_name" "text", "p_program_slugs" "text"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_observation_stats"("p_program_slugs" "text"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."get_observation_stats"("p_program_slugs" "text"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_observation_stats"("p_program_slugs" "text"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_program_stats"() TO "anon";
GRANT ALL ON FUNCTION "public"."get_program_stats"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_program_stats"() TO "service_role";



GRANT ALL ON FUNCTION "public"."get_programs_overview"() TO "anon";
GRANT ALL ON FUNCTION "public"."get_programs_overview"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_programs_overview"() TO "service_role";



GRANT ALL ON FUNCTION "public"."get_targets_for_sync"("p_program_slugs" "text"[], "p_updated_since" timestamp without time zone, "p_limit" integer, "p_offset" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."get_targets_for_sync"("p_program_slugs" "text"[], "p_updated_since" timestamp without time zone, "p_limit" integer, "p_offset" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_targets_for_sync"("p_program_slugs" "text"[], "p_updated_since" timestamp without time zone, "p_limit" integer, "p_offset" integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_targets_in_viewport"("p_ra_min" double precision, "p_ra_max" double precision, "p_dec_min" double precision, "p_dec_max" double precision, "p_field" "text", "p_limit" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."get_targets_in_viewport"("p_ra_min" double precision, "p_ra_max" double precision, "p_dec_min" double precision, "p_dec_max" double precision, "p_field" "text", "p_limit" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_targets_in_viewport"("p_ra_min" double precision, "p_ra_max" double precision, "p_dec_min" double precision, "p_dec_max" double precision, "p_field" "text", "p_limit" integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_extract_query_trgm"("text", "internal", smallint, "internal", "internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_extract_value_trgm"("text", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_trgm_consistent"("internal", smallint, "text", integer, "internal", "internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gin_trgm_triconsistent"("internal", smallint, "text", integer, "internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_compress"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_consistent"("internal", "text", smallint, "oid", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_decompress"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_distance"("internal", "text", smallint, "oid", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_options"("internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_penalty"("internal", "internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_picksplit"("internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_same"("public"."gtrgm", "public"."gtrgm", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "postgres";
GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "anon";
GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "authenticated";
GRANT ALL ON FUNCTION "public"."gtrgm_union"("internal", "internal") TO "service_role";



GRANT ALL ON FUNCTION "public"."increment_tile_version"("p_field" "text", "p_filter" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."increment_tile_version"("p_field" "text", "p_filter" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."increment_tile_version"("p_field" "text", "p_filter" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."is_admin"() TO "anon";
GRANT ALL ON FUNCTION "public"."is_admin"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."is_admin"() TO "service_role";



GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "anon";
GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "service_role";



GRANT ALL ON FUNCTION "public"."propagate_crossmatch_inspection"("p_target_id" integer, "p_radius_arcsec" double precision, "p_redshift_tolerance" double precision) TO "anon";
GRANT ALL ON FUNCTION "public"."propagate_crossmatch_inspection"("p_target_id" integer, "p_radius_arcsec" double precision, "p_redshift_tolerance" double precision) TO "authenticated";
GRANT ALL ON FUNCTION "public"."propagate_crossmatch_inspection"("p_target_id" integer, "p_radius_arcsec" double precision, "p_redshift_tolerance" double precision) TO "service_role";



GRANT ALL ON FUNCTION "public"."refresh_filter_options"() TO "anon";
GRANT ALL ON FUNCTION "public"."refresh_filter_options"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."refresh_filter_options"() TO "service_role";



GRANT ALL ON FUNCTION "public"."refresh_programs_overview"() TO "anon";
GRANT ALL ON FUNCTION "public"."refresh_programs_overview"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."refresh_programs_overview"() TO "service_role";



GRANT ALL ON FUNCTION "public"."revoke_all_user_refresh_tokens"("p_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."revoke_all_user_refresh_tokens"("p_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."revoke_all_user_refresh_tokens"("p_user_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."revoke_refresh_token"("p_token_id" "uuid", "p_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."revoke_refresh_token"("p_token_id" "uuid", "p_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."revoke_refresh_token"("p_token_id" "uuid", "p_user_id" "uuid") TO "service_role";



GRANT ALL ON FUNCTION "public"."rotate_refresh_token"("p_old_token_hash" "text", "p_new_token_hash" "text", "p_expires_at" timestamp with time zone, "p_client_ip" "text", "p_user_agent" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."rotate_refresh_token"("p_old_token_hash" "text", "p_new_token_hash" "text", "p_expires_at" timestamp with time zone, "p_client_ip" "text", "p_user_agent" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."rotate_refresh_token"("p_old_token_hash" "text", "p_new_token_hash" "text", "p_expires_at" timestamp with time zone, "p_client_ip" "text", "p_user_agent" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "postgres";
GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "anon";
GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_limit"(real) TO "service_role";



GRANT ALL ON FUNCTION "public"."show_limit"() TO "postgres";
GRANT ALL ON FUNCTION "public"."show_limit"() TO "anon";
GRANT ALL ON FUNCTION "public"."show_limit"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."show_limit"() TO "service_role";



GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "postgres";
GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "anon";
GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."show_trgm"("text") TO "service_role";



GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."similarity"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."similarity_dist"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."similarity_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_dist_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."strict_word_similarity_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."update_api_key_last_used"("key_hash_input" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."update_api_key_last_used"("key_hash_input" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."update_api_key_last_used"("key_hash_input" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."update_target_max_exposure_time"() TO "anon";
GRANT ALL ON FUNCTION "public"."update_target_max_exposure_time"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."update_target_max_exposure_time"() TO "service_role";



GRANT ALL ON FUNCTION "public"."update_target_max_snr"() TO "anon";
GRANT ALL ON FUNCTION "public"."update_target_max_snr"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."update_target_max_snr"() TO "service_role";



GRANT ALL ON FUNCTION "public"."validate_api_key"("key_hash_input" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."validate_api_key"("key_hash_input" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."validate_api_key"("key_hash_input" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."validate_refresh_token"("p_token_hash" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."validate_refresh_token"("p_token_hash" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."validate_refresh_token"("p_token_hash" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_commutator_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_dist_op"("text", "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "postgres";
GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "anon";
GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."word_similarity_op"("text", "text") TO "service_role";


















GRANT ALL ON TABLE "public"."access_codes" TO "anon";
GRANT ALL ON TABLE "public"."access_codes" TO "authenticated";
GRANT ALL ON TABLE "public"."access_codes" TO "service_role";



GRANT ALL ON TABLE "public"."account_requests" TO "anon";
GRANT ALL ON TABLE "public"."account_requests" TO "authenticated";
GRANT ALL ON TABLE "public"."account_requests" TO "service_role";



GRANT ALL ON SEQUENCE "public"."account_requests_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."account_requests_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."account_requests_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."api_keys" TO "anon";
GRANT ALL ON TABLE "public"."api_keys" TO "authenticated";
GRANT ALL ON TABLE "public"."api_keys" TO "service_role";



GRANT ALL ON TABLE "public"."code_redemptions" TO "anon";
GRANT ALL ON TABLE "public"."code_redemptions" TO "authenticated";
GRANT ALL ON TABLE "public"."code_redemptions" TO "service_role";



GRANT ALL ON TABLE "public"."comments" TO "anon";
GRANT ALL ON TABLE "public"."comments" TO "authenticated";
GRANT ALL ON TABLE "public"."comments" TO "service_role";



GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."device_codes" TO "anon";
GRANT ALL ON TABLE "public"."device_codes" TO "authenticated";
GRANT ALL ON TABLE "public"."device_codes" TO "service_role";



GRANT ALL ON TABLE "public"."download_log" TO "anon";
GRANT ALL ON TABLE "public"."download_log" TO "authenticated";
GRANT ALL ON TABLE "public"."download_log" TO "service_role";



GRANT ALL ON TABLE "public"."flag_audit_log" TO "anon";
GRANT ALL ON TABLE "public"."flag_audit_log" TO "authenticated";
GRANT ALL ON TABLE "public"."flag_audit_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."flag_definitions" TO "anon";
GRANT ALL ON TABLE "public"."flag_definitions" TO "authenticated";
GRANT ALL ON TABLE "public"."flag_definitions" TO "service_role";



GRANT ALL ON TABLE "public"."map_layers" TO "anon";
GRANT ALL ON TABLE "public"."map_layers" TO "authenticated";
GRANT ALL ON TABLE "public"."map_layers" TO "service_role";



GRANT ALL ON SEQUENCE "public"."map_layers_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."map_layers_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."map_layers_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."spectra" TO "anon";
GRANT ALL ON TABLE "public"."spectra" TO "authenticated";
GRANT ALL ON TABLE "public"."spectra" TO "service_role";



GRANT ALL ON TABLE "public"."targets" TO "anon";
GRANT ALL ON TABLE "public"."targets" TO "authenticated";
GRANT ALL ON TABLE "public"."targets" TO "service_role";



GRANT ALL ON TABLE "public"."mv_filter_options" TO "anon";
GRANT ALL ON TABLE "public"."mv_filter_options" TO "authenticated";
GRANT ALL ON TABLE "public"."mv_filter_options" TO "service_role";



GRANT ALL ON TABLE "public"."observations" TO "anon";
GRANT ALL ON TABLE "public"."observations" TO "authenticated";
GRANT ALL ON TABLE "public"."observations" TO "service_role";



GRANT ALL ON TABLE "public"."programs" TO "anon";
GRANT ALL ON TABLE "public"."programs" TO "authenticated";
GRANT ALL ON TABLE "public"."programs" TO "service_role";



GRANT ALL ON TABLE "public"."mv_programs_overview" TO "anon";
GRANT ALL ON TABLE "public"."mv_programs_overview" TO "authenticated";
GRANT ALL ON TABLE "public"."mv_programs_overview" TO "service_role";



GRANT ALL ON TABLE "public"."nircam_images" TO "anon";
GRANT ALL ON TABLE "public"."nircam_images" TO "authenticated";
GRANT ALL ON TABLE "public"."nircam_images" TO "service_role";



GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."password_reset_log" TO "anon";
GRANT ALL ON TABLE "public"."password_reset_log" TO "authenticated";
GRANT ALL ON TABLE "public"."password_reset_log" TO "service_role";



GRANT ALL ON TABLE "public"."pending_invites" TO "anon";
GRANT ALL ON TABLE "public"."pending_invites" TO "authenticated";
GRANT ALL ON TABLE "public"."pending_invites" TO "service_role";



GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."refresh_tokens" TO "anon";
GRANT ALL ON TABLE "public"."refresh_tokens" TO "authenticated";
GRANT ALL ON TABLE "public"."refresh_tokens" TO "service_role";



GRANT ALL ON TABLE "public"."shutters" TO "anon";
GRANT ALL ON TABLE "public"."shutters" TO "authenticated";
GRANT ALL ON TABLE "public"."shutters" TO "service_role";



GRANT ALL ON SEQUENCE "public"."shutters_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."shutters_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."shutters_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."slit_regions" TO "anon";
GRANT ALL ON TABLE "public"."slit_regions" TO "authenticated";
GRANT ALL ON TABLE "public"."slit_regions" TO "service_role";



GRANT ALL ON SEQUENCE "public"."slit_regions_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."slit_regions_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."slit_regions_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."target_flag_summary" TO "anon";
GRANT ALL ON TABLE "public"."target_flag_summary" TO "authenticated";
GRANT ALL ON TABLE "public"."target_flag_summary" TO "service_role";



GRANT ALL ON SEQUENCE "public"."targets_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."targets_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."targets_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."targets_with_flags" TO "anon";
GRANT ALL ON TABLE "public"."targets_with_flags" TO "authenticated";
GRANT ALL ON TABLE "public"."targets_with_flags" TO "service_role";



GRANT ALL ON TABLE "public"."user_profiles" TO "anon";
GRANT ALL ON TABLE "public"."user_profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."user_profiles" TO "service_role";



GRANT ALL ON TABLE "public"."user_program_access" TO "anon";
GRANT ALL ON TABLE "public"."user_program_access" TO "authenticated";
GRANT ALL ON TABLE "public"."user_program_access" TO "service_role";









ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";
































--
-- Dumped schema changes for auth and storage
--

