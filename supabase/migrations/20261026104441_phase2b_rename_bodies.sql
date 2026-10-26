SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP VIEW "public"."cat_photometry";

CREATE OR REPLACE FUNCTION public.dr_export_begin (
  p_tag text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
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
  -- Complete as written: dr_line_fits and dr_catalog_matches take their program
  -- slugs from these spectra and objects, so they cannot add a new one.
  IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(r.components) c
             WHERE c.value LIKE '%/%'
               AND (EXISTS (SELECT 1 FROM dr_spectra s WHERE s.tag = p_tag AND s.program_slug = c.value)
                    OR EXISTS (SELECT 1 FROM dr_objects o WHERE o.tag = p_tag AND c.value = ANY (o.evidence_programs))
                    OR EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag AND m.program_slug = c.value))) THEN
    RAISE EXCEPTION 'release % has a program slug equal to one of its component names; rename the program', p_tag;
  END IF;
  -- likewise 'page', the release page document's manifest component (P6b-3)
  IF EXISTS (SELECT 1 FROM dr_spectra s WHERE s.tag = p_tag AND s.program_slug = 'page')
     OR EXISTS (SELECT 1 FROM dr_objects o WHERE o.tag = p_tag AND 'page' = ANY (o.evidence_programs))
     OR EXISTS (SELECT 1 FROM dr_manifest m WHERE m.tag = p_tag AND m.program_slug = 'page') THEN
    RAISE EXCEPTION 'release % has a program slug equal to ''page'' (the page document''s component); rename the program', p_tag;
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
$function$;

CREATE OR REPLACE FUNCTION public.dr_export_partitions (
  p_tag text
)
  RETURNS TABLE (
    stream      text,
    partition   text,
    storage_key text,
    n_rows      bigint
  )
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
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
    FROM dr_catalog_matches ph
    JOIN dr_objects o ON o.tag = ph.tag AND o.id = ph.object_id, unnest(o.evidence_programs) AS p
    WHERE ph.tag = p_tag GROUP BY p
    UNION ALL
    -- the release page document (component 'page', P6b-3) is served by the
    -- release page, not exported
    SELECT 'manifest', COALESCE(m.program_slug, m.component), count(*)
    FROM dr_manifest m WHERE m.tag = p_tag AND m.component <> 'page'
    GROUP BY COALESCE(m.program_slug, m.component)
  )
  SELECT c.stream, c.partition, dr_export_key(p_tag, c.stream, c.partition), c.n
  FROM counts c
  ORDER BY c.stream, c.partition;
