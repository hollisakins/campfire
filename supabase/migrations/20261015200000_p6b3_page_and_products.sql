SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

ALTER TABLE "public"."deploy_events"
  DROP CONSTRAINT "deploy_events_action_check";

ALTER TABLE "public"."dr_releases"
  ADD COLUMN "page_md" text;

CREATE OR REPLACE FUNCTION public.dr_bytes_summary (
  p_tag text
)
  RETURNS jsonb
  LANGUAGE plpgsql
  STABLE
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  out jsonb;
BEGIN
  PERFORM dr_require_admin();
  IF NOT EXISTS (SELECT 1 FROM dr_releases r WHERE r.tag = p_tag) THEN
    RAISE EXCEPTION 'no release %', p_tag;
  END IF;
  SELECT jsonb_build_object(
    'tag', p_tag,
    'state', (SELECT state FROM dr_releases WHERE tag = p_tag),
    'manifest', count(*),
    'by_state', (SELECT COALESCE(jsonb_object_agg(s.byte_state, jsonb_build_object('count', s.n, 'bytes', s.b)), '{}'::jsonb)
                 FROM (SELECT byte_state, count(*) AS n, COALESCE(sum(size_bytes), 0) AS b
                       FROM dr_manifest WHERE tag = p_tag AND component <> 'page' GROUP BY byte_state) s),
    'pending_bytes', COALESCE(sum(size_bytes) FILTER (WHERE byte_state IN ('pending', 'failed')), 0),
    'failed_sample', (SELECT COALESCE(jsonb_agg(jsonb_build_object('storage_key', f.storage_key, 'error', f.byte_error)), '[]'::jsonb)
                      FROM (SELECT storage_key, byte_error FROM dr_manifest
                            WHERE tag = p_tag AND byte_state = 'failed' ORDER BY storage_key LIMIT 10) f))
  INTO out
  -- the page document's assets (component 'page') are outside the byte pass
  FROM dr_manifest WHERE tag = p_tag AND component <> 'page';
  RETURN out;
END;
$function$;

CREATE OR REPLACE FUNCTION public.dr_derive_state (
  p_tag text
)
  RETURNS text
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  v_cur text;
  n_ledger int; n_ledger_open int;
  n_members int; n_members_open int; n_members_done int;
  v_new text;
BEGIN
  SELECT state INTO v_cur FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF v_cur IS NULL OR v_cur NOT IN ('frozen', 'exporting', 'exported', 'copying', 'ready') THEN
    RETURN v_cur;
  END IF;
  SELECT count(*), count(*) FILTER (WHERE state <> 'ready') INTO n_ledger, n_ledger_open
  FROM dr_exports WHERE tag = p_tag;
  -- the release page document's assets (component 'page', P6b-3) are not
  -- archived by the byte pass and must not move the state machine
  SELECT count(*), count(*) FILTER (WHERE byte_state IN ('pending', 'failed')),
         count(*) FILTER (WHERE byte_state IN ('copied', 'stamped'))
    INTO n_members, n_members_open, n_members_done
  FROM dr_manifest WHERE tag = p_tag AND component <> 'page';
  v_new := CASE
    WHEN n_ledger > 0 AND n_ledger_open = 0 AND n_members_open = 0 THEN 'ready'
    WHEN n_members_done > 0 AND n_members_open > 0 THEN 'copying'
    WHEN n_ledger > 0 AND n_ledger_open = 0 THEN 'exported'
    WHEN n_ledger > 0 THEN 'exporting'
    ELSE 'frozen' END;
  IF v_new <> v_cur THEN
    UPDATE dr_releases SET state = v_new, updated_at = now() WHERE tag = p_tag;
  END IF;
  RETURN v_new;
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
    FROM dr_photometry ph
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
    -- Likewise every active field product of a selected field (same predicate
    -- as the insert; guards a future edit of either side, not a data state).
    SELECT string_agg(so.storage_key, ', ' ORDER BY so.storage_key) INTO v_missing
    FROM storage_objects so
    WHERE so.field = ANY (comp.fields) AND so.root = 'live' AND so.status = 'active'
      AND so.backend = 'osn' AND so.bucket = 'data'
      AND so.product_type IN ('nircam_expmap', 'nircam_expmap_plot', 'nircam_layout',
                              'nircam_mosaic_thumbnail', 'nircam_mosaic_quicklook')
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
                                       jsonb_build_object('files', t.n, 'bytes', t.b, 'mosaics', t.n_mosaics)
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

CREATE OR REPLACE FUNCTION public.dr_page_set (
  p_tag      text,
  p_markdown text,
  p_assets   jsonb DEFAULT '[]'::jsonb
)
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public', 'pg_temp'
  AS $function$
DECLARE
  r dr_releases%ROWTYPE;
  v_asset jsonb;
  v_name text; v_key text; v_hash text; v_size bigint;
  v_prefix text;
  n_assets int := 0;
