SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP VIEW "public"."cat_spectra";

ALTER TABLE "public"."storage_objects"
  DROP CONSTRAINT "storage_objects_product_type_check";

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "has_mbkg_spectrum" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "mbkg_preferred" boolean;

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "mbkg_reason" text;

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "n_exp" integer;

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "n_exp_bkg" integer;

ALTER TABLE "public"."spectra"
  ADD COLUMN "has_mbkg_spectrum" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."spectra"
  ADD COLUMN "mbkg_preferred" boolean;

ALTER TABLE "public"."spectra"
  ADD COLUMN "mbkg_reason" text;

ALTER TABLE "public"."spectra"
  ADD COLUMN "n_exp" integer;

ALTER TABLE "public"."spectra"
  ADD COLUMN "n_exp_bkg" integer;

CREATE OR REPLACE FUNCTION public.catalog_query_whitelist()
  RETURNS jsonb
  LANGUAGE sql
  IMMUTABLE
  AS $function$
  SELECT $j${
    "objects": {
      "id": "int", "object_id": "text", "field": "text", "ra": "num", "dec": "num",
      "max_snr": "num", "max_exposure_time": "num",
      "photo_z": "num", "photo_z_err_lo": "num", "photo_z_err_hi": "num", "has_photometry": "bool",
      "redshift": "num", "redshift_quality": "int", "redshift_auto": "num", "redshift_inspected": "num",
      "inspected_used_auto": "bool", "needs_review": "bool", "last_inspected_at": "ts",
      "last_data_change_at": "ts", "version": "int", "is_active": "bool",
      "has_published_spectrum": "bool", "is_public": "bool", "created_at": "ts", "updated_at": "ts"
    },
    "targets": {
      "id": "int", "target_id": "text", "field": "text", "ra": "num", "dec": "num",
      "program_slug": "text", "observation": "text", "max_snr": "num", "max_exposure_time": "num",
      "redshift_auto": "num", "dq_flags": "bits", "spectral_features": "bits",
      "has_published_spectrum": "bool", "is_public": "bool", "object_id": "int"
    },
    "spectra": {
      "id": "int", "spectrum_id": "text", "target_id": "text", "grating": "text",
      "signal_to_noise": "num", "exposure_time": "num", "redshift_auto": "num", "chi2_min": "num",
      "confidence": "num", "has_mbkg_spectrum": "bool", "mbkg_preferred": "bool", "mbkg_reason": "text",
      "n_exp": "int", "n_exp_bkg": "int", "dq_flags": "bits", "deploy_status": "text", "program_slug": "text",
      "observation": "text", "date_obs": "text", "cfpipe_version": "text", "jwst_version": "text",
      "crds_context": "text", "is_public": "bool", "reduced_at": "ts", "created_at": "ts", "updated_at": "ts"
    },
    "lines": {
      "spectrum_id": "int", "object_id": "int", "line": "text", "component": "text",
      "wave_rest": "num", "wave_obs": "num", "flux": "num", "flux_err": "num", "snr": "num",
      "ew_rest": "num", "ew_rest_err": "num", "cont": "num", "cont_err": "num",
      "dv": "num", "dv_err": "num", "sigma_v": "num", "sigma_v_err": "num", "sigma_lsf_kms": "num",
      "complex": "int", "chi2": "num", "dof": "int", "npix": "int", "flags": "bits",
      "blend_into": "text", "tied_to": "text", "label": "text", "members": "text[]"
    },
    "line_fits": {
      "spectrum_id": "int", "target_id": "text", "grating": "text", "program_slug": "text",
      "observation": "text", "z_used": "num", "z_source": "text", "z_quality": "int",
      "object_id": "text", "object_version": "int", "z_fit": "num", "z_fit_err": "num",
      "dv": "num", "dv_err": "num", "sigma_v": "num", "sigma_v_err": "num", "kin_source": "text",
      "n_lines": "int", "n_detected": "int", "n_broad": "int", "chi2": "num", "dof": "int",
      "fit_version": "text", "cfpipe_version": "text", "f_lsf": "num", "fitted_at": "ts",
      "stale_redshift": "bool"
    },
    "photometry": {
      "match_id": "int", "object_id": "int", "band": "text", "flux": "num", "flux_err": "num",
      "wav": "num", "wav_min": "num", "wav_max": "num", "snr": "num", "mag": "num"
    },
    "catalog_matches": {
      "id": "int", "object_id": "int", "field": "text", "ra": "num", "dec": "num",
      "catalog_name": "text", "catalog_id": "text", "match_distance_arcsec": "num",
      "photo_z": "num", "photo_z_err_lo": "num", "photo_z_err_hi": "num", "has_pz": "bool"
    },
    "object_list_members": {
      "list_id": "int", "object_id": "int", "added_at": "ts"
    }
  }$j$::jsonb;
