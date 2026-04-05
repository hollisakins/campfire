-- =============================================================================
-- CAMPFIRE Supabase Schema: Tables
-- =============================================================================
-- Canonical source of truth for all table definitions, sequences, constraints,
-- extensions, table grants, and default privileges.
--
-- Workflow: edit here → supabase db diff -f <description> → commit migration
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Session / search_path boilerplate
-- ---------------------------------------------------------------------------

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


COMMENT ON SCHEMA "public" IS 'standard public schema';


-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS "pg_graphql" WITH SCHEMA "graphql";

CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";

CREATE EXTENSION IF NOT EXISTS "pg_trgm" WITH SCHEMA "public";

CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";

CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";


-- ---------------------------------------------------------------------------
-- Table access method defaults
-- ---------------------------------------------------------------------------

SET default_tablespace = '';

SET default_table_access_method = "heap";


-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

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
    "observation" "text" NOT NULL,
    "object_id" integer
);


ALTER TABLE "public"."targets" OWNER TO "postgres";


COMMENT ON COLUMN "public"."targets"."redshift" IS 'Generated column: NULL when redshift_quality = 1 (Impossible), otherwise COALESCE(redshift_inspected, redshift_auto). This allows "Impossible" objects to be excluded from redshift range filters.';



COMMENT ON COLUMN "public"."targets"."has_sed_plot" IS 'Indicates whether an SED plot PDF exists in R2. Set during deployment to avoid runtime R2 HeadObject calls.';



