SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- P6b-1 (docs/design-data-releases-impl-plan.md §8 P6b): the frozen catalog
-- tables become full-row copies of the live tables (tag + every live column +
-- extras) so a release can be browsed through the same RPCs as live;
-- dr_targets, dr_spectrum_lines, dr_photometry_bands, dr_nircam_images,
-- dr_fields and dr_filter_options are added; the freeze, purge, export and
-- diff functions read the new shape.
--
-- Hand-assembled from the schema files (the declarative diff would ALTER the
-- four existing tables column by column; recreating them keeps the migration
-- readable and the tables identical to the schema files). All four are empty
-- in production: no release has been frozen (dr_releases is empty, 2026-10-15).

DROP TABLE "public"."dr_spectra";
DROP TABLE "public"."dr_objects";
DROP TABLE "public"."dr_line_fits";
DROP TABLE "public"."dr_photometry";

-- ---------------------------------------------------------------------------
-- Frozen catalog tables (P6b, docs/design-data-releases-impl-plan.md §8 P6b).
-- Every table is `tag` + the live table's columns IN THE LIVE ORDER, same
-- names and types (generated columns become plain columns holding the
-- generated value; auth.users references become bare uuids; no foreign
-- keys), then the frozen-only extras. dr_freeze_job writes them as
-- `INSERT INTO dr_x SELECT p_tag, x.*, <extras>`, and
-- supabase/tests/check_dr_parity.sql fails CI when a live column is added
-- without its copy. Values are copied verbatim ("the catalog as it stood"),
-- so the catalog RPCs can read a release through the cat_* source views
-- (P6b-2) exactly as they read live.
-- ---------------------------------------------------------------------------

-- dr_spectra: spectra.* of the published members at the freeze.
CREATE TABLE IF NOT EXISTS "public"."dr_spectra" (
    "tag" "text" NOT NULL,
    "id" integer NOT NULL,
    "grating" "text" NOT NULL,
    "fits_path" "text" NOT NULL,
    "signal_to_noise" double precision,
    "created_at" timestamp without time zone,
    "updated_at" timestamp with time zone NOT NULL,
    "target_id" "text" NOT NULL,
    "thumbnail_svg_fnu" "text",
    "thumbnail_svg_flambda" "text",
    "file_hash" "text",
    "science_hash" "text",
    "file_size" bigint,
    "exposure_time" double precision,
    "crds_context" "text",
    "jwst_version" "text",
    "cfpipe_version" "text",
    "date_obs" "text",
    "reduced_at" timestamp with time zone,
    "redshift_auto" double precision,
    "chi2_min" double precision,
    "confidence" double precision,
    "dq_flags" integer NOT NULL,
    "deploy_status" "text" NOT NULL,
    "program_slug" "text" NOT NULL,
    "observation" "text" NOT NULL,
    -- generated on the live table; the value is copied
    "spectrum_id" "text" NOT NULL,
    "search_text" "text",
    CONSTRAINT "dr_spectra_pkey" PRIMARY KEY ("tag", "id"),
    CONSTRAINT "dr_spectra_tag_spectrum_id_key" UNIQUE ("tag", "spectrum_id")
);

ALTER TABLE "public"."dr_spectra" OWNER TO "postgres";
-- same read pattern as the live table: the two SVG thumbnails go out of line
ALTER TABLE "public"."dr_spectra" SET (toast_tuple_target = 1024);

COMMENT ON TABLE "public"."dr_spectra" IS 'Frozen copy of spectra (every live column, verbatim) for the published members of a data release at the freeze. id is the live spectra.id at the time. Joins to dr_targets on (tag, target_id), like the live tables.';

-- dr_targets: targets.* of the targets with at least one frozen spectrum.
CREATE TABLE IF NOT EXISTS "public"."dr_targets" (
    "tag" "text" NOT NULL,
    "id" integer NOT NULL,
    "target_id" "text" NOT NULL,
    "field" "text" NOT NULL,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "redshift_auto" double precision,
    "redshift_quality" integer,
    "spectral_features" integer,
    "dq_flags" integer,
    "created_at" timestamp without time zone,
    "updated_at" timestamp without time zone,
    "redshift_inspected" numeric(10,6),
    "last_inspected_at" timestamp with time zone,
    "last_inspected_by" "uuid",
    "max_snr" double precision,
    -- generated on the live table; the value is copied
    "redshift" numeric(10,6),
    "max_exposure_time" double precision,
    "program_slug" "text" NOT NULL,
    "observation" "text" NOT NULL,
    "object_id" integer,
    "has_published_spectrum" boolean NOT NULL,
    CONSTRAINT "dr_targets_pkey" PRIMARY KEY ("tag", "id"),
    CONSTRAINT "dr_targets_tag_target_id_key" UNIQUE ("tag", "target_id")
);

ALTER TABLE "public"."dr_targets" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_targets" IS 'Frozen copy of targets (every live column, verbatim) for the targets of a data release''s frozen spectra. object_id is the live objects.id at the time (NULL or an unfrozen inactive object is possible, as live).';

-- dr_objects: objects.* of the active objects with at least one frozen
-- spectrum, plus the frozen-only extras the exports need.
CREATE TABLE IF NOT EXISTS "public"."dr_objects" (
    "tag" "text" NOT NULL,
    "id" integer NOT NULL,
    "object_id" "text" NOT NULL,
    "field" "text" NOT NULL,
    "ra" double precision NOT NULL,
    "dec" double precision NOT NULL,
    "created_at" timestamp with time zone,
    "n_targets" integer NOT NULL,
    "n_spectra" integer NOT NULL,
    "programs" "text"[] NOT NULL,
    "gratings" "text"[] NOT NULL,
    "observations" "text"[] NOT NULL,
    "max_snr" double precision,
    "max_exposure_time" double precision,
    "photo_z" double precision,
    "photo_z_err_lo" double precision,
    "photo_z_err_hi" double precision,
    "has_photometry" boolean NOT NULL,
    "updated_at" timestamp with time zone,
    "redshift_auto" double precision,
    "redshift_inspected" numeric(10,6),
    "redshift_quality" integer NOT NULL,
    "inspected_used_auto" boolean NOT NULL,
    -- generated on the live table; the value is copied
    "redshift" numeric(10,6),
    "last_inspected_at" timestamp with time zone,
    "last_inspected_by" "uuid",
    "last_data_change_at" timestamp with time zone,
    "staleness_reason" "text",
    "reviewed_spectrum_ids" integer[],
    "needs_review" boolean NOT NULL,
    "version" integer NOT NULL,
    "is_active" boolean NOT NULL,
    "has_published_spectrum" boolean NOT NULL,
    "search_text" "text",
    -- frozen-only: the programs of the published members (what the export
    -- partitions on; the live `programs` array can name a program whose only
    -- member of this object is a draft) and the published member whose auto
    -- fit anchors the compute_object_redshift_auto rule in the snapshot.
    "evidence_programs" "text"[] DEFAULT '{}'::"text"[] NOT NULL,
    "z_source_spectrum_id" integer,
    CONSTRAINT "dr_objects_pkey" PRIMARY KEY ("tag", "id"),
    CONSTRAINT "dr_objects_tag_object_id_key" UNIQUE ("tag", "object_id")
);

ALTER TABLE "public"."dr_objects" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_objects" IS 'Frozen copy of objects (every live column, verbatim) for the active objects of a data release with a published member at the freeze, plus evidence_programs and z_source_spectrum_id. id is the live objects.id at the time, object_id the public name.';

-- dr_line_fits: spectrum_line_fits.* of the frozen spectra, plus the
-- staleness flags evaluated in the same snapshot.
CREATE TABLE IF NOT EXISTS "public"."dr_line_fits" (
    "tag" "text" NOT NULL,
    "spectrum_id" integer NOT NULL,
    "target_id" "text" NOT NULL,
    "grating" "text" NOT NULL,
    "program_slug" "text" NOT NULL,
    "observation" "text" NOT NULL,
    "z_used" double precision NOT NULL,
    "z_source" "text" NOT NULL,
    "z_quality" integer NOT NULL,
    "object_id" "text",
    "object_version" integer,
    "z_fit" double precision,
    "z_fit_err" double precision,
    "dv" double precision,
    "dv_err" double precision,
    "sigma_v" double precision,
    "sigma_v_err" double precision,
    "kin_source" "text",
    "n_lines" integer NOT NULL,
    "n_detected" integer NOT NULL,
    "n_broad" integer NOT NULL,
    "chi2" double precision,
    "dof" integer,
    "lines" "jsonb" NOT NULL,
    "fit_version" "text" NOT NULL,
    "cfpipe_version" "text",
    "f_lsf" double precision,
    "spectrum_hash" "text",
    "spectrum_science_hash" "text",
    "fitted_at" timestamp with time zone,
    "created_at" timestamp with time zone NOT NULL,
    "updated_at" timestamp with time zone NOT NULL,
    -- frozen-only: the spectrum_line_fits_status predicates in the snapshot
    "stale_redshift" boolean DEFAULT false NOT NULL,
    "stale_spectrum" boolean DEFAULT false NOT NULL,
    CONSTRAINT "dr_line_fits_pkey" PRIMARY KEY ("tag", "spectrum_id")
);

ALTER TABLE "public"."dr_line_fits" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_line_fits" IS 'Frozen copy of spectrum_line_fits (every live column, verbatim) for the fits of a data release''s frozen spectra, with stale_redshift / stale_spectrum computed inside the freeze snapshot. spectrum_id = the live spectra.id at the freeze.';

