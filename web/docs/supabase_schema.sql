


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


CREATE SCHEMA IF NOT EXISTS "public";


ALTER SCHEMA "public" OWNER TO "pg_database_owner";


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE OR REPLACE FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[] DEFAULT NULL::integer[], "p_fields" "text"[] DEFAULT NULL::"text"[], "p_gratings" "text"[] DEFAULT NULL::"text"[], "p_redshift_quality" integer[] DEFAULT NULL::integer[], "p_redshift_min" double precision DEFAULT NULL::double precision, "p_redshift_max" double precision DEFAULT NULL::double precision, "p_spectral_features" integer DEFAULT NULL::integer, "p_object_flags" integer DEFAULT NULL::integer, "p_dq_flags" integer DEFAULT NULL::integer, "p_search" "text" DEFAULT NULL::"text", "p_inspected_only" boolean DEFAULT NULL::boolean, "p_coord_ra" double precision DEFAULT NULL::double precision, "p_coord_dec" double precision DEFAULT NULL::double precision, "p_radius_degrees" double precision DEFAULT NULL::double precision, "p_sort_column" "text" DEFAULT 'object_id'::"text", "p_sort_direction" "text" DEFAULT 'asc'::"text", "p_page" integer DEFAULT 1, "p_page_size" integer DEFAULT 50) RETURNS TABLE("objects" "jsonb", "total_count" bigint, "page" integer, "page_size" integer)
    LANGUAGE "plpgsql" STABLE
    AS $$
DECLARE
  v_offset INTEGER;
  v_filtered_program_ids INTEGER[];
  v_grating_object_ids TEXT[];
