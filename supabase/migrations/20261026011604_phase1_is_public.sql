SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP POLICY "select_objects_by_access" ON "public"."objects";

DROP POLICY "select_spectra_by_access" ON "public"."spectra";

DROP POLICY "select_targets_by_access" ON "public"."targets";

DROP VIEW "public"."cat_objects";

DROP VIEW "public"."cat_spectra";

DROP VIEW "public"."cat_targets";

ALTER TABLE "public"."dr_objects"
  ADD COLUMN "is_public" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."dr_spectra"
  ADD COLUMN "is_public" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."dr_targets"
  ADD COLUMN "is_public" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."objects"
  ADD COLUMN "is_public" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."spectra"
  ADD COLUMN "is_public" boolean NOT NULL DEFAULT false;

ALTER TABLE "public"."targets"
  ADD COLUMN "is_public" boolean NOT NULL DEFAULT false;

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
        -- the live storage gate shows field-level rows through their
        -- deployment's status: a --draft redeploy registers active rows under
        -- its draft deployment, and those must not be frozen into the open
        -- nircam/<field> partition (P6b-3 review)
        AND so.deployment_id IS NOT NULL
        AND EXISTS (SELECT 1 FROM deployments d
                    WHERE d.id = so.deployment_id AND d.field = so.field AND d.status = 'published')
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
  SELECT p.object_id, min(b.mag), max(b.snr)
  FROM public.cat_photometry_bands b
  JOIN public.cat_photometry p ON p.id = b.photometry_id
  JOIN public.cat_objects o ON o.id = p.object_id
  WHERE p_band IS NOT NULL
    AND b.band = p_band
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
  GROUP BY p.object_id
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
  SELECT t.object_id, max(l.snr)
  FROM public.cat_spectrum_lines l
  -- unique-key lookups, pinned with LIMIT 1 (see the plan-shape note above)
  CROSS JOIN LATERAL (
    SELECT f.program_slug, f.object_version, f.z_quality, f.z_used
    FROM public.cat_line_fits f WHERE f.spectrum_id = l.spectrum_id LIMIT 1
  ) f
  CROSS JOIN LATERAL (
    SELECT s.target_id, s.program_slug, s.observation, s.deploy_status
    FROM public.cat_spectra s WHERE s.id = l.spectrum_id LIMIT 1
  ) s
  CROSS JOIN LATERAL (
    SELECT t.object_id
    FROM public.cat_targets t WHERE t.target_id = s.target_id LIMIT 1
  ) t
  CROSS JOIN LATERAL (
    SELECT o.version, o.redshift_quality, o.redshift, o.programs,
           o.has_published_spectrum, o.observations, o.is_active
    FROM public.cat_objects o WHERE o.id = t.object_id LIMIT 1
  ) o
  WHERE p_line IS NOT NULL
    AND l.line = p_line
    AND l.snr IS NOT NULL
    -- Gate 1: the caller's access-checked scope (what the RPCs apply themselves).
    AND f.program_slug = ANY(p_program_slugs)
    AND (p_include_unpublished OR s.deploy_status = 'published')
    AND (p_include_stale OR NOT public.line_fit_stale_redshift(
           o.version, o.redshift_quality, (o.redshift)::double precision,
           f.object_version, f.z_quality, f.z_used))
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
  GROUP BY t.object_id
  HAVING (p_snr_min IS NULL OR max(l.snr) >= p_snr_min)
     AND (p_snr_max IS NULL OR max(l.snr) <= p_snr_max);
$function$;

CREATE OR REPLACE FUNCTION public.propagate_program_is_public()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.is_public IS NOT DISTINCT FROM OLD.is_public THEN
    RETURN NULL;
  END IF;
  -- Only the rows whose value changes are written, so the per-row triggers
  -- on these tables (updated_at bumps, needs_review) see nothing else move.
  UPDATE targets t SET is_public = NEW.is_public
   WHERE t.program_slug = NEW.slug AND t.is_public IS DISTINCT FROM NEW.is_public;
  UPDATE spectra s SET is_public = NEW.is_public
   WHERE s.program_slug = NEW.slug AND s.is_public IS DISTINCT FROM NEW.is_public;
  UPDATE objects o SET is_public = v.pub
    FROM (SELECT o2.id,
                 EXISTS (SELECT 1 FROM programs p WHERE p.is_public AND p.slug = ANY(o2.programs)) AS pub
            FROM objects o2 WHERE NEW.slug = ANY(o2.programs)) v
   WHERE v.id = o.id AND o.is_public IS DISTINCT FROM v.pub;
  RETURN NULL;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."propagate_program_is_public"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.sync_object_is_public()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  NEW.is_public := EXISTS (
    SELECT 1 FROM programs p WHERE p.is_public AND p.slug = ANY(NEW.programs));
  RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."sync_object_is_public"() FROM PUBLIC, "anon";

CREATE OR REPLACE FUNCTION public.sync_row_is_public()
  RETURNS TRIGGER
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path TO 'public'
  AS $function$
