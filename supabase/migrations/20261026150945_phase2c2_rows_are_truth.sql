SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP TRIGGER "sync_photometry_trigger" ON "public"."catalog_matches";

DROP TRIGGER "sync_lines_trigger" ON "public"."line_fits";

DROP VIEW "public"."cat_catalog_matches";

DROP VIEW "public"."cat_line_fits";

DROP FUNCTION "public"."sync_lines"();

DROP FUNCTION "public"."sync_photometry"();

ALTER TABLE "public"."catalog_matches"
  DROP COLUMN "photometry";

ALTER TABLE "public"."dr_catalog_matches"
  DROP COLUMN "photometry";

ALTER TABLE "public"."dr_line_fits"
  DROP COLUMN "lines";

ALTER TABLE "public"."line_fits"
  DROP COLUMN "lines";

CREATE OR REPLACE FUNCTION public.bands_jsonb_of (
  p_match_id integer,
  p_tag      text    DEFAULT NULL::text
)
  RETURNS jsonb
  LANGUAGE sql
  STABLE
  SET search_path TO 'public'
  SET extra_float_digits TO '1'
  AS $function$
  SELECT jsonb_build_object(
           'flux_unit', 'uJy',
           'bands', COALESCE(jsonb_object_agg(b.band,
                      jsonb_build_object('flux', b.flux, 'flux_err', b.flux_err)
                      || CASE WHEN b.wav IS NULL THEN '{}'::jsonb
                              ELSE jsonb_build_object('wav', b.wav, 'wav_min', b.wav_min, 'wav_max', b.wav_max) END),
                    '{}'::jsonb))
  FROM (
    SELECT band, flux, flux_err, wav, wav_min, wav_max
      FROM public.photometry
     WHERE p_tag IS NULL AND match_id = p_match_id
    UNION ALL
    SELECT band, flux, flux_err, wav, wav_min, wav_max
      FROM public.dr_photometry
     WHERE p_tag IS NOT NULL AND tag = p_tag AND match_id = p_match_id
  ) b;
$function$;

REVOKE ALL ON FUNCTION "public"."bands_jsonb_of"(integer, text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.catalog_matches_upsert (
  p_rows jsonb
)
  RETURNS integer
  LANGUAGE plpgsql
  SET search_path TO 'public'
  AS $function$
DECLARE
  r jsonb;
  v_id integer;
  n integer := 0;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'catalog_matches_upsert: p_rows must be a JSON array of cross-match records';
  END IF;
  FOR r IN SELECT jsonb_array_elements(p_rows) LOOP
    INSERT INTO public.catalog_matches (
      object_id, field, ra, dec, catalog_name, catalog_id, match_distance_arcsec,
      photo_z, photo_z_err_lo, photo_z_err_hi, has_pz, updated_at
    ) VALUES (
      (r ->> 'object_id')::integer, r ->> 'field',
      (r ->> 'ra')::double precision, (r ->> 'dec')::double precision,
      r ->> 'catalog_name', r ->> 'catalog_id', (r ->> 'match_distance_arcsec')::double precision,
      (r ->> 'photo_z')::double precision, (r ->> 'photo_z_err_lo')::double precision,
      (r ->> 'photo_z_err_hi')::double precision, COALESCE((r ->> 'has_pz')::boolean, false),
      COALESCE((r ->> 'updated_at')::timestamptz, now())
    )
    ON CONFLICT (field, catalog_name, catalog_id) DO UPDATE SET
      object_id = EXCLUDED.object_id, ra = EXCLUDED.ra, dec = EXCLUDED.dec,
      match_distance_arcsec = EXCLUDED.match_distance_arcsec,
      photo_z = EXCLUDED.photo_z, photo_z_err_lo = EXCLUDED.photo_z_err_lo,
      photo_z_err_hi = EXCLUDED.photo_z_err_hi, has_pz = EXCLUDED.has_pz,
      updated_at = EXCLUDED.updated_at
    RETURNING id INTO v_id;
    -- a re-match replaces the whole set
    DELETE FROM public.photometry WHERE match_id = v_id;
    INSERT INTO public.photometry (match_id, band, flux, flux_err, wav, wav_min, wav_max)
    SELECT
      v_id,
      e.key,
      (e.value ->> 'flux')::double precision,
      (e.value ->> 'flux_err')::double precision,
      (e.value ->> 'wav')::double precision,
      (e.value ->> 'wav_min')::double precision,
      (e.value ->> 'wav_max')::double precision
    FROM jsonb_each(COALESCE(r -> 'photometry' -> 'bands', '{}'::jsonb)) AS e
    WHERE jsonb_typeof(e.value) = 'object';
    n := n + 1;
  END LOOP;
  RETURN n;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."catalog_matches_upsert"(jsonb) FROM PUBLIC, "anon";

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
    SELECT public.bands_jsonb_of(op.id, public.dr_browse_tag()) AS photometry FROM public.cat_catalog_matches op
    WHERE op.object_id = po.id ORDER BY op.updated_at DESC LIMIT 1
  ) phot ON true
  ORDER BY po.object_id ASC;
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
           f.n_lines, f.n_detected, f.n_broad, f.chi2, f.dof,
           public.lines_jsonb_of(f.spectrum_id) AS lines,
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
           op.match_distance_arcsec, public.bands_jsonb_of(op.id) AS photometry, op.photo_z,
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