CREATE TABLE IF NOT EXISTS "public"."objects" (
    "id" integer NOT NULL,
    "object_id" "text" NOT NULL,
    "field" "text" NOT NULL,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "n_targets" integer NOT NULL DEFAULT 0,
    "n_spectra" integer NOT NULL DEFAULT 0,
    "programs" "text"[] NOT NULL DEFAULT '{}'::"text"[],
    "gratings" "text"[] NOT NULL DEFAULT '{}'::"text"[],
    "max_snr" double precision,
    "max_exposure_time" double precision,
    "best_redshift" double precision,
    "best_redshift_quality" integer DEFAULT 0,
    "updated_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."objects" OWNER TO "postgres";


COMMENT ON TABLE "public"."objects" IS 'Unique sky positions cross-matched across programs. One object groups one or more targets observed within ~0.2 arcsec. Static properties recomputed at deploy time; best_redshift/quality maintained by trigger.';



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



CREATE SEQUENCE IF NOT EXISTS "public"."targets_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."targets_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."targets_id_seq" OWNED BY "public"."targets"."id";



CREATE SEQUENCE IF NOT EXISTS "public"."objects_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."objects_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."objects_id_seq" OWNED BY "public"."objects"."id";



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


-- ---------------------------------------------------------------------------
-- Sequence defaults (SET DEFAULT for serial columns)
-- ---------------------------------------------------------------------------

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



ALTER TABLE ONLY "public"."objects" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."objects_id_seq"'::"regclass");



-- ---------------------------------------------------------------------------
-- Primary keys, unique constraints
-- ---------------------------------------------------------------------------

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



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_object_id_key" UNIQUE ("object_id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."user_program_access"
    ADD CONSTRAINT "user_program_access_pkey" PRIMARY KEY ("user_id", "program_slug");



-- ---------------------------------------------------------------------------
-- Foreign key constraints
-- ---------------------------------------------------------------------------

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



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "fk_targets_object" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id");



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



-- ---------------------------------------------------------------------------
-- Publication
-- ---------------------------------------------------------------------------

ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";


-- ---------------------------------------------------------------------------
-- Schema grants
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";


-- ---------------------------------------------------------------------------
-- Table grants (tables only — views and mat views excluded)
-- ---------------------------------------------------------------------------

GRANT ALL ON TABLE "public"."access_codes" TO "anon";
GRANT ALL ON TABLE "public"."access_codes" TO "authenticated";
GRANT ALL ON TABLE "public"."access_codes" TO "service_role";



GRANT ALL ON TABLE "public"."account_requests" TO "anon";
GRANT ALL ON TABLE "public"."account_requests" TO "authenticated";
GRANT ALL ON TABLE "public"."account_requests" TO "service_role";



GRANT ALL ON TABLE "public"."api_keys" TO "anon";
GRANT ALL ON TABLE "public"."api_keys" TO "authenticated";
GRANT ALL ON TABLE "public"."api_keys" TO "service_role";



GRANT ALL ON TABLE "public"."code_redemptions" TO "anon";
GRANT ALL ON TABLE "public"."code_redemptions" TO "authenticated";
GRANT ALL ON TABLE "public"."code_redemptions" TO "service_role";



GRANT ALL ON TABLE "public"."comments" TO "anon";
GRANT ALL ON TABLE "public"."comments" TO "authenticated";
GRANT ALL ON TABLE "public"."comments" TO "service_role";



GRANT ALL ON TABLE "public"."device_codes" TO "anon";
GRANT ALL ON TABLE "public"."device_codes" TO "authenticated";
GRANT ALL ON TABLE "public"."device_codes" TO "service_role";



GRANT ALL ON TABLE "public"."download_log" TO "anon";
GRANT ALL ON TABLE "public"."download_log" TO "authenticated";
GRANT ALL ON TABLE "public"."download_log" TO "service_role";



GRANT ALL ON TABLE "public"."flag_audit_log" TO "anon";
GRANT ALL ON TABLE "public"."flag_audit_log" TO "authenticated";
GRANT ALL ON TABLE "public"."flag_audit_log" TO "service_role";



GRANT ALL ON TABLE "public"."flag_definitions" TO "anon";
GRANT ALL ON TABLE "public"."flag_definitions" TO "authenticated";
GRANT ALL ON TABLE "public"."flag_definitions" TO "service_role";



GRANT ALL ON TABLE "public"."map_layers" TO "anon";
GRANT ALL ON TABLE "public"."map_layers" TO "authenticated";
GRANT ALL ON TABLE "public"."map_layers" TO "service_role";



GRANT ALL ON TABLE "public"."spectra" TO "anon";
GRANT ALL ON TABLE "public"."spectra" TO "authenticated";
GRANT ALL ON TABLE "public"."spectra" TO "service_role";



GRANT ALL ON TABLE "public"."targets" TO "anon";
GRANT ALL ON TABLE "public"."targets" TO "authenticated";
GRANT ALL ON TABLE "public"."targets" TO "service_role";



GRANT ALL ON TABLE "public"."objects" TO "anon";
GRANT ALL ON TABLE "public"."objects" TO "authenticated";
GRANT ALL ON TABLE "public"."objects" TO "service_role";



GRANT ALL ON TABLE "public"."observations" TO "anon";
GRANT ALL ON TABLE "public"."observations" TO "authenticated";
GRANT ALL ON TABLE "public"."observations" TO "service_role";



GRANT ALL ON TABLE "public"."programs" TO "anon";
GRANT ALL ON TABLE "public"."programs" TO "authenticated";
GRANT ALL ON TABLE "public"."programs" TO "service_role";



GRANT ALL ON TABLE "public"."nircam_images" TO "anon";
GRANT ALL ON TABLE "public"."nircam_images" TO "authenticated";
GRANT ALL ON TABLE "public"."nircam_images" TO "service_role";



GRANT ALL ON TABLE "public"."password_reset_log" TO "anon";
GRANT ALL ON TABLE "public"."password_reset_log" TO "authenticated";
GRANT ALL ON TABLE "public"."password_reset_log" TO "service_role";



GRANT ALL ON TABLE "public"."pending_invites" TO "anon";
GRANT ALL ON TABLE "public"."pending_invites" TO "authenticated";
GRANT ALL ON TABLE "public"."pending_invites" TO "service_role";



GRANT ALL ON TABLE "public"."refresh_tokens" TO "anon";
GRANT ALL ON TABLE "public"."refresh_tokens" TO "authenticated";
GRANT ALL ON TABLE "public"."refresh_tokens" TO "service_role";



GRANT ALL ON TABLE "public"."shutters" TO "anon";
GRANT ALL ON TABLE "public"."shutters" TO "authenticated";
GRANT ALL ON TABLE "public"."shutters" TO "service_role";



GRANT ALL ON TABLE "public"."slit_regions" TO "anon";
GRANT ALL ON TABLE "public"."slit_regions" TO "authenticated";
GRANT ALL ON TABLE "public"."slit_regions" TO "service_role";



GRANT ALL ON TABLE "public"."user_profiles" TO "anon";
GRANT ALL ON TABLE "public"."user_profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."user_profiles" TO "service_role";



GRANT ALL ON TABLE "public"."user_program_access" TO "anon";
GRANT ALL ON TABLE "public"."user_program_access" TO "authenticated";
GRANT ALL ON TABLE "public"."user_program_access" TO "service_role";



-- ---------------------------------------------------------------------------
-- Sequence grants
-- ---------------------------------------------------------------------------

GRANT ALL ON SEQUENCE "public"."account_requests_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."account_requests_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."account_requests_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."comments_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."flag_audit_log_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."map_layers_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."map_layers_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."map_layers_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nircam_images_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."pending_invites_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."shutters_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."shutters_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."shutters_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."slit_regions_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."slit_regions_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."slit_regions_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."spectra_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."targets_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."targets_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."targets_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."objects_id_seq" TO "service_role";



-- ---------------------------------------------------------------------------
-- Default privileges
-- ---------------------------------------------------------------------------

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