$function$;

CREATE OR REPLACE FUNCTION public.dr_export_rows (
  p_tag       text,
  p_stream    text,
  p_partition text,
  p_after     text    DEFAULT NULL::text,
  p_limit     integer DEFAULT 2000
)
  RETURNS TABLE (
    next_cursor text,
    row_json    jsonb
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
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
             'has_mbkg_spectrum', s.has_mbkg_spectrum, 'mbkg_preferred', s.mbkg_preferred,
             'mbkg_reason', s.mbkg_reason, 'n_exp', s.n_exp, 'n_exp_bkg', s.n_exp_bkg,
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
             'chi2', f.chi2, 'dof', f.dof, 'lines', public.lines_jsonb_of(f.spectrum_id, p_tag), 'fit_version', f.fit_version,
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
             'photometry', public.bands_jsonb_of(ph.id, p_tag), 'photo_z', ph.photo_z,
             'photo_z_err_lo', ph.photo_z_err_lo, 'photo_z_err_hi', ph.photo_z_err_hi, 'has_pz', ph.has_pz)
    FROM dr_catalog_matches ph
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
$function$;

CREATE OR REPLACE FUNCTION public.dr_freeze_job (
  p_tag text
)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '900s'
  SET lock_timeout TO '30s'
  AS $function$
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

  -- Two documented behaviours (P6b-1 validation), by design:
  --  (a) a statement_timeout armed by the CALLER before this call (the
  --      authenticated role's 8 s on the inline path) cancels the statement
  --      regardless of this function's own SET; a cancel landing before the
  --      protected block below leaves the row `freezing` (purge / re-enqueue
  --      recovers it). The pg_cron path runs with the function's settings.
  --  (b) a failed re-freeze of an already-frozen, unpublished tag rolls back
  --      its own leading DELETEs with the rest of the block, so the previous
  --      snapshot stays in place under a `failed` row until the next
  --      re-freeze or purge.

  BEGIN
    IF EXISTS (SELECT 1 FROM deploy_scope_state d
                WHERE d.in_progress_since IS NOT NULL
                  AND COALESCE(d.heartbeat_at, d.in_progress_since) >= now() - deploy_lease_ttl()) THEN
      RAISE EXCEPTION 'a deploy started after the freeze was enqueued (live deploy lease); re-run the freeze when it has finished';
    END IF;
    SELECT * INTO comp FROM dr_parse_components(r.components);

    -- Partial rows of a previous attempt under this tag. The release page
    -- document's assets (component 'page', written by dr_page_set, P6b-3) are
    -- authored independently of the catalog freeze and survive a re-freeze.
    DELETE FROM dr_manifest         WHERE tag = p_tag AND component <> 'page';
    DELETE FROM dr_lines   WHERE tag = p_tag;
    DELETE FROM dr_line_fits        WHERE tag = p_tag;
    DELETE FROM dr_photometry WHERE tag = p_tag;
    DELETE FROM dr_catalog_matches       WHERE tag = p_tag;
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
      INSERT INTO dr_spectra (tag, id, grating, fits_path, signal_to_noise, created_at, updated_at, target_id, thumbnail_svg_fnu, thumbnail_svg_flambda, file_hash, science_hash, file_size, exposure_time, crds_context, jwst_version, cfpipe_version, date_obs, reduced_at, redshift_auto, chi2_min, confidence, has_mbkg_spectrum, mbkg_preferred, mbkg_reason, n_exp, n_exp_bkg, dq_flags, deploy_status, program_slug, observation, spectrum_id, search_text, is_public)
      SELECT p_tag, s.id, s.grating, s.fits_path, s.signal_to_noise, s.created_at, s.updated_at, s.target_id, s.thumbnail_svg_fnu, s.thumbnail_svg_flambda, s.file_hash, s.science_hash, s.file_size, s.exposure_time, s.crds_context, s.jwst_version, s.cfpipe_version, s.date_obs, s.reduced_at, s.redshift_auto, s.chi2_min, s.confidence, s.has_mbkg_spectrum, s.mbkg_preferred, s.mbkg_reason, s.n_exp, s.n_exp_bkg, s.dq_flags, s.deploy_status, s.program_slug, s.observation, s.spectrum_id, s.search_text, s.is_public
      FROM spectra s JOIN pub p ON p.id = s.id
      RETURNING 1
    ),
    ins_targets AS (
      INSERT INTO dr_targets (tag, id, target_id, field, ra, dec, redshift_auto, redshift_quality, spectral_features, dq_flags, created_at, updated_at, redshift_inspected, last_inspected_at, last_inspected_by, max_snr, redshift, max_exposure_time, program_slug, observation, object_id, has_published_spectrum, is_public)
      SELECT p_tag, t.id, t.target_id, t.field, t.ra, t.dec, t.redshift_auto, t.redshift_quality, t.spectral_features, t.dq_flags, t.created_at, t.updated_at, t.redshift_inspected, t.last_inspected_at, t.last_inspected_by, t.max_snr, t.redshift, t.max_exposure_time, t.program_slug, t.observation, t.object_id, t.has_published_spectrum, t.is_public
      FROM targets t
      WHERE EXISTS (SELECT 1 FROM pub p WHERE p.target_id = t.target_id)
      RETURNING 1
    ),
    ins_objects AS (
      -- the active objects with a published member; verbatim row + extras
      INSERT INTO dr_objects (tag, id, object_id, field, ra, dec, created_at, n_targets, n_spectra, programs, gratings, observations, max_snr, max_exposure_time, photo_z, photo_z_err_lo, photo_z_err_hi, has_photometry, updated_at, redshift_auto, redshift_inspected, redshift_quality, inspected_used_auto, redshift, last_inspected_at, last_inspected_by, last_data_change_at, staleness_reason, reviewed_spectrum_ids, needs_review, version, is_active, has_published_spectrum, search_text, is_public, evidence_programs, z_source_spectrum_id)
      SELECT p_tag, o.id, o.object_id, o.field, o.ra, o.dec, o.created_at, o.n_targets, o.n_spectra, o.programs, o.gratings, o.observations, o.max_snr, o.max_exposure_time, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi, o.has_photometry, o.updated_at, o.redshift_auto, o.redshift_inspected, o.redshift_quality, o.inspected_used_auto, o.redshift, o.last_inspected_at, o.last_inspected_by, o.last_data_change_at, o.staleness_reason, o.reviewed_spectrum_ids, o.needs_review, o.version, o.is_active, o.has_published_spectrum, o.search_text, o.is_public,
             ev.programs, zsrc.id
      FROM objects o
      JOIN ev ON ev.obj_id = o.id
      LEFT JOIN zsrc ON zsrc.obj_id = o.id
      RETURNING 1
    ),
    ins_lines AS (
      INSERT INTO dr_line_fits (tag, spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, z_quality, object_id, object_version, z_fit, z_fit_err, dv, dv_err, sigma_v, sigma_v_err, kin_source, n_lines, n_detected, n_broad, chi2, dof, fit_version, cfpipe_version, f_lsf, spectrum_hash, spectrum_science_hash, fitted_at, created_at, updated_at, stale_redshift, stale_spectrum)
      SELECT p_tag, f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation, f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version, f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source, f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.spectrum_science_hash, f.fitted_at, f.created_at, f.updated_at,
             -- stale_redshift is the live, trigger-maintained column since phase 2a
             -- (copied like every other live column); stale_spectrum is still
             -- evaluated in this snapshot
             f.stale_redshift,
             line_fit_stale_spectrum(p.file_hash, p.science_hash, f.spectrum_hash, f.spectrum_science_hash)
      FROM line_fits f
      JOIN pub p ON p.id = f.spectrum_id
      LEFT JOIN objects o ON o.id = p.obj_id
      RETURNING 1
    ),
    ins_slines AS (
      INSERT INTO dr_lines (tag, spectrum_id, line, component, wave_rest, flux, flux_err, snr, ew_rest, ew_rest_err, flags, blend_into, object_id, label, wave_obs, cont, cont_err, dv, dv_err, sigma_v, sigma_v_err, sigma_lsf_kms, complex, chi2, dof, npix, tied_to, members)
      SELECT p_tag, l.spectrum_id, l.line, l.component, l.wave_rest, l.flux, l.flux_err, l.snr, l.ew_rest, l.ew_rest_err, l.flags, l.blend_into, l.object_id, l.label, l.wave_obs, l.cont, l.cont_err, l.dv, l.dv_err, l.sigma_v, l.sigma_v_err, l.sigma_lsf_kms, l.complex, l.chi2, l.dof, l.npix, l.tied_to, l.members
      FROM lines l JOIN pub p ON p.id = l.spectrum_id
      RETURNING 1
    ),
    phot AS MATERIALIZED (
      -- cross-match rows of the frozen objects in the SELECTED catalogs
      SELECT ph.id
      FROM catalog_matches ph
      JOIN ev ON ev.obj_id = ph.object_id
      WHERE (ph.field || '/' || ph.catalog_name) = ANY (comp.photometry)
    ),
    ins_phot AS (
      INSERT INTO dr_catalog_matches (tag, id, object_id, field, ra, dec, catalog_name, catalog_id, match_distance_arcsec, photo_z, photo_z_err_lo, photo_z_err_hi, has_pz, created_at, updated_at)
      SELECT p_tag, ph.id, ph.object_id, ph.field, ph.ra, ph.dec, ph.catalog_name, ph.catalog_id, ph.match_distance_arcsec, ph.photo_z, ph.photo_z_err_lo, ph.photo_z_err_hi, ph.has_pz, ph.created_at, ph.updated_at
      FROM catalog_matches ph JOIN phot ON phot.id = ph.id
      RETURNING 1
    ),
    ins_bands AS (
      INSERT INTO dr_photometry (tag, match_id, band, flux, flux_err, wav, snr, mag, object_id, wav_min, wav_max)
      SELECT p_tag, b.match_id, b.band, b.flux, b.flux_err, b.wav, b.snr, b.mag, b.object_id, b.wav_min, b.wav_max
      FROM photometry b JOIN phot ON phot.id = b.match_id
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
        AND so.product_type IN ('nirspec_spec', 'spectrum_json', 'spectrum_1d_json',
                                'spectrum_mbkg_json', 'zfit', 'nirspec_lines', 'nirspec_lines_json')
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
      UNION ALL
      -- Field-level NIRCam products of each component field (P6b-3): exposure
      -- maps and their renders, the layout plot, mosaic thumbnails and
      -- quick-looks -- what the field page shows besides the mosaics. Not tied
      -- to a deployment row (they are registered per field by push/deploy), so
      -- the active live registry rows are taken as they stand; archived by the
      -- byte pass like the mosaics (server-side copy, no stamp).
      SELECT p_tag, so.storage_key, 'nircam/' || so.field, NULL, so.field, so.product_type,
             NULL, so.backend, so.bucket, so.content_hash, so.science_hash, so.size_bytes, so.id
      FROM storage_objects so
      WHERE so.field = ANY (comp.fields) AND so.root = 'live' AND so.status = 'active'
        AND so.backend = 'osn' AND so.bucket = 'data'
        AND so.product_type IN ('nircam_expmap', 'nircam_expmap_plot', 'nircam_layout',
                                'nircam_mosaic_thumbnail', 'nircam_mosaic_quicklook')
        -- the live storage gate shows field-level rows through their
        -- deployment's status: a --draft redeploy registers active rows under
        -- its draft deployment, and those must not be frozen into the open
        -- nircam/<field> partition (P6b-3 review)
        AND so.deployment_id IS NOT NULL
        AND EXISTS (SELECT 1 FROM deployments d
                    WHERE d.id = so.deployment_id AND d.field = so.field AND d.status = 'published')
      -- (P(z) sidecars are not members: photometry_pz is a key-only product the
      -- layout cannot archive; the frozen dr_catalog_matches rows carry the catalog
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
            FROM dr_photometry b
            JOIN dr_catalog_matches ph ON ph.tag = b.tag AND ph.id = b.match_id
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
    -- Likewise every published-deployment field product of a selected field (same predicate
    -- as the insert; guards a future edit of either side, not a data state).
    SELECT string_agg(so.storage_key, ', ' ORDER BY so.storage_key) INTO v_missing
    FROM storage_objects so
    WHERE so.field = ANY (comp.fields) AND so.root = 'live' AND so.status = 'active'
      AND so.backend = 'osn' AND so.bucket = 'data'
      AND so.product_type IN ('nircam_expmap', 'nircam_expmap_plot', 'nircam_layout',
                              'nircam_mosaic_thumbnail', 'nircam_mosaic_quicklook')
      AND so.deployment_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM deployments d
                  WHERE d.id = so.deployment_id AND d.field = so.field AND d.status = 'published')
      AND NOT EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag AND m.storage_key = so.storage_key);
    IF v_missing IS NOT NULL THEN
      RAISE EXCEPTION 'active field products that did not become members: %', left(v_missing, 1500);
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
    WHERE NOT EXISTS (SELECT 1 FROM dr_catalog_matches ph
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
             -- (the release page document's assets, component 'page', are not
             -- catalog members: left out of every manifest statistic)
             'manifest_by_type', (SELECT COALESCE(jsonb_object_agg(t.product_type, t.n), '{}'::jsonb)
                                 FROM (SELECT m.product_type, count(*) AS n FROM dr_manifest m
                                       WHERE m.tag = p_tag AND m.component <> 'page' GROUP BY m.product_type) t),
             'nircam_fields', to_jsonb(comp.fields), 'photometry_components', to_jsonb(comp.photometry),
             -- per component (nirspec | nircam/<field> | photometry/...): file
             -- count and byte volume, the numbers the /releases pages show
             -- (P6a) without aggregating the manifest at read time
             'by_component', (SELECT COALESCE(jsonb_object_agg(t.component,
                                       jsonb_build_object('files', t.n, 'bytes', t.b)
                                       -- nircam/<field>: how many of the files are the mosaics themselves
                                       || CASE WHEN t.component LIKE 'nircam/%' THEN jsonb_build_object('mosaics', t.n_mosaics) ELSE '{}'::jsonb END
                                       -- nircam/<field>: the field's deploy-time sky coverage
                                       || COALESCE((SELECT jsonb_build_object('area_arcmin2', f.coverage_area_arcmin2)
                                                    FROM fields f
                                                    WHERE t.component = 'nircam/' || f.name AND f.coverage_area_arcmin2 IS NOT NULL),
                                                   '{}'::jsonb)), '{}'::jsonb)
                              FROM (SELECT m.component, count(*) AS n, COALESCE(sum(m.size_bytes), 0) AS b,
                                           count(*) FILTER (WHERE m.product_type = 'nircam_mosaic') AS n_mosaics
                                    FROM dr_manifest m WHERE m.tag = p_tag AND m.component <> 'page' GROUP BY m.component) t),
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
$function$;

CREATE OR REPLACE FUNCTION public.enforce_spectra_dq_user_update_scope()
  RETURNS TRIGGER
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
       -- zfit scalars on the row (perf T2-D2, #508): deploy / backfill only.
       OR OLD.chi2_min IS DISTINCT FROM NEW.chi2_min
       OR OLD.confidence IS DISTINCT FROM NEW.confidence
       -- master background (deploy only)
       OR OLD.has_mbkg_spectrum IS DISTINCT FROM NEW.has_mbkg_spectrum
       OR OLD.mbkg_preferred IS DISTINCT FROM NEW.mbkg_preferred
       OR OLD.mbkg_reason IS DISTINCT FROM NEW.mbkg_reason
       OR OLD.n_exp IS DISTINCT FROM NEW.n_exp
       OR OLD.n_exp_bkg IS DISTINCT FROM NEW.n_exp_bkg
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR OLD.program_slug IS DISTINCT FROM NEW.program_slug
       OR OLD.observation IS DISTINCT FROM NEW.observation
       OR OLD.is_public IS DISTINCT FROM NEW.is_public
    THEN
        RAISE EXCEPTION 'Non-admin updates to spectra may only change dq_flags'
            USING ERRCODE = '42501';  -- insufficient_privilege
    END IF;

    RETURN NEW;
END;
$function$;

ALTER TABLE "public"."spectra"
  ADD CONSTRAINT "spectra_mbkg_reason_check" CHECK (((mbkg_reason IS NULL) OR (mbkg_reason = ANY (ARRAY['no_clear_background'::text, 'single_exposure'::text, 'extended'::text]))));

ALTER TABLE "public"."storage_objects"
  ADD CONSTRAINT "storage_objects_product_type_check"
    CHECK
    ((product_type = ANY (ARRAY['nirspec_spec'::text, 'spectrum_json'::text, 'spectrum_1d_json'::text, 'spectrum_mbkg_json'::text, 'zfit'::text, 'nirspec_lines'::text,
    'nirspec_lines_json'::text,
    'nirspec_redshifts'::text,
    'nirspec_spectrum_exposure'::text,
    'nirspec_rate'::text,
    'nirspec_mbkg'::text,
    'rgb'::text,
    'sed'::text,
    'nircam_exposure'::text,
    'nircam_exposure_preview'::text,
    'nircam_exposure_full'::text,
    'nircam_mosaic'::text,
    'nircam_rgb'::text,
    'nircam_expmap'::text,
    'nircam_expmap_plot'::text,
    'nircam_mosaic_thumbnail'::text,
    'nircam_mosaic_quicklook'::text,
    'nircam_layout'::text,
    'tile'::text,
    'photometry_pz'::text,
    'nirspec_manual_mask'::text,
    'nirspec_stuck_shutters'::text,
    'nirspec_bkg_override'::text, 'nircam_mask'::text, 'nircam_astrom_cat'::text, 'nircam_bad_pixel'::text, 'nircam_flat'::text, 'nircam_wisp'::text])));

CREATE VIEW "public"."cat_spectra" WITH (security_invoker=true) AS  SELECT id,
    grating,
    fits_path,
    signal_to_noise,
    created_at,
    target_id,
    thumbnail_svg_fnu,
    thumbnail_svg_flambda,
    file_hash,
    file_size,
    exposure_time,
    crds_context,
    jwst_version,
    cfpipe_version,
    date_obs,
    dq_flags,
    redshift_auto,
    spectrum_id,
    updated_at,
    search_text,
    reduced_at,
    deploy_status,
    program_slug,
    observation,
    chi2_min,
    confidence,
    science_hash,
    is_public,
    has_mbkg_spectrum,
    mbkg_preferred,
    mbkg_reason,
    n_exp,
    n_exp_bkg
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            spectra.id,
            spectra.grating,
            spectra.fits_path,
            spectra.signal_to_noise,
            spectra.created_at,
            spectra.target_id,
            spectra.thumbnail_svg_fnu,
            spectra.thumbnail_svg_flambda,
            spectra.file_hash,
            spectra.file_size,
            spectra.exposure_time,
            spectra.crds_context,
            spectra.jwst_version,
            spectra.cfpipe_version,
            spectra.date_obs,
            spectra.dq_flags,
            spectra.redshift_auto,
            spectra.spectrum_id,
            spectra.updated_at,
            spectra.search_text,
            spectra.reduced_at,
            spectra.deploy_status,
            spectra.program_slug,
            spectra.observation,
            spectra.chi2_min,
            spectra.confidence,
            spectra.science_hash,
            spectra.is_public,
            spectra.has_mbkg_spectrum,
            spectra.mbkg_preferred,
            spectra.mbkg_reason,
            spectra.n_exp,
            spectra.n_exp_bkg
           FROM public.spectra
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_spectra.tag AS cat_tag,
            dr_spectra.id,
            dr_spectra.grating,
            dr_spectra.fits_path,
            dr_spectra.signal_to_noise,
            dr_spectra.created_at,
            dr_spectra.target_id,
            dr_spectra.thumbnail_svg_fnu,
            dr_spectra.thumbnail_svg_flambda,
            dr_spectra.file_hash,
            dr_spectra.file_size,
            dr_spectra.exposure_time,
            dr_spectra.crds_context,
            dr_spectra.jwst_version,
            dr_spectra.cfpipe_version,
            dr_spectra.date_obs,
            dr_spectra.dq_flags,
            dr_spectra.redshift_auto,
            dr_spectra.spectrum_id,
            dr_spectra.updated_at,
            dr_spectra.search_text,
            dr_spectra.reduced_at,
            dr_spectra.deploy_status,
            dr_spectra.program_slug,
            dr_spectra.observation,
            dr_spectra.chi2_min,
            dr_spectra.confidence,
            dr_spectra.science_hash,
            dr_spectra.is_public,
            dr_spectra.has_mbkg_spectrum,
            dr_spectra.mbkg_preferred,
            dr_spectra.mbkg_reason,
            dr_spectra.n_exp,
            dr_spectra.n_exp_bkg
           FROM public.dr_spectra) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "anon";

COMMENT ON COLUMN "public"."spectra"."has_mbkg_spectrum" IS 'The spec.fits carries the spectrum extracted from the master-background-subtracted exposures (SPEC1D_MBKG, SCI_MBKG, ERR_MBKG); deploy uploads it as the _spec_mbkg.json sidecar.';

COMMENT ON COLUMN "public"."spectra"."mbkg_preferred" IS 'MBKGPREF: the master-background spectrum is likely better than the nodded one. NULL without a master-background spectrum.';

COMMENT ON COLUMN "public"."spectra"."mbkg_reason" IS 'MBKGWHY, set only when mbkg_preferred: no_clear_background (a bright catalogued neighbor near the source in every background exposure of some exposure), single_exposure (no nodded subtraction), extended (the source is wider than the nod step).';

COMMENT ON COLUMN "public"."spectra"."n_exp" IS 'NEXP: exposures combined into the spectrum.';

COMMENT ON COLUMN "public"."spectra"."n_exp_bkg" IS 'NEXPBKG: the fewest background exposures subtracted from any one of the combined exposures (2 for a full 3-shutter nod, 1 when a background exposure was dropped for a neighbor, 0 when an exposure had none).';

COMMENT ON VIEW "public"."cat_spectra" IS 'Catalog source view: spectra for the live catalog, dr_spectra rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectra" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "service_role";