CREATE OR REPLACE FUNCTION public.line_fits_upsert (
  p_rows jsonb
)
  RETURNS integer
  LANGUAGE plpgsql
  SET search_path TO 'public'
  AS $function$
DECLARE
  r jsonb;
  v_spectrum_id integer;
  n integer := 0;
BEGIN
  IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
    RAISE EXCEPTION 'line_fits_upsert: p_rows must be a JSON array of fit records';
  END IF;
  FOR r IN SELECT jsonb_array_elements(p_rows) LOOP
    v_spectrum_id := (r ->> 'spectrum_id')::integer;
    IF v_spectrum_id IS NULL THEN
      RAISE EXCEPTION 'line_fits_upsert: a record has no spectrum_id';
    END IF;
    INSERT INTO public.line_fits (
      spectrum_id, target_id, grating, program_slug, observation,
      z_used, z_source, z_quality, object_id, object_version,
      z_fit, z_fit_err, dv, dv_err, sigma_v, sigma_v_err, kin_source,
      n_lines, n_detected, n_broad, chi2, dof,
      fit_version, cfpipe_version, f_lsf, spectrum_hash, spectrum_science_hash, fitted_at
    ) VALUES (
      v_spectrum_id, r ->> 'target_id', r ->> 'grating', r ->> 'program_slug', r ->> 'observation',
      (r ->> 'z_used')::double precision, COALESCE(r ->> 'z_source', 'inspected'),
      COALESCE((r ->> 'z_quality')::integer, 0), r ->> 'object_id', (r ->> 'object_version')::integer,
      (r ->> 'z_fit')::double precision, (r ->> 'z_fit_err')::double precision,
      (r ->> 'dv')::double precision, (r ->> 'dv_err')::double precision,
      (r ->> 'sigma_v')::double precision, (r ->> 'sigma_v_err')::double precision, r ->> 'kin_source',
      COALESCE((r ->> 'n_lines')::integer, 0), COALESCE((r ->> 'n_detected')::integer, 0),
      COALESCE((r ->> 'n_broad')::integer, 0), (r ->> 'chi2')::double precision, (r ->> 'dof')::integer,
      r ->> 'fit_version', r ->> 'cfpipe_version', (r ->> 'f_lsf')::double precision,
      r ->> 'spectrum_hash', r ->> 'spectrum_science_hash', (r ->> 'fitted_at')::timestamptz
    )
    ON CONFLICT (spectrum_id) DO UPDATE SET
      target_id = EXCLUDED.target_id, grating = EXCLUDED.grating,
      program_slug = EXCLUDED.program_slug, observation = EXCLUDED.observation,
      z_used = EXCLUDED.z_used, z_source = EXCLUDED.z_source, z_quality = EXCLUDED.z_quality,
      object_id = EXCLUDED.object_id, object_version = EXCLUDED.object_version,
      z_fit = EXCLUDED.z_fit, z_fit_err = EXCLUDED.z_fit_err,
      dv = EXCLUDED.dv, dv_err = EXCLUDED.dv_err, sigma_v = EXCLUDED.sigma_v, sigma_v_err = EXCLUDED.sigma_v_err,
      kin_source = EXCLUDED.kin_source,
      n_lines = EXCLUDED.n_lines, n_detected = EXCLUDED.n_detected, n_broad = EXCLUDED.n_broad,
      chi2 = EXCLUDED.chi2, dof = EXCLUDED.dof,
      fit_version = EXCLUDED.fit_version, cfpipe_version = EXCLUDED.cfpipe_version, f_lsf = EXCLUDED.f_lsf,
      spectrum_hash = EXCLUDED.spectrum_hash, spectrum_science_hash = EXCLUDED.spectrum_science_hash,
      fitted_at = EXCLUDED.fitted_at;
    -- a re-fit replaces the whole set: a line dropped from the catalog must not linger
    DELETE FROM public.lines WHERE spectrum_id = v_spectrum_id;
    INSERT INTO public.lines (
      spectrum_id, line, component, wave_rest, flux, flux_err, snr,
      ew_rest, ew_rest_err, flags, blend_into,
      label, wave_obs, cont, cont_err, dv, dv_err, sigma_v, sigma_v_err,
      sigma_lsf_kms, complex, chi2, dof, npix, tied_to, members
    )
    SELECT
      v_spectrum_id,
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
      e.value ->> 'label',
      (e.value ->> 'wave_obs')::double precision,
      (e.value ->> 'cont')::double precision,
      (e.value ->> 'cont_err')::double precision,
      (e.value ->> 'dv')::double precision,
      (e.value ->> 'dv_err')::double precision,
      (e.value ->> 'sigma_v')::double precision,
      (e.value ->> 'sigma_v_err')::double precision,
      (e.value ->> 'sigma_lsf_kms')::double precision,
      (e.value ->> 'complex')::integer,
      (e.value ->> 'chi2')::double precision,
      (e.value ->> 'dof')::integer,
      (e.value ->> 'npix')::integer,
      e.value ->> 'tied_to',
      CASE WHEN jsonb_typeof(e.value -> 'members') = 'array'
           THEN ARRAY(SELECT jsonb_array_elements_text(e.value -> 'members')) END
    FROM jsonb_each(COALESCE(r -> 'lines', '{}'::jsonb)) AS e
    WHERE jsonb_typeof(e.value) = 'object';
    n := n + 1;
  END LOOP;
  RETURN n;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."line_fits_upsert"(jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.lines_jsonb_of (
  p_spectrum_id integer,
  p_tag         text    DEFAULT NULL::text
)
  RETURNS jsonb
  LANGUAGE sql
  STABLE
  SET search_path TO 'public'
  SET extra_float_digits TO '1'
  AS $function$
  SELECT COALESCE(jsonb_object_agg(l.line,
           jsonb_build_object(
             'label', l.label, 'component', l.component,
             'wave_rest', l.wave_rest, 'wave_obs', l.wave_obs,
             'flux', l.flux, 'flux_err', l.flux_err, 'snr', l.snr,
             'ew_rest', l.ew_rest, 'ew_rest_err', l.ew_rest_err,
             'cont', l.cont, 'cont_err', l.cont_err,
             'dv', l.dv, 'dv_err', l.dv_err, 'sigma_v', l.sigma_v, 'sigma_v_err', l.sigma_v_err,
             'sigma_lsf_kms', l.sigma_lsf_kms, 'complex', l.complex,
             'chi2', l.chi2, 'dof', l.dof, 'npix', l.npix, 'flags', l.flags,
             'blend_into', l.blend_into, 'tied_to', l.tied_to)
           || CASE WHEN l.members IS NULL THEN '{}'::jsonb
                   ELSE jsonb_build_object('members', to_jsonb(l.members)) END),
         '{}'::jsonb)
  FROM (
    SELECT line, label, component, wave_rest, wave_obs, flux, flux_err, snr, ew_rest, ew_rest_err,
           cont, cont_err, dv, dv_err, sigma_v, sigma_v_err, sigma_lsf_kms, complex, chi2, dof, npix,
           flags, blend_into, tied_to, members
      FROM public.lines
     WHERE p_tag IS NULL AND spectrum_id = p_spectrum_id
    UNION ALL
    SELECT line, label, component, wave_rest, wave_obs, flux, flux_err, snr, ew_rest, ew_rest_err,
           cont, cont_err, dv, dv_err, sigma_v, sigma_v_err, sigma_lsf_kms, complex, chi2, dof, npix,
           flags, blend_into, tied_to, members
      FROM public.dr_lines
     WHERE p_tag IS NOT NULL AND tag = p_tag AND spectrum_id = p_spectrum_id
  ) l;
$function$;

REVOKE ALL ON FUNCTION "public"."lines_jsonb_of"(integer, text) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.sync_line_object()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  NEW.object_id := (SELECT t.object_id
                      FROM public.spectra s
                      JOIN public.targets t ON t.target_id = s.target_id
                     WHERE s.id = NEW.spectrum_id
                     FOR SHARE OF s, t);
  RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."sync_line_object"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.sync_photometry_object()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  NEW.object_id := (SELECT m.object_id
                      FROM public.catalog_matches m
                     WHERE m.id = NEW.match_id
                     FOR SHARE);
  RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."sync_photometry_object"() FROM PUBLIC, "anon";

CREATE VIEW "public"."cat_catalog_matches" WITH (security_invoker=true) AS  SELECT id,
    object_id,
    field,
    ra,
    "dec",
    catalog_name,
    catalog_id,
    match_distance_arcsec,
    photo_z,
    photo_z_err_lo,
    photo_z_err_hi,
    has_pz,
    created_at,
    updated_at
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            catalog_matches.id,
            catalog_matches.object_id,
            catalog_matches.field,
            catalog_matches.ra,
            catalog_matches."dec",
            catalog_matches.catalog_name,
            catalog_matches.catalog_id,
            catalog_matches.match_distance_arcsec,
            catalog_matches.photo_z,
            catalog_matches.photo_z_err_lo,
            catalog_matches.photo_z_err_hi,
            catalog_matches.has_pz,
            catalog_matches.created_at,
            catalog_matches.updated_at
           FROM public.catalog_matches
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_catalog_matches.tag AS cat_tag,
            dr_catalog_matches.id,
            dr_catalog_matches.object_id,
            dr_catalog_matches.field,
            dr_catalog_matches.ra,
            dr_catalog_matches."dec",
            dr_catalog_matches.catalog_name,
            dr_catalog_matches.catalog_id,
            dr_catalog_matches.match_distance_arcsec,
            dr_catalog_matches.photo_z,
            dr_catalog_matches.photo_z_err_lo,
            dr_catalog_matches.photo_z_err_hi,
            dr_catalog_matches.has_pz,
            dr_catalog_matches.created_at,
            dr_catalog_matches.updated_at
           FROM public.dr_catalog_matches) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_catalog_matches" FROM "anon";