BEGIN
  -- Calculate offset
  v_offset := (p_page - 1) * p_page_size;

  -- Validate sort direction
  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  -- Validate sort column (whitelist for security)
  IF p_sort_column NOT IN ('object_id', 'field', 'ra', 'dec', 'redshift', 'redshift_quality') THEN
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

  -- If grating filter is active, get object_ids that have matching spectra
  IF p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0 THEN
    SELECT ARRAY(
      SELECT DISTINCT s.object_id
      FROM spectra s
      WHERE s.grating = ANY(p_gratings)
    ) INTO v_grating_object_ids;

    IF v_grating_object_ids IS NULL OR array_length(v_grating_object_ids, 1) IS NULL THEN
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
    SELECT o.*
    FROM objects o
    WHERE
      -- Program access control
      o.program_id = ANY(v_filtered_program_ids)
      -- Grating filter (via pre-queried object IDs)
      AND (v_grating_object_ids IS NULL OR o.object_id = ANY(v_grating_object_ids))
      -- Field filter
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      -- Redshift quality filter
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality =
ANY(p_redshift_quality))
      -- Redshift range filters
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      -- Bitmask filters
      AND (p_spectral_features IS NULL OR (o.spectral_features & p_spectral_features) > 0)
      AND (p_object_flags IS NULL OR (o.object_flags & p_object_flags) > 0)
      AND (p_dq_flags IS NULL OR (o.dq_flags & p_dq_flags) > 0)
      -- Search filter
      AND (p_search IS NULL OR o.object_id ILIKE '%' || p_search || '%')
      -- Coordinate search (spatial filter)
      AND (
        p_coord_ra IS NULL OR p_coord_dec IS NULL OR p_radius_degrees IS NULL
        OR (
          2 * asin(sqrt(
            pow(sin(radians(p_coord_dec - o.dec) / 2), 2) +
            cos(radians(p_coord_dec)) * cos(radians(o.dec)) *
            pow(sin(radians(p_coord_ra - o.ra) / 2), 2)
          )) <= radians(p_radius_degrees)
        )
      )
      -- Inspected only filter
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND o.redshift_quality = 0)
      )
  ),
  counted AS (
    SELECT COUNT(*) as cnt FROM filtered_objects
  ),
  sorted_objects AS (
    SELECT fo.*
    FROM filtered_objects fo
    ORDER BY
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN fo.object_id END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN fo.object_id END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN fo.field END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN fo.field END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN fo.ra END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN fo.ra END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN fo.dec END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN fo.dec END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN fo.redshift END ASC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN fo.redshift END DESC NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN fo.redshift_quality END ASC
NULLS LAST,
      CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN fo.redshift_quality END DESC
  NULLS LAST,
      fo.object_id ASC
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
        'ra', p.ra,
        'dec', p.dec,
        'redshift', p.redshift,
        'redshift_auto', p.redshift_auto,
        'redshift_inspected', p.redshift_inspected,
        'redshift_quality', p.redshift_quality,
        'spectral_features', p.spectral_features,
        'object_flags', p.object_flags,
        'dq_flags', p.dq_flags,
        'last_inspected_at', p.last_inspected_at,
        'last_inspected_by', p.last_inspected_by,
        'created_at', p.created_at,
        'updated_at', p.updated_at,
        'distance', CASE
          WHEN p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL THEN
            degrees(2 * asin(sqrt(
              pow(sin(radians(p_coord_dec - p.dec) / 2), 2) +
              cos(radians(p_coord_dec)) * cos(radians(p.dec)) *
              pow(sin(radians(p_coord_ra - p.ra) / 2), 2)
            )))
          ELSE NULL
        END,
        'program_name', pr.program_name,
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
                'created_at', s.created_at
              )
              ORDER BY s.grating
            )
            FROM spectra s
            WHERE s.object_id = p.object_id
              AND (p_gratings IS NULL OR array_length(p_gratings, 1) IS NULL OR s.grating = ANY(p_gratings))
          ),
          '[]'::jsonb
        )
      ) as obj
    FROM paginated p
    LEFT JOIN programs pr ON pr.program_id = p.program_id
  )
  SELECT
    COALESCE(
      (
        SELECT jsonb_agg(wr.obj ORDER BY
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'asc' THEN wr.obj->>'object_id' END ASC
NULLS LAST,
          CASE WHEN p_sort_column = 'object_id' AND p_sort_direction = 'desc' THEN wr.obj->>'object_id' END DESC
NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'asc' THEN wr.obj->>'field' END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'field' AND p_sort_direction = 'desc' THEN wr.obj->>'field' END DESC NULLS
LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'asc' THEN (wr.obj->>'ra')::numeric END ASC NULLS
LAST,
          CASE WHEN p_sort_column = 'ra' AND p_sort_direction = 'desc' THEN (wr.obj->>'ra')::numeric END DESC
NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'asc' THEN (wr.obj->>'dec')::numeric END ASC
NULLS LAST,
          CASE WHEN p_sort_column = 'dec' AND p_sort_direction = 'desc' THEN (wr.obj->>'dec')::numeric END DESC
NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'asc' THEN (wr.obj->>'redshift')::numeric
END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift' AND p_sort_direction = 'desc' THEN (wr.obj->>'redshift')::numeric
END DESC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'asc' THEN
(wr.obj->>'redshift_quality')::integer END ASC NULLS LAST,
          CASE WHEN p_sort_column = 'redshift_quality' AND p_sort_direction = 'desc' THEN
(wr.obj->>'redshift_quality')::integer END DESC NULLS LAST
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


ALTER FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_search" "text", "p_inspected_only" boolean, "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer) OWNER TO "postgres";


COMMENT ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_search" "text", "p_inspected_only" boolean, "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer) IS 'Server-side filtering, sorting, and pagination for the spectra table.
Handles bitmask filters (spectral_features, object_flags, dq_flags) that cannot be done via PostgREST.
Supports dynamic sorting by: object_id, field, ra, dec, redshift, redshift_quality.
Returns objects with nested spectra and program name, along with total count for pagination.
Use with large page_size (e.g., 5000) to fetch all data for client-side sorting when result set is small.';



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
    "redshift" numeric(10,6) GENERATED ALWAYS AS (COALESCE(("redshift_inspected")::double precision, "redshift_auto")) STORED
);


ALTER TABLE "public"."objects" OWNER TO "postgres";


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


CREATE TABLE IF NOT EXISTS "public"."pending_invites" (
    "id" integer NOT NULL,
    "email" "text" NOT NULL,
    "program_ids" integer[] DEFAULT '{}'::integer[],
    "is_admin" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "invited_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "accepted_at" timestamp with time zone
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


CREATE TABLE IF NOT EXISTS "public"."spectra" (
    "id" integer NOT NULL,
    "grating" "text" NOT NULL,
    "fits_path" "text" NOT NULL,
    "reduction_version" "text" DEFAULT 'v1.0'::"text",
    "signal_to_noise" double precision,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "object_id" "text" NOT NULL
);


ALTER TABLE "public"."spectra" OWNER TO "postgres";


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
    "is_admin" boolean DEFAULT false
);


