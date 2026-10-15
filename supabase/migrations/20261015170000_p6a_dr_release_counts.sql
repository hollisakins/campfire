SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- P6a: dr_freeze_job records by_component (files, bytes), nirspec_programs and
-- nirspec_fields in dr_releases.counts for the /releases pages.
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
    DELETE FROM dr_manifest    WHERE tag = p_tag;
    DELETE FROM dr_line_fits  WHERE tag = p_tag;
    DELETE FROM dr_photometry WHERE tag = p_tag;
    DELETE FROM dr_spectra    WHERE tag = p_tag;
    DELETE FROM dr_objects    WHERE tag = p_tag;
    DELETE FROM dr_exports    WHERE tag = p_tag;

    -- ONE statement: every sub-statement sees the same snapshot.
    WITH
    pub AS MATERIALIZED (
      -- Published spectra (nirspec component) with the active object they
      -- roll up to (NULL while a target has no active object).
      SELECT s.id, s.spectrum_id, s.program_slug, s.observation, s.grating, s.target_id,
             t.field AS target_field, o.id AS obj_id, s.fits_path, s.file_hash, s.science_hash, s.file_size,
             s.exposure_time, s.signal_to_noise, s.cfpipe_version, s.crds_context,
             s.jwst_version, s.date_obs, s.reduced_at, s.redshift_auto, s.chi2_min,
             s.confidence, s.dq_flags,
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
      -- Evidence programs per object: the programs of its published members.
      SELECT p.obj_id, array_agg(DISTINCT p.program_slug ORDER BY p.program_slug) AS programs
      FROM pub p WHERE p.obj_id IS NOT NULL GROUP BY p.obj_id
    ),
    -- Auto-redshift source: the compute_object_redshift_auto rule, set-based.
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
    agg AS (
      -- Aggregates over the PUBLISHED members only (the live objects row counts
      -- draft / revoked members too and would make the frozen row inconsistent
      -- with the released spectra).
      SELECT p.obj_id, count(DISTINCT p.target_id) AS n_targets, count(*) AS n_spectra,
             array_agg(DISTINCT p.grating ORDER BY p.grating) AS gratings,
             array_agg(DISTINCT p.observation ORDER BY p.observation) AS observations,
             max(p.signal_to_noise) AS max_snr, max(p.exposure_time) AS max_exposure_time
      FROM pub p WHERE p.obj_id IS NOT NULL GROUP BY p.obj_id
    ),
    phz AS (
      -- photo-z from the SELECTED catalogs only, with the live rule (the newest
      -- cross-match, sync_photometry_to_objects); none selected -> NULL / false,
      -- so dr_objects never advertises photometry dr_photometry does not carry
      SELECT DISTINCT ON (ph.object_id) ph.object_id, ph.photo_z, ph.photo_z_err_lo, ph.photo_z_err_hi
      FROM object_photometry ph
      WHERE ph.object_id IS NOT NULL
        AND (ph.field || '/' || ph.catalog_name) = ANY (comp.photometry)
      ORDER BY ph.object_id, ph.updated_at DESC
    ),
    objs AS MATERIALIZED (
      SELECT o.id, o.object_id, o.field, o.ra, o.dec,
             -- the frozen access scope IS the evidence: programs of the
             -- published members (a program whose only member is a draft has
             -- nothing in the release)
             ev.programs AS programs, ev.programs AS evidence_programs,
             agg.observations, agg.gratings, agg.n_targets::int AS n_targets, agg.n_spectra::int AS n_spectra,
             agg.max_snr, agg.max_exposure_time,
             phz.photo_z, phz.photo_z_err_lo, phz.photo_z_err_hi, (phz.object_id IS NOT NULL) AS has_photometry,
             zsrc.id AS z_source_spectrum_id,
             -- the auto redshift of the published anchor spectrum (the live
             -- value may come from an excluded member)
             zsrc.z AS redshift_auto,
             o.redshift_inspected,
             (CASE WHEN o.redshift_quality = 1 THEN NULL
                   ELSE COALESCE(o.redshift_inspected::double precision, zsrc.z) END)::numeric(10,6) AS redshift,
             o.redshift_quality, o.inspected_used_auto, o.last_inspected_at, o.last_inspected_by,
             o.version, o.needs_review, o.reviewed_spectrum_ids
      FROM objects o
      JOIN ev ON ev.obj_id = o.id
      JOIN agg ON agg.obj_id = o.id
      LEFT JOIN zsrc ON zsrc.obj_id = o.id
      LEFT JOIN phz ON phz.object_id = o.id
    ),
    ins_spectra AS (
      INSERT INTO dr_spectra (tag, spectrum_id, id, program_slug, observation, grating, target_id,
                              field, object_id, fits_path, file_hash, science_hash, file_size, exposure_time,
                              signal_to_noise, cfpipe_version, crds_context, jwst_version, date_obs,
                              reduced_at, redshift_auto, chi2_min, confidence, dq_flags)
      SELECT p_tag, p.spectrum_id, p.id, p.program_slug, p.observation, p.grating, p.target_id,
             p.target_field, p.obj_id, p.fits_path, p.file_hash, p.science_hash, p.file_size, p.exposure_time,
             p.signal_to_noise, p.cfpipe_version, p.crds_context, p.jwst_version, p.date_obs,
             p.reduced_at, p.redshift_auto, p.chi2_min, p.confidence, p.dq_flags
      FROM pub p
      RETURNING 1
    ),
    ins_objects AS (
      INSERT INTO dr_objects (tag, object_id, name, field, ra, dec, programs, evidence_programs,
                              observations, gratings, n_targets, n_spectra, max_snr, max_exposure_time,
                              photo_z, photo_z_err_lo, photo_z_err_hi, has_photometry,
                              z_source_spectrum_id, redshift_auto, redshift_inspected, redshift,
                              redshift_quality, inspected_used_auto, last_inspected_at,
                              last_inspected_by, version, review_state)
      SELECT p_tag, o.id, o.object_id, o.field, o.ra, o.dec, o.programs, o.evidence_programs,
             o.observations, o.gratings, o.n_targets, o.n_spectra, o.max_snr, o.max_exposure_time,
             o.photo_z, o.photo_z_err_lo, o.photo_z_err_hi, o.has_photometry,
             o.z_source_spectrum_id, o.redshift_auto, o.redshift_inspected, o.redshift,
             o.redshift_quality, o.inspected_used_auto, o.last_inspected_at,
             o.last_inspected_by, o.version,
             jsonb_build_object('needs_review', o.needs_review,
                                'reviewed_spectrum_ids', o.reviewed_spectrum_ids)
      FROM objs o
      RETURNING 1
    ),
    ins_lines AS (
      INSERT INTO dr_line_fits (tag, spectrum_id, target_id, grating, program_slug, observation,
                                object_id, object_version, z_used, z_source, z_quality, z_fit,
                                z_fit_err, dv, dv_err, sigma_v, sigma_v_err, kin_source, n_lines,
                                n_detected, n_broad, chi2, dof, lines, fit_version, cfpipe_version,
                                f_lsf, spectrum_hash, spectrum_science_hash, fitted_at,
                                stale_redshift, stale_spectrum)
      -- target / grating / program / observation from the JOINED (frozen)
      -- spectrum, not the fit's own denormalized copies: those can be stale
      -- after a metadata correction, and dr_line_fits authorizes on
      -- program_slug, like the live policy authorizes through spectra (Codex on #599)
      SELECT p_tag, f.spectrum_id, p.target_id, p.grating, p.program_slug, p.observation,
             f.object_id, f.object_version, f.z_used, f.z_source, f.z_quality, f.z_fit,
             f.z_fit_err, f.dv, f.dv_err, f.sigma_v, f.sigma_v_err, f.kin_source, f.n_lines,
             f.n_detected, f.n_broad, f.chi2, f.dof, f.lines, f.fit_version, f.cfpipe_version,
             f.f_lsf, f.spectrum_hash, f.spectrum_science_hash, f.fitted_at,
             -- the spectrum_line_fits_status predicates, in this snapshot
             (o.id IS NOT NULL AND line_fit_stale_redshift(o.version, o.redshift_quality,
                 o.redshift::double precision, f.object_version, f.z_quality, f.z_used)),
             line_fit_stale_spectrum(p.file_hash, p.science_hash, f.spectrum_hash, f.spectrum_science_hash)
      FROM spectrum_line_fits f
      JOIN pub p ON p.id = f.spectrum_id
      LEFT JOIN objs o ON o.id = p.obj_id
      RETURNING 1
    ),
    ins_phot AS (
      INSERT INTO dr_photometry (tag, photometry_id, object_id, programs, field, catalog_name,
                                 catalog_id, ra, dec, match_distance_arcsec, photometry, photo_z,
                                 photo_z_err_lo, photo_z_err_hi, has_pz)
      SELECT p_tag, ph.id, ph.object_id, o.programs, ph.field, ph.catalog_name, ph.catalog_id,
             ph.ra, ph.dec, ph.match_distance_arcsec, ph.photometry, ph.photo_z,
             ph.photo_z_err_lo, ph.photo_z_err_hi, ph.has_pz
      FROM object_photometry ph
      JOIN objs o ON o.id = ph.object_id
      WHERE (ph.field || '/' || ph.catalog_name) = ANY (comp.photometry)
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
           pg_snapshot_xmin(pg_current_snapshot())::text::bigint
      INTO v_n_spectra, v_n_objects, v_n_lines, v_n_phot, v_n_manifest, v_xmin;

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
             'by_component', (SELECT COALESCE(jsonb_object_agg(t.component, jsonb_build_object('files', t.n, 'bytes', t.b)), '{}'::jsonb)
                              FROM (SELECT m.component, count(*) AS n, COALESCE(sum(m.size_bytes), 0) AS b
                                    FROM dr_manifest m WHERE m.tag = p_tag GROUP BY m.component) t),
             'nirspec_programs', (SELECT count(DISTINCT s.program_slug) FROM dr_spectra s WHERE s.tag = p_tag),
             'nirspec_fields', (SELECT count(DISTINCT s.field) FROM dr_spectra s WHERE s.tag = p_tag AND s.field IS NOT NULL)),
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