CREATE VIEW "public"."cat_line_fits" WITH (security_invoker=true) AS  SELECT spectrum_id,
    target_id,
    grating,
    program_slug,
    observation,
    z_used,
    z_source,
    z_quality,
    object_id,
    object_version,
    z_fit,
    z_fit_err,
    dv,
    dv_err,
    sigma_v,
    sigma_v_err,
    kin_source,
    n_lines,
    n_detected,
    n_broad,
    chi2,
    dof,
    fit_version,
    cfpipe_version,
    f_lsf,
    spectrum_hash,
    fitted_at,
    created_at,
    updated_at,
    spectrum_science_hash,
    stale_redshift
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            line_fits.spectrum_id,
            line_fits.target_id,
            line_fits.grating,
            line_fits.program_slug,
            line_fits.observation,
            line_fits.z_used,
            line_fits.z_source,
            line_fits.z_quality,
            line_fits.object_id,
            line_fits.object_version,
            line_fits.z_fit,
            line_fits.z_fit_err,
            line_fits.dv,
            line_fits.dv_err,
            line_fits.sigma_v,
            line_fits.sigma_v_err,
            line_fits.kin_source,
            line_fits.n_lines,
            line_fits.n_detected,
            line_fits.n_broad,
            line_fits.chi2,
            line_fits.dof,
            line_fits.fit_version,
            line_fits.cfpipe_version,
            line_fits.f_lsf,
            line_fits.spectrum_hash,
            line_fits.fitted_at,
            line_fits.created_at,
            line_fits.updated_at,
            line_fits.spectrum_science_hash,
            line_fits.stale_redshift
           FROM public.line_fits
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_line_fits.tag AS cat_tag,
            dr_line_fits.spectrum_id,
            dr_line_fits.target_id,
            dr_line_fits.grating,
            dr_line_fits.program_slug,
            dr_line_fits.observation,
            dr_line_fits.z_used,
            dr_line_fits.z_source,
            dr_line_fits.z_quality,
            dr_line_fits.object_id,
            dr_line_fits.object_version,
            dr_line_fits.z_fit,
            dr_line_fits.z_fit_err,
            dr_line_fits.dv,
            dr_line_fits.dv_err,
            dr_line_fits.sigma_v,
            dr_line_fits.sigma_v_err,
            dr_line_fits.kin_source,
            dr_line_fits.n_lines,
            dr_line_fits.n_detected,
            dr_line_fits.n_broad,
            dr_line_fits.chi2,
            dr_line_fits.dof,
            dr_line_fits.fit_version,
            dr_line_fits.cfpipe_version,
            dr_line_fits.f_lsf,
            dr_line_fits.spectrum_hash,
            dr_line_fits.fitted_at,
            dr_line_fits.created_at,
            dr_line_fits.updated_at,
            dr_line_fits.spectrum_science_hash,
            dr_line_fits.stale_redshift
           FROM public.dr_line_fits) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "anon";

