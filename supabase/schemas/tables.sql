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
    "target_id" integer,
    "object_id" integer,
    "user_id" "uuid" NOT NULL,
    "content" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "edited_at" timestamp without time zone,
    "is_deleted" boolean DEFAULT false,
    CONSTRAINT "comments_at_most_one_parent" CHECK (num_nonnulls(target_id, object_id) <= 1)
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
    -- Phase D: target_id was the original (and only) subject column. Now nullable
    -- so rows can attribute changes to either an object (object-level inspection)
    -- or a single spectrum (per-spectrum DQ flag edits) instead. Existing
    -- pre-Phase-D rows keep their target_id; new writes set exactly one of the
    -- three subject columns (enforced by the flag_audit_log_subject_check).
    "target_id" integer,
    "object_id" integer,
    "spectrum_id" integer,
    "user_id" "uuid",
    "field_name" "text" NOT NULL,
    "old_value" integer,
    "new_value" integer,
    "changed_at" timestamp without time zone DEFAULT "now"(),
    -- At-most-one subject (not exactly-one) so ON DELETE SET NULL on the
    -- subject FKs doesn't violate the constraint when a spectrum/target/object
    -- is deleted (e.g. pipeline delete-and-reinsert of a reprocessed spectrum).
    -- The audit row survives with degraded subject info instead of cascading
    -- away the history.  INSERT still requires exactly one subject — enforced
    -- by the trigger functions that write these rows.
    CONSTRAINT "flag_audit_log_subject_check" CHECK (
        (target_id IS NOT NULL)::int +
        (object_id IS NOT NULL)::int +
        (spectrum_id IS NOT NULL)::int <= 1
    )
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
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "target_id" "text" NOT NULL,
    "thumbnail_svg_fnu" "text",
    "thumbnail_svg_flambda" "text",
    "file_hash" "text",
    "file_size" bigint,
    "exposure_time" double precision,
    "crds_context" "text",
    "jwst_version" "text",
    "cfpipe_version" "text",
    "date_obs" "text",
    -- Phase A: per-spectrum auto-fit and DQ (populated in Phase B by deploy pipeline; backfilled in Phase D)
    "redshift_auto" double precision,
    "dq_flags" integer NOT NULL DEFAULT 0,
    -- Stable per-spectrum identifier derived from fits_path: strips the leading
    -- directory and the trailing "_spec.fits" suffix (e.g. ember_cosmos_p1_prism_clear_12345).
    -- Generated/stored so it stays in sync with fits_path with no application code path.
    "spectrum_id" "text" GENERATED ALWAYS AS (
      regexp_replace(
        regexp_replace("fits_path", '^.*/', ''),
        '_spec\.fits$', '', 'i'
      )
    ) STORED
);


ALTER TABLE "public"."spectra" OWNER TO "postgres";


COMMENT ON COLUMN "public"."spectra"."thumbnail_svg_fnu" IS 'Pre-generated SVG sparkline thumbnail in f_nu units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';



COMMENT ON COLUMN "public"."spectra"."thumbnail_svg_flambda" IS 'Pre-generated SVG sparkline thumbnail in f_lambda units. Set during deployment to avoid R2 fetches and CPU-intensive processing at runtime.';



COMMENT ON COLUMN "public"."spectra"."file_hash" IS 'SHA-256 hash of the FITS file in R2. Used for incremental sync to detect changed files.';



COMMENT ON COLUMN "public"."spectra"."file_size" IS 'Size of the FITS file in bytes. Used for download size estimation and verification.';



COMMENT ON COLUMN "public"."spectra"."exposure_time" IS 'Effective exposure time in seconds, from EFFEXPTM FITS header.';



COMMENT ON COLUMN "public"."spectra"."redshift_auto" IS 'Phase A: per-grating zfit redshift_auto from the pipeline ECSV. Populated in Phase B by deploy pipeline; backfilled to per-target value by Phase D.1b migration.';



COMMENT ON COLUMN "public"."spectra"."dq_flags" IS 'Phase A: per-spectrum DQ bitmask. Populated in Phase B by deploy pipeline; backfilled from targets.dq_flags by Phase D.1c migration.';