BEGIN
  NEW.is_public := COALESCE(
    (SELECT p.is_public FROM programs p WHERE p.slug = NEW.program_slug), false);
  RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION "public"."sync_row_is_public"() FROM PUBLIC, "anon";

CREATE VIEW "public"."cat_objects" WITH (security_invoker=true) AS  SELECT id,
    object_id,
    field,
    ra,
    "dec",
    created_at,
    n_targets,
    n_spectra,
    programs,
    gratings,
    max_snr,
    max_exposure_time,
    updated_at,
    observations,
    has_photometry,
    photo_z,
    photo_z_err_hi,
    photo_z_err_lo,
    redshift_auto,
    redshift_inspected,
    redshift_quality,
    redshift,
    last_inspected_at,
    last_inspected_by,
    last_data_change_at,
    staleness_reason,
    version,
    is_active,
    inspected_used_auto,
    search_text,
    has_published_spectrum,
    reviewed_spectrum_ids,
    needs_review,
    is_public
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            objects.id,
            objects.object_id,
            objects.field,
            objects.ra,
            objects."dec",
            objects.created_at,
            objects.n_targets,
            objects.n_spectra,
            objects.programs,
            objects.gratings,
            objects.max_snr,
            objects.max_exposure_time,
            objects.updated_at,
            objects.observations,
            objects.has_photometry,
            objects.photo_z,
            objects.photo_z_err_hi,
            objects.photo_z_err_lo,
            objects.redshift_auto,
            objects.redshift_inspected,
            objects.redshift_quality,
            objects.redshift,
            objects.last_inspected_at,
            objects.last_inspected_by,
            objects.last_data_change_at,
            objects.staleness_reason,
            objects.version,
            objects.is_active,
            objects.inspected_used_auto,
            objects.search_text,
            objects.has_published_spectrum,
            objects.reviewed_spectrum_ids,
            objects.needs_review,
            objects.is_public
           FROM public.objects
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_objects.tag AS cat_tag,
            dr_objects.id,
            dr_objects.object_id,
            dr_objects.field,
            dr_objects.ra,
            dr_objects."dec",
            dr_objects.created_at,
            dr_objects.n_targets,
            dr_objects.n_spectra,
            dr_objects.programs,
            dr_objects.gratings,
            dr_objects.max_snr,
            dr_objects.max_exposure_time,
            dr_objects.updated_at,
            dr_objects.observations,
            dr_objects.has_photometry,
            dr_objects.photo_z,
            dr_objects.photo_z_err_hi,
            dr_objects.photo_z_err_lo,
            dr_objects.redshift_auto,
            dr_objects.redshift_inspected,
            dr_objects.redshift_quality,
            dr_objects.redshift,
            dr_objects.last_inspected_at,
            dr_objects.last_inspected_by,
            dr_objects.last_data_change_at,
            dr_objects.staleness_reason,
            dr_objects.version,
            dr_objects.is_active,
            dr_objects.inspected_used_auto,
            dr_objects.search_text,
            dr_objects.has_published_spectrum,
            dr_objects.reviewed_spectrum_ids,
            dr_objects.needs_review,
            dr_objects.is_public
           FROM public.dr_objects) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_objects" FROM "anon";

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
    is_public
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
            spectra.is_public
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
            dr_spectra.is_public
           FROM public.dr_spectra) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "anon";

CREATE VIEW "public"."cat_targets" WITH (security_invoker=true) AS  SELECT id,
    target_id,
    field,
    ra,
    "dec",
    redshift_auto,
    redshift_quality,
    spectral_features,
    dq_flags,
    created_at,
    updated_at,
    redshift_inspected,
    last_inspected_at,
    last_inspected_by,
    max_snr,
    redshift,
    max_exposure_time,
    program_slug,
    observation,
    object_id,
    has_published_spectrum,
    is_public
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            targets.id,
            targets.target_id,
            targets.field,
            targets.ra,
            targets."dec",
            targets.redshift_auto,
            targets.redshift_quality,
            targets.spectral_features,
            targets.dq_flags,
            targets.created_at,
            targets.updated_at,
            targets.redshift_inspected,
            targets.last_inspected_at,
            targets.last_inspected_by,
            targets.max_snr,
            targets.redshift,
            targets.max_exposure_time,
            targets.program_slug,
            targets.observation,
            targets.object_id,
            targets.has_published_spectrum,
            targets.is_public
           FROM public.targets
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_targets.tag AS cat_tag,
            dr_targets.id,
            dr_targets.target_id,
            dr_targets.field,
            dr_targets.ra,
            dr_targets."dec",
            dr_targets.redshift_auto,
            dr_targets.redshift_quality,
            dr_targets.spectral_features,
            dr_targets.dq_flags,
            dr_targets.created_at,
            dr_targets.updated_at,
            dr_targets.redshift_inspected,
            dr_targets.last_inspected_at,
            dr_targets.last_inspected_by,
            dr_targets.max_snr,
            dr_targets.redshift,
            dr_targets.max_exposure_time,
            dr_targets.program_slug,
            dr_targets.observation,
            dr_targets.object_id,
            dr_targets.has_published_spectrum,
            dr_targets.is_public
           FROM public.dr_targets) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_targets" FROM "anon";

