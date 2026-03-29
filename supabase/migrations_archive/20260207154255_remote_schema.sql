


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


CREATE OR REPLACE FUNCTION "public"."get_adjacent_objects"("p_current_object_id" "text", "p_program_ids" integer[], "p_filter_programs" integer[] DEFAULT NULL::integer[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_spectral_features" integer DEFAULT NULL::integer, "p_object_flags" integer DEFAULT NULL::integer, "p_dq_flags" integer DEFAULT NULL::integer, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'object_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text") RETURNS TABLE("prev_object_id" "text", "next_object_id" "text", "current_index" bigint, "total_count" bigint)
    LANGUAGE "plpgsql" STABLE
    AS $$
DECLARE
  v_filtered_program_ids INTEGER[];
  v_grating_object_ids TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
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
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;
  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;
  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_ids)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_ids;
  ELSE
    v_filtered_program_ids := p_program_ids;
  END IF;

  IF v_filtered_program_ids IS NULL OR array_length(v_filtered_program_ids, 1) IS NULL THEN
    RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
    RETURN;
  END IF;

  -- Handle grating filter based on mode
  IF v_grating_filter_active THEN
    IF v_gratings_mode = 'any' THEN
      SELECT ARRAY(
        SELECT DISTINCT s.object_id FROM spectra s WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'all' THEN
      SELECT ARRAY(
        SELECT s.object_id FROM spectra s WHERE s.grating = ANY(p_gratings)
        GROUP BY s.object_id HAVING COUNT(DISTINCT s.grating) = array_length(p_gratings, 1)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'none' THEN
      SELECT ARRAY(
        SELECT DISTINCT s.object_id FROM spectra s WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    END IF;

    IF v_gratings_mode IN ('any', 'all') AND (v_grating_object_ids IS NULL OR array_length(v_grating_object_ids, 1) IS NULL) THEN
      RETURN QUERY SELECT NULL::TEXT, NULL::TEXT, 0::BIGINT, 0::BIGINT;
      RETURN;
    END IF;
  END IF;

  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.object_id,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) *
            POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance,
      o.field, o.observation, o.ra, o.dec, o.redshift, o.redshift_quality, o.max_snr
    FROM objects o
    WHERE
      o.program_id = ANY(v_filtered_program_ids)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode IN ('any', 'all') AND o.object_id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (v_grating_object_ids IS NULL OR NOT o.object_id = ANY(v_grating_object_ids)))
      )
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (v_sf_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_exclude) = 0)
      AND (v_of_include_any IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(o.object_flags, 0) & v_of_exclude) = 0)
      AND (v_dq_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_exclude) = 0)
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.object_id = o.id
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
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fo.*
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  sorted_with_row AS (
    SELECT
      df.object_id,
      ROW_NUMBER() OVER (
        ORDER BY
          CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
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
          df.object_id ASC
      ) as row_num
    FROM distance_filtered df
  ),
  current_row AS (
    SELECT row_num FROM sorted_with_row WHERE object_id = p_current_object_id
  ),
  total AS (
    SELECT COUNT(*) as cnt FROM sorted_with_row
  )
  SELECT
    (SELECT object_id FROM sorted_with_row WHERE row_num = (SELECT row_num - 1 FROM current_row)) as prev_object_id,
    (SELECT object_id FROM sorted_with_row WHERE row_num = (SELECT row_num + 1 FROM current_row)) as next_object_id,
    COALESCE((SELECT row_num FROM current_row), 0) as current_index,
    (SELECT cnt FROM total) as total_count;
END;
$$;