CREATE TABLE IF NOT EXISTS "public"."targets" (
    "id" integer NOT NULL,
    "target_id" "text" NOT NULL,
    "field" "text" NOT NULL,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "redshift_auto" double precision,
    "redshift_quality" integer DEFAULT 0,
    "spectral_features" integer DEFAULT 0,
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


COMMENT ON COLUMN "public"."targets"."has_sed_plot" IS 'Indicates whether an SED plot PDF exists in R2. Set during deployment to avoid runtime R2 HeadObject calls.';

-- ---------------------------------------------------------------------------
-- DEPRECATED target-tier inspection columns
-- ---------------------------------------------------------------------------
-- Phase D promoted inspection state to objects.  The columns below are kept
-- transitionally to satisfy (a) pre-Phase-D migration provenance and (b) the
-- member_targets payload in get_filtered_objects_paginated, which still reads
-- targets.redshift_auto for display of per-target auto-z.  Remove in Phase E
-- after the reader is swapped to spectra.redshift_auto.
--
-- Until removal:
--   - UI must not surface these as current inspection state.
--   - Writes go to objects (user inspection) or spectra (per-spectrum DQ).
--   - admin_targets_update policy still allows admin writes — needed so the
--     deploy CLI can touch targets during Phase E backfill scripts.

COMMENT ON COLUMN "public"."targets"."redshift_auto" IS 'DEPRECATED (Phase D): per-target auto-z moved to spectra.redshift_auto + objects.redshift_auto aggregate. Still read by get_filtered_objects_paginated member_targets payload for transitional UI. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."redshift_quality" IS 'DEPRECATED (Phase D): inspection state moved to objects.redshift_quality. No-op — no consumer reads this. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."spectral_features" IS 'DEPRECATED (Phase D): spectral-feature flags moved to objects.spectral_features (pending Phase E feature) and/or per-spectrum comments. No-op. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."dq_flags" IS 'DEPRECATED (Phase D): DQ flags moved to spectra.dq_flags (per-spectrum). No-op — no consumer reads this. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."redshift_inspected" IS 'DEPRECATED (Phase D): user override moved to objects.redshift_inspected. No-op. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."last_inspected_at" IS 'DEPRECATED (Phase D): inspection attribution moved to objects.last_inspected_at. No-op. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."last_inspected_by" IS 'DEPRECATED (Phase D): inspection attribution moved to objects.last_inspected_by. No-op. Remove in Phase E.';

COMMENT ON COLUMN "public"."targets"."redshift" IS 'DEPRECATED (Phase D): generated column derived from targets.redshift_inspected/_auto/_quality, all of which are now no-op state. Object-level equivalent lives at objects.redshift. Remove in Phase E with its inputs.';



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
    "observations" "text"[] NOT NULL DEFAULT '{}'::"text"[],
    "max_snr" double precision,
    "max_exposure_time" double precision,
    "photo_z" double precision,
    "photo_z_err_lo" double precision,
    "photo_z_err_hi" double precision,
    "has_photometry" boolean NOT NULL DEFAULT false,
    "updated_at" timestamp with time zone DEFAULT "now"(),
    -- Phase A: inspection state (populated in Phase D migration; see docs/design-objects-migration.md)
    "redshift_auto" double precision,
    "redshift_inspected" numeric(10,6),
    "redshift_quality" integer NOT NULL DEFAULT 0,
    "inspected_used_auto" boolean NOT NULL DEFAULT false,
    "redshift" numeric(10,6) GENERATED ALWAYS AS (
        CASE
            WHEN ("redshift_quality" = 1) THEN NULL::double precision
            ELSE COALESCE(("redshift_inspected")::double precision, "redshift_auto")
        END
    ) STORED,
    "last_inspected_at" timestamp with time zone,
    "last_inspected_by" "uuid",
    "last_data_change_at" timestamp with time zone,
    "staleness_reason" "text",
    "version" integer NOT NULL DEFAULT 1,
    "is_active" boolean NOT NULL DEFAULT true
);


ALTER TABLE "public"."objects" OWNER TO "postgres";


COMMENT ON TABLE "public"."objects" IS 'Unique sky positions cross-matched across programs. One object groups one or more targets observed within ~0.2 arcsec. Aggregate columns (n_targets, programs, max_snr, etc.) refreshed by reconcile_field_objects() at deploy time; redshift / redshift_quality / inspection state are user-editable and persist across reconciliation.';


COMMENT ON COLUMN "public"."objects"."redshift_auto" IS 'Phase A: per-object auto-fit redshift, computed post-reconciliation by compute_object_redshift_auto() from the best member spectrum under a grating-priority hierarchy (PRISM > medium > high-res, tiebreak on exposure_time). Empty until Phase D migration.';