CREATE TRIGGER sync_object_is_public_trigger
  BEFORE INSERT OR UPDATE OF programs ON public.objects
  FOR EACH ROW
  EXECUTE FUNCTION public.sync_object_is_public();

CREATE TRIGGER propagate_program_is_public_trigger
  AFTER INSERT OR UPDATE OF is_public ON public.programs
  FOR EACH ROW
  EXECUTE FUNCTION public.propagate_program_is_public();

CREATE TRIGGER sync_spectrum_is_public_trigger
  BEFORE INSERT OR UPDATE OF program_slug ON public.spectra
  FOR EACH ROW
  EXECUTE FUNCTION public.sync_row_is_public();

CREATE TRIGGER sync_target_is_public_trigger
  BEFORE INSERT OR UPDATE OF program_slug ON public.targets
  FOR EACH ROW
  EXECUTE FUNCTION public.sync_row_is_public();

CREATE POLICY "select_objects_by_access" ON "public"."objects"
  FOR SELECT
  TO "authenticated"
  USING
    ((((( SELECT (NOT public.is_link_account())) AND is_public) OR (programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs))) AND
    (has_published_spectrum OR ( SELECT public.is_admin() AS is_admin) OR ( SELECT public.link_sees_drafts() AS link_sees_drafts)) AND
    (is_active OR ( SELECT public.is_admin() AS is_admin)) AND
    (( SELECT (NOT public.is_link_account())) OR (observations && ARRAY[( SELECT public.link_observation() AS link_observation)]))));

CREATE POLICY "select_spectra_by_access" ON "public"."spectra"
  FOR SELECT
  TO "authenticated"
  USING
    ((((( SELECT (NOT public.is_link_account())) AND is_public) OR (program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))) AND ((
    SELECT (NOT public.is_link_account())) OR (observation = ( SELECT public.link_observation() AS link_observation))) AND
    ((deploy_status = 'published'::text) OR ( SELECT public.is_admin() AS is_admin) OR ((deploy_status = 'draft'::text) AND ( SELECT public.link_sees_drafts() AS
    link_sees_drafts)))));

CREATE POLICY "select_targets_by_access" ON "public"."targets"
  FOR SELECT
  TO "authenticated"
  USING
    ((((( SELECT (NOT public.is_link_account())) AND is_public) OR (program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))) AND
    (has_published_spectrum OR ( SELECT public.is_admin() AS is_admin) OR ( SELECT public.link_sees_drafts() AS link_sees_drafts)) AND
    (( SELECT (NOT public.is_link_account())) OR (observation = ( SELECT public.link_observation() AS link_observation)))));

COMMENT ON COLUMN "public"."objects"."is_public" IS 'Planner-visible access scope (Catalog Read Model phase 1, #533): true when any member program is public. Trigger-maintained (sync_object_is_public on programs[] changes; propagate_program_is_public when a program flips), never written by hand. select_objects_by_access tests (NOT link AND is_public) OR programs && accessible_program_slugs(): identical row set, but a real column with statistics instead of an opaque InitPlan array, so the planner stops choosing the programs index for a predicate that matches ~76% of rows.';

COMMENT ON COLUMN "public"."spectra"."is_public" IS 'Planner-visible access scope (Catalog Read Model phase 1, #533): the program''s is_public, trigger-maintained (sync_row_is_public on program_slug changes; propagate_program_is_public when a program flips). select_spectra_by_access tests (NOT link AND is_public) OR program_slug = ANY(scope); same row set, real statistics.';

COMMENT ON COLUMN "public"."targets"."is_public" IS 'Planner-visible access scope (Catalog Read Model phase 1, #533): the program''s is_public, trigger-maintained (sync_row_is_public on program_slug changes; propagate_program_is_public when a program flips). select_targets_by_access tests (NOT link AND is_public) OR program_slug = ANY(scope); same row set, real statistics.';

COMMENT ON VIEW "public"."cat_objects" IS 'Catalog source view: objects for the live catalog, dr_objects rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_spectra" IS 'Catalog source view: spectra for the live catalog, dr_spectra rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_targets" IS 'Catalog source view: targets for the live catalog, dr_targets rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

GRANT EXECUTE ON FUNCTION "public"."propagate_program_is_public"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."propagate_program_is_public"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."propagate_program_is_public"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."propagate_program_is_public"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."sync_object_is_public"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."sync_object_is_public"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_object_is_public"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_object_is_public"() TO "service_role";

GRANT EXECUTE ON FUNCTION "public"."sync_row_is_public"() TO "authenticated";

REVOKE ALL ON FUNCTION "public"."sync_row_is_public"() FROM "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_row_is_public"() TO "postgres";

GRANT EXECUTE ON FUNCTION "public"."sync_row_is_public"() TO "service_role";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_objects" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_objects" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_objects" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectra" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_targets" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_targets" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_targets" TO "service_role";