CREATE TRIGGER sync_line_object_trigger
  BEFORE INSERT OR UPDATE OF spectrum_id, object_id ON public.lines
  FOR EACH ROW
  EXECUTE FUNCTION public.sync_line_object();

CREATE TRIGGER sync_photometry_object_trigger
  BEFORE INSERT OR UPDATE OF match_id, object_id ON public.photometry
  FOR EACH ROW
  EXECUTE FUNCTION public.sync_photometry_object();

COMMENT ON FUNCTION "public"."bands_jsonb_of"(integer, text) IS 'The {flux_unit: "uJy", bands: {band: {flux, flux_err, wav, wav_min, wav_max}}} map of a cross-match, rebuilt from its photometry rows (live, or the frozen rows of release p_tag): the shape catalog_matches.photometry had before Catalog Read Model phase 2c, which the sync stream, the exports and the CSV export still speak. The wavelength keys are present only where the band has a pivot wavelength, as the deploy wrote it.';

COMMENT ON FUNCTION "public"."catalog_matches_upsert"(jsonb) IS 'Writes photometric cross-matches as rows (Catalog Read Model phase 2c): one array element per match, the match''s scalar columns plus a {flux_unit, bands: {band: {flux, flux_err, wav, wav_min, wav_max}}} map under ''photometry''; upserts the catalog_matches row on (field, catalog_name, catalog_id) and replaces the match''s photometry rows. The deploy CLI''s only write path for matches; gated by the admin policies of both tables.';

