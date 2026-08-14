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



-- FitsGL tile-pyramid datasets (epic #337, Phase 3). One row per *scoped* dataset
-- deployed to the campfire-tiles bucket: a field's fiducial composite
-- (kind='field') or a single standalone tile (kind='tile') — NOT one per
-- nircam_images extension. Thin pointer table the map viewer reads; the pyramid
-- itself (per-band manifest.json + .fits.fz supertiles + fitsgl.json) lives under
-- `prefix` in R2. `bands`/`tiles` record what the dataset spans; `source_hashes`
-- maps each backing mosaic (tile→filter→sha256, from storage_objects.content_hash)
-- so a rebuild can skip byte-identical inputs. Deliberately carries NO
-- deploy_status/deployment_id: public visibility DERIVES from the backing
-- nircam_images (RLS in policies.sql), so a deploy against draft mosaics never
-- exposes a manifest.
CREATE TABLE IF NOT EXISTS "public"."fitsgl_datasets" (
    "prefix" "text" NOT NULL,
    "field" "text" NOT NULL,
    "kind" "text" NOT NULL CONSTRAINT "fitsgl_datasets_kind_check" CHECK (("kind" = ANY (ARRAY['field'::"text", 'tile'::"text"]))),
    "tile" "text",
    "tiles" "text"[] NOT NULL,
    "pixel_scale" "text" NOT NULL,
    "fitsgl_json_url" "text" NOT NULL,
    "bands" "text"[] NOT NULL,
    "source_hashes" "jsonb" NOT NULL,
    "is_default" boolean DEFAULT false NOT NULL,
    "schema_version" integer DEFAULT 1 NOT NULL,
    "deployed_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."fitsgl_datasets" OWNER TO "postgres";



CREATE TABLE IF NOT EXISTS "public"."spectra" (
    "id" integer NOT NULL,
    "grating" "text" NOT NULL,
    "fits_path" "text" NOT NULL,
    "signal_to_noise" double precision,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "target_id" "text" NOT NULL,
    "thumbnail_svg_fnu" "text",
    "thumbnail_svg_flambda" "text",
    "file_hash" "text",
    "file_size" bigint,
    "exposure_time" double precision,
    -- Provenance, carried verbatim from the FITS primary header. cfpipe_version
    -- (CMPFRVER) is the single pipeline-version string; reduced_at (CMPFRTIM)
    -- is the actual reduction time, distinct from the row's created_at/updated_at.
    "crds_context" "text",
    "jwst_version" "text",
    "cfpipe_version" "text",
    "date_obs" "text",
    "reduced_at" timestamp with time zone,
    -- Phase A: per-spectrum auto-fit and DQ (populated in Phase B by deploy pipeline; backfilled in Phase D)
    "redshift_auto" double precision,
    "dq_flags" integer NOT NULL DEFAULT 0,
    -- B1 (#217): deploy lifecycle. 'published' is the only status visible to
    -- non-admins; 'draft' (admin-only draft, written by B2) and 'revoked'
    -- (soft-deleted, recoverable) are hidden by the status predicate threaded
    -- through every reader. Default 'published' so existing rows and any deploy
    -- path that omits the column stay visible (fail-closed: forgetting to mark a
    -- row keeps it published; forgetting to *filter* a reader is the hazard the
    -- predicate guards). Mirrors storage_objects.status.
    "deploy_status" "text" NOT NULL DEFAULT 'published' CONSTRAINT "spectra_deploy_status_check" CHECK (("deploy_status" = ANY (ARRAY['draft'::"text", 'published'::"text", 'revoked'::"text"]))),
    -- Stable per-spectrum identifier derived from fits_path: strips the leading
    -- directory and the trailing "_spec.fits" suffix (e.g. ember_cosmos_p1_prism_clear_12345).
    -- Generated/stored so it stays in sync with fits_path with no application code path.
    "spectrum_id" "text" GENERATED ALWAYS AS (
      regexp_replace(
        regexp_replace("fits_path", '^.*/', ''),
        '_spec\.fits$', '', 'i'
      )
    ) STORED,
    -- Denormalized search blob for the spectra-list p_search path: target_id plus
    -- the spectrum_id derivation. A generated column may not reference another
    -- generated column (spectrum_id), so the fits_path regexp is mirrored here —
    -- keep in sync with the spectrum_id expression above. trgm-indexed.
    "search_text" "text" GENERATED ALWAYS AS (
      "target_id" || ' ' ||
      regexp_replace(
        regexp_replace(COALESCE("fits_path", ''), '^.*/', ''),
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
    "object_id" integer,
    -- B1 (#217): true iff this target has >= 1 published member spectrum.
    -- Object/target-derived readers (map markers, sed-plot, /api/targets/[id],
    -- tile-thumbnail) gate on this rather than on per-spectrum deploy_status,
    -- since they never join spectra. Recomputed from member spectra by
    -- recompute_target_aggregates / deploy; always true in B1 (all published),
    -- the wiring is what B2 needs. Default true = fail-closed visible.
    "has_published_spectrum" boolean NOT NULL DEFAULT true
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
    "is_active" boolean NOT NULL DEFAULT true,
    -- B1 (#217): true iff this object has >= 1 published member spectrum.
    -- Object-derived readers (filter/markers/lists/photometry) gate on this
    -- rather than per-spectrum deploy_status. Recomputed by reconcile alongside
    -- is_active; always true in B1, the wiring is what B2 needs. Default true =
    -- fail-closed visible. Distinct from is_active (reconciliation soft-delete).
    "has_published_spectrum" boolean NOT NULL DEFAULT true,
    -- Denormalized search blob: object_id + member target_ids + program_slugs +
    -- observations. Cross-table (member targets), so it cannot be a generated
    -- column; maintained by recompute_object_search_text() at the end of
    -- apply_object_reconciliation (and the legacy objects.py rebuild). Backs the
    -- trgm-indexed p_search path in get_filtered_objects_paginated / _object_ids.
    "search_text" "text"
);


ALTER TABLE "public"."objects" OWNER TO "postgres";


COMMENT ON TABLE "public"."objects" IS 'Unique sky positions cross-matched across programs. One object groups one or more targets observed within ~0.2 arcsec. Aggregate columns (n_targets, programs, max_snr, etc.) refreshed by reconcile_field_objects() at deploy time; redshift / redshift_quality / inspection state are user-editable and persist across reconciliation.';


COMMENT ON COLUMN "public"."objects"."redshift_auto" IS 'Phase A: per-object auto-fit redshift, computed post-reconciliation by compute_object_redshift_auto() from the best member spectrum under a grating-priority hierarchy (PRISM > G395M > G395H > G235M > G235H > G140M > G140H, tiebreak on exposure_time); when a grating auto-fit agrees with the PRISM anchor within |dz| <= 0.03, the best such grating redshift is adopted for its higher precision. Empty until Phase D migration.';


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



CREATE TABLE IF NOT EXISTS "public"."object_list_shares" (
    "id" integer NOT NULL,
    "list_id" integer NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'viewer'::"text" NOT NULL,
    "granted_by" "uuid",
    "granted_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "object_list_shares_role_check" CHECK (("role" = ANY (ARRAY['viewer'::"text", 'editor'::"text"])))
);


ALTER TABLE "public"."object_list_shares" OWNER TO "postgres";


COMMENT ON TABLE "public"."object_list_shares" IS 'Per-user sharing grants on object_lists (issue #450). A share gives the grantee visibility of the list regardless of its visibility setting; role=editor additionally allows adding/removing members (same scope as public_edit — list metadata stays owner-only). Owner manages shares; grantees can remove their own share (leave).';



CREATE TABLE IF NOT EXISTS "public"."observations" (
    "name" "text" NOT NULL,
    "program_slug" "text" NOT NULL,
    "jwst_program_id" integer NOT NULL,
    "field" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "latest_deployment_id" integer,
    "file_globs" "text"[] NOT NULL DEFAULT '{}',
    "gratings" "text"[] NOT NULL DEFAULT '{}',
    "data_subdir" "text",
    "pointings" "jsonb"
);


ALTER TABLE "public"."observations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."deployments" (
    "id" integer NOT NULL,
    -- A deployment is anchored to EITHER a NIRSpec observation OR a NIRCam field
    -- (epic #261, N1): exactly one is non-null. `observation` was NOT NULL before
    -- NIRCam deploy became first-class; it is now nullable so a field-scoped deploy
    -- can record the same provenance (deployed_by/at, cfpipe_version, crds_context,
    -- config_snapshot) and drive the same draft->published->revoked lifecycle. The
    -- observation FK still holds (NULL references nothing).
    "observation" "text",
    "field" "text",
    "deployed_by" "uuid",
    "deployed_at" timestamp with time zone DEFAULT "now"(),
    "cfpipe_version" "text",
    "jwst_version" "text",
    "crds_context" "text",
    "config_snapshot" "jsonb",
    "n_targets" integer,
    "n_spectra" integer,
    "n_new_targets" integer,
    "force_overwrite" boolean DEFAULT false,
    "source_ids_filter" integer[],
    "supabase_only" boolean DEFAULT false,
    "stuck_shutters" "jsonb",
    "reduced_at" timestamp with time zone,
    -- B2 (#218): deployment lifecycle. A deployment is the batch anchor for the
    -- draft -> published -> revoked flow. 'published' is the default so existing
    -- rows (and ordinary deploys) stay published; `deploy --in-prep` inserts with
    -- 'draft'. published_at/revoked_at stamp the transitions (set by the
    -- set_deployment_status RPC). The user-facing visibility gate lives on
    -- spectra.deploy_status; this column is the provenance/audit record.
    "status" "text" NOT NULL DEFAULT 'published' CONSTRAINT "deployments_status_check" CHECK (("status" = ANY (ARRAY['draft'::"text", 'published'::"text", 'revoked'::"text"]))),
    "published_at" timestamp with time zone,
    "revoked_at" timestamp with time zone,
    -- Exactly one scope: a NIRSpec observation or a NIRCam field (epic #261, N1).
    CONSTRAINT "deployments_scope_check" CHECK (("num_nonnulls"("observation", "field") = 1))
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


-- First-class NIRCam field registry (issue #303) — the missing third leg of the
-- cloud-as-source-of-truth config loop (programs / observations / fields),
-- symmetric with `observations`. Synced from fields.toml, scoped to fields that
-- already have deployed NIRCam data, so the cloud table reflects what is actually
-- reduced. The full fields.toml section is mirrored losslessly into `config`
-- (jsonb) so nothing — nested tile WCS, jhat, wcs_shift, epoch defs — is dropped;
-- the commonly-queried bits are also lifted into typed columns. `latest_deployment_id`
-- mirrors observations. `coverage_area_*` are deploy-computed from <field>_layout.json
-- (exact survey area = non-zero pixels of the stacked exposure map x pixel area);
-- `expmap_pixel_scale_arcsec` comes from the same file. sync-fields upserts only
-- config columns so it never clobbers them. Read by get_nircam_fields /
-- get_nircam_field_summary / getNircamExpmaps.
CREATE TABLE IF NOT EXISTS "public"."fields" (
    "name" "text" NOT NULL,
    "display_name" "text",
    "filters" "text"[] NOT NULL DEFAULT '{}',
    "tiles" "text"[] NOT NULL DEFAULT '{}',
    "fiducial_tiles" "text"[] NOT NULL DEFAULT '{}',
    "epochs" "text"[] NOT NULL DEFAULT '{}',
    "programs" "text"[] NOT NULL DEFAULT '{}',
    "jwst_program_ids" integer[] NOT NULL DEFAULT '{}',
    "file_globs" "text"[] NOT NULL DEFAULT '{}',
    "center_ra" double precision,
    "center_dec" double precision,
    "config" "jsonb",
    "coverage_area_arcmin2" double precision,
    "coverage_area_deg2" double precision,
    -- Pixel scale (arcsec/pix) of the field's exposure-map grid. Deploy-owned,
    -- from <field>_layout.json. One value per field: expmap builds a single
    -- shared auto-WCS across every filter in the invocation, so all of a
    -- field's expmap FITS are pixel-registered on the same grid. Unlike the
    -- mosaics (whose scale is a key axis in nircam_images) an expmap carries
    -- its scale only in CDELT1/2, so the web table has nothing else to read.
    "expmap_pixel_scale_arcsec" double precision,
    "latest_deployment_id" integer,
    "created_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "fields_pkey" PRIMARY KEY ("name")
);


ALTER TABLE "public"."fields" OWNER TO "postgres";


-- One logical mosaic per (field, tile, filter, pixel_scale, extension, epoch).
-- The `version` axis is retired (epic #261, N2 / D3): the pipeline emits a
-- single canonical mosaic name per slot and re-combine overwrites it in place.
-- `epoch` names an exposure subset (fields.toml [<field>.epochs.<name>]) built
-- into its own mosaic; `''` is the full-field mosaic. It is NOT NULL DEFAULT ''
-- (never NULL) so the unique constraint still dedups full-field mosaics —
-- Postgres treats NULLs as distinct, which would defeat the upsert.
CREATE TABLE IF NOT EXISTS "public"."nircam_images" (
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "tile" "text" NOT NULL,
    "filter" "text" NOT NULL,
    "pixel_scale" "text" NOT NULL,
    "extension" "text" NOT NULL,
    "epoch" "text" NOT NULL DEFAULT '',
    "file_path" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "file_size" bigint,
    -- Lifecycle (epic #261, N2): mosaics are public science with the same
    -- draft->published->revoked gate as spectra, flipped by set_deployment_status.
    -- `deployment_id` links the mosaic to its NIRCam field deployment (provenance +
    -- the batch the lifecycle transition acts on). `deploy_status` is the public
    -- visibility gate the RLS reads (published => everyone; draft/revoked => admin).
    "deploy_status" "text" NOT NULL DEFAULT 'published' CONSTRAINT "nircam_images_deploy_status_check" CHECK (("deploy_status" = ANY (ARRAY['draft'::"text", 'published'::"text", 'revoked'::"text"]))),
    "deployment_id" integer
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


CREATE TABLE IF NOT EXISTS "public"."nircam_exposures" (
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "filter" "text" NOT NULL,
    "detector" "text" NOT NULL,
    "filename" "text" NOT NULL,
    "visit" "text",
    "date_obs" timestamp without time zone,
    "ra_center" double precision,
    "dec_center" double precision,
    "stage" "text" NOT NULL DEFAULT 'uncal'::"text",
    "review_status" "text" NOT NULL DEFAULT 'pending'::"text",
    "correction" "text" NOT NULL DEFAULT 'none'::"text",
    "png_path" "text",
    "full_png_path" "text",
    "image_width" integer,
    "image_height" integer,
    "mask_regions" "jsonb",
    "notes" "text",
    "created_at" timestamp without time zone DEFAULT "now"(),
    "updated_at" timestamp without time zone DEFAULT "now"()
);


ALTER TABLE "public"."nircam_exposures" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."nircam_exposures_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."nircam_exposures_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."nircam_exposures_id_seq" OWNED BY "public"."nircam_exposures"."id";


-- NIRSpec rate-mask triage (P2, design §3.2). Detector-grain analogue of
-- nircam_exposures, keyed on (observation, exposure_root, detector). Admin-only
-- (RLS); deploy-populated (split-ownership upsert preserves web review state).
CREATE TABLE IF NOT EXISTS "public"."nirspec_rate_exposures" (
    "id" integer NOT NULL,
    "observation" "text" NOT NULL,
    "exposure_root" "text" NOT NULL,
    "detector" "text" NOT NULL,
    "filename" "text" NOT NULL,
    "grating" "text",
    "image_width" integer,
    "image_height" integer,
    "storage_key" "text",
    "stage" "text" NOT NULL DEFAULT 'rate'::"text",
    "review_status" "text" NOT NULL DEFAULT 'pending'::"text",
    "mask_regions" "jsonb",
    "notes" "text",
    "created_at" timestamp with time zone NOT NULL DEFAULT "now"(),
    "updated_at" timestamp with time zone NOT NULL DEFAULT "now"()
);


ALTER TABLE "public"."nirspec_rate_exposures" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."nirspec_rate_exposures_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."nirspec_rate_exposures_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."nirspec_rate_exposures_id_seq" OWNED BY "public"."nirspec_rate_exposures"."id";


-- spectrum_exposures: NIRSpec nods-renderer grid (NIRSpec review loop, P4, design
-- §2c/§4.2). One row per canonical per-source spectrum-exposure
-- (observation, exposure_root, nod, detector, source_id) — the render backbone the
-- web nods renderer groups as rows=(exp_group, nod) × cols=detector per source.
-- REVIVED + REVISED from the epic-#210-B2 dead scaffold: the old spectrum_id NOT
-- NULL FK to spectra was unsatisfiable for an intermediates-only deploy (which runs
-- BEFORE stage-3 combine makes any spectrum row), so it is dropped and the table is
-- re-keyed to observations.name (no FK). Deploy-populated (split-ownership upsert,
-- campfire.deploy.spectrum_grid) — exp_group comes from the pipeline's CFEXPGRP
-- header stamp. Admin-only: reduction intermediates, never user-facing science. The
-- physical object (key/hash/size/backend) lives in storage_objects (product_type
-- 'nirspec_spectrum_exposure'); this table owns render columns + reviewer lifecycle
-- state (review_status, notes — set by the web, not deploy).
CREATE TABLE IF NOT EXISTS "public"."spectrum_exposures" (
    "id" integer NOT NULL,
    "observation" "text" NOT NULL,
    "exposure_root" "text" NOT NULL,
    "nod" "text" NOT NULL,
    "detector" "text" NOT NULL,
    "source_id" integer NOT NULL,
    "exp_group" integer,
    "grating" "text",
    "filename" "text" NOT NULL,
    "storage_key" "text",
    "image_width" integer,
    "image_height" integer,
    "stage" "text" NOT NULL DEFAULT 'cal'::"text",
    "review_status" "text" NOT NULL DEFAULT 'pending'::"text",
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."spectrum_exposures" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."spectrum_exposures_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."spectrum_exposures_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."spectrum_exposures_id_seq" OWNED BY "public"."spectrum_exposures"."id";


COMMENT ON TABLE "public"."spectrum_exposures" IS 'NIRSpec nods-renderer grid (review loop P4). One row per canonical per-source spectrum-exposure (observation, exposure_root, nod, detector, source_id); deploy-populated, admin-only. exp_group is the pipeline CFEXPGRP grouping. Revived+revised from the epic-#210-B2 scaffold (dropped the spectra FK; re-keyed to observations.name).';

COMMENT ON COLUMN "public"."spectrum_exposures"."exposure_root" IS 'Pipeline 2-token root (e.g. jw07076020001_04101), with the exposure token broken out as `nod`. NB: DIFFERENT semantics from nirspec_rate_exposures.exposure_root (3 tokens, detector-stripped) — deliberate, matching observation.group_files and the nods grid key.';

COMMENT ON COLUMN "public"."spectrum_exposures"."exp_group" IS 'Pipeline-computed exposure-group id (subpixel-dither), read from the canonical FITS CFEXPGRP header card. One value per group, spanning nods+detectors; a render/grouping attribute, NOT part of the unique key. Nullable (files predating the P4 stamp).';


-- nirspec_source_review: the editable flag channel for the NIRSpec nods review
-- loop (P6, design §4.3). One row per (observation, exposure_root, source_id) — a
-- SOURCE-scoped grain, coarser than the spectrum_exposures render grid (which is
-- per nod×detector), so the two flag channels live here rather than duplicating
-- across every nod/detector row. Both channels are jsonb mirroring the local
-- reference/nirspec/<obs>/ TOMLs 1:1, so P7's pull serializes without transform:
--   stuck_shutters  = [1,2,3]     ordinal list   (mirrors stuck_closed_shutters.toml)
--   bkg_overrides   = {"3":[1]}   {nod: [nods]}  (mirrors nodded_background_overrides.toml;
--                                  keys/values are exposure-sequence numbers, not indices)
-- Admin-only (RLS); web-editable; NOT deployed to OSN (deployments.stuck_shutters is a
-- per-deploy snapshot, this is the live editable record). exposure_root is the 2-token
-- root (jw07076020001_04101), same semantics as spectrum_exposures.exposure_root.
CREATE TABLE IF NOT EXISTS "public"."nirspec_source_review" (
    "id" integer NOT NULL,
    "observation" "text" NOT NULL,
    "exposure_root" "text" NOT NULL,
    "source_id" integer NOT NULL,
    "stuck_shutters" "jsonb",
    "bkg_overrides" "jsonb",
    "notes" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."nirspec_source_review" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."nirspec_source_review_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."nirspec_source_review_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."nirspec_source_review_id_seq" OWNED BY "public"."nirspec_source_review"."id";


COMMENT ON TABLE "public"."nirspec_source_review" IS 'Editable flag channel for the NIRSpec nods review loop (P6). One row per (observation, exposure_root, source_id); admin-only, web-editable, NOT deployed. stuck_shutters/bkg_overrides jsonb mirror the reference/nirspec/<obs>/ TOMLs 1:1 for the P7 pull-back.';


-- storage_objects: the keystone shadow index of every object in cloud storage
-- (epic #210, F1). Deploy writes one row per uploaded object using canonical keys
-- from the campfire_layout contract; a backfill/reconcile pass covers historical
-- pointers and adopts bucket orphans. Additive + inert: nothing reads it as
-- authoritative until a coverage gate proves 100% of live pointers have rows.
CREATE TABLE IF NOT EXISTS "public"."storage_objects" (
    "id" bigint NOT NULL,
    "backend" "text" NOT NULL,
    "bucket" "text" NOT NULL,
    "storage_key" "text" NOT NULL,
    "content_hash" "text" NOT NULL,
    -- Secondary, science-only content digest (epic #261, N1 / D1). For products
    -- that re-save identical science with volatile bytes (a NIRCam canonical
    -- exposure re-written by the pipeline gets fresh header timestamps, so its
    -- whole-file sha256 changes even when SCI+DQ do not), deploy compares this
    -- stable sha256(SCI+DQ) to skip re-uploading unchanged exposures. content_hash
    -- stays the authoritative whole-file hash that download/copy verification
    -- checks; sci_dq_hash is only a change-detection key. NULL for products with
    -- no partial digest (everything except nircam_exposure today).
    "sci_dq_hash" "text",
    "size_bytes" bigint NOT NULL,
    -- Bytes as stored in the bucket for transport-compressed products (gzipped
    -- mosaic FITS, epic #261 / PR #383); NULL = stored verbatim. size_bytes
    -- stays the LOGICAL (uncompressed) size that client-side dedup and the
    -- stat fast-path compare against local plain files.
    "stored_size_bytes" bigint,
    "content_type" "text" NOT NULL,
    "product_type" "text" NOT NULL,
    "instrument" "text",
    "status" "text" NOT NULL DEFAULT 'active'::"text",
    "observation" "text",
    "field" "text",
    "filter" "text",
    "spectrum_id" "text",
    "exposure_ref" "text",
    "deployment_id" integer,
    "cfpipe_version" "text",
    "uploaded_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "storage_objects_backend_check" CHECK (("backend" = ANY (ARRAY['r2'::"text", 'osn'::"text"]))),
    CONSTRAINT "storage_objects_bucket_check" CHECK (("bucket" = ANY (ARRAY['data'::"text", 'tiles'::"text"]))),
    CONSTRAINT "storage_objects_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'superseded'::"text", 'revoked'::"text"]))),
    CONSTRAINT "storage_objects_instrument_check" CHECK (("instrument" IS NULL OR "instrument" = ANY (ARRAY['nirspec'::"text", 'nircam'::"text"]))),
    -- content_hash is scheme-prefixed: 'sha256:<hex>' is authoritative (deploy hashes
    -- the local file; spectra backfill reuses spectra.file_hash); 'etag:<hex>' is
    -- provisional from an S3 LIST/HEAD (no GET) for backfilled objects with no stored
    -- sha256, upgraded to sha256 by the A1 copy+verify pass (#215). No raw/bare hex.
    CONSTRAINT "storage_objects_content_hash_check" CHECK (("content_hash" ~ '^(sha256|etag):'::"text")),
    -- sci_dq_hash, when present, is always an authoritative sha256 (no provisional
    -- etag form — it is computed from the local FITS arrays, never a HEAD).
    CONSTRAINT "storage_objects_sci_dq_hash_check" CHECK (("sci_dq_hash" IS NULL OR "sci_dq_hash" ~ '^sha256:'::"text")),
    -- product_type tracks the campfire_layout PRODUCTS registry (every entry with a
    -- non-null bucket). A new cloud-backed product type requires a migration here.
    CONSTRAINT "storage_objects_product_type_check" CHECK (("product_type" = ANY (ARRAY[
        'nirspec_spec'::"text", 'spectrum_json'::"text", 'zfit'::"text",
        'nirspec_spectrum_exposure'::"text", 'nirspec_rate'::"text",
        'rgb'::"text", 'sed'::"text",
        'nircam_exposure'::"text", 'nircam_exposure_preview'::"text",
        'nircam_exposure_full'::"text", 'nircam_mosaic'::"text", 'nircam_rgb'::"text",
        'nircam_expmap'::"text", 'nircam_expmap_plot'::"text",
        'nircam_mosaic_thumbnail'::"text", 'nircam_mosaic_quicklook'::"text", 'nircam_layout'::"text",
        'tile'::"text", 'photometry_pz'::"text",
        'nirspec_manual_mask'::"text", 'nirspec_stuck_shutters'::"text",
        'nirspec_bkg_override'::"text", 'nircam_mask'::"text", 'nircam_astrom_cat'::"text",
        'nircam_bad_pixel'::"text", 'nircam_flat'::"text", 'nircam_wisp'::"text"
    ])))
);


ALTER TABLE "public"."storage_objects" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."storage_objects_id_seq"
    AS bigint
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."storage_objects_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."storage_objects_id_seq" OWNED BY "public"."storage_objects"."id";


COMMENT ON TABLE "public"."storage_objects" IS 'Shadow index of every object in cloud storage (epic #210, F1). One row per data-bucket object; written by deploy and a backfill/reconcile pass. Tiles are intentionally NOT indexed per-object (kept on R2, aggregated in map_layers.total_size_bytes); the budget RPC unions both. Additive and inert until a coverage gate proves it complete.';

COMMENT ON COLUMN "public"."storage_objects"."content_hash" IS 'Scheme-prefixed integrity token. ''sha256:<hex>'' is authoritative (deploy hashes the local file; spectra backfill reuses spectra.file_hash). ''etag:<hex>'' is provisional from an S3 LIST/HEAD (no GET) for backfilled objects with no stored sha256; the A1 copy+verify pass (#215) upgrades it to sha256.';

COMMENT ON COLUMN "public"."storage_objects"."filter" IS 'Typed, indexed scope column for per-filter NIRCam products (nircam_exposure/_preview/_full, nircam_mosaic, nircam_expmap), denormalized from the campfire_layout key''s <field>/<filter>/ segment by deploy/row_for_key. NULL for NIRSpec and field-level products (no filter concept). Mirrors observation/field/spectrum_id — lets the registry filter/aggregate by filter without parsing keys.';

COMMENT ON COLUMN "public"."storage_objects"."exposure_ref" IS 'Stable per-exposure reference for intermediate products (nircam rootname; nirspec (root,nod,detector,source) tuple). Backs the partial unique (product_type, exposure_ref) WHERE status=''active'' — one current object per product/exposure.';

COMMENT ON COLUMN "public"."storage_objects"."sci_dq_hash" IS 'Science-only sha256(SCI+DQ) change-detection digest (epic #261, N1). Lets deploy skip re-uploading a NIRCam canonical exposure whose science is unchanged even though its whole-file content_hash shifted (pipeline re-save bumps header timestamps). content_hash remains the authoritative whole-file integrity token; this is never used for download/copy verification. NULL for products without a partial digest.';

COMMENT ON COLUMN "public"."storage_objects"."status" IS 'active = current object; superseded = replaced by a newer hash (tombstone, GC-eligible later); revoked = un-published. Only active rows count toward the budget and the partial-unique constraint.';


-- deploy_events: append-only audit log for the intermediate-product lifecycle
-- (epic #210, B2/B3). One row per lifecycle action (upload/publish/revoke/
-- recover/supersede/delete). Mirrors download_log's shape. Written only by the
-- lifecycle RPCs (SECURITY DEFINER, service_role/admin) — never by a direct
-- client insert — so the action record is trustworthy. Admin-readable.
CREATE TABLE IF NOT EXISTS "public"."deploy_events" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "actor" "uuid",
    "action" "text" NOT NULL,
    "deployment_id" integer,
    "observation" "text",
    -- NIRCam field scope (audit B5, Phase 3). Mirrors deployments.field; first-
    -- class column (was buried in metadata->>'field'). NULL for NIRSpec events.
    "field" "text",
    "spectrum_id" "text",
    "object_id" integer,
    "status_from" "text",
    "status_to" "text",
    "affected_count" integer,
    "host" "text",
    "metadata" "jsonb",
    "occurred_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "deploy_events_action_check" CHECK (("action" = ANY (ARRAY['upload'::"text", 'publish'::"text", 'revoke'::"text", 'recover'::"text", 'supersede'::"text", 'delete'::"text"])))
);


ALTER TABLE "public"."deploy_events" OWNER TO "postgres";


COMMENT ON TABLE "public"."deploy_events" IS 'Append-only audit log for the intermediate-product lifecycle (epic #210, B2/B3). One row per action (upload/publish/revoke/recover/supersede/delete), written only by the lifecycle RPCs (SECURITY DEFINER). deployment_id is nullable (some events are not tied to a deployment). Admin-readable; never client-inserted.';


-- deploy_scope_state: optimistic concurrency for multi-reducer safety (epic #210,
-- B4). One row per deploy scope ((observation|field), key). A deploy reads the
-- scope version when it starts and compare-and-sets it at finalize via
-- claim_deploy_scope; a mismatch means another reducer deployed the same scope
-- concurrently, so the clobber is DETECTED (and surfaced) rather than silent.
-- Deliberately the simplest mechanism that satisfies the gate: no leases, no
-- heartbeats — concurrent same-scope deploys are rare.
CREATE TABLE IF NOT EXISTS "public"."deploy_scope_state" (
    "scope_type" "text" NOT NULL,
    "scope_key" "text" NOT NULL,
    "version" integer NOT NULL DEFAULT 0,
    "last_actor" "uuid",
    "last_deploy_at" timestamp with time zone,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "deploy_scope_state_scope_type_check" CHECK (("scope_type" = ANY (ARRAY['observation'::"text", 'field'::"text"])))
);


ALTER TABLE "public"."deploy_scope_state" OWNER TO "postgres";


COMMENT ON TABLE "public"."deploy_scope_state" IS 'Optimistic-concurrency version per deploy scope (epic #210, B4). claim_deploy_scope does the compare-and-set so concurrent same-scope deploys are detected, not silently clobbered. Admin/internal.';


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
    "can_inspect" boolean DEFAULT false,
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



-- Inspection-access requests. A signed-in user without can_inspect can ask an
-- admin to grant inspection privileges. Admins review these in the admin UI;
-- approving flips user_profiles.can_inspect. One open ('pending') row per user
-- is enforced by a partial unique index (see indexes.sql).
CREATE TABLE IF NOT EXISTS "public"."inspection_access_requests" (
    "id" integer NOT NULL,
    "user_id" "uuid" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "message" "text",
    "created_at" timestamp with time zone DEFAULT "now"(),
    "reviewed_at" timestamp with time zone,
    "reviewed_by" "uuid",
    CONSTRAINT "inspection_access_requests_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'approved'::"text", 'rejected'::"text"])))
);


ALTER TABLE "public"."inspection_access_requests" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."inspection_access_requests_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."inspection_access_requests_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."inspection_access_requests_id_seq" OWNED BY "public"."inspection_access_requests"."id";



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
    "aperture_name" "text" DEFAULT 'MSA'::"text" NOT NULL,
    "aperture_width_arcsec" double precision DEFAULT 0.22 NOT NULL,
    "aperture_height_arcsec" double precision DEFAULT 0.46 NOT NULL,
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



CREATE SEQUENCE IF NOT EXISTS "public"."object_list_shares_id_seq"
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."object_list_shares_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."object_list_shares_id_seq" OWNED BY "public"."object_list_shares"."id";



CREATE TABLE IF NOT EXISTS "public"."user_profiles" (
    "user_id" "uuid" NOT NULL,
    "username" "text" NOT NULL,
    "full_name" "text" NOT NULL,
    "created_at" timestamp without time zone DEFAULT "now"(),
    "is_group_account" boolean DEFAULT false,
    "can_comment" boolean DEFAULT true,
    "can_inspect" boolean DEFAULT false,
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

ALTER TABLE ONLY "public"."comments" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."comments_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."flag_audit_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."flag_audit_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."map_layers" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."map_layers_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."nircam_images" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nircam_images_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."nircam_exposures" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nircam_exposures_id_seq"'::"regclass");


ALTER TABLE ONLY "public"."nirspec_rate_exposures" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nirspec_rate_exposures_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."spectrum_exposures" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."spectrum_exposures_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."nirspec_source_review" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."nirspec_source_review_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."storage_objects" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."storage_objects_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."pending_invites" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."pending_invites_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."inspection_access_requests" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."inspection_access_requests_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."shutters" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."shutters_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."slit_regions" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."slit_regions_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."spectra" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."spectra_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."targets" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."targets_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."objects" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."objects_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."object_lists" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_lists_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."object_list_members" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_list_members_id_seq"'::"regclass");

ALTER TABLE ONLY "public"."object_photometry" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_photometry_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."list_audit_log" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."list_audit_log_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."object_list_shares" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."object_list_shares_id_seq"'::"regclass");



-- ---------------------------------------------------------------------------
-- Primary keys, unique constraints
-- ---------------------------------------------------------------------------

ALTER TABLE ONLY "public"."access_codes"
    ADD CONSTRAINT "access_codes_code_key" UNIQUE ("code");



ALTER TABLE ONLY "public"."access_codes"
    ADD CONSTRAINT "access_codes_pkey" PRIMARY KEY ("id");



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



ALTER TABLE ONLY "public"."fitsgl_datasets"
    ADD CONSTRAINT "fitsgl_datasets_pkey" PRIMARY KEY ("prefix");



ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_pkey" PRIMARY KEY ("id");



-- Single version-free uniqueness (epic #261, N2 / D3). The former redundant
-- twin `nircam_images_field_tile_filter_pixel_scale_version_extensi_key` is
-- dropped alongside the `version` axis. `epoch` ('' = full field) is part of the
-- key so a subset mosaic gets its own row instead of colliding with the
-- full-field mosaic of the same (field, tile, filter, pixel_scale, extension).
ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_unique" UNIQUE ("field", "tile", "filter", "pixel_scale", "extension", "epoch");


ALTER TABLE ONLY "public"."nircam_exposures"
    ADD CONSTRAINT "nircam_exposures_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."nircam_exposures"
    ADD CONSTRAINT "nircam_exposures_unique" UNIQUE ("field", "filter", "filename");

ALTER TABLE ONLY "public"."nirspec_rate_exposures"
    ADD CONSTRAINT "nirspec_rate_exposures_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."nirspec_rate_exposures"
    ADD CONSTRAINT "nirspec_rate_exposures_unique" UNIQUE ("observation", "exposure_root", "detector");

ALTER TABLE ONLY "public"."spectrum_exposures"
    ADD CONSTRAINT "spectrum_exposures_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."spectrum_exposures"
    ADD CONSTRAINT "spectrum_exposures_unique" UNIQUE ("observation", "exposure_root", "nod", "detector", "source_id");

ALTER TABLE ONLY "public"."nirspec_source_review"
    ADD CONSTRAINT "nirspec_source_review_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."nirspec_source_review"
    ADD CONSTRAINT "nirspec_source_review_unique" UNIQUE ("observation", "exposure_root", "source_id");

ALTER TABLE ONLY "public"."deploy_events"
    ADD CONSTRAINT "deploy_events_pkey" PRIMARY KEY ("id");

ALTER TABLE ONLY "public"."deploy_scope_state"
    ADD CONSTRAINT "deploy_scope_state_pkey" PRIMARY KEY ("scope_type", "scope_key");
-- (FK constraints for spectrum_exposures/deploy_events are added below, after
-- their referenced PKs (spectra_pkey, deployments_pkey) exist in the build order.)


ALTER TABLE ONLY "public"."storage_objects"
    ADD CONSTRAINT "storage_objects_pkey" PRIMARY KEY ("id");

-- A logical key can coexist across backends during the OSN cutover window, so
-- uniqueness is (backend, bucket, storage_key) — not storage_key alone.
ALTER TABLE ONLY "public"."storage_objects"
    ADD CONSTRAINT "storage_objects_backend_bucket_key_unique" UNIQUE ("backend", "bucket", "storage_key");



ALTER TABLE ONLY "public"."observations"
    ADD CONSTRAINT "observations_pkey" PRIMARY KEY ("name");



ALTER TABLE ONLY "public"."password_reset_log"
    ADD CONSTRAINT "password_reset_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."inspection_access_requests"
    ADD CONSTRAINT "inspection_access_requests_pkey" PRIMARY KEY ("id");



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

-- B2 (#218) cross-table FKs, placed after the referenced PKs (spectra_pkey above,
-- deployments_pkey earlier) so the declarative build order resolves them.
-- (spectrum_exposures no longer FKs to spectra — the review loop P4 revive re-keyed
-- it to observations.name so intermediates-only deploys, which precede stage-3
-- combine, can populate it before any spectrum row exists.)
-- deploy_events -> deployments.id, SET NULL: audit rows outlive the deployment
-- they reference (don't cascade-delete history), matching storage_objects.deployment_id.
ALTER TABLE ONLY "public"."deploy_events"
    ADD CONSTRAINT "deploy_events_deployment_id_fkey" FOREIGN KEY ("deployment_id") REFERENCES "public"."deployments"("id") ON DELETE SET NULL;



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



ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_list_id_user_id_key" UNIQUE ("list_id", "user_id");


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


-- observation and deployment_id reference PK columns, so they are real FKs.
-- spectrum_id / field / exposure_ref are typed, indexed scope columns (not FKs):
-- spectra.spectrum_id is GENERATED from fits_path and not uniquely constrained,
-- so an FK would over-constrain and force tight insert ordering — an index gives
-- the joinability without those hazards.
ALTER TABLE ONLY "public"."storage_objects"
    ADD CONSTRAINT "storage_objects_observation_fkey" FOREIGN KEY ("observation") REFERENCES "public"."observations"("name");

ALTER TABLE ONLY "public"."storage_objects"
    ADD CONSTRAINT "storage_objects_deployment_id_fkey" FOREIGN KEY ("deployment_id") REFERENCES "public"."deployments"("id") ON DELETE SET NULL;

ALTER TABLE ONLY "public"."nircam_images"
    ADD CONSTRAINT "nircam_images_deployment_id_fkey" FOREIGN KEY ("deployment_id") REFERENCES "public"."deployments"("id") ON DELETE SET NULL;



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



ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_list_id_fkey" FOREIGN KEY ("list_id") REFERENCES "public"."object_lists"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."object_list_shares"
    ADD CONSTRAINT "object_list_shares_granted_by_fkey" FOREIGN KEY ("granted_by") REFERENCES "auth"."users"("id");



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

ALTER TABLE ONLY "public"."fields"
    ADD CONSTRAINT "fields_latest_deployment_fkey" FOREIGN KEY ("latest_deployment_id") REFERENCES "public"."deployments"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."password_reset_log"
    ADD CONSTRAINT "password_reset_log_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."pending_invites"
    ADD CONSTRAINT "pending_invites_invited_by_fkey" FOREIGN KEY ("invited_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."inspection_access_requests"
    ADD CONSTRAINT "inspection_access_requests_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."inspection_access_requests"
    ADD CONSTRAINT "inspection_access_requests_reviewed_by_fkey" FOREIGN KEY ("reviewed_by") REFERENCES "auth"."users"("id");



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



GRANT ALL ON TABLE "public"."fitsgl_datasets" TO "anon";
GRANT ALL ON TABLE "public"."fitsgl_datasets" TO "authenticated";
GRANT ALL ON TABLE "public"."fitsgl_datasets" TO "service_role";



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



GRANT ALL ON TABLE "public"."object_list_shares" TO "anon";
GRANT ALL ON TABLE "public"."object_list_shares" TO "authenticated";
GRANT ALL ON TABLE "public"."object_list_shares" TO "service_role";



GRANT ALL ON TABLE "public"."observations" TO "anon";
GRANT ALL ON TABLE "public"."observations" TO "authenticated";
GRANT ALL ON TABLE "public"."observations" TO "service_role";



GRANT ALL ON TABLE "public"."programs" TO "anon";
GRANT ALL ON TABLE "public"."programs" TO "authenticated";
GRANT ALL ON TABLE "public"."programs" TO "service_role";


GRANT ALL ON TABLE "public"."fields" TO "anon";
GRANT ALL ON TABLE "public"."fields" TO "authenticated";
GRANT ALL ON TABLE "public"."fields" TO "service_role";



GRANT ALL ON TABLE "public"."nircam_images" TO "anon";
GRANT ALL ON TABLE "public"."nircam_images" TO "authenticated";
GRANT ALL ON TABLE "public"."nircam_images" TO "service_role";


GRANT ALL ON TABLE "public"."nircam_exposures" TO "authenticated";
GRANT ALL ON TABLE "public"."nircam_exposures" TO "service_role";


-- nirspec_rate_exposures is admin-only (RLS); not granted to anon. Mirrors nircam_exposures.
GRANT ALL ON TABLE "public"."nirspec_rate_exposures" TO "authenticated";
GRANT ALL ON TABLE "public"."nirspec_rate_exposures" TO "service_role";


-- spectrum_exposures is admin-only (RLS); not granted to anon. Mirrors nircam_exposures.
GRANT ALL ON TABLE "public"."spectrum_exposures" TO "authenticated";
GRANT ALL ON TABLE "public"."spectrum_exposures" TO "service_role";

-- nirspec_source_review is admin-only (RLS); not granted to anon.
GRANT ALL ON TABLE "public"."nirspec_source_review" TO "authenticated";
GRANT ALL ON TABLE "public"."nirspec_source_review" TO "service_role";


-- deploy_events is an admin/internal audit log (RLS admin-only); not granted to anon.
GRANT ALL ON TABLE "public"."deploy_events" TO "authenticated";
GRANT ALL ON TABLE "public"."deploy_events" TO "service_role";


-- deploy_scope_state is admin/internal concurrency state (RLS admin-only); not anon.
GRANT ALL ON TABLE "public"."deploy_scope_state" TO "authenticated";
GRANT ALL ON TABLE "public"."deploy_scope_state" TO "service_role";


-- storage_objects is an admin/internal registry (RLS admin-only); not granted to anon.
GRANT ALL ON TABLE "public"."storage_objects" TO "authenticated";
GRANT ALL ON TABLE "public"."storage_objects" TO "service_role";



GRANT ALL ON TABLE "public"."password_reset_log" TO "anon";
GRANT ALL ON TABLE "public"."password_reset_log" TO "authenticated";
GRANT ALL ON TABLE "public"."password_reset_log" TO "service_role";



GRANT ALL ON TABLE "public"."pending_invites" TO "anon";
GRANT ALL ON TABLE "public"."pending_invites" TO "authenticated";
GRANT ALL ON TABLE "public"."pending_invites" TO "service_role";



GRANT ALL ON TABLE "public"."inspection_access_requests" TO "anon";
GRANT ALL ON TABLE "public"."inspection_access_requests" TO "authenticated";
GRANT ALL ON TABLE "public"."inspection_access_requests" TO "service_role";



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

GRANT ALL ON SEQUENCE "public"."inspection_access_requests_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."inspection_access_requests_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."inspection_access_requests_id_seq" TO "service_role";



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


GRANT ALL ON SEQUENCE "public"."nircam_exposures_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nircam_exposures_id_seq" TO "service_role";


GRANT ALL ON SEQUENCE "public"."nirspec_rate_exposures_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nirspec_rate_exposures_id_seq" TO "service_role";


GRANT ALL ON SEQUENCE "public"."spectrum_exposures_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."spectrum_exposures_id_seq" TO "service_role";

GRANT ALL ON SEQUENCE "public"."nirspec_source_review_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."nirspec_source_review_id_seq" TO "service_role";


GRANT ALL ON SEQUENCE "public"."storage_objects_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."storage_objects_id_seq" TO "service_role";



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



GRANT ALL ON SEQUENCE "public"."object_list_shares_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."object_list_shares_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."object_list_shares_id_seq" TO "service_role";



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