ALTER FUNCTION "public"."get_adjacent_objects"("p_current_object_id" "text", "p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_download_stats"("p_days" integer DEFAULT 30) RETURNS json
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
BEGIN
  -- Check if user is admin
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
    'total_objects', (
      SELECT COALESCE(SUM(object_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'recent_downloads', (
      SELECT json_agg(t)
      FROM (
        SELECT
          dl.id,
          dl.download_type,
          dl.object_count,
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
    'most_downloaded_objects', (
      SELECT json_agg(t)
      FROM (
        SELECT
          object_id,
          COUNT(*) as download_count
        FROM download_log, unnest(object_ids) as object_id
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY object_id
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


CREATE OR REPLACE FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[] DEFAULT NULL::integer[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_gratings_mode" "text" DEFAULT 'any'::"text", "p_observations" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_max_snr_min" double precision DEFAULT NULL::double precision, "p_max_snr_max" double precision DEFAULT NULL::double precision, "p_spectral_features" integer DEFAULT NULL::integer, "p_object_flags" integer DEFAULT NULL::integer, "p_dq_flags" integer DEFAULT NULL::integer, "p_spectral_features_include_any" integer DEFAULT NULL::integer, "p_spectral_features_include_all" integer DEFAULT NULL::integer, "p_spectral_features_exclude" integer DEFAULT NULL::integer, "p_object_flags_include_any" integer DEFAULT NULL::integer, "p_object_flags_include_all" integer DEFAULT NULL::integer, "p_object_flags_exclude" integer DEFAULT NULL::integer, "p_dq_flags_include_any" integer DEFAULT NULL::integer, "p_dq_flags_include_all" integer DEFAULT NULL::integer, "p_dq_flags_exclude" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_comment_search" "text" DEFAULT NULL::"text", "p_comment_search_scope" "text" DEFAULT NULL::"text", "p_comment_user_id" "uuid" DEFAULT NULL::"uuid", "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'object_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text", "p_page" integer DEFAULT 1, "p_page_size" integer DEFAULT 50, "p_include_thumbnails" boolean DEFAULT false) RETURNS TABLE("objects" "jsonb", "total_count" bigint, "page" integer, "page_size" integer)
    LANGUAGE "plpgsql" STABLE
    AS $$
DECLARE
  v_offset INTEGER;
  v_filtered_program_ids INTEGER[];
  v_grating_object_ids TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  -- Effective flag masks (merge legacy + new params)
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
  -- Calculate offset
  v_offset := (p_page - 1) * p_page_size;

  -- Check if coordinate search is active
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);

  -- Check if comment search is active
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );

  -- Check if grating filter is active
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);

  -- Validate grating mode
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN
    v_gratings_mode := 'any';
  END IF;

  -- Merge legacy single-mask params with new multi-mode params
  -- Legacy params are treated as include_any when new params are NULL
  v_sf_include_any := COALESCE(p_spectral_features_include_any, p_spectral_features);
  v_sf_include_all := p_spectral_features_include_all;
  v_sf_exclude := p_spectral_features_exclude;

  v_of_include_any := COALESCE(p_object_flags_include_any, p_object_flags);
  v_of_include_all := p_object_flags_include_all;
  v_of_exclude := p_object_flags_exclude;

  v_dq_include_any := COALESCE(p_dq_flags_include_any, p_dq_flags);
  v_dq_include_all := p_dq_flags_include_all;
  v_dq_exclude := p_dq_flags_exclude;

  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  IF NOT (p_sort_column IN ('object_id', 'field', 'observation', 'ra', 'dec', 'redshift', 'redshift_quality', 'max_snr')
       OR (p_sort_column = 'distance' AND v_coord_search_active)) THEN
    p_sort_column := 'object_id';
  END IF;

  -- Determine which programs to query (intersection of accessible and filtered)
  IF p_filter_programs IS NOT NULL AND array_length(p_filter_programs, 1) > 0 THEN
    SELECT ARRAY(
      SELECT unnest(p_program_ids)
      INTERSECT
      SELECT unnest(p_filter_programs)
    ) INTO v_filtered_program_ids;
  ELSE
    v_filtered_program_ids := p_program_ids;
  END IF;

  -- If no programs to query, return empty result
  IF v_filtered_program_ids IS NULL OR array_length(v_filtered_program_ids, 1) IS NULL THEN
    RETURN QUERY SELECT
      '[]'::JSONB as objects,
      0::BIGINT as total_count,
      p_page as page,
      p_page_size as page_size;
    RETURN;
  END IF;

  -- Handle grating filter based on mode
  IF v_grating_filter_active THEN
    IF v_gratings_mode = 'any' THEN
      -- Mode: ANY - object has at least one spectrum with a matching grating
      SELECT ARRAY(
        SELECT DISTINCT s.object_id
        FROM spectra s
        WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'all' THEN
      -- Mode: ALL - object must have spectra for ALL selected gratings
      SELECT ARRAY(
        SELECT s.object_id
        FROM spectra s
        WHERE s.grating = ANY(p_gratings)
        GROUP BY s.object_id
        HAVING COUNT(DISTINCT s.grating) = array_length(p_gratings, 1)
      ) INTO v_grating_object_ids;
    ELSIF v_gratings_mode = 'none' THEN
      -- Mode: NONE - object must NOT have any spectra with selected gratings
      -- We'll handle this differently in the WHERE clause
      SELECT ARRAY(
        SELECT DISTINCT s.object_id
        FROM spectra s
        WHERE s.grating = ANY(p_gratings)
      ) INTO v_grating_object_ids;
    END IF;

    -- For 'any' and 'all' modes, if no matching objects, return empty
    IF v_gratings_mode IN ('any', 'all') AND (v_grating_object_ids IS NULL OR array_length(v_grating_object_ids, 1) IS NULL) THEN
      RETURN QUERY SELECT
        '[]'::JSONB as objects,
        0::BIGINT as total_count,
        p_page as page,
        p_page_size as page_size;
      RETURN;
    END IF;
  END IF;

  -- Return paginated results with count
  RETURN QUERY
  WITH filtered_objects AS (
    SELECT
      o.*,
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
      -- Program access control
      o.program_id = ANY(v_filtered_program_ids)
      -- Grating filter (mode-aware)
      AND (
        NOT v_grating_filter_active
        OR (v_gratings_mode IN ('any', 'all') AND o.object_id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (v_grating_object_ids IS NULL OR NOT o.object_id = ANY(v_grating_object_ids)))
      )
      -- Field filter
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      -- Observation filter
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR o.observation = ANY(p_observations))
      -- Redshift quality filter
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      -- Redshift range filters
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      -- Max S/N range filters
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      -- Spectral features filter (three modes)
      AND (v_sf_include_any IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_any) != 0)
      AND (v_sf_include_all IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_include_all) = v_sf_include_all)
      AND (v_sf_exclude IS NULL OR (COALESCE(o.spectral_features, 0) & v_sf_exclude) = 0)
      -- Object flags filter (three modes)
      AND (v_of_include_any IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_any) != 0)
      AND (v_of_include_all IS NULL OR (COALESCE(o.object_flags, 0) & v_of_include_all) = v_of_include_all)
      AND (v_of_exclude IS NULL OR (COALESCE(o.object_flags, 0) & v_of_exclude) = 0)
      -- DQ flags filter (three modes)
      AND (v_dq_include_any IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_any) != 0)
      AND (v_dq_include_all IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_include_all) = v_dq_include_all)
      AND (v_dq_exclude IS NULL OR (COALESCE(o.dq_flags, 0) & v_dq_exclude) = 0)
      -- Object ID text search
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Inspected only filter
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
      -- Comment search filter
      AND (
        NOT v_comment_search_active
        OR EXISTS (
          SELECT 1 FROM comments c
          WHERE c.object_id = o.id
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      -- Coordinate search bounding box pre-filter
      AND (
        NOT v_coord_search_active
        OR (
          o.ra BETWEEN (p_coord_ra - p_radius_degrees) AND (p_coord_ra + p_radius_degrees)
          AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS (
    SELECT fo.*
    FROM filtered_objects fo
    WHERE NOT v_coord_search_active OR fo.distance <= p_radius_degrees
  ),
  counted AS (
    SELECT COUNT(*) as cnt FROM distance_filtered
  ),
  sorted_objects AS (
    SELECT df.*
    FROM distance_filtered df
    ORDER BY
      CASE WHEN v_coord_search_active THEN df.distance END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN df.object_id END ASC NULLS LAST,
      CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN df.object_id END DESC NULLS LAST,
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
      df.object_id ASC
  ),
  paginated AS (
    SELECT so.*
    FROM sorted_objects so
    LIMIT p_page_size
    OFFSET v_offset
  ),
  with_relations AS (
    SELECT
      jsonb_build_object(
        'id', p.id,
        'object_id', p.object_id,
        'program_id', p.program_id,
        'field', p.field,
        'observation', p.observation,
        'ra', p.ra,
        'dec', p.dec,
        'redshift', p.redshift,
        'redshift_auto', p.redshift_auto,
        'redshift_inspected', p.redshift_inspected,
        'redshift_quality', p.redshift_quality,
        'spectral_features', p.spectral_features,
        'object_flags', p.object_flags,
        'dq_flags', p.dq_flags,
        'max_snr', p.max_snr,
        'last_inspected_at', p.last_inspected_at,
        'last_inspected_by', p.last_inspected_by,
        'created_at', p.created_at,
        'updated_at', p.updated_at,
        'program_name', pr.program_name,
        'distance', CASE WHEN v_coord_search_active THEN p.distance ELSE NULL END,
        'spectra', COALESCE(
          (
            SELECT jsonb_agg(
              jsonb_build_object(
                'id', s.id,
                'object_id', s.object_id,
                'grating', s.grating,
                'fits_path', s.fits_path,
                'reduction_version', s.reduction_version,
                'signal_to_noise', s.signal_to_noise,
                'created_at', s.created_at,
                -- Conditionally include thumbnails
                'thumbnail_svg_fnu', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_fnu ELSE NULL END,
                'thumbnail_svg_flambda', CASE WHEN p_include_thumbnails THEN s.thumbnail_svg_flambda ELSE NULL END
              )
              ORDER BY s.grating
            )
            FROM spectra s
            WHERE s.object_id = p.object_id
              -- For 'none' mode, show all spectra; for 'any'/'all' mode, filter to selected gratings
              AND (NOT v_grating_filter_active OR v_gratings_mode = 'none' OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        )
      ) as obj,
      p.object_id AS sort_object_id,
      p.field AS sort_field,
      p.observation AS sort_observation,
      p.ra AS sort_ra,
      p.dec AS sort_dec,
      p.redshift AS sort_redshift,
      p.redshift_quality AS sort_redshift_quality,
      p.max_snr AS sort_max_snr,
      p.distance AS sort_distance
    FROM paginated p
    LEFT JOIN programs pr ON pr.program_id = p.program_id
  )
  SELECT
    COALESCE(
      (
        SELECT jsonb_agg(wr.obj ORDER BY
          CASE WHEN v_coord_search_active THEN wr.sort_distance END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN wr.sort_object_id END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN wr.sort_object_id END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'asc' THEN wr.sort_field END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'field' AND p_sort_direction = 'desc' THEN wr.sort_field END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'asc' THEN wr.sort_observation END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'observation' AND p_sort_direction = 'desc' THEN wr.sort_observation END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN wr.sort_ra END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN wr.sort_ra END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN wr.sort_dec END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN wr.sort_dec END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN wr.sort_redshift END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN wr.sort_redshift END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN wr.sort_redshift_quality END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN wr.sort_redshift_quality END DESC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'asc' THEN wr.sort_max_snr END ASC NULLS LAST,
          CASE WHEN NOT v_coord_search_active AND p_sort_column = 'max_snr' AND p_sort_direction = 'desc' THEN wr.sort_max_snr END DESC NULLS LAST,
          wr.sort_object_id ASC
        )
        FROM with_relations wr
      ),
      '[]'::jsonb
    ) as objects,
    (SELECT cnt FROM counted) as total_count,
    p_page as page,
    p_page_size as page_size;
END;
$$;


ALTER FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) OWNER TO "postgres";


COMMENT ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) IS 'Server-side filtering, sorting, and pagination for the NIRSpec objects catalog.

GRATING FILTERING (three modes):
- any: Object has at least one spectrum with a matching grating (OR)
- all: Object must have spectra for ALL selected gratings (AND)
- none: Object must NOT have any spectra with selected gratings (NOT)

FLAG FILTERING (three modes per flag type):
- include_any: (flags & mask) != 0 - match any of these flags (OR)
- include_all: (flags & mask) = mask - must have all of these flags (AND)
- exclude: (flags & mask) = 0 - must not have any of these flags (NOT)

THUMBNAIL SUPPORT:
- p_include_thumbnails: When TRUE, includes thumbnail_svg_fnu and thumbnail_svg_flambda
  in the spectra JSONB. Defaults to FALSE for lean API responses.

BACKWARD COMPATIBILITY:
- Legacy params (p_spectral_features, p_object_flags, p_dq_flags) treated as include_any
- p_gratings_mode defaults to "any" for backward compatibility

RETURNS:
- objects: JSONB array of objects with nested spectra and program name
- total_count: Total matching rows
- page/page_size: Pagination info';



CREATE OR REPLACE FUNCTION "public"."get_program_stats"() RETURNS TABLE("program_id" integer, "object_count" bigint, "user_access_count" bigint)
    LANGUAGE "sql" STABLE SECURITY DEFINER
    AS $$
  SELECT 
    p.program_id,
    COALESCE(o.cnt, 0) AS object_count,
    COALESCE(a.cnt, 0) AS user_access_count
  FROM programs p
  LEFT JOIN (
    SELECT program_id, COUNT(*) AS cnt 
    FROM objects 
    GROUP BY program_id
  ) o ON p.program_id = o.program_id
  LEFT JOIN (
    SELECT program_id, COUNT(*) AS cnt 
    FROM user_program_access 
    GROUP BY program_id
  ) a ON p.program_id = a.program_id;
$$;


ALTER FUNCTION "public"."get_program_stats"() OWNER TO "postgres";


COMMENT ON FUNCTION "public"."get_program_stats"() IS 'Returns object counts and user access counts per program using efficient GROUP BY aggregation';



CREATE OR REPLACE FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") RETURNS json
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    AS $$
DECLARE
  result JSON;
  objects_inspected BIGINT;
  comments_posted BIGINT;
  last_comment_at TIMESTAMPTZ;
  last_inspection_at TIMESTAMPTZ;
  last_activity TIMESTAMPTZ;
BEGIN
  -- Get distinct objects inspected count
  SELECT COUNT(DISTINCT object_id) INTO objects_inspected
  FROM flag_audit_log
  WHERE user_id = p_user_id;

  -- Get comments count
  SELECT COUNT(*) INTO comments_posted
  FROM comments
  WHERE user_id = p_user_id AND is_deleted = false;

  -- Get last comment timestamp
  SELECT created_at INTO last_comment_at
  FROM comments
  WHERE user_id = p_user_id
  ORDER BY created_at DESC
  LIMIT 1;

  -- Get last inspection timestamp
  SELECT changed_at INTO last_inspection_at
  FROM flag_audit_log
  WHERE user_id = p_user_id
  ORDER BY changed_at DESC
  LIMIT 1;

  -- Determine most recent activity
  last_activity := GREATEST(
    COALESCE(last_comment_at, '1970-01-01'::timestamptz),
    COALESCE(last_inspection_at, '1970-01-01'::timestamptz)
  );
  IF last_activity = '1970-01-01'::timestamptz THEN
    last_activity := NULL;
  END IF;

  -- Build result JSON
  result := json_build_object(
    'objects_inspected', COALESCE(objects_inspected, 0),
    'comments_posted', COALESCE(comments_posted, 0),
    'last_activity', last_activity
  );

  RETURN result;
END;
$$;


ALTER FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") OWNER TO "postgres";


COMMENT ON FUNCTION "public"."get_user_profile_stats"("p_user_id" "uuid") IS 'Returns aggregated user stats (objects inspected, comments posted, last activity) in a single call';



CREATE OR REPLACE FUNCTION "public"."log_flag_changes"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
    IF OLD.redshift_quality IS DISTINCT FROM NEW.redshift_quality THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'redshift_quality', OLD.redshift_quality, NEW.redshift_quality);
    END IF;
    
    IF OLD.spectral_features IS DISTINCT FROM NEW.spectral_features THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'spectral_features', OLD.spectral_features, NEW.spectral_features);
    END IF;
    
    IF OLD.object_flags IS DISTINCT FROM NEW.object_flags THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'object_flags', OLD.object_flags, NEW.object_flags);
    END IF;
    
    IF OLD.dq_flags IS DISTINCT FROM NEW.dq_flags THEN
        INSERT INTO flag_audit_log (object_id, user_id, field_name, old_value, new_value)
        VALUES (NEW.id, auth.uid(), 'dq_flags', OLD.dq_flags, NEW.dq_flags);
    END IF;
    
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."log_flag_changes"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."refresh_filter_options"() RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
  BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_filter_options;
  END;
  $$;


ALTER FUNCTION "public"."refresh_filter_options"() OWNER TO "postgres";


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


CREATE OR REPLACE FUNCTION "public"."update_object_max_snr"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
  target_object_id TEXT;
BEGIN
  -- Determine which object_id was affected
  IF TG_OP = 'DELETE' THEN
    target_object_id := OLD.object_id;
  ELSE
    target_object_id := NEW.object_id;
  END IF;

  -- Update the max_snr for the affected object
  UPDATE objects
  SET max_snr = (
    SELECT MAX(signal_to_noise)
    FROM spectra
    WHERE spectra.object_id = target_object_id
  )
  WHERE objects.object_id = target_object_id;

  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."update_object_max_snr"() OWNER TO "postgres";


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
    "program_ids" integer[],
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "expires_at" timestamp with time zone,
    "max_uses" integer,
    "use_count" integer DEFAULT 0,
    "is_active" boolean DEFAULT true
);


ALTER TABLE "public"."access_codes" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."account_requests" (
    "id" integer NOT NULL,
    "email" "text" NOT NULL,
    "full_name" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "is_admin" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "program_ids" integer[] DEFAULT '{}'::integer[],
    "created_at" timestamp with time zone DEFAULT "now"(),
    "reviewed_at" timestamp with time zone,
    "reviewed_by" "uuid",
    "rejection_reason" "text",
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
    "object_id" integer NOT NULL,
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
    "object_count" integer,
    "file_count" integer,
    "object_ids" "text"[],
    "filter_snapshot" "jsonb",
    "ip_address" "text",
    "user_agent" "text",
    "requested_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "download_log_download_type_check" CHECK (("download_type" = ANY (ARRAY['fits_single'::"text", 'fits_object'::"text", 'fits_batch'::"text", 'fits_zip'::"text", 'csv'::"text", 'sed_plot'::"text"])))
);


ALTER TABLE "public"."download_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."flag_audit_log" (
    "id" integer NOT NULL,
    "object_id" integer NOT NULL,
    "user_id" "uuid" NOT NULL,
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


CREATE TABLE IF NOT EXISTS "public"."objects" (
    "id" integer NOT NULL,
    "object_id" "text" NOT NULL,
    "program_id" integer NOT NULL,
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
    "observation" "text" GENERATED ALWAYS AS ("substring"("object_id", '^(.+)_[0-9]+$'::"text")) STORED,
    "max_snr" double precision,
    "redshift" numeric(10,6) GENERATED ALWAYS AS (
CASE
    WHEN ("redshift_quality" = 1) THEN NULL::double precision
    ELSE COALESCE(("redshift_inspected")::double precision, "redshift_auto")
END) STORED,
    "has_sed_plot" boolean DEFAULT false NOT NULL
);


ALTER TABLE "public"."objects" OWNER TO "postgres";


COMMENT ON COLUMN "public"."objects"."redshift" IS 'Generated column: NULL when redshift_quality = 1 (Impossible), otherwise COALESCE(redshift_inspected, redshift_auto). This allows "Impossible" objects to be excluded from redshift range filters.';



COMMENT ON COLUMN "public"."objects"."has_sed_plot" IS 'Indicates whether an SED plot PDF exists in R2. Set during deployment to avoid runtime R2 HeadObject calls.';



CREATE TABLE IF NOT EXISTS "public"."spectra" (
    "id" integer NOT NULL,
    "grating" "text" NOT NULL,
    "fits_path" "text" NOT NULL,
    "reduction_version" "text" DEFAULT 'v1.0'::"text",
    "signal_to_noise" double precision,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "object_id" "text" NOT NULL,
    "thumbnail_svg_fnu" "text",
    "thumbnail_svg_flambda" "text"
);


ALTER TABLE "public"."spectra" OWNER TO "postgres";


COMMENT ON COLUMN "public"."spectra"."thumbnail_svg_fnu" IS 'Pre-generated SVG sparkline thumbnail in f_nu units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';



COMMENT ON COLUMN "public"."spectra"."thumbnail_svg_flambda" IS 'Pre-generated SVG sparkline thumbnail in f_lambda units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';



CREATE MATERIALIZED VIEW "public"."mv_filter_options" AS
 SELECT 1 AS "id",
    ARRAY( SELECT DISTINCT "objects"."field"
           FROM "public"."objects"
          ORDER BY "objects"."field") AS "fields",
    ARRAY( SELECT DISTINCT "objects"."observation"
           FROM "public"."objects"
          WHERE ("objects"."observation" IS NOT NULL)
          ORDER BY "objects"."observation") AS "observations",
    ARRAY( SELECT DISTINCT "spectra"."grating"
           FROM "public"."spectra"
          ORDER BY "spectra"."grating") AS "gratings"
  WITH NO DATA;


ALTER MATERIALIZED VIEW "public"."mv_filter_options" OWNER TO "postgres";


COMMENT ON MATERIALIZED VIEW "public"."mv_filter_options" IS 'Cached distinct filter options (fields, observations, gratings). Refresh after data deployments using refresh_filter_options()';



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



CREATE OR REPLACE VIEW "public"."object_flag_summary" AS
 SELECT "o"."id",
    "o"."object_id",
    "array_agg"(DISTINCT "fd"."label") FILTER (WHERE (("fd"."category" = 'spectral_features'::"text") AND (("o"."spectral_features" & "fd"."value") > 0))) AS "spectral_features_labels",
    "array_agg"(DISTINCT "fd"."label") FILTER (WHERE (("fd"."category" = 'object_flags'::"text") AND (("o"."object_flags" & "fd"."value") > 0))) AS "object_flags_labels",
    "array_agg"(DISTINCT "fd"."label") FILTER (WHERE (("fd"."category" = 'dq_flags'::"text") AND (("o"."dq_flags" & "fd"."value") > 0))) AS "dq_flags_labels"
   FROM ("public"."objects" "o"
     CROSS JOIN "public"."flag_definitions" "fd")
  GROUP BY "o"."id", "o"."object_id";


ALTER VIEW "public"."object_flag_summary" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."objects_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."objects_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."objects_id_seq" OWNED BY "public"."objects"."id";



CREATE OR REPLACE VIEW "public"."objects_with_flags" AS
 SELECT "o"."id",
    "o"."object_id",
    "o"."program_id",
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
   FROM ("public"."objects" "o"
     LEFT JOIN "public"."flag_definitions" "rq" ON ((("rq"."category" = 'redshift_quality'::"text") AND ("rq"."value" = "o"."redshift_quality"))));


ALTER VIEW "public"."objects_with_flags" OWNER TO "postgres";


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
    "program_ids" integer[] DEFAULT '{}'::integer[],
    "is_admin" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "invited_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "accepted_at" timestamp with time zone,
    "full_name" "text"
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



CREATE TABLE IF NOT EXISTS "public"."programs" (
    "program_id" integer NOT NULL,
    "program_name" "text",
    "pi_name" "text",
    "description" "text",
    "created_at" timestamp without time zone DEFAULT "now"(),
    "is_public" boolean DEFAULT false
);


ALTER TABLE "public"."programs" OWNER TO "postgres";


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


CREATE SEQUENCE IF NOT EXISTS "public"."spectra_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."spectra_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."spectra_id_seq" OWNED BY "public"."spectra"."id";



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
    "program_id" integer NOT NULL,
    "granted_at" timestamp without time zone DEFAULT "now"(),
    "granted_by" "uuid"
);


ALTER TABLE "public"."user_program_access" OWNER TO "postgres";


ALTER TABLE ONLY "public"."account_requests" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."account_requests_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."comments" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."comments_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."flag_audit_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."flag_audit_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."nircam_images" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nircam_images_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."objects" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."objects_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."pending_invites" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."pending_invites_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."spectra" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."spectra_id_seq"'::"regclass");



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



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_field_tile_filter_pixel_scale_version_extensi_key" UNIQUE ("field", "tile", "filter", "pixel_scale", "version", "extension");



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_unique" UNIQUE ("field", "tile", "filter", "pixel_scale", "version", "extension");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_object_id_key" UNIQUE ("object_id");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."password_reset_log"
    ADD CONSTRAINT "password_reset_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."programs"
    ADD CONSTRAINT "programs_pkey" PRIMARY KEY ("program_id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_token_hash_key" UNIQUE ("token_hash");



ALTER TABLE ONLY "public"."spectra"
    ADD CONSTRAINT "spectra_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_pkey" PRIMARY KEY ("user_id", "program_id");



CREATE INDEX "idx_access_codes_code" ON "public"."access_codes" USING "btree" ("code");



CREATE INDEX "idx_account_requests_created_at" ON "public"."account_requests" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_account_requests_email" ON "public"."account_requests" USING "btree" ("email");



CREATE INDEX "idx_account_requests_status" ON "public"."account_requests" USING "btree" ("status");



CREATE INDEX "idx_api_keys_is_active" ON "public"."api_keys" USING "btree" ("is_active") WHERE ("is_active" = true);



CREATE INDEX "idx_api_keys_key_hash" ON "public"."api_keys" USING "btree" ("key_hash");



CREATE INDEX "idx_api_keys_user_id" ON "public"."api_keys" USING "btree" ("user_id");



CREATE INDEX "idx_audit_object" ON "public"."flag_audit_log" USING "btree" ("object_id");



CREATE INDEX "idx_audit_time" ON "public"."flag_audit_log" USING "btree" ("changed_at" DESC);



CREATE INDEX "idx_audit_user" ON "public"."flag_audit_log" USING "btree" ("user_id");



CREATE INDEX "idx_code_redemptions_user" ON "public"."code_redemptions" USING "btree" ("user_id");



CREATE INDEX "idx_comments_content_trgm" ON "public"."comments" USING "gin" ("content" "public"."gin_trgm_ops");



CREATE INDEX "idx_comments_created" ON "public"."comments" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_comments_object" ON "public"."comments" USING "btree" ("object_id");



CREATE INDEX "idx_comments_user" ON "public"."comments" USING "btree" ("user_id");



CREATE INDEX "idx_device_codes_device_code" ON "public"."device_codes" USING "btree" ("device_code");



CREATE INDEX "idx_device_codes_expires_at" ON "public"."device_codes" USING "btree" ("expires_at");



CREATE INDEX "idx_device_codes_status" ON "public"."device_codes" USING "btree" ("status") WHERE ("status" = 'pending'::"text");



CREATE INDEX "idx_device_codes_user_code" ON "public"."device_codes" USING "btree" ("user_code");



CREATE INDEX "idx_download_log_download_type" ON "public"."download_log" USING "btree" ("download_type");



CREATE INDEX "idx_download_log_object_ids" ON "public"."download_log" USING "gin" ("object_ids");



CREATE INDEX "idx_download_log_requested_at" ON "public"."download_log" USING "btree" ("requested_at" DESC);



CREATE INDEX "idx_download_log_user_id" ON "public"."download_log" USING "btree" ("user_id");



CREATE INDEX "idx_flag_audit_log_object_id" ON "public"."flag_audit_log" USING "btree" ("object_id");



CREATE INDEX "idx_images_field" ON "public"."nircam_images" USING "btree" ("field");



CREATE INDEX "idx_images_filter" ON "public"."nircam_images" USING "btree" ("filter");



CREATE INDEX "idx_objects_coords" ON "public"."objects" USING "btree" ("ra", "dec");



CREATE INDEX "idx_objects_field" ON "public"."objects" USING "btree" ("field");



CREATE INDEX "idx_objects_field_observation" ON "public"."objects" USING "btree" ("field", "observation");



COMMENT ON INDEX "public"."idx_objects_field_observation" IS 'Composite index for field + observation drill-down';



CREATE INDEX "idx_objects_has_sed_plot" ON "public"."objects" USING "btree" ("has_sed_plot") WHERE ("has_sed_plot" = true);



CREATE INDEX "idx_objects_max_snr" ON "public"."objects" USING "btree" ("max_snr") WHERE ("max_snr" IS NOT NULL);



CREATE INDEX "idx_objects_object_id_trgm" ON "public"."objects" USING "gin" ("object_id" "public"."gin_trgm_ops");



COMMENT ON INDEX "public"."idx_objects_object_id_trgm" IS 'Trigram index for fuzzy text search on object_id. Supports ILIKE with leading/trailing wildcards.
Example: WHERE object_id ILIKE ''%cosmos%'' will use this index.
Alternative: For prefix-only search, use text_pattern_ops index instead.';



CREATE INDEX "idx_objects_observation" ON "public"."objects" USING "btree" ("observation");



CREATE INDEX "idx_objects_program" ON "public"."objects" USING "btree" ("program_id");



CREATE INDEX "idx_objects_program_field" ON "public"."objects" USING "btree" ("program_id", "field");



COMMENT ON INDEX "public"."idx_objects_program_field" IS 'Composite index for program + field filtering';



CREATE INDEX "idx_objects_program_quality" ON "public"."objects" USING "btree" ("program_id", "redshift_quality");



COMMENT ON INDEX "public"."idx_objects_program_quality" IS 'Composite index for program + inspection quality filtering';



CREATE INDEX "idx_objects_redshift_generated" ON "public"."objects" USING "btree" ("redshift") WHERE ("redshift" IS NOT NULL);



CREATE INDEX "idx_objects_redshift_quality" ON "public"."objects" USING "btree" ("redshift_quality");



CREATE INDEX "idx_password_reset_log_reset_at" ON "public"."password_reset_log" USING "btree" ("reset_at" DESC);



CREATE INDEX "idx_password_reset_log_user_id" ON "public"."password_reset_log" USING "btree" ("user_id");



CREATE INDEX "idx_pending_invites_email" ON "public"."pending_invites" USING "btree" ("email");



CREATE INDEX "idx_refresh_tokens_active" ON "public"."refresh_tokens" USING "btree" ("user_id", "expires_at") WHERE ("is_revoked" = false);



CREATE INDEX "idx_refresh_tokens_token_hash" ON "public"."refresh_tokens" USING "btree" ("token_hash");



CREATE INDEX "idx_refresh_tokens_user_id" ON "public"."refresh_tokens" USING "btree" ("user_id");



CREATE INDEX "idx_spectra_grating" ON "public"."spectra" USING "btree" ("grating");



CREATE INDEX "idx_spectra_object_grating" ON "public"."spectra" USING "btree" ("object_id", "grating");



CREATE INDEX "idx_spectra_object_id" ON "public"."spectra" USING "btree" ("object_id");



CREATE INDEX "idx_user_profiles_preferences" ON "public"."user_profiles" USING "gin" ("preferences");



CREATE UNIQUE INDEX "mv_filter_options_id" ON "public"."mv_filter_options" USING "btree" ("id");



CREATE OR REPLACE TRIGGER "track_flag_changes" BEFORE UPDATE ON "public"."objects" FOR EACH ROW EXECUTE FUNCTION "public"."log_flag_changes"();



CREATE OR REPLACE TRIGGER "update_max_snr_trigger" AFTER INSERT OR DELETE OR UPDATE ON "public"."spectra" FOR EACH ROW EXECUTE FUNCTION "public"."update_object_max_snr"();



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
    ADD CONSTRAINT "comments_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."device_codes"
    ADD CONSTRAINT "device_codes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."download_log"
    ADD CONSTRAINT "download_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_last_inspected_by_fkey" FOREIGN KEY ("last_inspected_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_program_id_fkey" FOREIGN KEY ("program_id") REFERENCES "public"."programs"("program_id");



ALTER TABLE ONLY "public"."password_reset_log"
    ADD CONSTRAINT "password_reset_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_invited_by_fkey" FOREIGN KEY ("invited_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_replaced_by_fkey" FOREIGN KEY ("replaced_by") REFERENCES "public"."refresh_tokens"("id");



ALTER TABLE ONLY "public"."refresh_tokens"
    ADD CONSTRAINT "refresh_tokens_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."spectra"
    ADD CONSTRAINT "spectra_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("object_id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_granted_by_fkey" FOREIGN KEY ("granted_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



CREATE POLICY "Admins can create invites" ON "public"."pending_invites" FOR INSERT TO "authenticated" WITH CHECK ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can delete invites" ON "public"."pending_invites" FOR DELETE TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can manage codes" ON "public"."access_codes" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can see all redemptions" ON "public"."code_redemptions" FOR SELECT USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can update invites" ON "public"."pending_invites" FOR UPDATE TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can update requests" ON "public"."account_requests" FOR UPDATE TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can view all downloads" ON "public"."download_log" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can view all reset logs" ON "public"."password_reset_log" FOR SELECT USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can view invites" ON "public"."pending_invites" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Admins can view requests" ON "public"."account_requests" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Allow authenticated users to insert comments" ON "public"."comments" FOR INSERT TO "authenticated" WITH CHECK (("auth"."uid"() = "user_id"));



CREATE POLICY "Allow authenticated users to read comments" ON "public"."comments" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "Allow authenticated users to update objects" ON "public"."objects" FOR UPDATE TO "authenticated" USING (true) WITH CHECK (true);



CREATE POLICY "Anyone can read active codes" ON "public"."access_codes" FOR SELECT USING (("is_active" = true));



CREATE POLICY "Anyone can submit requests" ON "public"."account_requests" FOR INSERT TO "authenticated", "anon" WITH CHECK (true);



CREATE POLICY "Service role full access" ON "public"."device_codes" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "Service role full access" ON "public"."refresh_tokens" TO "service_role" USING (true) WITH CHECK (true);



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


ALTER TABLE "public"."account_requests" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."api_keys" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."code_redemptions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."comments" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."device_codes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."download_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."flag_audit_log" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "insert_audit_by_access" ON "public"."flag_audit_log" FOR INSERT TO "authenticated" WITH CHECK (("object_id" IN ( SELECT "objects"."id"
   FROM "public"."objects"
  WHERE (("objects"."program_id" IN ( SELECT "user_program_access"."program_id"
           FROM "public"."user_program_access"
          WHERE ("user_program_access"."user_id" = "auth"."uid"()))) OR ("objects"."program_id" IN ( SELECT "programs"."program_id"
           FROM "public"."programs"
          WHERE ("programs"."is_public" = true)))))));



CREATE POLICY "insert_comments_by_access" ON "public"."comments" FOR INSERT WITH CHECK ((("object_id" IN ( SELECT "objects"."id"
   FROM "public"."objects"
  WHERE ("objects"."program_id" IN ( SELECT "user_program_access"."program_id"
           FROM "public"."user_program_access"
          WHERE ("user_program_access"."user_id" = "auth"."uid"()))))) AND (( SELECT "user_profiles"."can_comment"
   FROM "public"."user_profiles"
  WHERE ("user_profiles"."user_id" = "auth"."uid"())) = true)));



ALTER TABLE "public"."objects" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."password_reset_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."pending_invites" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."refresh_tokens" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "select_audit_by_access" ON "public"."flag_audit_log" FOR SELECT USING (("object_id" IN ( SELECT "objects"."id"
   FROM "public"."objects"
  WHERE ("objects"."program_id" IN ( SELECT "user_program_access"."program_id"
           FROM "public"."user_program_access"
          WHERE ("user_program_access"."user_id" = "auth"."uid"()))))));