COMMENT ON COLUMN "public"."objects"."redshift_inspected" IS 'Phase A: user-set redshift override at the object level. Empty until Phase D migration. After the pin-on-signoff migration, also populated automatically (= redshift_auto, with inspected_used_auto = true) when an inspector commits a quality flag without typing a numeric override — this stabilizes the displayed redshift across reprocessing.';


COMMENT ON COLUMN "public"."objects"."inspected_used_auto" IS 'True when redshift_inspected was auto-pinned from redshift_auto at sign-off (implicit sign-off path). False for explicit user-typed overrides and for uninspected/impossible rows. Maintained exclusively by the pin_redshift_on_signoff trigger; the UI uses this to avoid showing an "(overridden)" hint when the inspector merely accepted the auto-fit.';


COMMENT ON COLUMN "public"."objects"."redshift_quality" IS 'Phase A: 0=uninspected, 1=Impossible, 2=Tentative, 3=Probable, 4=Secure. Default 0. Empty until Phase D migration.';


COMMENT ON COLUMN "public"."objects"."redshift" IS 'Generated column: NULL when redshift_quality = 1 (Impossible), otherwise COALESCE(redshift_inspected, redshift_auto). Mirrors targets.redshift semantics at object level.';


COMMENT ON COLUMN "public"."objects"."staleness_reason" IS 'Phase A: one of new_target | reprocessed | membership_changed | migration_conflict. Set by reconcile_field_objects() (Phase C) when last_data_change_at advances past last_inspected_at.';


COMMENT ON COLUMN "public"."objects"."version" IS 'Phase A: optimistic-locking counter. Incremented by trigger only when redshift_inspected or redshift_quality changes. Clients pass expected_version on PATCH; mismatch → 409 Conflict.';


COMMENT ON COLUMN "public"."objects"."is_active" IS 'Phase A: false = soft-deleted (orphaned by reconciliation). Hidden from list/map/queue/CSV; reachable via direct URL with banner; admin endpoint reactivates.';