ALTER TABLE "public"."user_profiles" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_program_access" (
    "user_id" "uuid" NOT NULL,
    "program_id" integer NOT NULL,
    "granted_at" timestamp without time zone DEFAULT "now"(),
    "granted_by" "uuid"
);


ALTER TABLE "public"."user_program_access" OWNER TO "postgres";


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



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_code_id_user_id_key" UNIQUE ("code_id", "user_id");



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_pkey" PRIMARY KEY ("id");



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



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."programs"
    ADD CONSTRAINT "programs_pkey" PRIMARY KEY ("program_id");



ALTER TABLE ONLY "public"."spectra"
    ADD CONSTRAINT "spectra_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_pkey" PRIMARY KEY ("user_id", "program_id");



CREATE INDEX "idx_access_codes_code" ON "public"."access_codes" USING "btree" ("code");



CREATE INDEX "idx_audit_object" ON "public"."flag_audit_log" USING "btree" ("object_id");



CREATE INDEX "idx_audit_time" ON "public"."flag_audit_log" USING "btree" ("changed_at" DESC);



CREATE INDEX "idx_audit_user" ON "public"."flag_audit_log" USING "btree" ("user_id");



CREATE INDEX "idx_code_redemptions_user" ON "public"."code_redemptions" USING "btree" ("user_id");



CREATE INDEX "idx_comments_created" ON "public"."comments" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_comments_object" ON "public"."comments" USING "btree" ("object_id");



CREATE INDEX "idx_comments_user" ON "public"."comments" USING "btree" ("user_id");



CREATE INDEX "idx_flag_audit_log_object_id" ON "public"."flag_audit_log" USING "btree" ("object_id");



CREATE INDEX "idx_images_field" ON "public"."nircam_images" USING "btree" ("field");



CREATE INDEX "idx_images_filter" ON "public"."nircam_images" USING "btree" ("filter");



CREATE INDEX "idx_objects_coords" ON "public"."objects" USING "btree" ("ra", "dec");



CREATE INDEX "idx_objects_field" ON "public"."objects" USING "btree" ("field");



CREATE INDEX "idx_objects_program" ON "public"."objects" USING "btree" ("program_id");



CREATE INDEX "idx_objects_redshift" ON "public"."objects" USING "btree" ("redshift_auto");



CREATE INDEX "idx_objects_redshift_quality" ON "public"."objects" USING "btree" ("redshift_quality");



CREATE INDEX "idx_pending_invites_email" ON "public"."pending_invites" USING "btree" ("email");



CREATE INDEX "idx_spectra_object_id" ON "public"."spectra" USING "btree" ("object_id");



CREATE OR REPLACE TRIGGER "track_flag_changes" BEFORE UPDATE ON "public"."objects" FOR EACH ROW EXECUTE FUNCTION "public"."log_flag_changes"();



ALTER TABLE ONLY "public"."access_codes"
    ADD CONSTRAINT "access_codes_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_code_id_fkey" FOREIGN KEY ("code_id") REFERENCES "public"."access_codes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."code_redemptions"
    ADD CONSTRAINT "code_redemptions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_last_inspected_by_fkey" FOREIGN KEY ("last_inspected_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_program_id_fkey" FOREIGN KEY ("program_id") REFERENCES "public"."programs"("program_id");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_invited_by_fkey" FOREIGN KEY ("invited_by") REFERENCES "auth"."users"("id");



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



CREATE POLICY "Admins can view invites" ON "public"."pending_invites" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."user_profiles"
  WHERE (("user_profiles"."user_id" = "auth"."uid"()) AND ("user_profiles"."is_admin" = true)))));



CREATE POLICY "Allow authenticated users to insert comments" ON "public"."comments" FOR INSERT TO "authenticated" WITH CHECK (("auth"."uid"() = "user_id"));



CREATE POLICY "Allow authenticated users to read comments" ON "public"."comments" FOR SELECT TO "authenticated" USING (true);



CREATE POLICY "Allow authenticated users to update objects" ON "public"."objects" FOR UPDATE TO "authenticated" USING (true) WITH CHECK (true);



CREATE POLICY "Anyone can read active codes" ON "public"."access_codes" FOR SELECT USING (("is_active" = true));



CREATE POLICY "Users can read own invite by email" ON "public"."pending_invites" FOR SELECT TO "authenticated" USING (("email" = (( SELECT "users"."email"
   FROM "auth"."users"
  WHERE ("users"."id" = "auth"."uid"())))::"text"));