-- dr_spectrum_lines: spectrum_lines.* of the frozen fits (the line filter's table).
CREATE TABLE IF NOT EXISTS "public"."dr_spectrum_lines" (
    "tag" "text" NOT NULL,
    "spectrum_id" integer NOT NULL,
    "line" "text" NOT NULL,
    "component" "text" NOT NULL,
    "wave_rest" double precision,
    "flux" double precision,
    "flux_err" double precision,
    "snr" double precision,
    "ew_rest" double precision,
    "ew_rest_err" double precision,
    "flags" integer NOT NULL,
    "blend_into" "text",
    CONSTRAINT "dr_spectrum_lines_pkey" PRIMARY KEY ("tag", "spectrum_id", "line")
);

ALTER TABLE "public"."dr_spectrum_lines" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_spectrum_lines" IS 'Frozen copy of spectrum_lines (the unnested line fits the catalog filters on) for the fits of a data release.';

-- dr_photometry: object_photometry.* of the frozen objects in the selected
-- photometry/<field>/<catalog> components.
CREATE TABLE IF NOT EXISTS "public"."dr_photometry" (
    "tag" "text" NOT NULL,
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
    "has_pz" boolean NOT NULL,
    "created_at" timestamp with time zone,
    "updated_at" timestamp with time zone,
    CONSTRAINT "dr_photometry_pkey" PRIMARY KEY ("tag", "id")
);

ALTER TABLE "public"."dr_photometry" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_photometry" IS 'Frozen copy of object_photometry (every live column, verbatim) for the frozen objects of a data release in its photometry/<field>/<catalog> components. Access scope is the frozen object''s, like live.';

-- dr_photometry_bands: object_photometry_bands.* of the frozen photometry rows
-- (the band filter's table; snr / mag are the generated values, copied).
CREATE TABLE IF NOT EXISTS "public"."dr_photometry_bands" (
    "tag" "text" NOT NULL,
    "photometry_id" integer NOT NULL,
    "band" "text" NOT NULL,
    "flux" double precision,
    "flux_err" double precision,
    "wav" double precision,
    "snr" double precision,
    "mag" double precision,
    CONSTRAINT "dr_photometry_bands_pkey" PRIMARY KEY ("tag", "photometry_id", "band")
);

ALTER TABLE "public"."dr_photometry_bands" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_photometry_bands" IS 'Frozen copy of object_photometry_bands (the unnested photometry the catalog filters on) for the frozen photometry rows of a data release.';

-- dr_nircam_images: nircam_images.* of the published mosaics of the release's
-- NIRCam field components (the files themselves are dr_manifest rows).
CREATE TABLE IF NOT EXISTS "public"."dr_nircam_images" (
    "tag" "text" NOT NULL,
    "id" integer NOT NULL,
    "field" "text" NOT NULL,
    "tile" "text" NOT NULL,
    "filter" "text" NOT NULL,
    "pixel_scale" "text" NOT NULL,
    "extension" "text" NOT NULL,
    "epoch" "text" NOT NULL,
    "file_path" "text" NOT NULL,
    "created_at" timestamp without time zone,
    "file_size" bigint,
    "deploy_status" "text" NOT NULL,
    "deployment_id" integer,
    CONSTRAINT "dr_nircam_images_pkey" PRIMARY KEY ("tag", "id")
);

ALTER TABLE "public"."dr_nircam_images" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_nircam_images" IS 'Frozen copy of nircam_images (every live column, verbatim) for the published mosaics of a data release''s NIRCam field components at the freeze.';

-- dr_fields: fields.* of the release's NIRCam components and of the fields its
-- frozen targets belong to (what the field pages read).
CREATE TABLE IF NOT EXISTS "public"."dr_fields" (
    "tag" "text" NOT NULL,
    "name" "text" NOT NULL,
    "display_name" "text",
    "filters" "text"[] NOT NULL,
    "tiles" "text"[] NOT NULL,
    "fiducial_tiles" "text"[] NOT NULL,
    "epochs" "text"[] NOT NULL,
    "programs" "text"[] NOT NULL,
    "jwst_program_ids" integer[] NOT NULL,
    "file_globs" "text"[] NOT NULL,
    "center_ra" double precision,
    "center_dec" double precision,
    "config" "jsonb",
    "coverage_area_arcmin2" double precision,
    "coverage_area_deg2" double precision,
    "expmap_pixel_scale_arcsec" double precision,
    "latest_deployment_id" integer,
    "created_at" timestamp with time zone,
    "config_hash" "text",
    "config_updated_at" timestamp with time zone,
    "retired_at" timestamp with time zone,
    CONSTRAINT "dr_fields_pkey" PRIMARY KEY ("tag", "name")
);

ALTER TABLE "public"."dr_fields" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_fields" IS 'Frozen copy of fields (every live column, verbatim) for a data release''s NIRCam field components and the fields of its frozen targets.';

-- dr_filter_options: the mv_filter_options row computed over the frozen rows
-- (the catalog's dropdowns in browsing mode).
CREATE TABLE IF NOT EXISTS "public"."dr_filter_options" (
    "tag" "text" NOT NULL,
    "id" integer NOT NULL,
    "fields" "text"[],
    "observations" "text"[],
    "gratings" "text"[],
    "photometry_bands" "text"[],
    CONSTRAINT "dr_filter_options_pkey" PRIMARY KEY ("tag")
);

ALTER TABLE "public"."dr_filter_options" OWNER TO "postgres";

COMMENT ON TABLE "public"."dr_filter_options" IS 'The mv_filter_options row (fields, observations, gratings, photometry bands) computed over a data release''s frozen rows at the freeze.';

REVOKE ALL ON TABLE "public"."dr_spectra" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_spectra" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_spectra" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_spectra" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_objects" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_objects" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_objects" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_objects" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_line_fits" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_line_fits" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_line_fits" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_line_fits" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_photometry" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_photometry" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_photometry" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_photometry" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_targets" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_targets" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_targets" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_targets" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_spectrum_lines" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_spectrum_lines" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_spectrum_lines" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_spectrum_lines" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_photometry_bands" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_photometry_bands" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_photometry_bands" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_photometry_bands" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_nircam_images" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_nircam_images" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_nircam_images" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_nircam_images" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_fields" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_fields" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_fields" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_fields" TO "service_role";
REVOKE ALL ON TABLE "public"."dr_filter_options" FROM "anon";
REVOKE ALL ON TABLE "public"."dr_filter_options" FROM "authenticated";
GRANT SELECT ON TABLE "public"."dr_filter_options" TO "authenticated";
GRANT ALL ON TABLE "public"."dr_filter_options" TO "service_role";

CREATE OR REPLACE FUNCTION public.dr_freeze_job(p_tag text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
-- A whole-catalog freeze is a few minutes on the Small instance (measured on
-- the production-shaped scale DB; see the P4a PR); never run it under the role
-- default.
SET statement_timeout = '900s'
SET lock_timeout = '30s'
AS $$
DECLARE
  r dr_releases%ROWTYPE;
  comp record;
  v_t0 timestamptz := clock_timestamp();
  v_n_spectra bigint; v_n_objects bigint; v_n_lines bigint; v_n_phot bigint; v_n_manifest bigint;
  v_n_targets bigint; v_n_slines bigint; v_n_bands bigint; v_n_nircam bigint; v_n_fields bigint;
  v_mismatch bigint;
  v_xmin bigint;
  v_missing text;
  v_err text;
BEGIN
  -- Only the cron runner / seed (postgres) or an admin / service caller.
  IF NOT (session_user = 'postgres' OR current_user = 'postgres'
          OR COALESCE(auth.role(), '') = 'service_role'
          OR (SELECT public.is_admin())) THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  -- The one-shot cron job is a `5 seconds` schedule (dr_enqueue_freeze): drop
  -- it FIRST so whatever this run does, the commit removes the job. A wrong
  -- state is reported and returns (never raised: a raise would roll the
  -- unschedule back and the job would fire again every 5 s).
  PERFORM dr_cron_unschedule(p_tag);
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN
    RAISE NOTICE 'dr_freeze_job: no release %', p_tag;
    RETURN;
  END IF;
  IF r.state <> 'freezing' THEN
    RAISE NOTICE 'dr_freeze_job: release % is in state % (expected freezing); nothing done', p_tag, r.state;
    RETURN;
  END IF;

  -- Interlock with begin_deploy_scope (which takes the same lock and refuses
  -- while a release is `freezing`): held for the whole transaction, so no
  -- deploy begins between here and the commit. A lease taken between the
  -- enqueue and this run (the pg_cron delay) fails the attempt cleanly.
  PERFORM pg_advisory_xact_lock(hashtext('campfire.dr_freeze'));

  BEGIN
    IF EXISTS (SELECT 1 FROM deploy_scope_state d
                WHERE d.in_progress_since IS NOT NULL
                  AND COALESCE(d.heartbeat_at, d.in_progress_since) >= now() - deploy_lease_ttl()) THEN
      RAISE EXCEPTION 'a deploy started after the freeze was enqueued (live deploy lease); re-run the freeze when it has finished';
    END IF;
    SELECT * INTO comp FROM dr_parse_components(r.components);

    -- Partial rows of a previous attempt under this tag.
    DELETE FROM dr_manifest         WHERE tag = p_tag;
    DELETE FROM dr_spectrum_lines   WHERE tag = p_tag;
    DELETE FROM dr_line_fits        WHERE tag = p_tag;
    DELETE FROM dr_photometry_bands WHERE tag = p_tag;
    DELETE FROM dr_photometry       WHERE tag = p_tag;
    DELETE FROM dr_spectra          WHERE tag = p_tag;
    DELETE FROM dr_targets          WHERE tag = p_tag;
    DELETE FROM dr_objects          WHERE tag = p_tag;
    DELETE FROM dr_nircam_images    WHERE tag = p_tag;
    DELETE FROM dr_fields           WHERE tag = p_tag;
    DELETE FROM dr_filter_options   WHERE tag = p_tag;
    DELETE FROM dr_exports          WHERE tag = p_tag;

    -- ONE statement: every sub-statement sees the same snapshot. Every
    -- frozen catalog table is `tag` + the live row verbatim (P6b), copied
    -- column BY NAME (never `x.*`: the physical column order of a live table
    -- is whatever its migrations left, and two same-typed columns swapped
    -- would copy silently); check_dr_parity.sql keeps the three lists -- the
    -- live table, the frozen table and these INSERTs -- in step.
    WITH
    pub AS MATERIALIZED (
      -- Published spectra (nirspec component) with the active object they
      -- roll up to (NULL while a target has no active object).
      SELECT s.id, s.spectrum_id, s.program_slug, s.observation, s.target_id,
             t.field AS target_field, o.id AS obj_id, s.file_hash, s.science_hash,
             s.exposure_time, s.redshift_auto,
             CASE s.grating
               WHEN 'PRISM' THEN 0 WHEN 'G395M' THEN 1 WHEN 'G395H' THEN 2
               WHEN 'G235M' THEN 3 WHEN 'G235H' THEN 4 WHEN 'G140M' THEN 5
               WHEN 'G140H' THEN 6 ELSE 7 END AS prio
      FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      LEFT JOIN objects o ON o.id = t.object_id AND o.is_active
      WHERE comp.nirspec AND s.deploy_status = 'published'
    ),
    ev AS (
      -- Evidence programs per object: the programs of its published members
      -- (what the exports partition on; the live `programs` array may name a
      -- program whose only member here is a draft).
      SELECT p.obj_id, array_agg(DISTINCT p.program_slug ORDER BY p.program_slug) AS programs
      FROM pub p WHERE p.obj_id IS NOT NULL GROUP BY p.obj_id
    ),
    -- Auto-redshift source: the compute_object_redshift_auto rule, set-based,
    -- for the export's z_source_spectrum_id (the row itself keeps the live
    -- redshift_auto verbatim; disagreements are counted, not corrected).
    m AS (
      SELECT p.obj_id, p.id, p.redshift_auto AS z, p.exposure_time, p.prio
      FROM pub p WHERE p.obj_id IS NOT NULL AND p.redshift_auto IS NOT NULL
    ),
    pz AS (
      SELECT DISTINCT ON (obj_id) obj_id, z AS prism_z FROM m WHERE prio = 0
      ORDER BY obj_id, exposure_time DESC NULLS LAST, id
    ),
    base AS (
      SELECT DISTINCT ON (obj_id) obj_id, id, z FROM m
      ORDER BY obj_id, prio, exposure_time DESC NULLS LAST, id
    ),
    refined AS (
      SELECT DISTINCT ON (m.obj_id) m.obj_id, m.id, m.z
      FROM m JOIN pz ON pz.obj_id = m.obj_id
      WHERE m.prio BETWEEN 1 AND 6 AND abs(m.z - pz.prism_z) / (1.0 + pz.prism_z) < 0.01
      ORDER BY m.obj_id, m.prio, m.exposure_time DESC NULLS LAST, m.id
    ),
    zsrc AS (
      SELECT b.obj_id, COALESCE(rf.id, b.id) AS id, COALESCE(rf.z, b.z) AS z
      FROM base b LEFT JOIN refined rf ON rf.obj_id = b.obj_id
    ),
    mism AS (
      SELECT count(*) AS n
      FROM objects o JOIN ev ON ev.obj_id = o.id LEFT JOIN zsrc ON zsrc.obj_id = o.id
      WHERE o.redshift_auto IS DISTINCT FROM zsrc.z
    ),
    ins_spectra AS (
      INSERT INTO dr_spectra (tag, id, grating, fits_path, signal_to_noise, created_at, updated_at, target_id, thumbnail_svg_fnu, thumbnail_svg_flambda, file_hash, science_hash, file_size, exposure_time, crds_context, jwst_version, cfpipe_version, date_obs, reduced_at, redshift_auto, chi2_min, confidence, dq_flags, deploy_status, program_slug, observation, spectrum_id, search_text)
      SELECT p_tag, s.id, s.grating, s.fits_path, s.signal_to_noise, s.created_at, s.updated_at, s.target_id, s.thumbnail_svg_fnu, s.thumbnail_svg_flambda, s.file_hash, s.science_hash, s.file_size, s.exposure_time, s.crds_context, s.jwst_version, s.cfpipe_version, s.date_obs, s.reduced_at, s.redshift_auto, s.chi2_min, s.confidence, s.dq_flags, s.deploy_status, s.program_slug, s.observation, s.spectrum_id, s.search_text
      FROM spectra s JOIN pub p ON p.id = s.id
      RETURNING 1
    ),
    ins_targets AS (
      INSERT INTO dr_targets (tag, id, target_id, field, ra, dec, redshift_auto, redshift_quality, spectral_features, dq_flags, created_at, updated_at, redshift_inspected, last_inspected_at, last_inspected_by, max_snr, redshift, max_exposure_time, program_slug, observation, object_id, has_published_spectrum)
      SELECT p_tag, t.id, t.target_id, t.field, t.ra, t.dec, t.redshift_auto, t.redshift_quality, t.spectral_features, t.dq_flags, t.created_at, t.updated_at, t.redshift_inspected, t.last_inspected_at, t.last_inspected_by, t.max_snr, t.redshift, t.max_exposure_time, t.program_slug, t.observation, t.object_id, t.has_published_spectrum
      FROM targets t
      WHERE EXISTS (SELECT 1 FROM pub p WHERE p.target_id = t.target_id)
      RETURNING 1
    ),
    ins_objects AS (
      -- the active objects with a published member; verbatim row + extras
      INSERT INTO dr_objects (tag, id, object_id, field, ra, dec, created_at, n_targets, n_spectra, programs, gratings, observations, max_snr, max_exposure_time, photo_z, photo_z_err_lo, photo_z_err_hi, has_photometry, updated_at, redshift_auto, redshift_inspected, redshift_quality, inspected_used_auto, redshift, last_inspected_at, last_inspected_by, last_data_change_at, staleness_reason, reviewed_spectrum_ids, needs_review, version, is_active, has_published_spectrum, search_text, evidence_programs, z_source_spectrum_id)
      SELECT p_tag, o.id, o.object_id, o.field, o.ra, o.dec, o.created_at, o.n_targets, o.n_spectra, o.programs, o.gratings, o.observations, o.max_snr, o.max_exposure_time, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi, o.has_photometry, o.updated_at, o.redshift_auto, o.redshift_inspected, o.redshift_quality, o.inspected_used_auto, o.redshift, o.last_inspected_at, o.last_inspected_by, o.last_data_change_at, o.staleness_reason, o.reviewed_spectrum_ids, o.needs_review, o.version, o.is_active, o.has_published_spectrum, o.search_text,
             ev.programs, zsrc.id
      FROM objects o
      JOIN ev ON ev.obj_id = o.id
      LEFT JOIN zsrc ON zsrc.obj_id = o.id
      RETURNING 1
    ),
    ins_lines AS (
      INSERT INTO dr_line_fits (tag, spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, z_quality, object_id, object_version, z_fit, z_fit_err, dv, dv_err, sigma_v, sigma_v_err, kin_source, n_lines, n_detected, n_broad, chi2, dof, lines, fit_version, cfpipe_version, f_lsf, spectrum_hash, spectrum_science_hash, fitted_at, created_at, updated_at, stale_redshift, stale_spectrum)
      SELECT p_tag, f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation, f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version, f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source, f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.lines, f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.spectrum_science_hash, f.fitted_at, f.created_at, f.updated_at,
             -- the spectrum_line_fits_status predicates, in this snapshot
             (o.id IS NOT NULL AND line_fit_stale_redshift(o.version, o.redshift_quality,
                 o.redshift::double precision, f.object_version, f.z_quality, f.z_used)),
             line_fit_stale_spectrum(p.file_hash, p.science_hash, f.spectrum_hash, f.spectrum_science_hash)
      FROM spectrum_line_fits f
      JOIN pub p ON p.id = f.spectrum_id
      LEFT JOIN objects o ON o.id = p.obj_id
      RETURNING 1
    ),
    ins_slines AS (
      INSERT INTO dr_spectrum_lines (tag, spectrum_id, line, component, wave_rest, flux, flux_err, snr, ew_rest, ew_rest_err, flags, blend_into)
      SELECT p_tag, l.spectrum_id, l.line, l.component, l.wave_rest, l.flux, l.flux_err, l.snr, l.ew_rest, l.ew_rest_err, l.flags, l.blend_into
      FROM spectrum_lines l JOIN pub p ON p.id = l.spectrum_id
      RETURNING 1
    ),
    phot AS MATERIALIZED (
      -- cross-match rows of the frozen objects in the SELECTED catalogs
      SELECT ph.id
      FROM object_photometry ph
      JOIN ev ON ev.obj_id = ph.object_id
      WHERE (ph.field || '/' || ph.catalog_name) = ANY (comp.photometry)
    ),
    ins_phot AS (
      INSERT INTO dr_photometry (tag, id, object_id, field, ra, dec, catalog_name, catalog_id, match_distance_arcsec, photometry, photo_z, photo_z_err_lo, photo_z_err_hi, has_pz, created_at, updated_at)
      SELECT p_tag, ph.id, ph.object_id, ph.field, ph.ra, ph.dec, ph.catalog_name, ph.catalog_id, ph.match_distance_arcsec, ph.photometry, ph.photo_z, ph.photo_z_err_lo, ph.photo_z_err_hi, ph.has_pz, ph.created_at, ph.updated_at
      FROM object_photometry ph JOIN phot ON phot.id = ph.id
      RETURNING 1
    ),
    ins_bands AS (
      INSERT INTO dr_photometry_bands (tag, photometry_id, band, flux, flux_err, wav, snr, mag)
      SELECT p_tag, b.photometry_id, b.band, b.flux, b.flux_err, b.wav, b.snr, b.mag
      FROM object_photometry_bands b JOIN phot ON phot.id = b.photometry_id
      RETURNING 1
    ),
    ins_nircam AS (
      INSERT INTO dr_nircam_images (tag, id, field, tile, filter, pixel_scale, extension, epoch, file_path, created_at, file_size, deploy_status, deployment_id)
      SELECT p_tag, ni.id, ni.field, ni.tile, ni.filter, ni.pixel_scale, ni.extension, ni.epoch, ni.file_path, ni.created_at, ni.file_size, ni.deploy_status, ni.deployment_id
      FROM nircam_images ni
      WHERE ni.deploy_status = 'published' AND ni.field = ANY (comp.fields)
      RETURNING 1
    ),
    ins_fields AS (
      -- the NIRCam components and the fields of the frozen targets
      INSERT INTO dr_fields (tag, name, display_name, filters, tiles, fiducial_tiles, epochs, programs, jwst_program_ids, file_globs, center_ra, center_dec, config, coverage_area_arcmin2, coverage_area_deg2, expmap_pixel_scale_arcsec, latest_deployment_id, created_at, config_hash, config_updated_at, retired_at)
      SELECT p_tag, f.name, f.display_name, f.filters, f.tiles, f.fiducial_tiles, f.epochs, f.programs, f.jwst_program_ids, f.file_globs, f.center_ra, f.center_dec, f.config, f.coverage_area_arcmin2, f.coverage_area_deg2, f.expmap_pixel_scale_arcsec, f.latest_deployment_id, f.created_at, f.config_hash, f.config_updated_at, f.retired_at
      FROM fields f
      WHERE f.name = ANY (comp.fields)
         OR EXISTS (SELECT 1 FROM pub p WHERE p.target_field = f.name)
      RETURNING 1
    ),
    ins_members AS (
      INSERT INTO dr_manifest (tag, storage_key, component, program_slug, field, product_type,
                              spectrum_id, backend, bucket, content_hash, science_hash, size_bytes,
                              registry_id)
      -- NIRSpec finals + the sidecars registered with a spectrum_id, joined
      -- FROM the published spectra.
      SELECT p_tag, so.storage_key, 'nirspec', p.program_slug, NULL, so.product_type,
             p.spectrum_id, so.backend, so.bucket, so.content_hash, so.science_hash,
             so.size_bytes, so.id
      FROM pub p
      JOIN storage_objects so ON so.spectrum_id = p.spectrum_id
      -- members are the OSN data-bucket rows (where every data product lives
      -- and what the byte pass archives): a legacy R2 twin of the same key is
      -- never picked by plan order (Codex on #599)
      WHERE so.root = 'live' AND so.status = 'active' AND so.backend = 'osn' AND so.bucket = 'data'
        AND so.product_type IN ('nirspec_spec', 'spectrum_json', 'spectrum_1d_json', 'zfit',
                                'nirspec_lines', 'nirspec_lines_json')
      UNION ALL
      -- The zfit / line-fit sidecars deploy registered WITHOUT a spectrum_id
      -- (every zfit, nirspec_lines and nirspec_lines_json row in production on
      -- 2026-10-12): the layout puts them next to the spectrum under the same
      -- base name (<base>_spec.fits -> <base>_zfit.json, _lines.fits,
      -- _lines.json), so derive the key from the spectrum's registry row.
      SELECT p_tag, so.storage_key, 'nirspec', c.program_slug, NULL, so.product_type,
             c.spectrum_id, so.backend, so.bucket, so.content_hash, so.science_hash,
             so.size_bytes, so.id
      FROM (
        SELECT p.spectrum_id, p.program_slug, p.observation,
               regexp_replace(spec.storage_key, '_spec\.fits$', '') || sfx AS key
        FROM pub p
        JOIN storage_objects spec ON spec.spectrum_id = p.spectrum_id
                                 AND spec.product_type = 'nirspec_spec'
                                 AND spec.root = 'live' AND spec.status = 'active'
                                 AND spec.backend = 'osn' AND spec.bucket = 'data'
        CROSS JOIN unnest(ARRAY['_zfit.json', '_lines.fits', '_lines.json']) AS sfx
      ) c
      JOIN storage_objects so ON so.storage_key = c.key
      WHERE so.spectrum_id IS NULL AND so.root = 'live' AND so.status = 'active'
        AND so.backend = 'osn' AND so.bucket = 'data'
        AND so.product_type IN ('zfit', 'nirspec_lines', 'nirspec_lines_json')
        AND so.observation IS NOT DISTINCT FROM c.observation
      UNION ALL
      -- Published mosaics of each NIRCam field component, joined FROM nircam_images.
      SELECT p_tag, so.storage_key, 'nircam/' || ni.field, NULL, ni.field, so.product_type,
             NULL, so.backend, so.bucket, so.content_hash, so.science_hash, so.size_bytes, so.id
      FROM nircam_images ni
      JOIN storage_objects so ON so.storage_key = ni.file_path
      WHERE ni.deploy_status = 'published' AND ni.field = ANY (comp.fields)
        AND so.root = 'live' AND so.status = 'active' AND so.backend = 'osn' AND so.bucket = 'data'
        -- the bytes of the PUBLISHED deployment: `campfire push --field` can
        -- register a replacement mosaic (deployment_id NULL) before the deploy
        -- that publishes it; that row must not be frozen under the old image
        -- (the post-freeze check then fails the freeze; Codex on #599). Plain
        -- equality: a NULL on either side (a legacy image row, an unattached
        -- push) proves nothing, so it is not frozen and the freeze fails
        -- loudly instead (production: no published image or live mosaic row
        -- has a NULL deployment, 2026-10-13).
        AND so.deployment_id = ni.deployment_id
      -- (P(z) sidecars are not members: photometry_pz is a key-only product the
      -- layout cannot archive; the frozen dr_photometry rows carry the catalog
      -- values, the sidecars stay live-only -- plan amendment D-P4-5.)
      ON CONFLICT (tag, storage_key) DO NOTHING
      RETURNING 1
    )
    SELECT (SELECT count(*) FROM ins_spectra), (SELECT count(*) FROM ins_objects),
           (SELECT count(*) FROM ins_lines), (SELECT count(*) FROM ins_phot),
           (SELECT count(*) FROM ins_members),
           (SELECT count(*) FROM ins_targets), (SELECT count(*) FROM ins_slines),
           (SELECT count(*) FROM ins_bands), (SELECT count(*) FROM ins_nircam),
           (SELECT count(*) FROM ins_fields), (SELECT n FROM mism),
           pg_snapshot_xmin(pg_current_snapshot())::text::bigint
      INTO v_n_spectra, v_n_objects, v_n_lines, v_n_phot, v_n_manifest,
           v_n_targets, v_n_slines, v_n_bands, v_n_nircam, v_n_fields, v_mismatch, v_xmin;

    -- The dropdown options of the release: the mv_filter_options query over
    -- the frozen rows (same transaction; reads the frozen tables only).
    INSERT INTO dr_filter_options (tag, id, fields, observations, gratings, photometry_bands)
    SELECT p_tag, 1,
      ARRAY(SELECT DISTINCT t.field FROM dr_targets t
            WHERE t.tag = p_tag AND t.has_published_spectrum ORDER BY t.field),
      ARRAY(SELECT DISTINCT t.observation FROM dr_targets t
            WHERE t.tag = p_tag AND t.observation IS NOT NULL AND t.has_published_spectrum ORDER BY t.observation),
      ARRAY(SELECT DISTINCT sp.grating FROM dr_spectra sp
            WHERE sp.tag = p_tag AND sp.deploy_status = 'published' ORDER BY sp.grating),
      ARRAY(SELECT b.band
            FROM dr_photometry_bands b
            JOIN dr_photometry ph ON ph.tag = b.tag AND ph.id = b.photometry_id
            JOIN dr_objects o ON o.tag = ph.tag AND o.id = ph.object_id
            WHERE b.tag = p_tag AND o.has_published_spectrum AND o.is_active
            GROUP BY b.band
            ORDER BY min(b.wav) NULLS LAST, b.band);

    -- Every published mosaic of a selected field must have become a member: a
    -- mosaic without an active registry row would otherwise be dropped with no
    -- trace (there is no frozen NIRCam table to recover it from). Raising here
    -- rolls the inserts back and records the release as failed (Codex on #599).
    SELECT string_agg(ni.file_path, ', ' ORDER BY ni.file_path) INTO v_missing
    FROM nircam_images ni
    WHERE ni.deploy_status = 'published' AND ni.field = ANY (comp.fields)
      AND NOT EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag AND m.storage_key = ni.file_path);
    IF v_missing IS NOT NULL THEN
      RAISE EXCEPTION 'published mosaics without an active registry row of their deployment (finish or redo the field deploy, or leave the field out): %',
        left(v_missing, 1500);
    END IF;
    -- Likewise every frozen spectrum needs its final FITS as a member (a
    -- --supabase-only deploy or registry drift would leave a catalogued
    -- spectrum the byte pass can never archive).
    SELECT string_agg(d.spectrum_id, ', ' ORDER BY d.spectrum_id) INTO v_missing
    FROM dr_spectra d
    WHERE d.tag = p_tag
      AND NOT EXISTS (SELECT 1 FROM dr_manifest m
                      WHERE m.tag = p_tag AND m.spectrum_id = d.spectrum_id AND m.product_type = 'nirspec_spec'
                        -- the SAME bytes the catalog row describes: a --supabase-only
                        -- redeploy leaves the old registry row active (Codex on #599)
                        AND (d.file_hash IS NULL OR m.content_hash = d.file_hash));
    IF v_missing IS NOT NULL THEN
      RAISE EXCEPTION 'published spectra without an active registered final of the same file hash (redeploy them): %',
        left(v_missing, 1500);
    END IF;
    -- ...the nirspec component must freeze at least one spectrum (everything
    -- still draft or revoked would list it with nothing behind it; Codex on #599)...
    IF comp.nirspec AND v_n_spectra = 0 THEN
      RAISE EXCEPTION 'the nirspec component froze no spectra (nothing is published)';
    END IF;
    -- ...and every selected photometry catalog must freeze at least one row: a
    -- catalog whose rows are all orphans or match no frozen object would be
    -- listed as a component with nothing behind it (Codex on #599)
    SELECT string_agg(c, ', ' ORDER BY c) INTO v_missing
    FROM unnest(comp.photometry) c
    WHERE NOT EXISTS (SELECT 1 FROM dr_photometry ph
                      WHERE ph.tag = p_tag AND (ph.field || '/' || ph.catalog_name) = c);
    IF v_missing IS NOT NULL THEN
      RAISE EXCEPTION 'photometry components matching no frozen object (leave them out, or cross-match first): %',
        left(v_missing, 1500);
    END IF;

    UPDATE dr_releases
       SET state = 'frozen',
           frozen_at = now(),
           -- The requester of THIS freeze (a refreeze by another admin is theirs).
           frozen_by = COALESCE(auth.uid(), r.freeze_requested_by, r.created_by),
           freeze_duration_ms = (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int,
           snapshot_xmin = v_xmin,
           counts = jsonb_build_object(
             'spectra', v_n_spectra, 'objects', v_n_objects, 'line_fits', v_n_lines,
             'photometry', v_n_phot, 'manifest', v_n_manifest,
             'targets', v_n_targets, 'spectrum_lines', v_n_slines, 'photometry_bands', v_n_bands,
             'nircam_images', v_n_nircam, 'fields', v_n_fields,
             -- objects whose live redshift_auto is not the in-snapshot anchor's
             -- value (informational; the row keeps the live value)
             'redshift_auto_mismatch', v_mismatch,
             -- per product type, so a shortfall (e.g. sidecars whose registry
             -- row did not match the key) shows at freeze time, not in the
             -- byte pass
             'manifest_by_type', (SELECT COALESCE(jsonb_object_agg(t.product_type, t.n), '{}'::jsonb)
                                 FROM (SELECT m.product_type, count(*) AS n FROM dr_manifest m
                                       WHERE m.tag = p_tag GROUP BY m.product_type) t),
             'nircam_fields', to_jsonb(comp.fields), 'photometry_components', to_jsonb(comp.photometry),
             -- per component (nirspec | nircam/<field> | photometry/...): file
             -- count and byte volume, the numbers the /releases pages show
             -- (P6a) without aggregating the manifest at read time
             'by_component', (SELECT COALESCE(jsonb_object_agg(t.component,
                                       jsonb_build_object('files', t.n, 'bytes', t.b)
                                       -- nircam/<field>: the field's deploy-time sky coverage
                                       || COALESCE((SELECT jsonb_build_object('area_arcmin2', f.coverage_area_arcmin2)
                                                    FROM fields f
                                                    WHERE t.component = 'nircam/' || f.name AND f.coverage_area_arcmin2 IS NOT NULL),
                                                   '{}'::jsonb)), '{}'::jsonb)
                              FROM (SELECT m.component, count(*) AS n, COALESCE(sum(m.size_bytes), 0) AS b
                                    FROM dr_manifest m WHERE m.tag = p_tag GROUP BY m.component) t),
             'nirspec_programs', (SELECT count(DISTINCT s.program_slug) FROM dr_spectra s WHERE s.tag = p_tag),
             'nirspec_fields', (SELECT count(DISTINCT t.field) FROM dr_spectra s
                                JOIN dr_targets t ON t.tag = s.tag AND t.target_id = s.target_id
                                WHERE s.tag = p_tag)),
           error = NULL,
           updated_at = now()
     WHERE tag = p_tag;

    -- Directly (log_deploy_event needs an auth uid; the cron runner has none).
    INSERT INTO deploy_events (actor, action, affected_count, metadata)
    VALUES (COALESCE(auth.uid(), r.freeze_requested_by, r.created_by), 'freeze', v_n_spectra::int,
            jsonb_build_object('dr_tag', p_tag, 'components', r.components,
                               'counts', jsonb_build_object('spectra', v_n_spectra, 'objects', v_n_objects,
                                 'line_fits', v_n_lines, 'photometry', v_n_phot, 'manifest', v_n_manifest),
                               'duration_ms', (extract(epoch FROM clock_timestamp() - v_t0) * 1000)::int));
  EXCEPTION WHEN query_canceled OR OTHERS THEN
    -- query_canceled is listed explicitly: OTHERS does not include it, and a
    -- statement_timeout must leave the row in `failed` (not `freezing`
    -- forever) so the next attempt is not refused as "already freezing".
    GET STACKED DIAGNOSTICS v_err = MESSAGE_TEXT;
    -- The block's writes are rolled back; record the failure on the row.
    UPDATE dr_releases
       SET state = 'failed', error = left(v_err, 2000), updated_at = now()
     WHERE tag = p_tag;
  END;

  PERFORM dr_cron_unschedule(p_tag);
END;
$$;
CREATE OR REPLACE FUNCTION public.dr_purge_release(p_tag text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
SET statement_timeout = '300s'
AS $$
DECLARE
  r dr_releases%ROWTYPE;
  n_members bigint; n_lines bigint; n_phot bigint; n_spectra bigint; n_objects bigint; n_exports bigint;
  n_targets bigint; n_slines bigint; n_bands bigint; n_nircam bigint; n_fields bigint;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no release %', p_tag;
  END IF;
  IF r.state IN ('published', 'withdrawn') THEN
    RAISE EXCEPTION 'release % is %; a published DR is never purged (withdraw hides it, bytes stay)', p_tag, r.state;
  END IF;
  IF r.state = 'freezing' AND r.freeze_started_at > now() - interval '1 hour' THEN
    RAISE EXCEPTION 'release % is freezing (started %); wait for it to finish or fail', p_tag, r.freeze_started_at;
  END IF;
  -- An export run still building a file would upload it after the prefix is
  -- deleted, and nothing would name it again. A builder claims under this
  -- same release-row lock, so once the purge commits no new claim is
  -- possible; a claim older than the 10-minute takeover window is dead.
  IF EXISTS (SELECT 1 FROM dr_exports x WHERE x.tag = p_tag AND x.state = 'building'
              AND x.claimed_at > now() - interval '10 minutes') THEN
    RAISE EXCEPTION 'release % is being exported (a build claim is live); wait for `campfire dr export` to finish', p_tag;
  END IF;
  -- likewise an archive run: it would upload archive objects no row names (P4c)
  IF r.archive_claim IS NOT NULL AND r.archive_claimed_at > now() - interval '10 minutes' THEN
    RAISE EXCEPTION 'release % is being archived (a claim is live); wait for `campfire dr archive` to finish', p_tag;
  END IF;
  PERFORM dr_cron_unschedule(p_tag);
  DELETE FROM dr_manifest         WHERE tag = p_tag; GET DIAGNOSTICS n_members = ROW_COUNT;
  DELETE FROM dr_spectrum_lines   WHERE tag = p_tag; GET DIAGNOSTICS n_slines = ROW_COUNT;
  DELETE FROM dr_line_fits        WHERE tag = p_tag; GET DIAGNOSTICS n_lines = ROW_COUNT;
  DELETE FROM dr_photometry_bands WHERE tag = p_tag; GET DIAGNOSTICS n_bands = ROW_COUNT;
  DELETE FROM dr_photometry       WHERE tag = p_tag; GET DIAGNOSTICS n_phot = ROW_COUNT;
  DELETE FROM dr_spectra          WHERE tag = p_tag; GET DIAGNOSTICS n_spectra = ROW_COUNT;
  DELETE FROM dr_targets          WHERE tag = p_tag; GET DIAGNOSTICS n_targets = ROW_COUNT;
  DELETE FROM dr_objects          WHERE tag = p_tag; GET DIAGNOSTICS n_objects = ROW_COUNT;
  DELETE FROM dr_nircam_images    WHERE tag = p_tag; GET DIAGNOSTICS n_nircam = ROW_COUNT;
  DELETE FROM dr_fields           WHERE tag = p_tag; GET DIAGNOSTICS n_fields = ROW_COUNT;
  DELETE FROM dr_filter_options   WHERE tag = p_tag;
  DELETE FROM dr_exports          WHERE tag = p_tag; GET DIAGNOSTICS n_exports = ROW_COUNT;
  DELETE FROM dr_releases         WHERE tag = p_tag;
  INSERT INTO deploy_events (actor, action, affected_count, metadata)
  VALUES (auth.uid(), 'delete', n_spectra::int,
          jsonb_build_object('dr_tag', p_tag, 'purge', true, 'state_before', r.state,
                             'rows', jsonb_build_object('spectra', n_spectra, 'objects', n_objects,
                               'line_fits', n_lines, 'photometry', n_phot, 'manifest', n_members,
                               'targets', n_targets, 'spectrum_lines', n_slines, 'photometry_bands', n_bands,
                               'nircam_images', n_nircam, 'fields', n_fields,
                               'exports', n_exports)));
  RETURN jsonb_build_object('tag', p_tag, 'state_before', r.state,
                            'deleted', jsonb_build_object('spectra', n_spectra, 'objects', n_objects,
                              'line_fits', n_lines, 'photometry', n_phot, 'manifest', n_members,
                              'targets', n_targets, 'spectrum_lines', n_slines, 'photometry_bands', n_bands,
                              'nircam_images', n_nircam, 'fields', n_fields,
                              'exports', n_exports));
END;
$$;
CREATE OR REPLACE FUNCTION public.dr_export_partitions(p_tag text)
RETURNS TABLE(stream text, partition text, storage_key text, n_rows bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
-- catalog-wide GROUP BYs over every frozen table: service_role would inherit
-- authenticator's 8 s (the sync-RPC trap), as dr_export_rows does
SET statement_timeout = '120s'
AS $$
BEGIN
  PERFORM dr_require_admin();
  RETURN QUERY
  WITH counts AS (
    -- objects and photometry partition on the frozen object's EVIDENCE
    -- programs (the programs of its published members); line fits on their
    -- frozen spectrum's program, not the fit's own denormalized copy
    SELECT 'objects'::text AS stream, p AS partition, count(*) AS n
    FROM dr_objects o, unnest(o.evidence_programs) AS p WHERE o.tag = p_tag GROUP BY p
    UNION ALL
    SELECT 'spectra', s.program_slug, count(*) FROM dr_spectra s WHERE s.tag = p_tag GROUP BY s.program_slug
    UNION ALL
    SELECT 'line_fits', s.program_slug, count(*)
    FROM dr_line_fits f JOIN dr_spectra s ON s.tag = f.tag AND s.id = f.spectrum_id
    WHERE f.tag = p_tag GROUP BY s.program_slug
    UNION ALL
    SELECT 'photometry', p, count(*)
    FROM dr_photometry ph
    JOIN dr_objects o ON o.tag = ph.tag AND o.id = ph.object_id, unnest(o.evidence_programs) AS p
    WHERE ph.tag = p_tag GROUP BY p
    UNION ALL
    SELECT 'manifest', COALESCE(m.program_slug, m.component), count(*)
    FROM dr_manifest m WHERE m.tag = p_tag GROUP BY COALESCE(m.program_slug, m.component)
  )
  SELECT c.stream, c.partition, dr_export_key(p_tag, c.stream, c.partition), c.n
  FROM counts c
  ORDER BY c.stream, c.partition;
END;
$$;
CREATE OR REPLACE FUNCTION public.dr_export_rows(
  p_tag text,
  p_stream text,
  p_partition text,
  p_after text DEFAULT NULL,
  p_limit integer DEFAULT 2000
)
RETURNS TABLE(next_cursor text, row_json jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
SET statement_timeout = '120s'
AS $$
BEGIN
  PERFORM dr_require_admin();
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 10000 THEN
    RAISE EXCEPTION 'p_limit must be between 1 and 10000';
  END IF;

  IF p_stream = 'objects' THEN
    RETURN QUERY
    SELECT o.object_id,
           jsonb_build_object(
             'id', o.id, 'object_id', o.object_id, 'field', o.field, 'ra', o.ra, 'dec', o.dec,
             -- program membership scoped to THIS partition: the file is handed
             -- to any holder of p_partition, who must not learn which other
             -- (possibly proprietary) programs share the object (Codex on #600)
             'programs', ARRAY[p_partition], 'evidence_programs', ARRAY[p_partition],
             -- aggregates scoped to this partition's program (the live scoped
             -- sync shape for a caller holding that one program)
             'observations', COALESCE(agg.observations, '{}'::text[]),
             'gratings', COALESCE(agg.gratings, '{}'::text[]),
             'n_targets', COALESCE(agg.n_targets, 0), 'n_spectra', COALESCE(agg.n_spectra, 0),
             'max_snr', agg.max_snr, 'max_exposure_time', agg.max_exposure_time,
             'photo_z', o.photo_z, 'photo_z_err_lo', o.photo_z_err_lo, 'photo_z_err_hi', o.photo_z_err_hi,
             'has_photometry', o.has_photometry,
             -- the anchor spectrum's id only when it is one of this program's
             'z_source_spectrum_id', CASE WHEN EXISTS (
                 -- the anchor is one of the object's spectra by construction;
                 -- the object join goes through dr_targets, as live
                 SELECT 1 FROM dr_spectra zs
                 JOIN dr_targets zt ON zt.tag = zs.tag AND zt.target_id = zs.target_id
                 WHERE zs.tag = o.tag AND zt.object_id = o.id
                   AND zs.id = o.z_source_spectrum_id AND zs.program_slug = p_partition) THEN o.z_source_spectrum_id END,
             'redshift_auto', o.redshift_auto, 'redshift_inspected', o.redshift_inspected,
             'redshift', o.redshift, 'redshift_quality', o.redshift_quality,
             'inspected_used_auto', o.inspected_used_auto,
             'last_inspected_at', o.last_inspected_at, 'last_inspected_by', o.last_inspected_by,
             'version', o.version, 'is_active', o.is_active,
             'needs_review', o.needs_review,
             -- only this program's spectra among the signed-off set
             -- (a NULL set is passed through as a JSON null)
             'reviewed_spectrum_ids', CASE
               WHEN o.reviewed_spectrum_ids IS NOT NULL THEN (
                 SELECT COALESCE(jsonb_agg(to_jsonb(v) ORDER BY v::text), '[]'::jsonb)
                 FROM unnest(o.reviewed_spectrum_ids) v
                 WHERE EXISTS (SELECT 1 FROM dr_spectra rs
                               JOIN dr_targets rt ON rt.tag = rs.tag AND rt.target_id = rs.target_id
                               WHERE rs.tag = o.tag AND rt.object_id = o.id AND rs.program_slug = p_partition
                                 AND rs.id = v))
               ELSE NULL::jsonb END)
    FROM dr_objects o
    LEFT JOIN LATERAL (
      SELECT count(*) AS n_spectra, count(DISTINCT s.target_id) AS n_targets,
             array_agg(DISTINCT s.grating ORDER BY s.grating) AS gratings,
             array_agg(DISTINCT s.observation ORDER BY s.observation) AS observations,
             max(s.signal_to_noise) AS max_snr, max(s.exposure_time) AS max_exposure_time
      FROM dr_spectra s
      JOIN dr_targets t ON t.tag = s.tag AND t.target_id = s.target_id
      WHERE s.tag = o.tag AND t.object_id = o.id AND s.program_slug = p_partition
    ) agg ON true
    WHERE o.tag = p_tag AND p_partition = ANY (o.evidence_programs)
      AND (p_after IS NULL OR o.object_id > p_after)
    ORDER BY o.object_id
    LIMIT p_limit;

  ELSIF p_stream = 'spectra' THEN
    RETURN QUERY
    SELECT s.spectrum_id,
           jsonb_build_object(
             'id', s.id, 'spectrum_id', s.spectrum_id, 'target_id', s.target_id,
             'object_id', o.object_id, 'object_db_id', o.id,
             'grating', s.grating, 'fits_path', s.fits_path,
             'file_hash', s.file_hash, 'science_hash', s.science_hash, 'file_size', s.file_size,
             'signal_to_noise', s.signal_to_noise, 'exposure_time', s.exposure_time,
             'cfpipe_version', s.cfpipe_version, 'crds_context', s.crds_context,
             'jwst_version', s.jwst_version, 'date_obs', s.date_obs, 'reduced_at', s.reduced_at,
             'redshift_auto', s.redshift_auto, 'chi2_min', s.chi2_min, 'confidence', s.confidence,
             'dq_flags', s.dq_flags, 'program_slug', s.program_slug, 'observation', s.observation,
             -- the frozen target's field: an objectless spectrum has no object row
             'field', COALESCE(t.field, o.field))
    FROM dr_spectra s
    LEFT JOIN dr_targets t ON t.tag = s.tag AND t.target_id = s.target_id
    LEFT JOIN dr_objects o ON o.tag = s.tag AND o.id = t.object_id
    WHERE s.tag = p_tag AND s.program_slug = p_partition
      AND (p_after IS NULL OR s.spectrum_id > p_after)
    ORDER BY s.spectrum_id
    LIMIT p_limit;

  ELSIF p_stream = 'line_fits' THEN
    RETURN QUERY
    SELECT f.spectrum_id::text,
           jsonb_build_object(
             'spectrum_id', f.spectrum_id, 'spectrum_name', s.spectrum_id,
             -- target / grating / program / observation of the FROZEN spectrum,
             -- not the fit's denormalized copies (stale after a correction)
             'target_id', s.target_id, 'grating', s.grating,
             'program_slug', s.program_slug, 'observation', s.observation,
             'object_id', f.object_id, 'object_version', f.object_version,
             'z_used', f.z_used, 'z_source', f.z_source, 'z_quality', f.z_quality,
             'z_fit', f.z_fit, 'z_fit_err', f.z_fit_err, 'dv', f.dv, 'dv_err', f.dv_err,
             'sigma_v', f.sigma_v, 'sigma_v_err', f.sigma_v_err, 'kin_source', f.kin_source,
             'n_lines', f.n_lines, 'n_detected', f.n_detected, 'n_broad', f.n_broad,
             'chi2', f.chi2, 'dof', f.dof, 'lines', f.lines, 'fit_version', f.fit_version,
             'cfpipe_version', f.cfpipe_version, 'f_lsf', f.f_lsf,
             'spectrum_hash', f.spectrum_hash, 'spectrum_science_hash', f.spectrum_science_hash,
             'fitted_at', f.fitted_at, 'stale_redshift', f.stale_redshift, 'stale_spectrum', f.stale_spectrum)
    FROM dr_line_fits f
    JOIN dr_spectra s ON s.tag = f.tag AND s.id = f.spectrum_id
    WHERE f.tag = p_tag AND s.program_slug = p_partition
      AND (p_after IS NULL OR f.spectrum_id > p_after::integer)
    ORDER BY f.spectrum_id
    LIMIT p_limit;

  ELSIF p_stream = 'photometry' THEN
    RETURN QUERY
    SELECT ph.id::text,
           jsonb_build_object(
             -- scoped to this partition, as in the objects stream: the file
             -- must not reveal the object's other programs (Codex on #600)
             'id', ph.id, 'object_id', ph.object_id, 'programs', ARRAY[p_partition],
             'field', ph.field, 'catalog_name', ph.catalog_name, 'catalog_id', ph.catalog_id,
             'ra', ph.ra, 'dec', ph.dec, 'match_distance_arcsec', ph.match_distance_arcsec,
             'photometry', ph.photometry, 'photo_z', ph.photo_z,
             'photo_z_err_lo', ph.photo_z_err_lo, 'photo_z_err_hi', ph.photo_z_err_hi, 'has_pz', ph.has_pz)
    FROM dr_photometry ph
    JOIN dr_objects o ON o.tag = ph.tag AND o.id = ph.object_id
    WHERE ph.tag = p_tag AND p_partition = ANY (o.evidence_programs)
      AND (p_after IS NULL OR ph.id > p_after::integer)
    ORDER BY ph.id
    LIMIT p_limit;

  ELSIF p_stream = 'manifest' THEN
    RETURN QUERY
    SELECT m.storage_key,
           jsonb_build_object(
             'storage_key', m.storage_key, 'component', m.component,
             'program_slug', m.program_slug, 'field', m.field, 'product_type', m.product_type,
             'spectrum_id', m.spectrum_id, 'backend', m.backend, 'bucket', m.bucket,
             'content_hash', m.content_hash, 'science_hash', m.science_hash,
             'size_bytes', m.size_bytes, 'archive_key', m.archive_key,
             'byte_state', m.byte_state, 'stamped_hash', m.stamped_hash)
    FROM dr_manifest m
    WHERE m.tag = p_tag AND COALESCE(m.program_slug, m.component) = p_partition
      AND (p_after IS NULL OR m.storage_key > p_after)
    ORDER BY m.storage_key
    LIMIT p_limit;

  ELSE
    RAISE EXCEPTION 'unknown export stream % (objects, spectra, line_fits, photometry, manifest)', p_stream;
  END IF;
END;
$$;
CREATE OR REPLACE FUNCTION public.dr_export_begin(p_tag text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
-- seeds the ledger through dr_export_partitions (catalog-wide)
SET statement_timeout = '120s'
AS $$
DECLARE
  r dr_releases%ROWTYPE;
  n_new int; n_pending int; n_total int;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF r.state NOT IN ('frozen', 'exporting', 'exported', 'copying', 'ready') THEN
    RAISE EXCEPTION 'release % is % (exports need a frozen, unpublished release)', p_tag, r.state;
  END IF;
  -- Partitions are named by program slug or by component; a program slugged
  -- exactly like one of the release's field-level components would share the
  -- open component file with its private rows (program slugs carry no format
  -- constraint). Refuse such a release rather than guess (Codex on #600).
  -- Complete as written: dr_line_fits and dr_photometry take their program
  -- slugs from these spectra and objects, so they cannot add a new one.
  IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(r.components) c
             WHERE c.value LIKE '%/%'
               AND (EXISTS (SELECT 1 FROM dr_spectra s WHERE s.tag = p_tag AND s.program_slug = c.value)
                    OR EXISTS (SELECT 1 FROM dr_objects o WHERE o.tag = p_tag AND c.value = ANY (o.evidence_programs))
                    OR EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag AND m.program_slug = c.value))) THEN
    RAISE EXCEPTION 'release % has a program slug equal to one of its component names; rename the program', p_tag;
  END IF;
  INSERT INTO dr_exports (tag, stream, partition, storage_key, state)
  SELECT p_tag, p.stream, p.partition, p.storage_key, 'pending'
  FROM dr_export_partitions(p_tag) p
  ON CONFLICT (tag, stream, partition) DO NOTHING;
  GET DIAGNOSTICS n_new = ROW_COUNT;
  -- An empty release would seed an empty ledger and never become ready
  -- (dr_derive_state needs ledger rows), while the builder reported nothing
  -- remaining: refuse it outright (Codex on #600).
  IF NOT EXISTS (SELECT 1 FROM dr_exports e WHERE e.tag = p_tag) THEN
    RAISE EXCEPTION 'release % has nothing to export (the freeze captured no rows); purge it and freeze again', p_tag;
  END IF;
  SELECT count(*) FILTER (WHERE state <> 'ready'), count(*) INTO n_pending, n_total
  FROM dr_exports WHERE tag = p_tag;
  RETURN jsonb_build_object('tag', p_tag, 'new', n_new, 'pending', n_pending, 'total', n_total,
                            'state', dr_derive_state(p_tag));
END;
$$;
CREATE OR REPLACE FUNCTION public.dr_diff(p_tag text, p_limit integer DEFAULT 20)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
SET statement_timeout = '120s'
AS $$
DECLARE
  r dr_releases%ROWTYPE;
  v_limit int := LEAST(GREATEST(COALESCE(p_limit, 20), 0), 500);
  v_out jsonb;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF r.state IN ('draft', 'freezing', 'failed') THEN
    RAISE EXCEPTION 'release % is % (nothing frozen to compare)', p_tag, r.state;
  END IF;

  WITH
  sp AS (
    SELECT d.spectrum_id, d.program_slug, d.observation,
           CASE
             WHEN s.id IS NULL THEN 'spectrum_gone'
             WHEN s.deploy_status IS DISTINCT FROM 'published' THEN 'spectrum_unpublished'
             WHEN d.science_hash IS NOT NULL AND s.science_hash IS NOT NULL
                  AND d.science_hash <> s.science_hash THEN 'spectrum_science_changed'
             WHEN (d.science_hash IS NULL OR s.science_hash IS NULL)
                  AND d.file_hash IS DISTINCT FROM s.file_hash THEN 'spectrum_science_changed'
           END AS kind,
           -- the hashes the comparison above used: the file hash when either
           -- side has no science hash (Codex on #602)
           CASE WHEN d.science_hash IS NOT NULL AND s.science_hash IS NOT NULL
                THEN jsonb_build_object('frozen', d.science_hash, 'live', s.science_hash,
                                        'live_status', s.deploy_status)
                ELSE jsonb_build_object('frozen', d.file_hash, 'live', s.file_hash,
                                        'compared', 'file_hash', 'live_status', s.deploy_status)
           END AS detail
    FROM dr_spectra d
    LEFT JOIN spectra s ON s.spectrum_id = d.spectrum_id
    WHERE d.tag = p_tag
  ),
  sp_new AS (
    SELECT s.spectrum_id, s.program_slug, s.observation, 'spectrum_new'::text AS kind,
           jsonb_build_object('live', s.science_hash) AS detail
    FROM spectra s
    WHERE s.deploy_status = 'published'
      AND (r.components ? 'nirspec')
      AND NOT EXISTS (SELECT 1 FROM dr_spectra d WHERE d.tag = p_tag AND d.spectrum_id = s.spectrum_id)
  ),
  ob AS (
    -- d.id is the live objects.id at the freeze; d.object_id the frozen name
    SELECT d.id AS object_id, d.object_id AS name, k.kind, k.detail
    FROM dr_objects d
    LEFT JOIN objects o ON o.id = d.id
    CROSS JOIN LATERAL (VALUES
      (CASE WHEN o.id IS NULL OR NOT o.is_active THEN 'object_gone' END,
       jsonb_build_object('live_active', o.is_active)),
      (CASE WHEN o.is_active AND o.object_id IS DISTINCT FROM d.object_id THEN 'object_renamed' END,
       jsonb_build_object('frozen', d.object_id, 'live', o.object_id)),
      (CASE WHEN o.is_active AND (o.redshift IS NULL) <> (d.redshift IS NULL)
             OR o.is_active AND abs(o.redshift - d.redshift) > 1e-6 THEN 'object_redshift_changed' END,
       jsonb_build_object('frozen', d.redshift, 'live', o.redshift)),
      (CASE WHEN o.is_active AND o.redshift_quality IS DISTINCT FROM d.redshift_quality THEN 'object_quality_changed' END,
       jsonb_build_object('frozen', d.redshift_quality, 'live', o.redshift_quality)),
      (CASE WHEN o.is_active AND COALESCE(o.needs_review, false)
                 AND NOT d.needs_review THEN 'review_raised' END,
       jsonb_build_object('live_reviewed', o.reviewed_spectrum_ids)),
      (CASE WHEN o.is_active AND NOT COALESCE(o.needs_review, false)
                 AND d.needs_review THEN 'review_cleared' END,
       '{}'::jsonb)
    ) AS k(kind, detail)
    WHERE d.tag = p_tag AND k.kind IS NOT NULL
  ),
  allk AS (
    SELECT kind, jsonb_build_object('spectrum_id', spectrum_id, 'program_slug', program_slug,
                                    'observation', observation) || detail AS item
    FROM sp WHERE kind IS NOT NULL
    UNION ALL
    SELECT kind, jsonb_build_object('spectrum_id', spectrum_id, 'program_slug', program_slug,
                                    'observation', observation) || detail
    FROM sp_new
    UNION ALL
    SELECT kind, jsonb_build_object('object_id', object_id, 'name', name) || detail FROM ob
  ),
  ranked AS (
    SELECT kind, item, count(*) OVER (PARTITION BY kind) AS n,
           row_number() OVER (PARTITION BY kind ORDER BY item::text) AS rn
    FROM allk
  )
  SELECT jsonb_build_object(
           'tag', p_tag, 'state', r.state, 'frozen_at', r.frozen_at,
           'counts', COALESCE((SELECT jsonb_object_agg(kind, n) FROM (SELECT DISTINCT kind, n FROM ranked) c), '{}'::jsonb),
           'samples', COALESCE((SELECT jsonb_object_agg(kind, items) FROM (
                                  SELECT kind, jsonb_agg(item ORDER BY rn) AS items
                                  FROM ranked WHERE rn <= v_limit GROUP BY kind) s), '{}'::jsonb))
    INTO v_out;
  RETURN v_out;
END;
$$;

-- Frozen catalog tables (P6b): each mirrors the live indexes the catalog RPCs
-- and the exports use, tag-prefixed, so browsing a release plans like live.
-- dr_spectra
CREATE INDEX IF NOT EXISTS idx_dr_spectra_tag_program
    ON public.dr_spectra USING btree (tag, program_slug);
CREATE INDEX IF NOT EXISTS idx_dr_spectra_tag_observation
    ON public.dr_spectra USING btree (tag, observation);
CREATE INDEX IF NOT EXISTS idx_dr_spectra_tag_target
    ON public.dr_spectra USING btree (tag, target_id, grating);
CREATE INDEX IF NOT EXISTS idx_dr_spectra_search_text_trgm
    ON public.dr_spectra USING gin (search_text public.gin_trgm_ops);
-- "which DRs is this spectrum in" (provenance line, identify).
CREATE INDEX IF NOT EXISTS idx_dr_spectra_spectrum
    ON public.dr_spectra USING btree (spectrum_id);
-- dr_targets
CREATE INDEX IF NOT EXISTS idx_dr_targets_tag_object
    ON public.dr_targets USING btree (tag, object_id);
CREATE INDEX IF NOT EXISTS idx_dr_targets_tag_program_observation
    ON public.dr_targets USING btree (tag, program_slug, observation);
CREATE INDEX IF NOT EXISTS idx_dr_targets_tag_field_observation
    ON public.dr_targets USING btree (tag, field, observation);
CREATE INDEX IF NOT EXISTS idx_dr_targets_tag_coords
    ON public.dr_targets USING btree (tag, ra, dec);
-- dr_objects
CREATE INDEX IF NOT EXISTS idx_dr_objects_tag_field
    ON public.dr_objects USING btree (tag, field);
CREATE INDEX IF NOT EXISTS idx_dr_objects_tag_coords
    ON public.dr_objects USING btree (tag, ra, dec);
CREATE INDEX IF NOT EXISTS idx_dr_objects_tag_redshift
    ON public.dr_objects USING btree (tag, redshift);
CREATE INDEX IF NOT EXISTS idx_dr_objects_tag_needs_review
    ON public.dr_objects USING btree (tag, id) WHERE needs_review;
CREATE INDEX IF NOT EXISTS idx_dr_objects_programs
    ON public.dr_objects USING gin (programs);
CREATE INDEX IF NOT EXISTS idx_dr_objects_evidence_programs
    ON public.dr_objects USING gin (evidence_programs);
CREATE INDEX IF NOT EXISTS idx_dr_objects_gratings
    ON public.dr_objects USING gin (gratings);
CREATE INDEX IF NOT EXISTS idx_dr_objects_observations
    ON public.dr_objects USING gin (observations);
CREATE INDEX IF NOT EXISTS idx_dr_objects_search_text_trgm
    ON public.dr_objects USING gin (search_text public.gin_trgm_ops);
-- dr_manifest: the byte pass walks pending (and, on a retry, failed) rows per tag; dedup looks up by science hash.
CREATE INDEX IF NOT EXISTS idx_dr_manifest_tag_pending
    ON public.dr_manifest USING btree (tag, storage_key) WHERE (byte_state = ANY (ARRAY['pending'::text, 'failed'::text]));
CREATE INDEX IF NOT EXISTS idx_dr_manifest_tag_component
    ON public.dr_manifest USING btree (tag, component);
CREATE INDEX IF NOT EXISTS idx_dr_manifest_science_hash
    ON public.dr_manifest USING btree (science_hash) WHERE (science_hash IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_dr_manifest_spectrum
    ON public.dr_manifest USING btree (spectrum_id) WHERE (spectrum_id IS NOT NULL);
-- dr_line_fits / dr_spectrum_lines
CREATE INDEX IF NOT EXISTS idx_dr_line_fits_tag_target
    ON public.dr_line_fits USING btree (tag, target_id);
CREATE INDEX IF NOT EXISTS idx_dr_spectrum_lines_tag_line_snr
    ON public.dr_spectrum_lines USING btree (tag, line, snr DESC);
-- dr_photometry / dr_photometry_bands
CREATE INDEX IF NOT EXISTS idx_dr_photometry_tag_object
    ON public.dr_photometry USING btree (tag, object_id);
CREATE INDEX IF NOT EXISTS idx_dr_photometry_tag_field_catalog
    ON public.dr_photometry USING btree (tag, field, catalog_name);
CREATE INDEX IF NOT EXISTS idx_dr_photometry_bands_tag_band_mag
    ON public.dr_photometry_bands USING btree (tag, band, mag);
CREATE INDEX IF NOT EXISTS idx_dr_photometry_bands_tag_band_snr
    ON public.dr_photometry_bands USING btree (tag, band, snr DESC);
-- dr_nircam_images
CREATE INDEX IF NOT EXISTS idx_dr_nircam_images_tag_field
    ON public.dr_nircam_images USING btree (tag, field);

ALTER TABLE dr_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_spectra ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_manifest ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_line_fits ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_spectrum_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_photometry ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_photometry_bands ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_nircam_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_fields ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_filter_options ENABLE ROW LEVEL SECURITY;
ALTER TABLE dr_exports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "select_dr_releases" ON dr_releases;
CREATE POLICY "select_dr_releases"
  ON dr_releases FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
  );

-- Frozen catalog tables (P6b): each policy is the LIVE table's SELECT
-- predicate verbatim (so a release reads through the cat_* views exactly as
-- live does, and the RPCs scope aggregates per viewer the same way), plus
-- "no link accounts" and "the tag is visible" (published to everyone,
-- every state to admins; dr_visible_tags). Parent lookups stay within the
-- same tag.

-- spectra: select_spectra_by_access
DROP POLICY IF EXISTS "select_dr_spectra" ON dr_spectra;
CREATE POLICY "select_dr_spectra"
  ON dr_spectra FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    AND (deploy_status = 'published' OR (SELECT public.is_admin()))
  );

-- targets: select_targets_by_access
DROP POLICY IF EXISTS "select_dr_targets" ON dr_targets;
CREATE POLICY "select_dr_targets"
  ON dr_targets FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
    AND (has_published_spectrum OR (SELECT public.is_admin()))
  );

-- objects: select_objects_by_access (array overlap: a reader holding any of
-- the object's programs sees the row; the RPCs scope the aggregates)
DROP POLICY IF EXISTS "select_dr_objects" ON dr_objects;
CREATE POLICY "select_dr_objects"
  ON dr_objects FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND programs && (SELECT public.accessible_program_slugs())
    AND (has_published_spectrum OR (SELECT public.is_admin()))
  );

-- Program-scoped rows (NIRSpec finals and sidecars) by program_slug;
-- field-level rows (mosaics; program_slug IS NULL) are visible to every
-- authenticated account, as published field deployments are live.
DROP POLICY IF EXISTS "select_dr_manifest" ON dr_manifest;
CREATE POLICY "select_dr_manifest"
  ON dr_manifest FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND (program_slug IS NULL
         OR program_slug = ANY((SELECT public.accessible_program_slugs())::text[]))
  );

-- spectrum_line_fits: select_spectrum_line_fits_by_spectrum (the frozen parent)
DROP POLICY IF EXISTS "select_dr_line_fits" ON dr_line_fits;
CREATE POLICY "select_dr_line_fits"
  ON dr_line_fits FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.dr_spectra s
      WHERE s.tag = dr_line_fits.tag AND s.id = dr_line_fits.spectrum_id
    )
  );

-- spectrum_lines: select_spectrum_lines_by_spectrum
DROP POLICY IF EXISTS "select_dr_spectrum_lines" ON dr_spectrum_lines;
CREATE POLICY "select_dr_spectrum_lines"
  ON dr_spectrum_lines FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.dr_spectra s
      WHERE s.tag = dr_spectrum_lines.tag AND s.id = dr_spectrum_lines.spectrum_id
    )
  );

-- object_photometry: select_object_photometry_by_access (through the frozen object)
DROP POLICY IF EXISTS "select_dr_photometry" ON dr_photometry;
CREATE POLICY "select_dr_photometry"
  ON dr_photometry FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND object_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.dr_objects o
      WHERE o.tag = dr_photometry.tag AND o.id = dr_photometry.object_id
        AND o.programs && (SELECT public.accessible_program_slugs())
        AND (o.has_published_spectrum OR (SELECT public.is_admin()))
    )
  );

-- object_photometry_bands: select_object_photometry_bands_by_parent
DROP POLICY IF EXISTS "select_dr_photometry_bands" ON dr_photometry_bands;
CREATE POLICY "select_dr_photometry_bands"
  ON dr_photometry_bands FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.dr_photometry p
      WHERE p.tag = dr_photometry_bands.tag AND p.id = dr_photometry_bands.photometry_id
    )
  );

-- nircam_images: authenticated_select_nircam (no link accounts here, so the
-- non-link branch alone)
DROP POLICY IF EXISTS "select_dr_nircam_images" ON dr_nircam_images;
CREATE POLICY "select_dr_nircam_images"
  ON dr_nircam_images FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND (deploy_status = 'published' OR (SELECT public.is_admin()))
  );

-- fields: accessible_fields_select (a field with a visible frozen mosaic)
DROP POLICY IF EXISTS "select_dr_fields" ON dr_fields;
CREATE POLICY "select_dr_fields"
  ON dr_fields FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
    AND EXISTS (
      SELECT 1 FROM public.dr_nircam_images ni
      WHERE ni.tag = dr_fields.tag AND ni.field = dr_fields.name AND ni.deploy_status = 'published'
    )
  );

-- the dropdown options of a visible release
DROP POLICY IF EXISTS "select_dr_filter_options" ON dr_filter_options;
CREATE POLICY "select_dr_filter_options"
  ON dr_filter_options FOR SELECT TO authenticated
  USING (
    (SELECT NOT public.is_link_account())
    AND tag = ANY((SELECT public.dr_visible_tags())::text[])
  );
