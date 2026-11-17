SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- #683: spectra.filter (and the dr_spectra twin), filled by deploy and by
-- set_spectrum_filter from fits_path. Rows that predate the column are
-- filled by scripts/backfill_spectra_filter.sql, not here.
SET local check_function_bodies = off;

DROP TRIGGER "bump_spectra_updated_at_trigger" ON "public"."spectra";

DROP VIEW "public"."cat_spectra";

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "filter" text;

ALTER TABLE "public"."spectra"
  ADD COLUMN "filter" text;

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
      "id": "int", "spectrum_id": "text", "target_id": "text", "grating": "text", "filter": "text",
      "signal_to_noise": "num", "exposure_time": "num", "redshift_auto": "num", "chi2_min": "num",
      "confidence": "num", "dq_flags": "bits", "deploy_status": "text", "program_slug": "text",
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
             'grating', s.grating, 'filter', s.filter, 'fits_path', s.fits_path,
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
      INSERT INTO dr_spectra (tag, id, grating, fits_path, signal_to_noise, created_at, updated_at, target_id, thumbnail_svg_fnu, thumbnail_svg_flambda, file_hash, science_hash, file_size, exposure_time, crds_context, jwst_version, cfpipe_version, date_obs, reduced_at, redshift_auto, chi2_min, confidence, dq_flags, deploy_status, program_slug, observation, spectrum_id, search_text, is_public, filter)
      SELECT p_tag, s.id, s.grating, s.fits_path, s.signal_to_noise, s.created_at, s.updated_at, s.target_id, s.thumbnail_svg_fnu, s.thumbnail_svg_flambda, s.file_hash, s.science_hash, s.file_size, s.exposure_time, s.crds_context, s.jwst_version, s.cfpipe_version, s.date_obs, s.reduced_at, s.redshift_auto, s.chi2_min, s.confidence, s.dq_flags, s.deploy_status, s.program_slug, s.observation, s.spectrum_id, s.search_text, s.is_public, s.filter
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

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated (
  p_program_slugs             text[],
  p_filter_programs           text[]           DEFAULT NULL::text[],
  p_fields                    text[]           DEFAULT NULL::text[],
  p_gratings                  text[]           DEFAULT NULL::text[],
  p_gratings_mode             text             DEFAULT 'any'::text,
  p_observations              text[]           DEFAULT NULL::text[],
  p_redshift_quality          integer[]        DEFAULT NULL::integer[],
  p_redshift_min              double precision DEFAULT NULL::double precision,
  p_redshift_max              double precision DEFAULT NULL::double precision,
  p_max_snr_min               double precision DEFAULT NULL::double precision,
  p_max_snr_max               double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min     double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max     double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any      integer          DEFAULT NULL::integer,
  p_dq_flags_include_all      integer          DEFAULT NULL::integer,
  p_dq_flags_exclude          integer          DEFAULT NULL::integer,
  p_list_ids                  integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode             text             DEFAULT 'any'::text,
  p_search                    text             DEFAULT NULL::text,
  p_inspected_only            boolean          DEFAULT NULL::boolean,
  p_needs_review              boolean          DEFAULT NULL::boolean,
  p_has_photometry            boolean          DEFAULT NULL::boolean,
  p_comment_search            text             DEFAULT NULL::text,
  p_comment_search_scope      text             DEFAULT NULL::text,
  p_comment_user_id           uuid             DEFAULT NULL::uuid,
  p_coord_ra                  double precision DEFAULT NULL::double precision,
  p_coord_dec                 double precision DEFAULT NULL::double precision,
  p_radius_degrees            double precision DEFAULT NULL::double precision,
  p_sort_column               text             DEFAULT 'target_id'::text,
  p_sort_direction            text             DEFAULT 'asc'::text,
  p_page                      integer          DEFAULT 1,
  p_page_size                 integer          DEFAULT 50,
  p_include_thumbnails        boolean          DEFAULT false,
  p_include_unpublished       boolean          DEFAULT false,
  p_line                      text             DEFAULT NULL::text,
  p_line_snr_min              double precision DEFAULT NULL::double precision,
  p_line_snr_max              double precision DEFAULT NULL::double precision,
  p_line_include_stale        boolean          DEFAULT false,
  p_band                      text             DEFAULT NULL::text,
  p_band_mag_min              double precision DEFAULT NULL::double precision,
  p_band_mag_max              double precision DEFAULT NULL::double precision,
  p_band_snr_min              double precision DEFAULT NULL::double precision,
  p_band_snr_max              double precision DEFAULT NULL::double precision,
  p_include_count             boolean          DEFAULT true,
  p_after_sort_text           text             DEFAULT NULL::text,
  p_after_sort_num            double precision DEFAULT NULL::double precision,
  p_after_tiebreak            text[]           DEFAULT NULL::text[],
  p_spectrum_ids              integer[]        DEFAULT NULL::integer[],
  p_line_columns              jsonb            DEFAULT NULL::jsonb,
  p_band_columns              jsonb            DEFAULT NULL::jsonb,
  p_measurements_display_only boolean          DEFAULT false
)
  RETURNS TABLE (
    targets        jsonb,
    total_count    bigint,
    page           integer,
    page_size      integer,
    has_more       boolean,
    next_sort_text text,
    next_sort_num  double precision,
    next_tiebreak  text[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  AS $function$
DECLARE
  v_line_spectrum_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_offset INTEGER;
  v_sort_is_text BOOLEAN;
  v_keyset_active BOOLEAN;
BEGIN
  p_page := COALESCE(p_page, 1);
  p_page_size := COALESCE(p_page_size, 50);
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

  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN
    v_list_ids_mode := 'any';
  END IF;

  IF p_sort_direction NOT IN ('asc', 'desc') THEN
    p_sort_direction := 'asc';
  END IF;

  IF NOT (p_sort_column IN (
    'target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'ra', 'dec', 'redshift',
    'redshift_quality', 'redshift_auto', 'signal_to_noise', 'exposure_time', 'grating'
  ) OR (p_sort_column = 'distance' AND v_coord_search_active)
    OR (p_sort_column = 'line_snr' AND p_line IS NOT NULL)
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)
    OR public.measurement_sort_key(p_sort_column, p_line_columns, p_band_columns) IS NOT NULL) THEN
    p_sort_column := 'spectrum_id';
  END IF;

  IF v_coord_search_active AND p_sort_column IN ('target_id', 'spectrum_id') AND p_sort_direction = 'asc' THEN
    p_sort_column := 'distance';
  END IF;

  -- T2-F: which cursor slot the resolved sort column lives in. Every other
  -- whitelisted column is numeric (double precision, or integer cast to it).
  v_sort_is_text := p_sort_column IN ('target_id', 'spectrum_id', 'field', 'observation', 'program_slug', 'grating');
  v_keyset_active := (p_after_tiebreak IS NOT NULL AND array_length(p_after_tiebreak, 1) = 3);

  -- A cursor page always starts at the cursor, never at an offset.
  v_offset := CASE WHEN v_keyset_active THEN 0 ELSE (p_page - 1) * p_page_size END;

  -- Emission-line filter: the spectra with a measurement of p_line in the S/N
  -- bounds, materialized once per call (see spectra_matching_line_filter).
  IF p_line IS NOT NULL THEN
    v_line_spectrum_ids := ARRAY(SELECT public.spectra_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false)));
  END IF;

  -- Photometry band filter: the objects whose photometry in p_band lies in the
  -- magnitude / S/N window, materialized once per call the same way (see
  -- objects_matching_band_filter for the invariants). Object-level even in the
  -- spectra RPCs — photometry belongs to the sky position, not the spectrum —
  -- so a spectrum matches iff its target's parent object does.
  IF p_band IS NOT NULL THEN
    v_band_object_ids := ARRAY(SELECT public.objects_matching_band_filter(
      p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max,
      p_program_slugs, p_include_unpublished));
  END IF;

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
    RETURN QUERY SELECT '[]'::jsonb, 0::BIGINT, p_page, p_page_size, false, NULL::TEXT, NULL::DOUBLE PRECISION, NULL::TEXT[];
    RETURN;
  END IF;

  -- filtered_spectra / distance_filtered are NOT MATERIALIZED (T2-F follow-up,
  -- #511): with the count skipped (every cursor page, every web page after the
  -- first) the count subquery folds away and the page is one scan with a
  -- top-N sort, instead of the ~80 k-row filtered set being materialized to
  -- temp (~32 MB) on every page as it was when the CTE was shared with the
  -- count. With the count requested (page 1) the filter runs twice — a scan
  -- each for the count and the page — which costs about what the spill did.
  -- The CTE carries NO thumbnail columns: the two SVGs are ~1.5 kB per row
  -- (103 MB temp spill per render before T1-5 / #501); they are joined onto
  -- the <= p_page_size page rows at the end, only when p_include_thumbnails.
  RETURN QUERY
  WITH filtered_spectra AS NOT MATERIALIZED (
    SELECT
      t.id AS tgt_db_id,
      t.target_id,
      t.program_slug,
      t.field,
      t.observation,
      t.ra,
      t.dec,
      -- Phase D: redshift / redshift_quality / inspected flags now live on the
      -- parent object. LEFT JOIN so spectra whose target has no object FK
      -- (shouldn't happen post-reconcile, but safe) still appear.
      o.redshift,
      o.redshift_quality,
      o.redshift_inspected,
      o.last_inspected_at,
      o.last_inspected_by,
      o.is_active AS object_is_active,
      o.has_photometry AS object_has_photometry,
      o.object_id AS parent_object_id,
      t.max_snr,
      t.max_exposure_time,
      t.created_at,
      t.updated_at,
      s.id AS spectrum_pk,
      s.spectrum_id,
      s.grating,
      s.filter,
      s.fits_path,
      s.signal_to_noise,
      s.exposure_time,
      s.redshift_auto,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      s.file_hash,
      s.file_size,
      -- the spectrum's S/N in the filtered line (sort key + 'Line S/N' column)
      CASE WHEN p_line IS NOT NULL THEN
        (SELECT __l.snr FROM public.cat_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      -- the parent object's magnitude and S/N in the filtered band (sort keys
      -- + 'Mag' / 'Band S/N' columns). Object-level: photometry belongs to the
      -- sky position, so every grating of one object reports the same pair.
      bv.mag AS band_mag,
      bv.snr AS band_snr,
      -- the several-columns maps (phase 3c): this spectrum's lines, its object's bands
      CASE WHEN jsonb_typeof(p_line_columns) = 'object' THEN
        (SELECT jsonb_object_agg(__l.line, jsonb_build_object('snr', __l.snr, 'flux', __l.flux))
         FROM jsonb_each(p_line_columns) __spec(line, w)
         JOIN public.cat_lines __l ON __l.spectrum_id = s.id AND __l.line = __spec.line
         -- the stale gate the object-level columns apply (the fit's stored flag)
         JOIN public.cat_line_fits __f ON __f.spectrum_id = s.id
         WHERE (COALESCE(p_line_include_stale, false) OR NOT __f.stale_redshift)
           AND ((__spec.w ->> 'snr_min') IS NULL OR __l.snr >= (__spec.w ->> 'snr_min')::double precision)
           AND ((__spec.w ->> 'snr_max') IS NULL OR __l.snr <= (__spec.w ->> 'snr_max')::double precision))
      END AS line_columns,
      bc.band_columns,
      CASE
        WHEN v_coord_search_active THEN
          2 * DEGREES(ASIN(SQRT(
            POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) +
            COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) *
            POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2)
          )))
        ELSE NULL
      END AS distance
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    LEFT JOIN public.object_band_columns(p_band_columns, p_program_slugs, p_include_unpublished) bc ON bc.object_id = t.object_id
    WHERE
      t.program_slug = ANY(v_filtered_program_slugs)
      -- Hide spectra whose parent object was soft-deleted.
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR p_measurements_display_only OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR p_measurements_display_only OR t.object_id = ANY(v_band_object_ids))
      AND (p_spectrum_ids IS NULL OR s.id = ANY(p_spectrum_ids))
      AND (p_dq_flags_include_any IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_any) != 0)
      AND (p_dq_flags_include_all IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_include_all) = p_dq_flags_include_all)
      AND (p_dq_flags_exclude IS NULL OR (COALESCE(s.dq_flags, 0) & p_dq_flags_exclude) = 0)
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND t.object_id IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = t.object_id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND (t.object_id IS NULL OR t.object_id NOT IN (
            SELECT olm.object_id FROM object_list_members olm WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        )))
      )
      AND (p_search IS NULL OR s.id IN (SELECT __s.id FROM public.cat_spectra __s WHERE __s.search_text ILIKE '%' || p_search || '%'))
      AND (
        p_inspected_only IS NULL
        OR (p_inspected_only = TRUE AND o.redshift_quality > 0)
        OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0)
      )
      AND (
        p_needs_review IS NULL
          -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
          OR o.needs_review = p_needs_review
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin: build the set of matching target_ids ONCE
        -- (trgm/seq scan over the tiny comments table) instead of re-probing
        -- comments per outer row. Correlated EXISTS-inside-OR can't be pulled
        -- up and re-executes per spectrum -> timeouts on broad access. See the
        -- objects path below for the object-level analogue.
        OR t.id IN (
          SELECT c.target_id FROM comments c
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
      AND (
        NOT v_coord_search_active OR p_measurements_display_only
        OR (
          -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
          -- every RA, so the RA bound is dropped there (the box is only the index
          -- pre-filter; the Haversine cut below decides membership either way).
          (ABS(p_coord_dec) + p_radius_degrees >= 90
           OR t.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                      AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
          AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        )
      )
  ),
  distance_filtered AS NOT MATERIALIZED (
    SELECT fs.*
    FROM filtered_spectra fs
    WHERE NOT v_coord_search_active OR p_measurements_display_only OR fs.distance <= p_radius_degrees
  ),
  -- T2-F: one row = one candidate plus its sort key, materialized in exactly
  -- one of two typed slots. The ORDER BY, the keyset predicate and the
  -- returned next-cursor all read these two columns, so they cannot drift
  -- apart. p_sort_column is a constant under force_custom_plan, so each CASE
  -- folds to the single referenced column at plan time.
  keyed AS (
    SELECT df.*,
      CASE p_sort_column
        WHEN 'target_id' THEN df.target_id
        WHEN 'spectrum_id' THEN df.spectrum_id
        WHEN 'field' THEN df.field
        WHEN 'observation' THEN df.observation
        WHEN 'program_slug' THEN df.program_slug
        WHEN 'grating' THEN df.grating
      END AS sort_text,
      CASE p_sort_column
        WHEN 'distance' THEN df.distance::double precision
        WHEN 'ra' THEN df.ra::double precision
        WHEN 'dec' THEN df."dec"::double precision
        WHEN 'redshift' THEN df.redshift::double precision
        WHEN 'redshift_quality' THEN df.redshift_quality::double precision
        WHEN 'redshift_auto' THEN df.redshift_auto::double precision
        WHEN 'signal_to_noise' THEN df.signal_to_noise::double precision
        WHEN 'exposure_time' THEN df.exposure_time::double precision
        WHEN 'line_snr' THEN df.line_snr
        WHEN 'band_mag' THEN df.band_mag
        WHEN 'band_snr' THEN df.band_snr
        -- 'line_snr:<line>' / 'band_mag:<band>' / 'band_snr:<band>' (phase 3c)
        ELSE CASE
          WHEN p_sort_column LIKE 'line_snr:%' THEN (df.line_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
          WHEN p_sort_column LIKE 'band_mag:%' THEN (df.band_columns -> split_part(p_sort_column, ':', 2) ->> 'mag')::double precision
          WHEN p_sort_column LIKE 'band_snr:%' THEN (df.band_columns -> split_part(p_sort_column, ':', 2) ->> 'snr')::double precision
        END
      END AS sort_num
    FROM distance_filtered df
  ),
  -- One row past the page (p_page_size + 1) so has_more is exact; the final
  -- SELECT drops it and the cursor is taken from row p_page_size.
  page_rows AS (
    SELECT *, ROW_NUMBER() OVER () as row_num
    FROM (
      SELECT * FROM keyed k
      WHERE
        NOT v_keyset_active
        -- Keyset: rows strictly after the cursor in (sort key <dir> NULLS
        -- LAST, target_id, grating, spectrum_id) order. With a non-NULL cursor
        -- value that is every row whose key sorts after it plus the whole NULL
        -- tail; with a NULL cursor value (the walk is inside the tail) only
        -- NULL-keyed rows past the tiebreak. Equal keys fall through to the
        -- row-value tiebreak comparison.
        OR (
          CASE WHEN v_sort_is_text THEN
            (p_after_sort_text IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_text > p_after_sort_text)
              OR (p_sort_direction = 'desc' AND k.sort_text < p_after_sort_text)
              OR k.sort_text IS NULL))
            OR (k.sort_text IS NOT DISTINCT FROM p_after_sort_text
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          ELSE
            (p_after_sort_num IS NOT NULL AND (
                 (p_sort_direction = 'asc'  AND k.sort_num > p_after_sort_num)
              OR (p_sort_direction = 'desc' AND k.sort_num < p_after_sort_num)
              OR k.sort_num IS NULL))
            OR (k.sort_num IS NOT DISTINCT FROM p_after_sort_num
                AND (k.target_id, k.grating, k.spectrum_id) > (p_after_tiebreak[1], p_after_tiebreak[2], p_after_tiebreak[3]))
          END
        )
      -- Exactly one of the four key terms is live for a given call (the other
      -- three are constant NULL); the tail makes the order total. spectrum_id
      -- was added to the tail in T2-F — (target_id, grating) alone is not
      -- unique (one grating can pair with several filters), which a keyset
      -- cursor cannot tolerate.
      ORDER BY
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_text END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_text END DESC NULLS LAST,
        CASE WHEN p_sort_direction = 'asc'  THEN k.sort_num  END ASC  NULLS LAST,
        CASE WHEN p_sort_direction = 'desc' THEN k.sort_num  END DESC NULLS LAST,
        k.target_id ASC, k.grating ASC, k.spectrum_id ASC
      LIMIT p_page_size + 1 OFFSET v_offset
    ) sorted_page
  ),
  -- The row the next cursor is built from: the page's last row, and only when
  -- an overflow row proved there is a next page.
  cursor_row AS (
    SELECT pr.sort_text, pr.sort_num, pr.target_id, pr.grating, pr.spectrum_id
    FROM page_rows pr
    WHERE pr.row_num = p_page_size
      AND EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size)
  )
  SELECT
    COALESCE(jsonb_agg(jsonb_build_object(
      'id', r.tgt_db_id,
      'target_id', r.target_id,
      'parent_object_id', r.parent_object_id,
      'program_slug', r.program_slug,
      'program_name', pr.program_name,
      'field', r.field,
      'observation', r.observation,
      'ra', r.ra,
      'dec', r.dec,
      -- Phase D: redshift fields are object-level reads
      'redshift', r.redshift,
      'redshift_inspected', r.redshift_inspected,
      'redshift_quality', r.redshift_quality,
      'last_inspected_at', r.last_inspected_at,
      'last_inspected_by', r.last_inspected_by,
      'max_snr', r.max_snr,
      'max_exposure_time', r.max_exposure_time,
      'line_snr', r.line_snr,
      'band_mag', r.band_mag,
      'band_snr', r.band_snr,
      'line_columns', r.line_columns,
      'band_columns', r.band_columns,
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
        'filter', r.filter,
        'fits_path', r.fits_path,
        'signal_to_noise', r.signal_to_noise,
        'exposure_time', r.exposure_time,
        -- Phase D: per-spectrum auto-z and DQ
        'redshift_auto', r.redshift_auto,
        'dq_flags', r.dq_flags,
        'file_hash', r.file_hash,
        'file_size', r.file_size,
        'thumbnail_svg_fnu', sth.thumbnail_svg_fnu,
        'thumbnail_svg_flambda', sth.thumbnail_svg_flambda
      ))
    ) ORDER BY r.row_num), '[]'::jsonb),
    CASE WHEN p_include_count THEN (SELECT COUNT(*) FROM distance_filtered) ELSE -1::BIGINT END,
    p_page,
    p_page_size,
    EXISTS (SELECT 1 FROM page_rows x WHERE x.row_num > p_page_size),
    (SELECT cr.sort_text FROM cursor_row cr),
    (SELECT cr.sort_num FROM cursor_row cr),
    (SELECT ARRAY[cr.target_id, cr.grating, cr.spectrum_id] FROM cursor_row cr)
  FROM page_rows r
  LEFT JOIN programs pr ON pr.slug = r.program_slug
  -- Thumbnails only for the page, only when asked (constant-false join
  -- condition otherwise, which the planner drops).
  LEFT JOIN public.cat_spectra sth ON p_include_thumbnails AND sth.id = r.spectrum_pk
  -- Drop the has_more overflow row.
  WHERE r.row_num <= p_page_size;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_objects_for_sync (
  p_program_slugs        text[],
  p_user_id              uuid                     DEFAULT NULL::uuid,
  p_updated_since        timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit                integer                  DEFAULT 1000,
  p_include_counts       boolean                  DEFAULT true,
  p_include_unpublished  boolean                  DEFAULT false,
  p_after_object_id      text                     DEFAULT NULL::text,
  p_filter_program_slugs text[]                   DEFAULT NULL::text[]
)
  RETURNS TABLE (
    objects                jsonb,
    total_count            bigint,
    total_accessible_count bigint,
    deleted_ids            integer[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  -- matched is MATERIALIZED so the three aggregate CTEs below each see the
  -- same ~p_limit-row set without re-evaluating the WHERE/ORDER/LIMIT.
  WITH matched AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
           o.n_targets, o.n_spectra, o.programs, o.gratings,
           o.max_snr, o.max_exposure_time,
           o.redshift, o.redshift_quality,
           o.redshift_inspected, o.redshift_auto,
           o.inspected_used_auto,
           o.last_inspected_at, o.last_inspected_by,
           o.last_data_change_at, o.staleness_reason, o.needs_review,
           o.version, o.is_active,
           o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi,
           o.created_at, o.updated_at
    FROM objects o
    WHERE o.programs && p_program_slugs
      -- Phase D: hide soft-deleted objects from sync. Reactivation rewrites
      -- updated_at, so re-activated rows get re-synced naturally on next pull.
      AND o.is_active = true
      -- B1: drop objects with no published spectrum (fail-closed).
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
      -- Keyset (#103): seek past the previous page's last object_id. object_id
      -- is UNIQUE, so a strict > needs no (sort_col, id) tiebreaker. Any future
      -- change to this ORDER BY must keep the ordering column UNIQUE (or switch
      -- to a row-value cursor) or keyset will skip/duplicate rows.
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND (p_filter_program_slugs IS NULL
           OR o.programs && p_filter_program_slugs
           OR o.id IN (
             SELECT olm.object_id
             FROM object_list_members olm
             JOIN object_lists ol ON ol.id = olm.list_id
             WHERE ol.visibility NOT IN ('public_read', 'public_edit')
               AND (ol.created_by = p_user_id
                    OR ol.id IN (SELECT list_id FROM object_list_shares
                                 WHERE user_id = p_user_id))))
    ORDER BY o.object_id
    LIMIT p_limit
  ),
  -- One pass over the page's members (#569 follow-up). Every target of every
  -- page object, flagged with the two scopes the aggregates below need; the
  -- spectra join rides on it. Replaces a per-row LATERAL
  -- object_scoped_aggregates() call (~24 buffer probes per object, ~1.6 s per
  -- 5000-row page on production) plus two separate targets x spectra scans.
  page_members AS MATERIALIZED (
    SELECT t.object_id, t.target_id, t.program_slug,
           -- member_target_ids / spectra payload scope: accessible programs.
           t.program_slug = ANY(p_program_slugs) AS in_programs,
           -- object_scoped_aggregates' recompute scope (its CTE m): accessible
           -- programs AND, B1, a target that contributes a published spectrum.
           (t.program_slug = ANY(p_program_slugs)
            AND (p_include_unpublished OR t.has_published_spectrum)) AS in_scope
    FROM targets t
    WHERE t.object_id IN (SELECT id FROM matched)
  ),
  member_spectra AS MATERIALIZED (
    SELECT pm.object_id, pm.in_programs, pm.in_scope,
           s.id, s.target_id, s.grating, s.filter, s.signal_to_noise, s.exposure_time,
           s.redshift_auto, s.dq_flags, s.deploy_status,
           (p_include_unpublished OR s.deploy_status = 'published') AS visible
    FROM page_members pm
    JOIN spectra s ON s.target_id = pm.target_id
  ),
  member_targets_agg AS (
    SELECT pm.object_id,
           jsonb_agg(pm.target_id ORDER BY pm.target_id) AS target_ids
    FROM page_members pm
    WHERE pm.in_programs
    GROUP BY pm.object_id
  ),
  -- Phase D: per-spectrum payload (per design doc) so the Python client
  -- can render redshift_auto and dq_flags per grating without a second
  -- round-trip.
  spectra_agg AS (
    SELECT ms.object_id,
           jsonb_agg(jsonb_build_object(
             'id', ms.id,
             'target_id', ms.target_id,
             'grating', ms.grating,
             'filter', ms.filter,
             'signal_to_noise', ms.signal_to_noise,
             'exposure_time', ms.exposure_time,
             'redshift_auto', ms.redshift_auto,
             'dq_flags', ms.dq_flags
           ) ORDER BY ms.target_id, ms.grating) AS spectra
    FROM member_spectra ms
    WHERE ms.in_programs AND ms.visible
    GROUP BY ms.object_id
  ),
  -- Set-based object_scoped_aggregates() for the page: the same fast path and
  -- recompute, per object. Keep in step with that function.
  scope_targets AS (
    SELECT pm.object_id,
           array_agg(DISTINCT pm.program_slug ORDER BY pm.program_slug) AS programs,
           COUNT(*)::integer AS n_targets
    FROM page_members pm
    WHERE pm.in_scope
    GROUP BY pm.object_id
  ),
  scope_spectra AS (
    SELECT ms.object_id,
           -- Fast-path probes, over ALL member spectra (any program): no
           -- non-published spectrum, and a visible count matching stored
           -- n_spectra. This RPC runs RLS-free, so the count probe is
           -- trivially true here, as it is for the helper's RLS-free callers.
           bool_or(ms.deploy_status <> 'published') AS has_unpublished,
           COUNT(*)::integer AS n_all,
           -- Recompute (the helper's CTE sp): visible spectra of in-scope targets.
           array_agg(DISTINCT ms.grating ORDER BY ms.grating)
             FILTER (WHERE ms.in_scope AND ms.visible AND ms.grating IS NOT NULL) AS gratings,
           (COUNT(*) FILTER (WHERE ms.in_scope AND ms.visible))::integer AS n_spectra,
           MAX(ms.signal_to_noise) FILTER (WHERE ms.in_scope AND ms.visible) AS max_snr,
           MAX(ms.exposure_time) FILTER (WHERE ms.in_scope AND ms.visible) AS max_exposure_time
    FROM member_spectra ms
    GROUP BY ms.object_id
  ),
  scoped_aggs AS (
    SELECT m.id AS object_id,
           -- The helper's `stored` branch condition; a NULL (e.g. a NULL
           -- stored n_spectra) falls through to the recompute, as there.
           COALESCE(NOT p_include_unpublished
                    AND m.programs <@ p_program_slugs
                    AND NOT COALESCE(ss.has_unpublished, false)
                    AND m.n_spectra = COALESCE(ss.n_all, 0), false) AS use_stored,
           st.programs AS r_programs, st.n_targets AS r_n_targets,
           ss.gratings AS r_gratings, ss.n_spectra AS r_n_spectra,
           ss.max_snr AS r_max_snr, ss.max_exposure_time AS r_max_exposure_time
    FROM matched m
    LEFT JOIN scope_targets st ON st.object_id = m.id
    LEFT JOIN scope_spectra ss ON ss.object_id = m.id
  ),
  lists_agg AS (
    SELECT olm.object_id,
           jsonb_agg(ol.slug ORDER BY ol.slug) AS list_slugs
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT id FROM matched)
      AND (ol.created_by = p_user_id
           OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = p_user_id))
    GROUP BY olm.object_id
  ),
  -- Count CTEs are gated on p_include_counts; when FALSE the planner
  -- collapses them to One-Time Filter: false and skips the scan.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_updated_since IS NULL OR o.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt
    FROM objects o
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
  ),
  -- Tombstones: on the first incremental page (p_updated_since set, no
  -- cursor), the ids of in-scope objects that changed since the cursor and are
  -- no longer visible to this caller -- soft-deleted (is_active = false;
  -- apply_object_reconciliation stamps updated_at) or, for non-admins, left without a
  -- published spectrum (recompute_has_published_spectrum stamps updated_at on
  -- the flip). The client deletes them locally, so an incremental sync no
  -- longer leaves ghosts that force the next pull into a full resync. Only the
  -- integer ids travel: nothing about an unpublished object is disclosed, and
  -- an id the client never mirrored deletes nothing.
  deleted AS (
    SELECT COALESCE(array_agg(o.id ORDER BY o.id), '{}'::INTEGER[]) AS ids
    FROM objects o
    WHERE p_updated_since IS NOT NULL
      AND p_after_object_id IS NULL
      AND o.programs && p_program_slugs
      AND o.updated_at > p_updated_since
      AND NOT (o.is_active AND (p_include_unpublished OR o.has_published_spectrum))
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'ra', m.ra,
        'dec', m.dec,
        -- Aggregates scoped to the caller's accessible programs so a sync that
        -- pulls a mixed-program object doesn't leak proprietary member metadata
        -- into the Python catalog. Same semantics as object_scoped_aggregates(),
        -- computed per page in scoped_aggs.
        'n_targets', CASE WHEN sa.use_stored THEN m.n_targets ELSE COALESCE(sa.r_n_targets, 0) END,
        'n_spectra', CASE WHEN sa.use_stored THEN m.n_spectra ELSE COALESCE(sa.r_n_spectra, 0) END,
        'programs', CASE WHEN sa.use_stored THEN m.programs ELSE COALESCE(sa.r_programs, '{}') END,
        'gratings', CASE WHEN sa.use_stored THEN m.gratings ELSE COALESCE(sa.r_gratings, '{}') END,
        'max_snr', CASE WHEN sa.use_stored THEN m.max_snr ELSE sa.r_max_snr END,
        'max_exposure_time', CASE WHEN sa.use_stored THEN m.max_exposure_time ELSE sa.r_max_exposure_time END,
        'redshift', m.redshift,
        'redshift_quality', m.redshift_quality,
        'redshift_inspected', m.redshift_inspected,
        'redshift_auto', m.redshift_auto,
        'inspected_used_auto', m.inspected_used_auto,
        'last_inspected_at', m.last_inspected_at,
        'last_inspected_by', m.last_inspected_by,
        'last_data_change_at', m.last_data_change_at,
        'staleness_reason', m.staleness_reason,
        'needs_review', m.needs_review,
        'version', m.version,
        'is_active', m.is_active,
        'has_photometry', m.has_photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'created_at', m.created_at,
        'updated_at', m.updated_at,
        'member_target_ids', COALESCE(mt.target_ids, '[]'::jsonb),
        'spectra',           COALESCE(sp.spectra,    '[]'::jsonb),
        'lists',             COALESCE(la.list_slugs, '[]'::jsonb)
      )
      -- Keyset (#103): the client uses the LAST element's object_id as the next
      -- page's cursor, so the page array MUST be in object_id order. matched is
      -- ORDER BY object_id, but the LEFT JOINs below can reorder it, so pin the
      -- aggregate order explicitly.
      ORDER BY m.object_id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m
  LEFT JOIN member_targets_agg mt ON mt.object_id = m.id
  LEFT JOIN spectra_agg         sp ON sp.object_id = m.id
  LEFT JOIN lists_agg           la ON la.object_id = m.id
  JOIN scoped_aggs              sa ON sa.object_id = m.id;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_spectra_for_sync (
  p_program_slugs       text[],
  p_user_id             uuid                     DEFAULT NULL::uuid,
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_counts      boolean                  DEFAULT true,
  p_include_unpublished boolean                  DEFAULT false,
  p_after_spectrum_id   text                     DEFAULT NULL::text
)
  RETURNS TABLE (
    spectra                jsonb,
    total_count            bigint,
    total_accessible_count bigint,
    deleted_ids            integer[]
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  WITH matched AS MATERIALIZED (
    SELECT s.id, s.spectrum_id, s.target_id, o.object_id AS object_id,
           s.grating, s.filter, s.fits_path, s.file_hash, s.science_hash, s.file_size,
           s.signal_to_noise, s.exposure_time,
           s.cfpipe_version, s.crds_context, s.jwst_version, s.date_obs, s.reduced_at,
           s.redshift_auto, s.dq_flags,
           -- program_slug / observation are the trigger-owned row-local copies
           -- of the parent target's (perf T2-A, #504), so they read off the
           -- spectra row; targets is joined only for field and the object link.
           s.program_slug, s.observation, t.field,
           s.created_at,
           -- The row's sync timestamp is the later of its own and its
           -- object's: a row re-sent because the object changed must advance
           -- the client's cursor (MAX of mirrored updated_at) past that change,
           -- or it would be re-sent on every incremental sync. Same pattern
           -- as get_line_fits_for_sync's effective_updated_at.
           GREATEST(s.updated_at, o.updated_at) AS updated_at
    FROM spectra s
    JOIN targets t ON t.target_id = s.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    -- Program scope on the row-local column: the keyset walk down
    -- idx_spectra_spectrum_id rejects out-of-scope rows without fetching the
    -- target first (a user with a few programs used to pay a targets lookup
    -- for every spectrum walked past).
    WHERE s.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      -- B1: fail-closed publish gate (this RPC always bypasses RLS). Drafts
      -- sync only for admins opting in; revoked never syncs -- it is the
      -- soft-deleted state and is tombstoned below instead.
      AND (s.deploy_status = 'published'
           OR (p_include_unpublished AND s.deploy_status = 'draft'))
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: a spectrum tombstoned as
      -- a member of a soft-deleted object comes back when the object is
      -- reactivated (which stamps objects.updated_at) even though the
      -- spectrum row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      -- Keyset (#103): spectrum_id is UNIQUE (idx_spectra_spectrum_id), so a
      -- strict > needs no tiebreaker; keep the ordering column UNIQUE.
      AND (p_after_spectrum_id IS NULL OR s.spectrum_id > p_after_spectrum_id)
    ORDER BY s.spectrum_id
    LIMIT p_limit
  ),
  -- First-page counts, gated on p_include_counts (when FALSE the planner
  -- collapses the CTE to One-Time Filter: false). One pass over spectra
  -- yields both numbers: total_count (the incremental window) is a FILTER on
  -- the scan that produces total_accessible_count, and the soft-deleted-
  -- object exclusion is an anti-join against the (tiny, partial-indexed) set
  -- of inactive objects. Previously two spectra x targets x objects hash
  -- joins ran back to back on every first page.
  counts AS (
    SELECT COUNT(*) FILTER (WHERE p_updated_since IS NULL
                              OR s.updated_at > p_updated_since
                              -- same object clause as matched; the recently
                              -- changed objects are a small hashed set
                              OR s.target_id IN (
                                SELECT t.target_id
                                FROM targets t
                                JOIN objects o ON o.id = t.object_id
                                WHERE o.updated_at > p_updated_since)) AS total_cnt,
           COUNT(*) AS accessible_cnt
    FROM spectra s
    WHERE p_include_counts
      AND s.program_slug = ANY(p_program_slugs)
      AND (s.deploy_status = 'published'
           OR (p_include_unpublished AND s.deploy_status = 'draft'))
      AND NOT EXISTS (
        SELECT 1
        FROM targets t
        JOIN objects o ON o.id = t.object_id
        WHERE t.target_id = s.target_id
          AND o.is_active = false)
  ),
  -- Tombstones (see get_objects_for_sync): first incremental page only.
  deleted AS (
    SELECT COALESCE(array_agg(x.id ORDER BY x.id), '{}'::INTEGER[]) AS ids
    FROM (
      -- Un-published since the cursor: revoked for everyone, plus a draft an
      -- admin's mirror may carry. set_spectra_deploy_status's write reaches
      -- here because deploy_status is in bump_spectra_updated_at_trigger's
      -- column list.
      SELECT s.id
      FROM spectra s
      WHERE p_updated_since IS NOT NULL
        AND p_after_spectrum_id IS NULL
        AND s.program_slug = ANY(p_program_slugs)
        AND s.updated_at > p_updated_since
        AND NOT (s.deploy_status = 'published'
                 OR (p_include_unpublished AND s.deploy_status = 'draft'))
      UNION
      -- Members of an object soft-deleted since the cursor: the spectrum row
      -- itself did not change, so its own updated_at says nothing.
      SELECT s.id
      FROM spectra s
      JOIN targets t ON t.target_id = s.target_id
      JOIN objects o ON o.id = t.object_id
      WHERE p_updated_since IS NOT NULL
        AND p_after_spectrum_id IS NULL
        AND s.program_slug = ANY(p_program_slugs)
        AND o.is_active = false
        AND o.updated_at > p_updated_since
    ) x
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'spectrum_id', m.spectrum_id,
        'target_id', m.target_id,
        'object_id', m.object_id,
        'grating', m.grating,
        'filter', m.filter,
        'fits_path', m.fits_path,
        'file_hash', m.file_hash,
        'science_hash', m.science_hash,
        'file_size', m.file_size,
        'signal_to_noise', m.signal_to_noise,
        'exposure_time', m.exposure_time,
        'cfpipe_version', m.cfpipe_version,
        'crds_context', m.crds_context,
        'jwst_version', m.jwst_version,
        'date_obs', m.date_obs,
        'reduced_at', m.reduced_at,
        'redshift_auto', m.redshift_auto,
        'dq_flags', m.dq_flags,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be spectrum_id-ordered (client cursors on
      -- the last element). matched has no post-ORDER joins, but pin it anyway.
      ORDER BY m.spectrum_id
    ), '[]'::jsonb),
    COALESCE((SELECT c.total_cnt FROM counts c), 0)::BIGINT,
    COALESCE((SELECT c.accessible_cnt FROM counts c), 0)::BIGINT,
    (SELECT d.ids FROM deleted d)
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.set_spectrum_filter()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SET search_path TO ''
  AS $function$
DECLARE
    v_parsed text;
BEGIN
    IF NEW.filter IS NULL THEN
        NEW.filter := public.spectrum_filter_from_path(NEW.fits_path);
    ELSIF TG_OP = 'UPDATE'
          AND NEW.fits_path IS DISTINCT FROM OLD.fits_path
          AND NEW.filter IS NOT DISTINCT FROM OLD.filter THEN
        v_parsed := public.spectrum_filter_from_path(NEW.fits_path);
        IF v_parsed IS NOT NULL THEN
            NEW.filter := v_parsed;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."set_spectrum_filter"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.spectrum_filter_from_path (
  p_fits_path text
)
  RETURNS text
  LANGUAGE sql
  IMMUTABLE
  PARALLEL SAFE
  SET search_path TO ''
  AS $function$
  SELECT upper((regexp_match(
    regexp_replace(p_fits_path, '^.*/', ''),
    '(?:^|_)(?:prism|g[0-9]{3}[mh])_(clear|f[0-9]{3}[a-z]+)_[^/]+_spec\.fits$', 'i'))[1]);
$function$;

REVOKE ALL ON FUNCTION "public"."spectrum_filter_from_path"(text) FROM PUBLIC, "anon";

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
    filter
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
            spectra.filter
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
            dr_spectra.filter
           FROM public.dr_spectra) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "anon";

CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE OF dq_flags,
    redshift_auto, signal_to_noise, thumbnail_svg_fnu, thumbnail_svg_flambda, fits_path, file_hash, science_hash, chi2_min, confidence, deploy_status, filter ON public.spectra
  FOR EACH ROW
  EXECUTE FUNCTION public.bump_spectra_updated_at();

CREATE TRIGGER set_spectrum_filter_trigger
  BEFORE INSERT OR UPDATE OF fits_path, filter ON public.spectra
  FOR EACH ROW
  EXECUTE FUNCTION public.set_spectrum_filter();

COMMENT ON COLUMN "public"."spectra"."filter" IS 'NIRSpec filter paired with grating (CLEAR, F070LP, F100LP, F170LP, F290LP; #683). Written by deploy; filled from fits_path by set_spectrum_filter when NULL (spectrum_filter_from_path).';

COMMENT ON FUNCTION "public"."spectrum_filter_from_path"(text) IS 'NIRSpec filter parsed from a spectrum file name ({obs}_{grating}_{filter}_{source_id}_spec.fits), upper-cased; NULL when the name does not match. Fills spectra.filter (set_spectrum_filter) and the backfill (#683).';

COMMENT ON VIEW "public"."cat_spectra" IS 'Catalog source view: spectra for the live catalog, dr_spectra rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

GRANT EXECUTE ON FUNCTION "public"."set_spectrum_filter"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."set_spectrum_filter"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."set_spectrum_filter"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."set_spectrum_filter"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."spectrum_filter_from_path"(text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."spectrum_filter_from_path"(text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."spectrum_filter_from_path"(text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."spectrum_filter_from_path"(text) TO "service_role";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectra" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "service_role";