CREATE POLICY "Users can redeem codes" ON "public"."code_redemptions" FOR INSERT WITH CHECK (("user_id" = "auth"."uid"()));



CREATE POLICY "Users can see own redemptions" ON "public"."code_redemptions" FOR SELECT USING (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."access_codes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."code_redemptions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."comments" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."flag_audit_log" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "insert_comments_by_access" ON "public"."comments" FOR INSERT WITH CHECK ((("object_id" IN ( SELECT "objects"."id"
   FROM "public"."objects"
  WHERE ("objects"."program_id" IN ( SELECT "user_program_access"."program_id"
           FROM "public"."user_program_access"
          WHERE ("user_program_access"."user_id" = "auth"."uid"()))))) AND (( SELECT "user_profiles"."can_comment"
   FROM "public"."user_profiles"
  WHERE ("user_profiles"."user_id" = "auth"."uid"())) = true)));



ALTER TABLE "public"."objects" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."pending_invites" ENABLE ROW LEVEL SECURITY;


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



GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";



GRANT ALL ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_search" "text", "p_inspected_only" boolean, "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_search" "text", "p_inspected_only" boolean, "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_filtered_objects_paginated"("p_program_ids" integer[], "p_filter_programs" integer[], "p_fields" "text"[], "p_gratings" "text"[], "p_redshift_quality" integer[], "p_redshift_min" double precision, "p_redshift_max" double precision, "p_spectral_features" integer, "p_object_flags" integer, "p_dq_flags" integer, "p_search" "text", "p_inspected_only" boolean, "p_coord_ra" double precision, "p_coord_dec" double precision, "p_radius_degrees" double precision, "p_sort_column" "text", "p_sort_direction" "text", "p_page" integer, "p_page_size" integer) TO "service_role";



GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "anon";
GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."log_flag_changes"() TO "service_role";



GRANT ALL ON TABLE "public"."access_codes" TO "anon";
GRANT ALL ON TABLE "public"."access_codes" TO "authenticated";
GRANT ALL ON TABLE "public"."access_codes" TO "service_role";



GRANT ALL ON TABLE "public"."code_redemptions" TO "anon";
GRANT ALL ON TABLE "public"."code_redemptions" TO "authenticated";
GRANT ALL ON TABLE "public"."code_redemptions" TO "service_role";



GRANT ALL ON TABLE "public"."comments" TO "anon";
GRANT ALL ON TABLE "public"."comments" TO "authenticated";
GRANT ALL ON TABLE "public"."comments" TO "service_role";



GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."flag_audit_log" TO "anon";
GRANT ALL ON TABLE "public"."flag_audit_log" TO "authenticated";
GRANT ALL ON TABLE "public"."flag_audit_log" TO "service_role";



GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."flag_definitions" TO "anon";
GRANT ALL ON TABLE "public"."flag_definitions" TO "authenticated";
GRANT ALL ON TABLE "public"."flag_definitions" TO "service_role";



GRANT ALL ON TABLE "public"."nircam_images" TO "anon";
GRANT ALL ON TABLE "public"."nircam_images" TO "authenticated";
GRANT ALL ON TABLE "public"."nircam_images" TO "service_role";



GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."objects" TO "anon";
GRANT ALL ON TABLE "public"."objects" TO "authenticated";
GRANT ALL ON TABLE "public"."objects" TO "service_role";



GRANT ALL ON TABLE "public"."object_flag_summary" TO "anon";
GRANT ALL ON TABLE "public"."object_flag_summary" TO "authenticated";
GRANT ALL ON TABLE "public"."object_flag_summary" TO "service_role";



GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."objects_with_flags" TO "anon";
GRANT ALL ON TABLE "public"."objects_with_flags" TO "authenticated";
GRANT ALL ON TABLE "public"."objects_with_flags" TO "service_role";



GRANT ALL ON TABLE "public"."pending_invites" TO "anon";
GRANT ALL ON TABLE "public"."pending_invites" TO "authenticated";
GRANT ALL ON TABLE "public"."pending_invites" TO "service_role";



GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."programs" TO "anon";
GRANT ALL ON TABLE "public"."programs" TO "authenticated";
GRANT ALL ON TABLE "public"."programs" TO "service_role";



GRANT ALL ON TABLE "public"."spectra" TO "anon";
GRANT ALL ON TABLE "public"."spectra" TO "authenticated";
GRANT ALL ON TABLE "public"."spectra" TO "service_role";



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