BEGIN
  PERFORM dr_require_admin();
  SELECT * INTO r FROM dr_releases WHERE tag = p_tag FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'no release %', p_tag; END IF;
  IF r.state IN ('published', 'withdrawn') THEN
    RAISE EXCEPTION 'release % is %; its page is frozen with it (withdraw does not reopen it)', p_tag, r.state;
  END IF;
  IF r.state IN ('draft', 'freezing') THEN
    RAISE EXCEPTION 'release % is %; set its page once the freeze has landed', p_tag, r.state;
  END IF;
  IF p_markdown IS NULL OR length(p_markdown) = 0 THEN
    RAISE EXCEPTION 'the page markdown is empty';
  END IF;
  IF octet_length(p_markdown) > 1048576 THEN
    RAISE EXCEPTION 'the page markdown exceeds 1 MB';
  END IF;
  IF p_assets IS NULL OR jsonb_typeof(p_assets) <> 'array' THEN
    RAISE EXCEPTION 'p_assets must be a JSON array';
  END IF;
  IF jsonb_array_length(p_assets) > 50 THEN
    RAISE EXCEPTION 'at most 50 page assets (got %)', jsonb_array_length(p_assets);
  END IF;
  v_prefix := 'dr/' || p_tag || '/page/';

  -- validate every asset before touching the manifest
  FOR v_asset IN SELECT * FROM jsonb_array_elements(p_assets) LOOP
    v_name := v_asset->>'name'; v_key := v_asset->>'storage_key'; v_hash := v_asset->>'content_hash';
    v_size := (v_asset->>'size_bytes')::bigint;
    IF v_name IS NULL OR v_name !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' THEN
      RAISE EXCEPTION 'bad page asset name %', coalesce(v_name, '(null)');
    END IF;
    IF v_key IS DISTINCT FROM (v_prefix || v_name) THEN
      RAISE EXCEPTION 'page asset % must live at %, not %', v_name, v_prefix || v_name, coalesce(v_key, '(null)');
    END IF;
    IF v_hash IS NULL OR v_hash !~ '^sha256:[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'page asset % needs a sha256 content hash', v_name;
    END IF;
    IF v_size IS NULL OR v_size < 1 OR v_size > 26214400 THEN
      RAISE EXCEPTION 'page asset % must be between 1 byte and 25 MB (got %)', v_name, coalesce(v_size::text, 'null');
    END IF;
    IF coalesce(v_asset->>'backend', 'osn') <> 'osn' OR coalesce(v_asset->>'bucket', 'data') <> 'data' THEN
      RAISE EXCEPTION 'page asset % must live on osn/data', v_name;
    END IF;
  END LOOP;
  IF (SELECT count(DISTINCT x->>'name') FROM jsonb_array_elements(p_assets) x) <> jsonb_array_length(p_assets) THEN
    RAISE EXCEPTION 'duplicate page asset names';
  END IF;

  DELETE FROM dr_manifest WHERE tag = p_tag AND component = 'page';
  INSERT INTO dr_manifest (tag, storage_key, component, program_slug, field, product_type, spectrum_id,
                           backend, bucket, content_hash, science_hash, size_bytes, registry_id,
                           archive_key, byte_state, stamped_hash, byte_error, updated_at)
  SELECT p_tag, x->>'storage_key', 'page', NULL, NULL, 'dr_page_asset', NULL,
         'osn', 'data', x->>'content_hash', NULL, (x->>'size_bytes')::bigint, NULL,
         x->>'storage_key', 'copied', NULL, NULL, now()
  FROM jsonb_array_elements(p_assets) x;
  GET DIAGNOSTICS n_assets = ROW_COUNT;

  UPDATE dr_releases SET page_md = p_markdown, updated_at = now() WHERE tag = p_tag;
  INSERT INTO deploy_events (actor, action, affected_count, metadata)
  VALUES (auth.uid(), 'page_dr', n_assets,
          jsonb_build_object('dr_tag', p_tag, 'markdown_bytes', octet_length(p_markdown), 'assets', n_assets));
  RETURN jsonb_build_object('tag', p_tag, 'state', r.state, 'markdown_bytes', octet_length(p_markdown), 'assets', n_assets);
END;
$function$;

REVOKE ALL ON FUNCTION "public"."dr_page_set"(text, text, jsonb) FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.log_deploy_event (
  p_action         text,
  p_actor          uuid    DEFAULT NULL::uuid,
  p_deployment_id  integer DEFAULT NULL::integer,
  p_observation    text    DEFAULT NULL::text,
  p_field          text    DEFAULT NULL::text,
  p_affected_count integer DEFAULT NULL::integer,
  p_metadata       jsonb   DEFAULT NULL::jsonb,
  p_host           text    DEFAULT NULL::text
)
  RETURNS uuid
  LANGUAGE plpgsql
  SECURITY DEFINER
  AS $function$
DECLARE
  v_is_admin boolean;
  v_id uuid;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete', 'config_sync',
                      'archive', 'collect', 'freeze', 'publish_dr', 'withdraw_dr', 'page_dr') THEN
    RAISE EXCEPTION 'Invalid deploy_event action: %', p_action;
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, observation, field, affected_count, metadata, host)
  VALUES (p_actor, p_action, p_deployment_id, p_observation, p_field, p_affected_count, p_metadata, p_host)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$function$;

ALTER TABLE "public"."deploy_events"
  ADD CONSTRAINT "deploy_events_action_check"
    CHECK
    ((action = ANY (ARRAY['upload'::text, 'publish'::text, 'revoke'::text, 'recover'::text, 'supersede'::text, 'delete'::text, 'config_sync'::text, 'archive'::text,
    'collect'::text, 'freeze'::text, 'publish_dr'::text, 'withdraw_dr'::text, 'page_dr'::text])));

COMMENT ON FUNCTION "public"."dr_page_set"(text, text, jsonb) IS 'Set a data release''s page document (P6b-3): the markdown shown on /releases/<tag> and its figures as dr_manifest rows of component ''page'' (already uploaded under dr/<tag>/page/, so they enter as copied). Admin only; refused once the release is published or withdrawn, or before its freeze has landed.';

GRANT EXECUTE ON FUNCTION "public"."dr_page_set"(text, text, jsonb) TO "authenticated";

REVOKE ALL ON FUNCTION "public"."dr_page_set"(text, text, jsonb) FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_page_set"(text, text, jsonb) TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."dr_page_set"(text, text, jsonb) TO "service_role";