COMMENT ON FUNCTION "public"."line_fits_upsert"(jsonb) IS 'Writes line fits as rows (Catalog Read Model phase 2c): one array element per spectrum, the fit''s scalar columns plus a {line: record} map under ''lines''; upserts the line_fits row on spectrum_id and replaces the spectrum''s lines rows. The deploy CLI''s only write path for fits; gated by the admin policies of both tables.';

COMMENT ON FUNCTION "public"."lines_jsonb_of"(integer, text) IS 'The {line: record} map of a fit, rebuilt from its lines rows (live, or the frozen rows of release p_tag): the shape line_fits.lines had before Catalog Read Model phase 2c, which the sync stream and the exports still speak. members is present on doublet totals only, as the pipeline wrote it.';

COMMENT ON TABLE "public"."line_fits" IS 'Emission-line fluxes per spectrum, measured at the inspected redshift (cfpipe nirspec linefit -> campfire deploy lines). One row per spectrum, replaced on re-fit by line_fits_upsert together with its lines rows, which hold the per-line measurements (the `lines` jsonb blob was dropped in Catalog Read Model phase 2c; lines_jsonb_of rebuilds the {line: record} map for the sync stream and the exports). z_used / z_quality / object_version record the inspection state the fit used (see line_fits_status for staleness). Formerly spectrum_line_fits (renamed 2026-10-26, Catalog Read Model phase 2b).';