CREATE TABLE IF NOT EXISTS "public"."object_photometry" (
    "id" integer NOT NULL,
    "object_id" integer,
    "field" "text" NOT NULL,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "catalog_name" "text" NOT NULL,
    "catalog_id" "text",
    "match_distance_arcsec" double precision,
    "photometry" jsonb NOT NULL,
    "photo_z" double precision,
    "photo_z_err_lo" double precision,
    "photo_z_err_hi" double precision,
    "has_pz" boolean NOT NULL DEFAULT false,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."object_photometry" OWNER TO "postgres";


COMMENT ON TABLE "public"."object_photometry" IS 'Photometric catalog cross-matches for objects. One row per object per catalog. Coordinates (ra, dec) are the durable positional key; object_id FK is refreshed after each objects rebuild via coordinate cross-matching.';



CREATE TABLE IF NOT EXISTS "public"."object_lists" (
    "id" integer NOT NULL,
    "name" "text" NOT NULL,
    "slug" "text" NOT NULL,
    "description" "text",
    "visibility" "text" DEFAULT 'private'::"text" NOT NULL,
    "is_system" boolean DEFAULT false NOT NULL,
    "color" "text",
    "icon" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "object_lists_visibility_check" CHECK (("visibility" = ANY (ARRAY['private'::"text", 'public_read'::"text", 'public_edit'::"text"])))
);


ALTER TABLE "public"."object_lists" OWNER TO "postgres";


COMMENT ON TABLE "public"."object_lists" IS 'User-created or system-seeded lists of objects. Visibility controls who can see and edit the list. System lists (is_system=true) are seeded at migration time and cannot be deleted by users.';



CREATE TABLE IF NOT EXISTS "public"."object_list_members" (
    "id" integer NOT NULL,
    "list_id" integer NOT NULL,
    "object_id" integer,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "notes" "text",
    "added_by" "uuid",
    "added_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."object_list_members" OWNER TO "postgres";


COMMENT ON TABLE "public"."object_list_members" IS 'Members of object lists. Coordinates (ra, dec) are the durable positional key; object_id is a fast query key that gets refreshed after each objects rebuild via coordinate cross-matching.';



CREATE TABLE IF NOT EXISTS "public"."list_audit_log" (
    "id" integer NOT NULL,
    "list_id" integer NOT NULL,
    "object_id" integer,
    "user_id" "uuid",
    "action" "text" NOT NULL,
    "ra" double precision,
    "dec" double precision,
    "changed_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "list_audit_log_action_check" CHECK (("action" = ANY (ARRAY['add'::"text", 'remove'::"text"])))
);


ALTER TABLE "public"."list_audit_log" OWNER TO "postgres";



CREATE TABLE IF NOT EXISTS "public"."observations" (
    "name" "text" NOT NULL,
    "program_slug" "text" NOT NULL,
    "jwst_program_id" integer NOT NULL,
    "field" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "latest_deployment_id" integer,
    "file_globs" "text"[] NOT NULL DEFAULT '{}',
    "gratings" "text"[] NOT NULL DEFAULT '{}',
    "data_subdir" "text"
);


ALTER TABLE "public"."observations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."deployments" (
    "id" integer NOT NULL,
    "observation" "text" NOT NULL,
    "deployed_by" "uuid" NOT NULL,
    "deployed_at" timestamp with time zone DEFAULT "now"(),
    "cfpipe_version" "text",
    "jwst_version" "text",
    "crds_context" "text",
    "reduction_version" "text",
    "config_snapshot" "jsonb",
    "n_targets" integer,
    "n_spectra" integer,
    "n_new_targets" integer,
    "force_overwrite" boolean DEFAULT false,
    "source_ids_filter" integer[],
    "supabase_only" boolean DEFAULT false,
    "stuck_shutters" "jsonb",
    "reduced_at" timestamp with time zone
);


ALTER TABLE "public"."deployments" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."deployments_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."deployments_id_seq" OWNED BY "public"."deployments"."id";


ALTER TABLE ONLY "public"."deployments" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."deployments_id_seq"'::"regclass");


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



CREATE SEQUENCE IF NOT EXISTS "public"."object_lists_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."object_lists_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."object_lists_id_seq" OWNED BY "public"."object_lists"."id";



CREATE SEQUENCE IF NOT EXISTS "public"."object_list_members_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."object_list_members_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."object_list_members_id_seq" OWNED BY "public"."object_list_members"."id";


CREATE SEQUENCE IF NOT EXISTS "public"."object_photometry_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."object_photometry_id_seq" OWNER TO "postgres";

ALTER SEQUENCE "public"."object_photometry_id_seq" OWNED BY "public"."object_photometry"."id";



CREATE SEQUENCE IF NOT EXISTS "public"."list_audit_log_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."list_audit_log_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."list_audit_log_id_seq" OWNED BY "public"."list_audit_log"."id";



CREATE TABLE IF NOT EXISTS "public"."user_profiles" (
    "user_id" "uuid" NOT NULL,
    "username" "text" NOT NULL,
    "full_name" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "is_group_account" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "is_admin" boolean DEFAULT false,
    "preferences" "jsonb" DEFAULT '{}'::"jsonb",
    CONSTRAINT "user_profiles_username_check" CHECK (("username" ~ '^[a-z0-9][a-z0-9._-]{0,38}[a-z0-9]$'::"text"))
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



ALTER TABLE ONLY "public"."object_lists" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_lists_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."object_list_members" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_list_members_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."object_photometry" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_photometry_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."list_audit_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."list_audit_log_id_seq"'::"regclass");



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



ALTER TABLE ONLY "public"."deployments"
    ADD CONSTRAINT "deployments_pkey" PRIMARY KEY ("id");



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



ALTER TABLE ONLY "public"."object_lists"
    ADD CONSTRAINT "object_lists_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."object_lists"
    ADD CONSTRAINT "object_lists_slug_key" UNIQUE ("slug");



ALTER TABLE ONLY "public"."object_list_members"
    ADD CONSTRAINT "object_list_members_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."object_list_members"
    ADD CONSTRAINT "object_list_members_list_id_ra_dec_key" UNIQUE ("list_id", "ra", "dec");


ALTER TABLE ONLY "public"."object_photometry"
    ADD CONSTRAINT "object_photometry_pkey" PRIMARY KEY ("id");


ALTER TABLE ONLY "public"."object_photometry"
    ADD CONSTRAINT "object_photometry_field_catalog_name_catalog_id_key" UNIQUE ("field", "catalog_name", "catalog_id");



ALTER TABLE ONLY "public"."list_audit_log"
    ADD CONSTRAINT "list_audit_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_username_key" UNIQUE ("username");



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
    ADD CONSTRAINT "comments_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE SET NULL;


ALTER TABLE ONLY "public"."comments"
    ADD CONSTRAINT "comments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."device_codes"
    ADD CONSTRAINT "device_codes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."deployments"
    ADD CONSTRAINT "deployments_observation_fkey" FOREIGN KEY ("observation") REFERENCES "public"."observations"("name");



ALTER TABLE ONLY "public"."deployments"
    ADD CONSTRAINT "deployments_deployed_by_fkey" FOREIGN KEY ("deployed_by") REFERENCES "public"."user_profiles"("user_id");



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



ALTER TABLE ONLY "public"."object_lists"
    ADD CONSTRAINT "object_lists_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."object_list_members"
    ADD CONSTRAINT "object_list_members_list_id_fkey" FOREIGN KEY ("list_id") REFERENCES "public"."object_lists"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."object_list_members"
    ADD CONSTRAINT "object_list_members_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."object_list_members"
    ADD CONSTRAINT "object_list_members_added_by_fkey" FOREIGN KEY ("added_by") REFERENCES "auth"."users"("id");


ALTER TABLE ONLY "public"."object_photometry"
    ADD CONSTRAINT "object_photometry_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."list_audit_log"
    ADD CONSTRAINT "list_audit_log_list_id_fkey" FOREIGN KEY ("list_id") REFERENCES "public"."object_lists"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."list_audit_log"
    ADD CONSTRAINT "list_audit_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



-- Subject FKs use ON DELETE SET NULL (not CASCADE) so pipeline churn
-- (delete-then-reinsert of a reprocessed spectrum, object rebuild) doesn't
-- wipe the audit trail.  Paired with the relaxed <= 1 subject-check above.
ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_object_id_fkey" FOREIGN KEY ("object_id") REFERENCES "public"."objects"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_spectrum_id_fkey" FOREIGN KEY ("spectrum_id") REFERENCES "public"."spectra"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."flag_audit_log"
    ADD CONSTRAINT "flag_audit_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."observations"
    ADD CONSTRAINT "observations_program_slug_fkey" FOREIGN KEY ("program_slug") REFERENCES "public"."programs"("slug");



ALTER TABLE ONLY "public"."observations"
    ADD CONSTRAINT "observations_latest_deployment_fkey" FOREIGN KEY ("latest_deployment_id") REFERENCES "public"."deployments"("id");



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



ALTER TABLE ONLY "public"."objects"
    ADD CONSTRAINT "objects_last_inspected_by_fkey" FOREIGN KEY ("last_inspected_by") REFERENCES "auth"."users"("id");



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



GRANT ALL ON TABLE "public"."deployments" TO "anon";
GRANT ALL ON TABLE "public"."deployments" TO "authenticated";
GRANT ALL ON TABLE "public"."deployments" TO "service_role";



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



GRANT ALL ON TABLE "public"."object_lists" TO "anon";
GRANT ALL ON TABLE "public"."object_lists" TO "authenticated";
GRANT ALL ON TABLE "public"."object_lists" TO "service_role";



GRANT ALL ON TABLE "public"."object_list_members" TO "anon";
GRANT ALL ON TABLE "public"."object_list_members" TO "authenticated";
GRANT ALL ON TABLE "public"."object_list_members" TO "service_role";



GRANT ALL ON TABLE "public"."object_photometry" TO "anon";
GRANT ALL ON TABLE "public"."object_photometry" TO "authenticated";
GRANT ALL ON TABLE "public"."object_photometry" TO "service_role";



GRANT ALL ON TABLE "public"."list_audit_log" TO "anon";
GRANT ALL ON TABLE "public"."list_audit_log" TO "authenticated";
GRANT ALL ON TABLE "public"."list_audit_log" TO "service_role";



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



GRANT ALL ON SEQUENCE "public"."deployments_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."deployments_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."deployments_id_seq" TO "service_role";



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



GRANT ALL ON SEQUENCE "public"."object_lists_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."object_lists_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."object_lists_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."object_list_members_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."object_list_members_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."object_list_members_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."object_photometry_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."object_photometry_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."object_photometry_id_seq" TO "service_role";



GRANT ALL ON SEQUENCE "public"."list_audit_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."list_audit_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."list_audit_log_id_seq" TO "service_role";



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