END;
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
      INSERT INTO dr_spectra (tag, id, grating, fits_path, signal_to_noise, created_at, updated_at, target_id, thumbnail_svg_fnu, thumbnail_svg_flambda, file_hash, science_hash, file_size, exposure_time, crds_context, jwst_version, cfpipe_version, date_obs, reduced_at, redshift_auto, chi2_min, confidence, dq_flags, deploy_status, program_slug, observation, spectrum_id, search_text, is_public)
      SELECT p_tag, s.id, s.grating, s.fits_path, s.signal_to_noise, s.created_at, s.updated_at, s.target_id, s.thumbnail_svg_fnu, s.thumbnail_svg_flambda, s.file_hash, s.science_hash, s.file_size, s.exposure_time, s.crds_context, s.jwst_version, s.cfpipe_version, s.date_obs, s.reduced_at, s.redshift_auto, s.chi2_min, s.confidence, s.dq_flags, s.deploy_status, s.program_slug, s.observation, s.spectrum_id, s.search_text, s.is_public
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
      INSERT INTO dr_line_fits (tag, spectrum_id, target_id, grating, program_slug, observation, z_used, z_source, z_quality, object_id, object_version, z_fit, z_fit_err, dv, dv_err, sigma_v, sigma_v_err, kin_source, n_lines, n_detected, n_broad, chi2, dof, lines, fit_version, cfpipe_version, f_lsf, spectrum_hash, spectrum_science_hash, fitted_at, created_at, updated_at, stale_redshift, stale_spectrum)
      SELECT p_tag, f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation, f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version, f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source, f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.lines, f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.spectrum_science_hash, f.fitted_at, f.created_at, f.updated_at,
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
      INSERT INTO dr_lines (tag, spectrum_id, line, component, wave_rest, flux, flux_err, snr, ew_rest, ew_rest_err, flags, blend_into, object_id)
      SELECT p_tag, l.spectrum_id, l.line, l.component, l.wave_rest, l.flux, l.flux_err, l.snr, l.ew_rest, l.ew_rest_err, l.flags, l.blend_into, l.object_id
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
      INSERT INTO dr_catalog_matches (tag, id, object_id, field, ra, dec, catalog_name, catalog_id, match_distance_arcsec, photometry, photo_z, photo_z_err_lo, photo_z_err_hi, has_pz, created_at, updated_at)
      SELECT p_tag, ph.id, ph.object_id, ph.field, ph.ra, ph.dec, ph.catalog_name, ph.catalog_id, ph.match_distance_arcsec, ph.photometry, ph.photo_z, ph.photo_z_err_lo, ph.photo_z_err_hi, ph.has_pz, ph.created_at, ph.updated_at
      FROM catalog_matches ph JOIN phot ON phot.id = ph.id
      RETURNING 1
    ),
    ins_bands AS (
      INSERT INTO dr_photometry (tag, match_id, band, flux, flux_err, wav, snr, mag, object_id)
      SELECT p_tag, b.match_id, b.band, b.flux, b.flux_err, b.wav, b.snr, b.mag, b.object_id
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

CREATE OR REPLACE FUNCTION public.dr_parse_components (
  p_components jsonb,
  OUT          nirspec boolean,
  OUT          fields text[],
  OUT          photometry text[]
)
  RETURNS record
  LANGUAGE plpgsql
  STABLE
  SET search_path TO 'public'
  AS $function$
DECLARE
  c text;
  v_field text;
  v_catalog text;
BEGIN
  nirspec := false; fields := '{}'; photometry := '{}';
  IF p_components IS NULL OR jsonb_typeof(p_components) <> 'array' OR jsonb_array_length(p_components) = 0 THEN
    RAISE EXCEPTION 'components must be a non-empty JSON array of "nirspec", "nircam/<field>", "photometry/<field>/<catalog_name>"';
  END IF;
  FOR c IN SELECT jsonb_array_elements_text(p_components) LOOP
    IF c = 'nirspec' THEN
      nirspec := true;
    ELSIF c LIKE 'nircam/%' THEN
      v_field := substr(c, 8);
      IF NOT EXISTS (SELECT 1 FROM nircam_images ni WHERE ni.field = v_field AND ni.deploy_status = 'published') THEN
        RAISE EXCEPTION 'component %: no published NIRCam images for field %', c, v_field;
      END IF;
      fields := array_append(fields, v_field);
    ELSIF c LIKE 'photometry/%/%' THEN
      v_field := split_part(substr(c, 12), '/', 1);
      v_catalog := substr(substr(c, 12), length(v_field) + 2);
      IF NOT EXISTS (SELECT 1 FROM catalog_matches op WHERE op.field = v_field AND op.catalog_name = v_catalog) THEN
        RAISE EXCEPTION 'component %: no photometry rows for field % catalog %', c, v_field, v_catalog;
      END IF;
      photometry := array_append(photometry, v_field || '/' || v_catalog);
    ELSE
      RAISE EXCEPTION 'unknown component %', c;
    END IF;
  END LOOP;
  SELECT array_agg(DISTINCT f ORDER BY f) INTO fields FROM unnest(fields) f;
  SELECT array_agg(DISTINCT f ORDER BY f) INTO photometry FROM unnest(photometry) f;
  fields := COALESCE(fields, '{}'); photometry := COALESCE(photometry, '{}');
  -- Photometry rows hang off the frozen objects (their access scope is the
  -- object's programs), so a photometry component needs the catalog.
  IF cardinality(photometry) > 0 AND NOT nirspec THEN
    RAISE EXCEPTION 'photometry components need the nirspec component (the rows are scoped by the frozen objects)';
  END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_purge_release (
  p_tag text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '300s'
  AS $function$
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
  DELETE FROM dr_lines   WHERE tag = p_tag; GET DIAGNOSTICS n_slines = ROW_COUNT;
  DELETE FROM dr_line_fits        WHERE tag = p_tag; GET DIAGNOSTICS n_lines = ROW_COUNT;
  DELETE FROM dr_photometry WHERE tag = p_tag; GET DIAGNOSTICS n_bands = ROW_COUNT;
  DELETE FROM dr_catalog_matches       WHERE tag = p_tag; GET DIAGNOSTICS n_phot = ROW_COUNT;
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
$function$;

CREATE OR REPLACE FUNCTION public.dr_readiness()
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_is_admin boolean;
  v_spectra jsonb;
  v_objects jsonb;
  v_spectra_status jsonb;
  v_nircam_drafts bigint;
  v_deployments jsonb;
  v_leases jsonb;
  v_redrafted jsonb;
  v_dangling jsonb;
  v_etag jsonb;
  v_lines jsonb;
  v_has_sh_spectra boolean;
  v_has_sh_storage boolean;
  v_sh_spectra bigint;
  v_sh_storage bigint;
  v_releases jsonb;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  -- 0. data releases (P4a): every tag, newest first.
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'tag', r.tag, 'state', r.state, 'components', r.components,
      'freeze_started_at', r.freeze_started_at,
      'frozen_at', r.frozen_at, 'published_at', r.published_at,
      'withdrawn_at', r.withdrawn_at, 'counts', r.counts, 'error', r.error)
    ORDER BY r.created_at DESC), '[]'::jsonb)
  INTO v_releases
  FROM dr_releases r;

  -- 1. published spectra: one pass, grouped on the three keys we need.
  WITH po AS MATERIALIZED (
    SELECT s.cfpipe_version, s.observation, s.program_slug, count(*) AS n
    FROM spectra s
    WHERE s.deploy_status = 'published'
    GROUP BY 1, 2, 3
  ),
  by_version AS (
    SELECT COALESCE(cfpipe_version, 'unknown') AS v,
           sum(n) AS spectra,
           count(DISTINCT observation) AS observations
    FROM po
    GROUP BY 1
  )
  SELECT jsonb_build_object(
    'spectra_by_version',
      (SELECT COALESCE(jsonb_object_agg(v, jsonb_build_object('spectra', spectra, 'observations', observations)), '{}'::jsonb)
       FROM by_version),
    'published',
      (SELECT jsonb_build_object(
         'spectra', COALESCE(sum(n), 0),
         'observations', count(DISTINCT observation),
         'programs', count(DISTINCT program_slug))
       FROM po))
  INTO v_spectra;

  -- 2. draft / revoked spectra (partial index on deploy_status <> 'published').
  SELECT jsonb_build_object(
    'draft', count(*) FILTER (WHERE deploy_status = 'draft'),
    'revoked', count(*) FILTER (WHERE deploy_status = 'revoked'))
  INTO v_spectra_status
  FROM spectra
  WHERE deploy_status <> 'published';

  -- 3. objects: published / needs_review / implicit pins in one pass over active rows.
  SELECT jsonb_build_object(
    'published', count(*) FILTER (WHERE has_published_spectrum),
    'needs_review', count(*) FILTER (WHERE needs_review),
    'implicit_pins', count(*) FILTER (WHERE inspected_used_auto))
  INTO v_objects
  FROM objects
  WHERE is_active;

  -- 4. NIRCam drafts and deployments by status (small tables).
  SELECT count(*) INTO v_nircam_drafts FROM nircam_images WHERE deploy_status = 'draft';

  SELECT jsonb_build_object(
    'draft', count(*) FILTER (WHERE status = 'draft'),
    'revoked', count(*) FILTER (WHERE status = 'revoked'))
  INTO v_deployments
  FROM deployments;

  -- 5. live deploy leases (with the stale flag the takeover rule uses).
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'scope_type', d.scope_type,
      'scope_key', d.scope_key,
      'version', d.version,
      'in_progress_by', d.in_progress_by,
      'in_progress_by_username', up.username,
      'in_progress_host', d.in_progress_host,
      'in_progress_since', d.in_progress_since,
      'heartbeat_at', d.heartbeat_at,
      'stale', COALESCE(d.heartbeat_at, d.in_progress_since) < now() - deploy_lease_ttl())
    ORDER BY d.in_progress_since), '[]'::jsonb)
  INTO v_leases
  FROM deploy_scope_state d
  LEFT JOIN user_profiles up ON up.user_id = d.in_progress_by
  WHERE d.in_progress_since IS NOT NULL;

  -- 6. re-drafted NIRCam fields (see the header comment for the definition).
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'field', f.field,
      'draft_images', f.n_draft,
      'published_images', f.n_published,
      'published_deployments', f.n_published_deployments)
    ORDER BY f.field), '[]'::jsonb)
  INTO v_redrafted
  FROM (
    SELECT ni.field,
           count(*) FILTER (WHERE ni.deploy_status = 'draft') AS n_draft,
           count(*) FILTER (WHERE ni.deploy_status = 'published') AS n_published,
           (SELECT count(*) FROM deployments dp
             WHERE dp.field = ni.field AND dp.status = 'published') AS n_published_deployments
    FROM nircam_images ni
    GROUP BY ni.field
  ) f
  WHERE f.n_draft > 0 AND (f.n_published > 0 OR f.n_published_deployments > 0);

  -- 7. dangling registry rows: active nirspec_spec rows whose spectrum has no
  --    spectra row any more (deploy remove deletes spectra, not registry rows).
  SELECT jsonb_build_object(
    'count', COALESCE(sum(n), 0),
    'by_observation', COALESCE(jsonb_object_agg(COALESCE(observation, 'unknown'), n), '{}'::jsonb))
  INTO v_dangling
  FROM (
    SELECT so.observation, count(*) AS n
    FROM storage_objects so
    WHERE so.status = 'active'
      AND so.product_type = 'nirspec_spec'
      AND NOT EXISTS (SELECT 1 FROM spectra s WHERE s.spectrum_id = so.spectrum_id)
    GROUP BY so.observation
  ) x;

  -- 8. provisional etag: hashes by product_type.
  SELECT COALESCE(jsonb_object_agg(product_type, n), '{}'::jsonb)
  INTO v_etag
  FROM (
    SELECT so.product_type, count(*) AS n
    FROM storage_objects so
    WHERE so.status = 'active'
      AND so.content_hash LIKE 'etag:%'
      AND (so.backend = 'r2' OR so.product_type IN ('nirspec_spec', 'nircam_mosaic'))
    GROUP BY so.product_type
  ) x;

  -- 9. line-fit staleness from the ledger view.
  SELECT jsonb_build_object(
    'total', count(*),
    'stale_redshift', count(*) FILTER (WHERE stale_redshift),
    'stale_spectrum', count(*) FILTER (WHERE stale_spectrum))
  INTO v_lines
  FROM line_fits_status;

  -- 10. science hash coverage, only once the columns exist (P0b, PR #586).
  SELECT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'spectra' AND column_name = 'science_hash'),
         EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'storage_objects' AND column_name = 'science_hash')
  INTO v_has_sh_spectra, v_has_sh_storage;
  IF v_has_sh_spectra THEN
    EXECUTE 'SELECT count(*) FROM public.spectra WHERE deploy_status = ''published'' AND science_hash IS NULL'
      INTO v_sh_spectra;
  END IF;
  IF v_has_sh_storage THEN
    EXECUTE 'SELECT count(*) FROM public.storage_objects WHERE status = ''active'' AND product_type = ''nirspec_spec'' AND science_hash IS NULL'
      INTO v_sh_storage;
  END IF;

  RETURN jsonb_build_object(
    'generated_at', now(),
    'spectra_by_version', v_spectra->'spectra_by_version',
    'published', (v_spectra->'published') || jsonb_build_object('objects', v_objects->'published'),
    'drafts', jsonb_build_object(
      'spectra', v_spectra_status->'draft',
      'nircam_images', v_nircam_drafts,
      'deployments', v_deployments->'draft'),
    'revoked', jsonb_build_object(
      'spectra', v_spectra_status->'revoked',
      'deployments', v_deployments->'revoked'),
    'leases', v_leases,
    'releases', v_releases,
    'redrafted_nircam_fields', v_redrafted,
    'dangling_registry_rows', v_dangling,
    'etag_hash_rows', v_etag,
    'line_fits', v_lines,
    'needs_review', v_objects->'needs_review',
    'implicit_pins', v_objects->'implicit_pins',
    'science_hash_missing', jsonb_build_object(
      'spectra', to_jsonb(v_sh_spectra),
      'storage_objects', to_jsonb(v_sh_storage)));
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_csv_export_objects (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_photo_z_min           double precision DEFAULT NULL::double precision,
  p_photo_z_max           double precision DEFAULT NULL::double precision,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_after_object_id       text             DEFAULT NULL::text,
  p_page_size             integer          DEFAULT 5000
)
  RETURNS TABLE (
    object_id           text,
    field               text,
    ra                  double precision,
    "dec"               double precision,
    redshift            numeric,
    redshift_quality    integer,
    redshift_inspected  numeric,
    redshift_auto       double precision,
    last_inspected_at   timestamp with time zone,
    last_inspected_by   text,
    last_data_change_at timestamp with time zone,
    staleness_reason    text,
    needs_review        boolean,
    version             integer,
    n_targets           integer,
    n_spectra           integer,
    programs            text,
    gratings            text,
    max_snr             double precision,
    max_exposure_time   double precision,
    member_target_ids   text,
    distance            double precision,
    lists               text,
    has_photometry      boolean,
    photo_z             double precision,
    photo_z_err_lo      double precision,
    photo_z_err_hi      double precision,
    photometry          jsonb,
    line_snr            double precision,
    band_mag            double precision,
    band_snr            double precision
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET jit TO 'off'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_line_object_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_gratings_mode TEXT;
  v_grating_object_ids INTEGER[];
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (
    p_comment_search IS NOT NULL
    AND p_comment_search != ''
    AND p_comment_search_scope IN ('just_me', 'everyone')
  );
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_gratings_mode := COALESCE(p_gratings_mode, 'any');
  IF v_gratings_mode NOT IN ('any', 'all', 'none') THEN v_gratings_mode := 'any'; END IF;
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);

  -- Emission-line filter: the viewer-visible object set with a measurement of
  -- p_line in the S/N bounds, materialized once per call (see
  -- objects_matching_line_filter for the invariants).
  IF p_line IS NOT NULL THEN
    v_line_object_ids := ARRAY(SELECT public.objects_matching_line_filter(
      p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false),
      p_program_slugs, p_include_unpublished));
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
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;


  -- Issue #488: materialize the viewer-visible grating match set ONCE per call
  -- (statements below consume it via hashed = ANY; an IN-subplan would be
  -- rebuilt per statement — twice in the count+page RPC). See
  -- objects_matching_grating_filter().
  --
  -- Deliberately scoped to p_program_slugs, NOT v_filtered_program_slugs,
  -- matching get_filtered_objects_paginated's filter scoping so this export
  -- returns exactly the row set the catalog table shows for identical filter
  -- args. Narrowing the filter to v_filtered_program_slugs would make exports
  -- silently drop rows the table displays. The asymmetry a reader may notice
  -- — this RPC's exported aggregate columns (sa.*) ARE narrowed by
  -- p_filter_programs while the table's are not — predates the grating
  -- filter and is a display-scoping question, not a filter one.
  IF v_grating_filter_active THEN
    v_grating_object_ids := ARRAY(SELECT public.objects_matching_grating_filter(p_gratings, v_gratings_mode, p_program_slugs, p_include_unpublished));
  END IF;

  RETURN QUERY
  -- Issue #490: page-first evaluation. The previous shape ran the per-row
  -- laterals (object_scoped_aggregates, photometry) for every row that passed
  -- the cheap filters — before the LIMIT — and the member_targets /
  -- visible_lists CTEs aggregated over the whole catalog, so per-page cost
  -- scaled with catalog size instead of page size. Select the page of ids
  -- first (cheap objects-only filters + keyset + LIMIT), then join the
  -- expensive work against just that page. MATERIALIZED fences the page so
  -- the planner can't push the laterals back under the LIMIT.
  WITH page_objects AS MATERIALIZED (
    SELECT o.id, o.object_id, o.field, o.ra, o.dec,
      o.redshift, o.redshift_quality,
      o.redshift_inspected, o.redshift_auto,
      o.last_inspected_at, o.last_inspected_by,
      o.last_data_change_at, o.staleness_reason, o.needs_review, o.version,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      o.has_photometry, o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi
    FROM public.cat_objects o
    WHERE o.programs && v_filtered_program_slugs
      AND (p_after_object_id IS NULL OR o.object_id > p_after_object_id)
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR o.field = ANY(p_fields))
      AND (
        NOT v_grating_filter_active
        -- Issue #488: the o.gratings array tests are index-backed pre-filters
        -- only (deploy-time aggregate over ALL member spectra, unpublished and
        -- inaccessible programs included); the viewer-visible decision is the
        -- hashed = ANY over the once-per-call v_grating_object_ids — see
        -- objects_matching_grating_filter() for the invariants.
        OR (v_gratings_mode = 'any' AND o.gratings && p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'all' AND o.gratings @> p_gratings
            AND o.id = ANY(v_grating_object_ids))
        OR (v_gratings_mode = 'none' AND (NOT o.gratings && p_gratings
            OR NOT (o.id = ANY(v_grating_object_ids))))
      )
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min)
      AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR o.max_snr >= p_max_snr_min)
      AND (p_max_snr_max IS NULL OR o.max_snr <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR o.max_exposure_time >= p_max_exposure_time_min)
      AND (p_max_exposure_time_max IS NULL OR o.max_exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR o.id = ANY(v_line_object_ids))
      AND (p_band IS NULL OR o.id = ANY(v_band_object_ids))
      AND (p_search IS NULL OR o.id IN (SELECT __o.id FROM public.cat_objects __o WHERE __o.search_text ILIKE '%' || p_search || '%'))
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND o.redshift_quality = 0))
      AND (p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review)
      AND (NOT v_coord_search_active OR (
        -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
        -- every RA, so the RA bound is dropped there (the box is only the index
        -- pre-filter; the Haversine cut below decides membership either way).
        (ABS(p_coord_dec) + p_radius_degrees >= 90
         OR o.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                    AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
        AND o.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)
        -- Exact haversine cut lives inside the page selection (it used to be a
        -- post-CTE distance_filtered pass) so the LIMIT counts only surviving
        -- rows and the keyset cursor stays correct.
        AND 2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(o.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(o.dec)) * POWER(SIN(RADIANS(o.ra - p_coord_ra) / 2), 2)))) <= p_radius_degrees
      ))
      AND (
        NOT v_list_filter_active
        OR (v_list_ids_mode = 'any' AND o.id IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
        OR (v_list_ids_mode = 'all' AND (
            SELECT COUNT(DISTINCT olm.list_id) FROM object_list_members olm
            WHERE olm.object_id = o.id AND olm.list_id = ANY(p_list_ids)
        ) = (SELECT COUNT(DISTINCT __list_id) FROM unnest(p_list_ids) __list_id))
        OR (v_list_ids_mode = 'none' AND o.id NOT IN (
            SELECT olm.object_id FROM object_list_members olm
            WHERE olm.list_id = ANY(p_list_ids) AND olm.object_id IS NOT NULL
        ))
      )
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (p_photo_z_min IS NULL OR o.photo_z >= p_photo_z_min)
      AND (p_photo_z_max IS NULL OR o.photo_z <= p_photo_z_max)
      AND (
        NOT v_comment_search_active
        -- Uncorrelated semijoin; see get_filtered_objects_paginated for rationale.
        OR o.id IN (
          SELECT c.object_id FROM comments c
          WHERE c.object_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
          UNION
          SELECT t.object_id FROM comments c
          JOIN public.cat_targets t ON t.id = c.target_id
          WHERE c.target_id IS NOT NULL
            AND c.is_deleted = false
            AND c.content ILIKE '%' || p_comment_search || '%'
            AND (
              p_comment_search_scope = 'everyone'
              OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id)
            )
        )
      )
    ORDER BY o.object_id ASC
    LIMIT v_page_size
  ),
  -- Both aggregation CTEs are restricted to the page's ids — previously they
  -- aggregated targets / list memberships for the entire catalog on every page.
  member_targets AS (
    SELECT t.object_id, string_agg(t.target_id, ';' ORDER BY t.target_id) AS member_target_ids
    FROM public.cat_targets t
    WHERE t.object_id IN (SELECT po.id FROM page_objects po)
      AND t.program_slug = ANY(v_filtered_program_slugs)
    GROUP BY t.object_id
  ),
  visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE olm.object_id IN (SELECT po.id FROM page_objects po)
      AND (ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
           OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid()))
    GROUP BY olm.object_id
  )
  SELECT po.object_id, po.field, po.ra, po.dec,
    po.redshift, po.redshift_quality,
    po.redshift_inspected, po.redshift_auto,
    po.last_inspected_at, up.full_name AS last_inspected_by,
    po.last_data_change_at, po.staleness_reason, po.needs_review, po.version,
    -- Aggregates scoped to accessible (+ filtered) programs so mixed-program
    -- objects don't export proprietary member metadata. See
    -- object_scoped_aggregates().
    sa.n_targets, sa.n_spectra,
    array_to_string(sa.programs, ';') AS programs,
    array_to_string(sa.gratings, ';') AS gratings,
    sa.max_snr, sa.max_exposure_time,
    mt.member_target_ids, po.distance, vl.lists,
    po.has_photometry, po.photo_z, po.photo_z_err_lo, po.photo_z_err_hi,
    phot.photometry,
    lv.snr AS line_snr,
    bv.mag AS band_mag,
    bv.snr AS band_snr
  FROM page_objects po
  -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
  LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = po.id
  -- line values ('Line S/N' column + line_snr sort key): one caller-scoped scan per statement, hash-joined; NULLs when no line filter is active (#619)
  LEFT JOIN public.object_line_values(p_line, p_line_snr_min, p_line_snr_max, COALESCE(p_line_include_stale, false), p_program_slugs, p_include_unpublished) lv ON lv.object_id = po.id
  LEFT JOIN member_targets mt ON mt.object_id = po.id
  LEFT JOIN visible_lists vl ON vl.object_id = po.id
  LEFT JOIN user_profiles up ON up.user_id = po.last_inspected_by
  LEFT JOIN LATERAL public.object_scoped_aggregates(po.id, v_filtered_program_slugs, p_include_unpublished) sa ON true
  LEFT JOIN LATERAL (
    SELECT op.photometry FROM public.cat_catalog_matches op
    WHERE op.object_id = po.id ORDER BY op.updated_at DESC LIMIT 1
  ) phot ON true
  ORDER BY po.object_id ASC;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_csv_export_spectra (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any  integer          DEFAULT NULL::integer,
  p_dq_flags_include_all  integer          DEFAULT NULL::integer,
  p_dq_flags_exclude      integer          DEFAULT NULL::integer,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_after_id              integer          DEFAULT NULL::integer,
  p_page_size             integer          DEFAULT 5000
)
  RETURNS TABLE (
    id                integer,
    spectrum_id       text,
    target_id         text,
    grating           text,
    field             text,
    observation       text,
    ra                double precision,
    "dec"             double precision,
    redshift          numeric,
    redshift_quality  integer,
    redshift_auto     double precision,
    signal_to_noise   double precision,
    exposure_time     double precision,
    fits_path         text,
    program_slug      text,
    program_name      text,
    last_inspected_at timestamp with time zone,
    last_inspected_by text,
    distance          double precision,
    dq_flags          integer,
    lists             text,
    line_snr          double precision,
    band_mag          double precision,
    band_snr          double precision
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
DECLARE
  v_line_spectrum_ids INTEGER[];
  v_band_object_ids INTEGER[];
  v_filtered_program_slugs TEXT[];
  v_coord_search_active BOOLEAN;
  v_comment_search_active BOOLEAN;
  v_grating_filter_active BOOLEAN;
  v_list_filter_active BOOLEAN;
  v_list_ids_mode TEXT;
  v_page_size INTEGER;
BEGIN
  v_coord_search_active := (p_coord_ra IS NOT NULL AND p_coord_dec IS NOT NULL AND p_radius_degrees IS NOT NULL);
  v_comment_search_active := (p_comment_search IS NOT NULL AND p_comment_search != '' AND p_comment_search_scope IN ('just_me', 'everyone'));
  v_grating_filter_active := (p_gratings IS NOT NULL AND array_length(p_gratings, 1) > 0);
  v_list_filter_active := (p_list_ids IS NOT NULL AND array_length(p_list_ids, 1) > 0);
  v_list_ids_mode := COALESCE(p_list_ids_mode, 'any');
  IF v_list_ids_mode NOT IN ('any', 'all', 'none') THEN v_list_ids_mode := 'any'; END IF;
  v_page_size := LEAST(GREATEST(COALESCE(p_page_size, 5000), 1), 10000);
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
    SELECT ARRAY(SELECT unnest(p_program_slugs) INTERSECT SELECT unnest(p_filter_programs)) INTO v_filtered_program_slugs;
  ELSE v_filtered_program_slugs := p_program_slugs; END IF;
  IF v_filtered_program_slugs IS NULL OR array_length(v_filtered_program_slugs, 1) IS NULL THEN RETURN; END IF;

  RETURN QUERY
  WITH visible_lists AS (
    SELECT olm.object_id, string_agg(ol.slug, ';' ORDER BY ol.slug) AS lists
    FROM object_list_members olm
    JOIN object_lists ol ON ol.id = olm.list_id
    WHERE ol.created_by = auth.uid() OR ol.visibility IN ('public_read', 'public_edit')
       OR ol.id IN (SELECT list_id FROM object_list_shares WHERE user_id = auth.uid())
    GROUP BY olm.object_id
  ),
  filtered_spectra AS (
    SELECT s.id, s.spectrum_id, t.target_id, s.grating, t.field, t.ra, t.dec,
      o.redshift, o.redshift_quality,
      s.redshift_auto,
      s.signal_to_noise, s.exposure_time, s.fits_path, t.program_slug, t.observation,
      o.last_inspected_at, o.last_inspected_by,
      CASE WHEN v_coord_search_active THEN
        2 * DEGREES(ASIN(SQRT(POWER(SIN(RADIANS(t.dec - p_coord_dec) / 2), 2) + COS(RADIANS(p_coord_dec)) * COS(RADIANS(t.dec)) * POWER(SIN(RADIANS(t.ra - p_coord_ra) / 2), 2))))
      ELSE NULL END AS distance,
      COALESCE(s.dq_flags, 0) AS dq_flags,
      vl.lists,
      CASE WHEN p_line IS NOT NULL THEN
        (SELECT __l.snr FROM public.cat_lines __l WHERE __l.spectrum_id = s.id AND __l.line = p_line)
      END AS line_snr,
      bv.mag AS band_mag,
      bv.snr AS band_snr
    FROM public.cat_targets t
    JOIN public.cat_spectra s ON s.target_id = t.target_id
    LEFT JOIN public.cat_objects o ON o.id = t.object_id
    LEFT JOIN visible_lists vl ON vl.object_id = t.object_id
    -- band values (Mag / Band S/N columns + sort keys): one caller-scoped scan per statement, hash-joined; NULLs when no band filter is active
    LEFT JOIN public.object_band_values(p_band, p_band_mag_min, p_band_mag_max, p_band_snr_min, p_band_snr_max, p_program_slugs, p_include_unpublished) bv ON bv.object_id = t.object_id
    WHERE t.program_slug = ANY(v_filtered_program_slugs)
      AND (p_after_id IS NULL OR s.id > p_after_id)
      AND (o.id IS NULL OR o.is_active = true)
      AND (NOT v_grating_filter_active OR s.grating = ANY(p_gratings))
      -- B1: hide unpublished spectra (fail-closed; admin opt-in only).
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_fields IS NULL OR array_length(p_fields, 1) IS NULL OR t.field = ANY(p_fields))
      AND (p_observations IS NULL OR array_length(p_observations, 1) IS NULL OR t.observation = ANY(p_observations))
      AND (p_redshift_quality IS NULL OR array_length(p_redshift_quality, 1) IS NULL OR o.redshift_quality = ANY(p_redshift_quality))
      AND (p_redshift_min IS NULL OR o.redshift >= p_redshift_min) AND (p_redshift_max IS NULL OR o.redshift <= p_redshift_max)
      AND (p_max_snr_min IS NULL OR s.signal_to_noise >= p_max_snr_min) AND (p_max_snr_max IS NULL OR s.signal_to_noise <= p_max_snr_max)
      AND (p_max_exposure_time_min IS NULL OR s.exposure_time >= p_max_exposure_time_min) AND (p_max_exposure_time_max IS NULL OR s.exposure_time <= p_max_exposure_time_max)
      AND (p_line IS NULL OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR t.object_id = ANY(v_band_object_ids))
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
      AND (p_inspected_only IS NULL OR (p_inspected_only = TRUE AND o.redshift_quality > 0) OR (p_inspected_only = FALSE AND COALESCE(o.redshift_quality, 0) = 0))
      AND (p_needs_review IS NULL
        -- P3: the maintained badge flag (docs/design-needs-review.md), indexed
        OR o.needs_review = p_needs_review)
      AND (p_has_photometry IS NULL OR o.has_photometry = p_has_photometry)
      AND (NOT v_comment_search_active OR t.id IN (
        SELECT c.target_id FROM comments c WHERE c.target_id IS NOT NULL AND c.is_deleted = false
          AND c.content ILIKE '%' || p_comment_search || '%'
          AND (p_comment_search_scope = 'everyone' OR (p_comment_search_scope = 'just_me' AND c.user_id = p_comment_user_id))))
      AND (NOT v_coord_search_active OR (
        -- #527: RA box widened by 1/cos(dec); a cone that reaches a pole spans
        -- every RA, so the RA bound is dropped there (the box is only the index
        -- pre-filter; the Haversine cut below decides membership either way).
        (ABS(p_coord_dec) + p_radius_degrees >= 90
         OR t.ra BETWEEN (p_coord_ra - p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6))
                    AND (p_coord_ra + p_radius_degrees / GREATEST(COS(RADIANS(p_coord_dec)), 1e-6)))
        AND t.dec BETWEEN (p_coord_dec - p_radius_degrees) AND (p_coord_dec + p_radius_degrees)))
  ),
  distance_filtered AS (SELECT fs.* FROM filtered_spectra fs WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees)
  SELECT df.id, df.spectrum_id, df.target_id, df.grating, df.field, df.observation,
    df.ra, df.dec, df.redshift, df.redshift_quality, df.redshift_auto,
    df.signal_to_noise, df.exposure_time, df.fits_path, df.program_slug,
    pr.program_name, df.last_inspected_at, up.full_name AS last_inspected_by,
    df.distance, df.dq_flags, df.lists, df.line_snr, df.band_mag, df.band_snr
  FROM distance_filtered df
  LEFT JOIN programs pr ON pr.slug = df.program_slug
  LEFT JOIN user_profiles up ON up.user_id = df.last_inspected_by
  ORDER BY df.id ASC
  LIMIT v_page_size;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_filtered_spectra_paginated (
  p_program_slugs         text[],
  p_filter_programs       text[]           DEFAULT NULL::text[],
  p_fields                text[]           DEFAULT NULL::text[],
  p_gratings              text[]           DEFAULT NULL::text[],
  p_gratings_mode         text             DEFAULT 'any'::text,
  p_observations          text[]           DEFAULT NULL::text[],
  p_redshift_quality      integer[]        DEFAULT NULL::integer[],
  p_redshift_min          double precision DEFAULT NULL::double precision,
  p_redshift_max          double precision DEFAULT NULL::double precision,
  p_max_snr_min           double precision DEFAULT NULL::double precision,
  p_max_snr_max           double precision DEFAULT NULL::double precision,
  p_max_exposure_time_min double precision DEFAULT NULL::double precision,
  p_max_exposure_time_max double precision DEFAULT NULL::double precision,
  p_dq_flags_include_any  integer          DEFAULT NULL::integer,
  p_dq_flags_include_all  integer          DEFAULT NULL::integer,
  p_dq_flags_exclude      integer          DEFAULT NULL::integer,
  p_list_ids              integer[]        DEFAULT NULL::integer[],
  p_list_ids_mode         text             DEFAULT 'any'::text,
  p_search                text             DEFAULT NULL::text,
  p_inspected_only        boolean          DEFAULT NULL::boolean,
  p_needs_review          boolean          DEFAULT NULL::boolean,
  p_has_photometry        boolean          DEFAULT NULL::boolean,
  p_comment_search        text             DEFAULT NULL::text,
  p_comment_search_scope  text             DEFAULT NULL::text,
  p_comment_user_id       uuid             DEFAULT NULL::uuid,
  p_coord_ra              double precision DEFAULT NULL::double precision,
  p_coord_dec             double precision DEFAULT NULL::double precision,
  p_radius_degrees        double precision DEFAULT NULL::double precision,
  p_sort_column           text             DEFAULT 'target_id'::text,
  p_sort_direction        text             DEFAULT 'asc'::text,
  p_page                  integer          DEFAULT 1,
  p_page_size             integer          DEFAULT 50,
  p_include_thumbnails    boolean          DEFAULT false,
  p_include_unpublished   boolean          DEFAULT false,
  p_line                  text             DEFAULT NULL::text,
  p_line_snr_min          double precision DEFAULT NULL::double precision,
  p_line_snr_max          double precision DEFAULT NULL::double precision,
  p_line_include_stale    boolean          DEFAULT false,
  p_band                  text             DEFAULT NULL::text,
  p_band_mag_min          double precision DEFAULT NULL::double precision,
  p_band_mag_max          double precision DEFAULT NULL::double precision,
  p_band_snr_min          double precision DEFAULT NULL::double precision,
  p_band_snr_max          double precision DEFAULT NULL::double precision,
  p_include_count         boolean          DEFAULT true,
  p_after_sort_text       text             DEFAULT NULL::text,
  p_after_sort_num        double precision DEFAULT NULL::double precision,
  p_after_tiebreak        text[]           DEFAULT NULL::text[]
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
    OR (p_sort_column IN ('band_mag', 'band_snr') AND p_band IS NOT NULL)) THEN
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
      AND (p_line IS NULL OR s.id = ANY(v_line_spectrum_ids))
      AND (p_band IS NULL OR t.object_id = ANY(v_band_object_ids))
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
        NOT v_coord_search_active
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
    WHERE NOT v_coord_search_active OR fs.distance <= p_radius_degrees
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
      'created_at', r.created_at,
      'updated_at', r.updated_at,
      'distance', CASE WHEN v_coord_search_active THEN r.distance ELSE NULL END,
      'spectra', jsonb_build_array(jsonb_build_object(
        'id', r.spectrum_pk,
        'spectrum_id', r.spectrum_id,
        'target_id', r.target_id,
        'grating', r.grating,
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

CREATE OR REPLACE FUNCTION public.get_line_fits_for_sync (
  p_program_slugs       text[],
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_unpublished boolean                  DEFAULT false,
  p_include_counts      boolean                  DEFAULT true,
  p_after_id            integer                  DEFAULT NULL::integer
)
  RETURNS TABLE (
    line_fit_records jsonb,
    total_count      bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  WITH matched AS (
    SELECT f.spectrum_id, f.target_id, f.grating, f.program_slug, f.observation,
           s.spectrum_id AS spectrum_name, t.field, o.object_id AS current_object_id,
           f.z_used, f.z_source, f.z_quality, f.object_id, f.object_version,
           f.z_fit, f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source,
           f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof, f.lines,
           f.fit_version, f.cfpipe_version, f.f_lsf, f.spectrum_hash, f.fitted_at,
           f.created_at, f.updated_at,
           -- The record's sync timestamp: the latest of the fit row and the two
           -- parent rows its staleness flags derive from, so the client's
           -- incremental cursor (max local updated_at) advances past a
           -- re-inspection or redeploy instead of re-sending the row forever.
           GREATEST(f.updated_at, s.updated_at, o.updated_at) AS effective_updated_at,
           -- staleness vs the live inspection state: the same two functions
           -- the line_fits_status view calls, so the mirrored record
           -- and the ledger cannot disagree.
           (o.id IS NOT NULL AND public.line_fit_stale_redshift(
              o.version, o.redshift_quality, (o.redshift)::double precision,
              f.object_version, f.z_quality, f.z_used)) AS stale_redshift,
           public.line_fit_stale_spectrum(
              s.file_hash, s.science_hash,
              f.spectrum_hash, f.spectrum_science_hash) AS stale_spectrum
    FROM line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE f.program_slug = ANY(p_program_slugs)
      -- Hide fits whose parent object was soft-deleted (same guard as
      -- get_spectra_for_sync), so the line catalog never carries a row the
      -- spectra / objects streams hide.
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      -- Incremental: stale_redshift / stale_spectrum are derived from the
      -- parent rows, so a re-inspected object or a redeployed spectrum must
      -- re-send the fit even though the fit row itself did not change.
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      AND (p_after_id IS NULL OR f.spectrum_id > p_after_id)
    ORDER BY f.spectrum_id
    LIMIT p_limit
  ),
  total AS (
    SELECT COUNT(*) AS cnt
    FROM line_fits f
    JOIN spectra s ON s.id = f.spectrum_id
    LEFT JOIN targets t ON t.target_id = f.target_id
    LEFT JOIN objects o ON o.id = t.object_id
    WHERE p_include_counts
      AND f.program_slug = ANY(p_program_slugs)
      AND (o.id IS NULL OR o.is_active = true)
      AND (p_include_unpublished OR s.deploy_status = 'published')
      AND (p_updated_since IS NULL
           OR f.updated_at > p_updated_since
           OR s.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'spectrum_id', m.spectrum_id,
        'spectrum_name', m.spectrum_name,
        'target_id', m.target_id,
        'grating', m.grating,
        'program_slug', m.program_slug,
        'observation', m.observation,
        'field', m.field,
        'current_object_id', m.current_object_id,
        'z_used', m.z_used,
        'z_source', m.z_source,
        'z_quality', m.z_quality,
        'object_id', m.object_id,
        'object_version', m.object_version,
        'z_fit', m.z_fit,
        'z_fit_err', m.z_fit_err,
        'dv', m.dv,
        'dv_err', m.dv_err,
        'sigma_v', m.sigma_v,
        'sigma_v_err', m.sigma_v_err,
        'kin_source', m.kin_source,
        'n_lines', m.n_lines,
        'n_detected', m.n_detected,
        'n_broad', m.n_broad,
        'chi2', m.chi2,
        'dof', m.dof,
        'lines', m.lines,
        'fit_version', m.fit_version,
        'cfpipe_version', m.cfpipe_version,
        'f_lsf', m.f_lsf,
        'spectrum_hash', m.spectrum_hash,
        'fitted_at', m.fitted_at,
        'stale_redshift', m.stale_redshift,
        'stale_spectrum', m.stale_spectrum,
        'created_at', m.created_at,
        'fit_updated_at', m.updated_at,
        'updated_at', m.effective_updated_at
      ) ORDER BY m.spectrum_id
    ), '[]'::jsonb) AS line_fit_records,
    (SELECT cnt FROM total) AS total_count
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_photometry_for_sync (
  p_program_slugs       text[],
  p_updated_since       timestamp with time zone DEFAULT NULL::timestamp WITH time zone,
  p_limit               integer                  DEFAULT 1000,
  p_include_unpublished boolean                  DEFAULT false,
  p_include_counts      boolean                  DEFAULT true,
  p_after_id            integer                  DEFAULT NULL::integer
)
  RETURNS TABLE (
    photometry_records jsonb,
    total_count        bigint
  )
  LANGUAGE plpgsql
  STABLE
  SET plan_cache_mode TO 'force_custom_plan'
  SET statement_timeout TO '120s'
  AS $function$
BEGIN
  PERFORM public.dr_assert_live();  -- live-only (P6b-2)
  RETURN QUERY
  WITH matched AS (
    SELECT op.id, o.object_id, op.field, op.catalog_name, op.catalog_id,
           op.match_distance_arcsec, op.photometry, op.photo_z,
           op.photo_z_err_lo, op.photo_z_err_hi, op.has_pz,
           op.created_at,
           -- Sync timestamp: the later of the row's own and its object's, so a
           -- row re-sent because the object changed advances the client's
           -- cursor (see get_spectra_for_sync).
           GREATEST(op.updated_at, o.updated_at) AS updated_at
    FROM catalog_matches op
    JOIN objects o ON o.id = op.object_id
    WHERE o.programs && p_program_slugs
      -- Soft-deleted objects are tombstoned by the objects stream (which
      -- cascades to their photometry locally); their rows must not ride the
      -- object-changed clause below back in.
      AND o.is_active = true
      -- B1: fail-closed publish gate (this RPC always bypasses RLS).
      AND (p_include_unpublished OR o.has_published_spectrum)
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: photometry deleted along
      -- with a tombstoned object (un-published, or soft-deleted) comes back
      -- when the object does -- both flips stamp objects.updated_at -- even
      -- though the photometry row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR op.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
      -- Keyset (#103): op.id is the PK, so a strict > needs no tiebreaker.
      AND (p_after_id IS NULL OR op.id > p_after_id)
    ORDER BY op.id
    LIMIT p_limit
  ),
  -- Count CTE gated on p_include_counts; when FALSE the planner collapses it to
  -- One-Time Filter: false and skips the scan/join.
  total AS (
    SELECT COUNT(*) AS cnt
    FROM catalog_matches op
    JOIN objects o ON o.id = op.object_id
    WHERE p_include_counts
      AND o.programs && p_program_slugs
      AND o.is_active = true
      AND (p_include_unpublished OR o.has_published_spectrum)
      -- Incremental: the row itself, or its object, changed since the cursor.
      -- The object clause is the resurrection path: photometry deleted along
      -- with a tombstoned object (un-published, or soft-deleted) comes back
      -- when the object does -- both flips stamp objects.updated_at -- even
      -- though the photometry row did not change. Mirrors get_line_fits_for_sync.
      AND (p_updated_since IS NULL
           OR op.updated_at > p_updated_since
           OR o.updated_at > p_updated_since)
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'object_id', m.object_id,
        'field', m.field,
        'catalog_name', m.catalog_name,
        'catalog_id', m.catalog_id,
        'match_distance_arcsec', m.match_distance_arcsec,
        'photometry', m.photometry,
        'photo_z', m.photo_z,
        'photo_z_err_lo', m.photo_z_err_lo,
        'photo_z_err_hi', m.photo_z_err_hi,
        'has_pz', m.has_pz,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
      -- Keyset (#103): page array must be id-ordered (client cursors on the
      -- last element).
      ORDER BY m.id
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT
  FROM matched m;
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_sync_deletions (
  p_since timestamp with time zone
)
  RETURNS TABLE (
    stream  text,
    row_ids bigint[]
  )
  LANGUAGE sql
  STABLE
  SET search_path TO 'public', 'pg_catalog'
  AS $function$
  SELECT public.dr_assert_live();  -- live-only (P6b-2)
  SELECT d.stream, array_agg(DISTINCT d.row_id ORDER BY d.row_id)
  FROM public.sync_deletions d
  WHERE d.deleted_at > p_since
    AND NOT CASE d.stream
      WHEN 'objects' THEN EXISTS (SELECT 1 FROM public.objects o WHERE o.id = d.row_id)
      WHEN 'spectra' THEN EXISTS (SELECT 1 FROM public.spectra s WHERE s.id = d.row_id)
      WHEN 'storage' THEN EXISTS (SELECT 1 FROM public.storage_objects so WHERE so.id = d.row_id)
      WHEN 'photometry' THEN EXISTS (SELECT 1 FROM public.catalog_matches p WHERE p.id = d.row_id)
      WHEN 'line_fits' THEN EXISTS (SELECT 1 FROM public.line_fits f WHERE f.spectrum_id = d.row_id)
    END
  GROUP BY d.stream;
$function$;

CREATE OR REPLACE FUNCTION public.object_band_values (
  p_band                text,
  p_mag_min             double precision,
  p_mag_max             double precision,
  p_snr_min             double precision,
  p_snr_max             double precision,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false
)
  RETURNS TABLE (
    object_id integer,
    mag       double precision,
    snr       double precision
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  ROWS 20000
  SET search_path TO 'public'
  AS $function$
  SELECT b.object_id, min(b.mag), max(b.snr)
  FROM public.cat_photometry b
  -- phase 2a: the band row carries its object; no cross-match hop
  JOIN public.cat_objects o ON o.id = b.object_id
  WHERE p_band IS NOT NULL
    AND b.band = p_band
    AND b.object_id IS NOT NULL
    -- Gate 1: the caller's access-checked scope (what the RPCs apply themselves).
    AND o.programs && p_program_slugs
    AND (p_include_unpublished OR o.has_published_spectrum)
    -- Gate 2: select_objects_by_access (policies.sql; its is_public
    -- short-circuit is a planner aid with the same row set) for every
    -- caller except the service role (the JWT role claim, the same test the
    -- deploy-side RPCs use; NOT auth.uid() IS NULL, which an `authenticated`
    -- request without a subject would also satisfy).
    AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
      o.programs && (SELECT public.accessible_program_slugs())
      AND (o.has_published_spectrum OR (SELECT public.is_admin())
           OR (SELECT public.link_sees_drafts()))
      AND (o.is_active OR (SELECT public.is_admin()))
      AND ((SELECT NOT public.is_link_account())
           OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
      -- (P6b-2) the frozen policies' extra conjuncts when a release is
      -- browsed: no link accounts, and the tag visible to the caller
      AND (public.dr_browse_tag() IS NULL
           OR ((SELECT NOT public.is_link_account())
               AND public.dr_browse_tag() = ANY ((SELECT public.dr_visible_tags())::text[])))
    ))
  GROUP BY b.object_id
  HAVING (p_mag_min IS NULL OR min(b.mag) >= p_mag_min)
     AND (p_mag_max IS NULL OR min(b.mag) <= p_mag_max)
     AND (p_snr_min IS NULL OR max(b.snr) >= p_snr_min)
     AND (p_snr_max IS NULL OR max(b.snr) <= p_snr_max);
$function$;

CREATE OR REPLACE FUNCTION public.object_line_values (
  p_line                text,
  p_snr_min             double precision,
  p_snr_max             double precision,
  p_include_stale       boolean,
  p_program_slugs       text[],
  p_include_unpublished boolean          DEFAULT false
)
  RETURNS TABLE (
    object_id integer,
    snr       double precision
  )
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  ROWS 2000
  SET search_path TO 'public'
  AS $function$
  SELECT l.object_id, max(l.snr)
  FROM public.cat_lines l
  -- unique-key lookups, pinned with LIMIT 1 (see the plan-shape note above)
  CROSS JOIN LATERAL (
    SELECT f.program_slug, f.stale_redshift
    FROM public.cat_line_fits f WHERE f.spectrum_id = l.spectrum_id LIMIT 1
  ) f
  CROSS JOIN LATERAL (
    SELECT s.program_slug, s.observation, s.deploy_status
    FROM public.cat_spectra s WHERE s.id = l.spectrum_id LIMIT 1
  ) s
  -- phase 2a: the line row carries its object; no target hop, and staleness
  -- is a stored column of the fit, not a join clause between fit and object
  CROSS JOIN LATERAL (
    SELECT o.programs, o.has_published_spectrum, o.observations, o.is_active
    FROM public.cat_objects o WHERE o.id = l.object_id LIMIT 1
  ) o
  WHERE p_line IS NOT NULL
    AND l.line = p_line
    AND l.snr IS NOT NULL
    AND l.object_id IS NOT NULL
    -- Gate 1: the caller's access-checked scope (what the RPCs apply themselves).
    AND f.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR s.deploy_status = 'published')
    AND (p_include_stale OR NOT f.stale_redshift)
    -- Gate 2a: verbatim select_spectra_by_access (policies.sql) for every
    -- caller except the service role (the JWT role claim, the same test the
    -- deploy-side RPCs use; NOT auth.uid() IS NULL, which an `authenticated`
    -- request without a subject would also satisfy).
    AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
      s.program_slug = ANY((SELECT public.accessible_program_slugs())::text[])
      AND ((SELECT NOT public.is_link_account())
           OR s.observation = (SELECT public.link_observation()))
      AND (s.deploy_status = 'published' OR (SELECT public.is_admin())
           OR (s.deploy_status = 'draft' AND (SELECT public.link_sees_drafts())))
    ))
    -- Gate 2b: select_objects_by_access (policies.sql), same callers. The
    -- policy's `(NOT link AND is_public) OR ...` short-circuit is a planner aid
    -- with the same row set; the scope here is the explicit array, so the
    -- plain `programs && scope` form is kept.
    -- A separate conjunct, not `svc OR (s... AND o...)`: a predicate naming
    -- both s and o would be a join clause between them again.
    AND (COALESCE((SELECT auth.role()), '') = 'service_role' OR (
      o.programs && (SELECT public.accessible_program_slugs())
      AND (o.has_published_spectrum OR (SELECT public.is_admin())
           OR (SELECT public.link_sees_drafts()))
      AND (o.is_active OR (SELECT public.is_admin()))
      AND ((SELECT NOT public.is_link_account())
           OR o.observations && ARRAY[(SELECT public.link_observation())]::text[])
      -- (P6b-2) the frozen policies' extra conjuncts when a release is
      -- browsed: no link accounts, and the tag visible to the caller
      AND (public.dr_browse_tag() IS NULL
           OR ((SELECT NOT public.is_link_account())
               AND public.dr_browse_tag() = ANY ((SELECT public.dr_visible_tags())::text[])))
    ))
  GROUP BY l.object_id
  HAVING (p_snr_min IS NULL OR max(l.snr) >= p_snr_min)
     AND (p_snr_max IS NULL OR max(l.snr) <= p_snr_max);
$function$;

CREATE OR REPLACE FUNCTION public.recompute_line_fit_stale (
  p_spectrum_ids integer[]
)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
DECLARE
  n integer;
BEGIN
  IF p_spectrum_ids IS NULL OR cardinality(p_spectrum_ids) = 0 THEN
    RETURN 0;
  END IF;
  -- serialize with an in-flight inspection of any of these objects (see the
  -- concurrency note above); the LEFT JOIN below cannot carry the lock itself
  PERFORM 1 FROM objects o
    WHERE o.id IN (SELECT t.object_id FROM spectra s JOIN targets t ON t.target_id = s.target_id
                    WHERE s.id = ANY(p_spectrum_ids))
    FOR SHARE;
  UPDATE line_fits f SET stale_redshift = v.stale
    FROM (SELECT f2.spectrum_id,
                 COALESCE(o.id IS NOT NULL AND public.line_fit_stale_redshift(
                   o.version, o.redshift_quality, (o.redshift)::double precision,
                   f2.object_version, f2.z_quality, f2.z_used), false) AS stale
            FROM line_fits f2
            JOIN spectra s ON s.id = f2.spectrum_id
            LEFT JOIN targets t ON t.target_id = s.target_id
            LEFT JOIN objects o ON o.id = t.object_id
           WHERE f2.spectrum_id = ANY(p_spectrum_ids)) v
   WHERE v.spectrum_id = f.spectrum_id
     AND f.stale_redshift IS DISTINCT FROM v.stale;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$;

CREATE OR REPLACE FUNCTION public.relink_lines_from_spectra()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  -- statement trigger on every spectra UPDATE: keep the rows whose target changed.
  -- Lock the destination targets first (the rule: every read that feeds a
  -- copied link holds FOR SHARE on the row it reads), so a concurrent regroup
  -- of the destination waits for this move or this move waits for it.
  PERFORM 1 FROM targets t
    WHERE t.target_id IN (SELECT n.target_id FROM new_rows n JOIN old_rows o ON o.id = n.id
                           WHERE o.target_id IS DISTINCT FROM n.target_id)
    FOR SHARE;
  UPDATE lines l SET object_id = t.object_id
    FROM new_rows n
    JOIN old_rows o ON o.id = n.id AND o.target_id IS DISTINCT FROM n.target_id
    JOIN targets t ON t.target_id = n.target_id
   WHERE l.spectrum_id = n.id
     AND l.object_id IS DISTINCT FROM t.object_id;
  PERFORM public.recompute_line_fit_stale(ARRAY(
    SELECT n.id FROM new_rows n JOIN old_rows o ON o.id = n.id
     WHERE o.target_id IS DISTINCT FROM n.target_id));
  RETURN NULL;
END;
$function$;

CREATE OR REPLACE FUNCTION public.relink_lines_from_targets()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  -- statement trigger on every targets UPDATE (a transition table cannot be
  -- combined with UPDATE OF): keep only the rows whose object changed
  CREATE TEMP TABLE IF NOT EXISTS _moved_targets (target_id text) ON COMMIT DROP;
  TRUNCATE _moved_targets;
  INSERT INTO _moved_targets
    SELECT n.target_id FROM new_rows n JOIN old_rows o ON o.id = n.id
     WHERE o.object_id IS DISTINCT FROM n.object_id;
  IF NOT EXISTS (SELECT 1 FROM _moved_targets) THEN RETURN NULL; END IF;
  UPDATE lines l SET object_id = t.object_id
    FROM spectra s
    JOIN _moved_targets m ON m.target_id = s.target_id
    JOIN targets t ON t.target_id = s.target_id
   WHERE l.spectrum_id = s.id
     AND l.object_id IS DISTINCT FROM t.object_id;
  PERFORM public.recompute_line_fit_stale(ARRAY(
    SELECT s.id FROM spectra s JOIN _moved_targets m ON m.target_id = s.target_id));
  RETURN NULL;
END;
$function$;

CREATE OR REPLACE FUNCTION public.relink_photometry_for_field (
  p_field text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  AS $function$
DECLARE
  n_relinked INTEGER := 0;
  n_orphaned INTEGER := 0;
  v_field_ra_min DOUBLE PRECISION;
  v_field_ra_max DOUBLE PRECISION;
  v_field_dec_min DOUBLE PRECISION;
  v_field_dec_max DOUBLE PRECISION;
  v_tolerance_deg DOUBLE PRECISION := 0.3 / 3600.0;  -- 0.3 arcsec in degrees
BEGIN
  -- Get bounding box of objects in this field (with padding)
  SELECT MIN(o.ra) - v_tolerance_deg, MAX(o.ra) + v_tolerance_deg,
         MIN(o.dec) - v_tolerance_deg, MAX(o.dec) + v_tolerance_deg
  INTO v_field_ra_min, v_field_ra_max, v_field_dec_min, v_field_dec_max
  FROM objects o WHERE o.field = p_field;

  IF v_field_ra_min IS NULL THEN
    RETURN jsonb_build_object('relinked', 0, 'orphaned', 0);
  END IF;

  -- Re-link: for each photometry row in this field,
  -- find the nearest object within 0.3 arcsec tolerance.
  WITH candidates AS (
    SELECT op.id AS phot_id,
           o.id AS obj_id,
           SQRT(
             POWER((op.ra - o.ra) * COS(RADIANS(op.dec)), 2) +
             POWER(op.dec - o.dec, 2)
           ) AS dist_deg,
           ROW_NUMBER() OVER (
             PARTITION BY op.id
             ORDER BY SQRT(
               POWER((op.ra - o.ra) * COS(RADIANS(op.dec)), 2) +
               POWER(op.dec - o.dec, 2)
             ) ASC
           ) AS rn
    FROM catalog_matches op
    CROSS JOIN LATERAL (
      SELECT o.id, o.ra, o.dec
      FROM objects o
      WHERE o.field = p_field
        AND o.ra BETWEEN op.ra - v_tolerance_deg AND op.ra + v_tolerance_deg
        AND o.dec BETWEEN op.dec - v_tolerance_deg AND op.dec + v_tolerance_deg
    ) o
    WHERE op.field = p_field
  ),
  best_match AS (
    SELECT phot_id, obj_id
    FROM candidates
    WHERE rn = 1 AND dist_deg <= v_tolerance_deg
  ),
  updated AS (
    UPDATE catalog_matches op
    SET object_id = bm.obj_id
    FROM best_match bm
    WHERE op.id = bm.phot_id
    RETURNING op.id
  )
  SELECT COUNT(*) INTO n_relinked FROM updated;

  -- Set NULL for unmatched rows in this field
  UPDATE catalog_matches
  SET object_id = NULL
  WHERE field = p_field
    AND id NOT IN (SELECT id FROM catalog_matches WHERE field = p_field AND object_id IS NOT NULL);

  -- Count orphaned
  SELECT COUNT(*) INTO n_orphaned
  FROM catalog_matches
  WHERE field = p_field AND object_id IS NULL;

  RETURN jsonb_build_object(
    'relinked', n_relinked,
    'orphaned', n_orphaned
  );
END;
$function$;

CREATE OR REPLACE FUNCTION public.relink_photometry_from_matches()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  UPDATE photometry b SET object_id = NEW.object_id
   WHERE b.match_id = NEW.id
     AND b.object_id IS DISTINCT FROM NEW.object_id;
  RETURN NULL;
END;
$function$;

CREATE OR REPLACE FUNCTION public.spectra_matching_line_filter (
  p_line          text,
  p_snr_min       double precision,
  p_snr_max       double precision,
  p_include_stale boolean
)
  RETURNS SETOF integer
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT l.spectrum_id
  FROM public.cat_lines l
  JOIN public.cat_line_fits f ON f.spectrum_id = l.spectrum_id
  WHERE l.line = p_line
    AND l.snr IS NOT NULL
    AND (p_snr_min IS NULL OR l.snr >= p_snr_min)
    AND (p_snr_max IS NULL OR l.snr <= p_snr_max)
    -- phase 2a: stored on the fit (false when the fit has no object, which
    -- is what the old `o.id IS NULL OR NOT stale(...)` form meant)
    AND (p_include_stale OR NOT f.stale_redshift);
$function$;

CREATE OR REPLACE FUNCTION public.sync_lines()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  DELETE FROM public.lines WHERE spectrum_id = NEW.spectrum_id;
  INSERT INTO public.lines (
    spectrum_id, line, component, wave_rest, flux, flux_err, snr,
    ew_rest, ew_rest_err, flags, blend_into, object_id
  )
  SELECT
    NEW.spectrum_id,
    e.key,
    COALESCE(e.value ->> 'component', 'narrow'),
    (e.value ->> 'wave_rest')::double precision,
    (e.value ->> 'flux')::double precision,
    (e.value ->> 'flux_err')::double precision,
    (e.value ->> 'snr')::double precision,
    (e.value ->> 'ew_rest')::double precision,
    (e.value ->> 'ew_rest_err')::double precision,
    COALESCE((e.value ->> 'flags')::integer, 0),
    e.value ->> 'blend_into',
    -- phase 2a: the object link, so a line row is addressable by object.
    -- FOR SHARE on the spectrum and target rows: a concurrent target regroup
    -- or spectrum move (UPDATE row locks) waits for this write, or this write
    -- waits for it; either way the link lands pointing at the committed object
    -- (see the concurrency note in the phase 2a block below).
    (SELECT t.object_id FROM public.spectra s
       JOIN public.targets t ON t.target_id = s.target_id
      WHERE s.id = NEW.spectrum_id
      FOR SHARE OF s, t)
  FROM jsonb_each(NEW.lines) AS e
  WHERE jsonb_typeof(e.value) = 'object';
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_photometry()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  DELETE FROM public.photometry WHERE match_id = NEW.id;
  INSERT INTO public.photometry (match_id, band, flux, flux_err, wav, object_id)
  SELECT
    NEW.id,
    e.key,
    (e.value ->> 'flux')::double precision,
    (e.value ->> 'flux_err')::double precision,
    (e.value ->> 'wav')::double precision,
    NEW.object_id   -- phase 2a: the object link, copied from the cross-match
  FROM jsonb_each(COALESCE(NEW.photometry -> 'bands', '{}'::jsonb)) AS e
  WHERE jsonb_typeof(e.value) = 'object';
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_photometry_to_objects (
  p_field text
)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  AS $function$
DECLARE
  n_updated INTEGER;
BEGIN
  -- Update objects that have linked photometry
  WITH phot AS (
    SELECT DISTINCT ON (op.object_id)
      op.object_id,
      op.photo_z,
      op.photo_z_err_lo,
      op.photo_z_err_hi
    FROM catalog_matches op
    WHERE op.field = p_field AND op.object_id IS NOT NULL
    ORDER BY op.object_id, op.updated_at DESC
  )
  UPDATE objects o
  SET photo_z = phot.photo_z,
      photo_z_err_lo = phot.photo_z_err_lo,
      photo_z_err_hi = phot.photo_z_err_hi,
      has_photometry = TRUE
  FROM phot
  WHERE o.id = phot.object_id;

  GET DIAGNOSTICS n_updated = ROW_COUNT;

  -- Clear photometry flags for objects in this field that have no linked photometry
  UPDATE objects o
  SET photo_z = NULL,
      photo_z_err_lo = NULL,
      photo_z_err_hi = NULL,
      has_photometry = FALSE
  WHERE o.field = p_field
    AND o.has_photometry = TRUE
    AND NOT EXISTS (
      SELECT 1 FROM catalog_matches op
      WHERE op.object_id = o.id
    );

  RETURN n_updated;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_spectra_target_scope()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
    SELECT t.program_slug, t.observation
      INTO NEW.program_slug, NEW.observation
      FROM targets t
     WHERE t.target_id = NEW.target_id
       FOR SHARE;  -- see the concurrency note above
    -- No parent row: both land NULL and the NOT NULL constraints reject the
    -- row (the FK spectra_target_id_fkey would too). No RAISE needed here.
    RETURN NEW;
END;
$function$;

CREATE VIEW "public"."cat_photometry" WITH (security_invoker=true) AS  SELECT match_id,
    band,
    flux,
    flux_err,
    wav,
    snr,
    mag,
    object_id
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            photometry.match_id,
            photometry.band,
            photometry.flux,
            photometry.flux_err,
            photometry.wav,
            photometry.snr,
            photometry.mag,
            photometry.object_id
           FROM public.photometry
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_photometry.tag AS cat_tag,
            dr_photometry.match_id,
            dr_photometry.band,
            dr_photometry.flux,
            dr_photometry.flux_err,
            dr_photometry.wav,
            dr_photometry.snr,
            dr_photometry.mag,
            dr_photometry.object_id
           FROM public.dr_photometry) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "anon";

COMMENT ON COLUMN "public"."catalog_matches"."photometry" IS 'Per-catalog photometry payload: {flux_unit: "uJy", bands: {<band>: {flux, flux_err [uJy], wav, wav_min, wav_max [um]}}}. Built by campfire.deploy.photometry.build_photometry_payload; band names come from the per-field [field.bands] block of photometry.toml. Fluxes are in microjansky ALWAYS (the reader converts), so AB magnitude is 23.9 - 2.5*log10(flux) and a non-positive flux simply has no magnitude. The `bands` map is unnested into photometry for indexed filter/sort — see that table.';

COMMENT ON COLUMN "public"."line_fits"."stale_redshift" IS 'Stored line_fit_stale_redshift(object, fit): the object''s inspected redshift / quality / version moved since this fit (Catalog Read Model phase 2a). Trigger-maintained: computed on every write of the row (compute_line_fit_stale) and recomputed when the object is inspected or regrouped (recompute_line_fit_stale); false when the fit has no object. The catalog filters read this column; line_fits_status still evaluates the rule live, and check_measurement_object_links.sql asserts the two agree.';

COMMENT ON COLUMN "public"."lines"."object_id" IS 'The object this measurement belongs to (Catalog Read Model phase 2a): targets.object_id through the row''s spectrum, trigger-maintained (set by sync_lines, re-pointed by relink_lines_from_targets / relink_lines_from_spectra when a target regroups or a spectrum moves; FK ON DELETE SET NULL when objects are rebuilt). What makes "object has line L above S/N x" one indexed EXISTS instead of a three-hop join.';

COMMENT ON COLUMN "public"."photometry"."flux" IS 'Band flux in microjansky, copied verbatim from the jsonb (catalog_matches.photometry.flux_unit is always "uJy").';

COMMENT ON COLUMN "public"."photometry"."object_id" IS 'The object of the parent cross-match (Catalog Read Model phase 2a): a copy of catalog_matches.object_id, set by sync_photometry and re-pointed by relink_photometry_from_matches when the parent is re-matched; FK ON DELETE SET NULL. Lets the band filter and the object page read one indexed table.';

COMMENT ON FUNCTION "public"."dr_freeze_job"(text) IS 'Data-release freeze (P4a, full-row copies since P6b): rewrites the frozen tables of an UNPUBLISHED tag (dr_spectra, dr_targets, dr_objects, dr_line_fits, dr_lines, dr_catalog_matches, dr_photometry, dr_nircam_images, dr_fields, dr_manifest; then dr_filter_options) in one statement (one snapshot) from the published live rows, then marks the row frozen with counts, snapshot xmin and duration; any error lands in state failed with the message. Runs as postgres from the one-shot pg_cron job dr_enqueue_freeze schedules, inline where pg_cron is absent, and from the seed. Idempotent per tag.';

COMMENT ON TABLE "public"."catalog_matches" IS 'Photometric catalog cross-matches for objects. One row per object per catalog. Coordinates (ra, dec) are the durable positional key; object_id FK is refreshed after each objects rebuild via coordinate cross-matching. Formerly object_photometry (renamed 2026-10-26, Catalog Read Model phase 2b).';

COMMENT ON TABLE "public"."dr_catalog_matches" IS 'Frozen copy of catalog_matches (every live column, verbatim) for the frozen objects of a data release in its photometry/<field>/<catalog> components. Access scope is the frozen object''s, like live.';

COMMENT ON TABLE "public"."dr_line_fits" IS 'Frozen copy of line_fits (every live column, verbatim) for the fits of a data release''s frozen spectra, with stale_redshift / stale_spectrum computed inside the freeze snapshot. spectrum_id = the live spectra.id at the freeze.';

COMMENT ON TABLE "public"."dr_lines" IS 'Frozen copy of lines (the unnested line fits the catalog filters on) for the fits of a data release.';

COMMENT ON TABLE "public"."dr_photometry" IS 'Frozen copy of photometry (the unnested photometry the catalog filters on) for the frozen photometry rows of a data release.';

COMMENT ON TABLE "public"."line_fits" IS 'Emission-line fluxes per spectrum, measured at the inspected redshift (cfpipe nirspec linefit -> campfire deploy lines). One row per spectrum, replaced on re-fit; per-line values in `lines` jsonb keyed by pipeline line name. z_used / z_quality / object_version record the inspection state the fit used (see line_fits_status for staleness). Formerly spectrum_line_fits (renamed 2026-10-26, Catalog Read Model phase 2b).';

COMMENT ON TABLE "public"."lines" IS 'line_fits.lines unnested: one row per (spectrum, catalog line) for indexed filter/sort on a line''s S/N or flux. Derived by the sync_lines trigger; never written directly. Select on doublet totals (CIII1908, OII3727, SII6725, ...) rather than components. Formerly spectrum_lines (renamed 2026-10-26, Catalog Read Model phase 2b).';

COMMENT ON TABLE "public"."photometry" IS 'catalog_matches.photometry->bands unnested: one row per (cross-match, band) for indexed filter/sort on a band''s AB magnitude or S/N. Derived by the sync_photometry trigger; never written directly. object_id is a trigger-maintained copy of the parent match''s (phase 2a), so filters read this table alone. Formerly object_photometry_bands (renamed 2026-10-26, Catalog Read Model phase 2b).';

COMMENT ON VIEW "public"."cat_catalog_matches" IS 'Catalog source view: catalog_matches for the live catalog, dr_catalog_matches rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_line_fits" IS 'Catalog source view: line_fits for the live catalog, dr_line_fits rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_lines" IS 'Catalog source view: lines for the live catalog, dr_lines rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_photometry" IS 'Catalog source view: photometry for the live catalog, dr_photometry rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_photometry" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_photometry" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_photometry" TO "service_role";