COMMENT ON TABLE "public"."lines" IS 'The emission-line measurements: one row per (spectrum, catalog line) with every field the pipeline measured (label, component, wavelengths, flux, S/N, equivalent width, continuum, kinematics, the fit complex''s chi2 / dof / npix, flags, blend_into / tied_to / members). The source of truth since Catalog Read Model phase 2c: written with the fit by line_fits_upsert (a re-fit replaces all of a spectrum''s rows), object_id trigger-maintained, never a client-supplied value. Select on doublet totals (CIII1908, OII3727, SII6725, ...) rather than components. Formerly spectrum_lines (renamed 2026-10-26, phase 2b).';

COMMENT ON TABLE "public"."photometry" IS 'The photometry: one row per (cross-match, band) with the flux and error in microjansky (always; AB magnitude = 23.9 - 2.5*log10(flux), NULL for a non-positive flux), the pivot wavelength and the band edges in microns, and the generated mag / snr. The source of truth since Catalog Read Model phase 2c: written with the cross-match by catalog_matches_upsert (a re-match replaces all of a match''s rows), object_id trigger-maintained, never a client-supplied value; bands_jsonb_of rebuilds the {flux_unit, bands} map for the sync stream and the exports. Band names are per-field photometry.toml config. Formerly object_photometry_bands (renamed 2026-10-26, phase 2b).';

COMMENT ON VIEW "public"."cat_catalog_matches" IS 'Catalog source view: catalog_matches for the live catalog, dr_catalog_matches rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_line_fits" IS 'Catalog source view: line_fits for the live catalog, dr_line_fits rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

GRANT EXECUTE ON FUNCTION "public"."bands_jsonb_of"(integer, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."bands_jsonb_of"(integer, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."bands_jsonb_of"(integer, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."bands_jsonb_of"(integer, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."catalog_matches_upsert"(jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."catalog_matches_upsert"(jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_matches_upsert"(jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."catalog_matches_upsert"(jsonb) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."line_fits_upsert"(jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."line_fits_upsert"(jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."line_fits_upsert"(jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."line_fits_upsert"(jsonb) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."lines_jsonb_of"(integer, text) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."lines_jsonb_of"(integer, text) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."lines_jsonb_of"(integer, text) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."lines_jsonb_of"(integer, text) TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."sync_line_object"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."sync_line_object"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_line_object"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_line_object"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."sync_photometry_object"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."sync_photometry_object"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_photometry_object"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_photometry_object"() TO "service_role";

REVOKE ALL ON TABLE "public"."cat_catalog_matches" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_catalog_matches" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_catalog_matches" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_catalog_matches" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_catalog_matches" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_catalog_matches" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_line_fits" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_line_fits" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_line_fits" TO "service_role";