CREATE POLICY "select_comments_by_access" ON "public"."comments" FOR SELECT USING (("object_id" IN ( SELECT "objects"."id"
   FROM "public"."objects"
  WHERE ("objects"."program_id" IN ( SELECT "user_program_access"."program_id"
           FROM "public"."user_program_access"
          WHERE ("user_program_access"."user_id" = "auth"."uid"()))))));



CREATE POLICY "select_objects_by_access" ON "public"."objects" FOR SELECT USING ((("program_id" IN ( SELECT "user_program_access"."program_id"
   FROM "public"."user_program_access"
  WHERE ("user_program_access"."user_id" = "auth"."uid"()))) OR ("program_id" IN ( SELECT "programs"."program_id"
   FROM "public"."programs"
  WHERE ("programs"."is_public" = true)))));



CREATE POLICY "select_spectra_by_access" ON "public"."spectra" FOR SELECT USING (("object_id" IN ( SELECT "objects"."object_id"
   FROM "public"."objects"
  WHERE (("objects"."program_id" IN ( SELECT "user_program_access"."program_id"
           FROM "public"."user_program_access"
          WHERE ("user_program_access"."user_id" = "auth"."uid"()))) OR ("objects"."program_id" IN ( SELECT "programs"."program_id"
           FROM "public"."programs"
          WHERE ("programs"."is_public" = true)))))));



ALTER TABLE "public"."spectra" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "update_objects_by_access" ON "public"."objects" FOR UPDATE USING ((("program_id" IN ( SELECT "user_program_access"."program_id"
   FROM "public"."user_program_access"
  WHERE ("user_program_access"."user_id" = "auth"."uid"()))) AND (( SELECT "user_profiles"."can_comment"
   FROM "public"."user_profiles"
  WHERE ("user_profiles"."user_id" = "auth"."uid"())) = true)));





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

























































































































































GRANT ALL ON FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."authorize_device_code"("p_user_code" "text", "p_user_id" "uuid") TO "service_role";



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



GRANT ALL ON FUNCTION "public"."get_adjacent_objects"("p_current_object_id" "text", "p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."get_adjacent_objects"("p_current_object_id" "text", "p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_adjacent_objects"("p_current_object_id" "text", "p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_download_stats"("p_days" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."get_download_stats"("p_days" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_download_stats"("p_days" integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) TO "anon";
GRANT ALL ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_gratings_mode" "text", "p_observations" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_max_snr_min" double precision, "p_max_snr_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_spectral_features_include_any" integer, "p_spectral_features_include_all" integer, "p_spectral_features_exclude" integer, "p_object_flags_include_any" integer, "p_object_flags_include_all" integer, "p_object_flags_exclude" integer, "p_dq_flags_include_any" integer, "p_dq_flags_include_all" integer, "p_dq_flags_exclude" integer, "p_search" "text", "p_inspected_only" boolean, "p_comment_search" "text", "p_comment_search_scope" "text", "p_comment_user_id" "uuid", "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer, "p_include_thumbnails" boolean) TO "service_role";



GRANT ALL ON FUNCTION "public"."get_program_stats"() TO "anon";
GRANT ALL ON FUNCTION "public"."get_program_stats"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_program_stats"() TO "service_role";



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



GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "anon";
GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "service_role";



GRANT ALL ON FUNCTION "public"."refresh_filter_options"() TO "anon";
GRANT ALL ON FUNCTION "public"."refresh_filter_options"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."refresh_filter_options"() TO "service_role";



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



GRANT ALL ON FUNCTION "public"."update_object_max_snr"() TO "anon";
GRANT ALL ON FUNCTION "public"."update_object_max_snr"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."update_object_max_snr"() TO "service_role";



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



GRANT ALL ON TABLE "public"."objects" TO "anon";
GRANT ALL ON TABLE "public"."objects" TO "authenticated";
GRANT ALL ON TABLE "public"."objects" TO "service_role";



GRANT ALL ON TABLE "public"."spectra" TO "anon";
GRANT ALL ON TABLE "public"."spectra" TO "authenticated";
GRANT ALL ON TABLE "public"."spectra" TO "service_role";



GRANT ALL ON TABLE "public"."mv_filter_options" TO "anon";
GRANT ALL ON TABLE "public"."mv_filter_options" TO "authenticated";
GRANT ALL ON TABLE "public"."mv_filter_options" TO "service_role";



GRANT ALL ON TABLE "public"."nircam_images" TO "anon";
GRANT ALL ON TABLE "public"."nircam_images" TO "authenticated";
GRANT ALL ON TABLE "public"."nircam_images" TO "service_role";



GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."object_flag_summary" TO "anon";
GRANT ALL ON TABLE "public"."object_flag_summary" TO "authenticated";
GRANT ALL ON TABLE "public"."object_flag_summary" TO "service_role";



GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."objects_with_flags" TO "anon";
GRANT ALL ON TABLE "public"."objects_with_flags" TO "authenticated";
GRANT ALL ON TABLE "public"."objects_with_flags" TO "service_role";



GRANT ALL ON TABLE "public"."password_reset_log" TO "anon";
GRANT ALL ON TABLE "public"."password_reset_log" TO "authenticated";
GRANT ALL ON TABLE "public"."password_reset_log" TO "service_role";



GRANT ALL ON TABLE "public"."pending_invites" TO "anon";
GRANT ALL ON TABLE "public"."pending_invites" TO "authenticated";
GRANT ALL ON TABLE "public"."pending_invites" TO "service_role";



GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."programs" TO "anon";
GRANT ALL ON TABLE "public"."programs" TO "authenticated";
GRANT ALL ON TABLE "public"."programs" TO "service_role";



GRANT ALL ON TABLE "public"."refresh_tokens" TO "anon";
GRANT ALL ON TABLE "public"."refresh_tokens" TO "authenticated";
GRANT ALL ON TABLE "public"."refresh_tokens" TO "service_role";



GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "service_role";



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































drop extension if exists "pg_net";

drop policy "Anyone can submit requests" on "public"."account_requests";

drop policy "Users can check own request status" on "public"."account_requests";


  create policy "Anyone can submit requests"
  on "public"."account_requests"
  as permissive
  for insert
  to anon, authenticated
with check (true);



  create policy "Users can check own request status"
  on "public"."account_requests"
  as permissive
  for select
  to anon, authenticated
using (true);




