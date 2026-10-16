SET lock_timeout = '5s';
SET statement_timeout = '60s';

DROP VIEW "public"."cat_fields";

DROP VIEW "public"."cat_filter_options";

DROP VIEW "public"."cat_line_fits";

DROP VIEW "public"."cat_nircam_images";

DROP VIEW "public"."cat_objects";

DROP VIEW "public"."cat_photometry_bands";

DROP VIEW "public"."cat_photometry";

DROP VIEW "public"."cat_spectra";

DROP VIEW "public"."cat_spectrum_lines";

DROP VIEW "public"."cat_targets";

CREATE VIEW "public"."cat_fields" WITH (security_invoker=true) AS  SELECT name,
    display_name,
    filters,
    tiles,
    fiducial_tiles,
    epochs,
    programs,
    jwst_program_ids,
    file_globs,
    center_ra,
    center_dec,
    config,
    coverage_area_arcmin2,
    coverage_area_deg2,
    latest_deployment_id,
    created_at,
    expmap_pixel_scale_arcsec,
    config_hash,
    config_updated_at,
    retired_at
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            fields.name,
            fields.display_name,
            fields.filters,
            fields.tiles,
            fields.fiducial_tiles,
            fields.epochs,
            fields.programs,
            fields.jwst_program_ids,
            fields.file_globs,
            fields.center_ra,
            fields.center_dec,
            fields.config,
            fields.coverage_area_arcmin2,
            fields.coverage_area_deg2,
            fields.latest_deployment_id,
            fields.created_at,
            fields.expmap_pixel_scale_arcsec,
            fields.config_hash,
            fields.config_updated_at,
            fields.retired_at
           FROM public.fields
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_fields.tag AS cat_tag,
            dr_fields.name,
            dr_fields.display_name,
            dr_fields.filters,
            dr_fields.tiles,
            dr_fields.fiducial_tiles,
            dr_fields.epochs,
            dr_fields.programs,
            dr_fields.jwst_program_ids,
            dr_fields.file_globs,
            dr_fields.center_ra,
            dr_fields.center_dec,
            dr_fields.config,
            dr_fields.coverage_area_arcmin2,
            dr_fields.coverage_area_deg2,
            dr_fields.latest_deployment_id,
            dr_fields.created_at,
            dr_fields.expmap_pixel_scale_arcsec,
            dr_fields.config_hash,
            dr_fields.config_updated_at,
            dr_fields.retired_at
           FROM public.dr_fields) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_fields" FROM "anon";

CREATE VIEW "public"."cat_filter_options" WITH (security_invoker=true) AS  SELECT id,
    fields,
    observations,
    gratings,
    photometry_bands
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            mv_filter_options.id,
            mv_filter_options.fields,
            mv_filter_options.observations,
            mv_filter_options.gratings,
            mv_filter_options.photometry_bands
           FROM public.mv_filter_options
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_filter_options.tag AS cat_tag,
            dr_filter_options.id,
            dr_filter_options.fields,
            dr_filter_options.observations,
            dr_filter_options.gratings,
            dr_filter_options.photometry_bands
           FROM public.dr_filter_options) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "anon";

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
    lines,
    fit_version,
    cfpipe_version,
    f_lsf,
    spectrum_hash,
    fitted_at,
    created_at,
    updated_at,
    spectrum_science_hash
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            spectrum_line_fits.spectrum_id,
            spectrum_line_fits.target_id,
            spectrum_line_fits.grating,
            spectrum_line_fits.program_slug,
            spectrum_line_fits.observation,
            spectrum_line_fits.z_used,
            spectrum_line_fits.z_source,
            spectrum_line_fits.z_quality,
            spectrum_line_fits.object_id,
            spectrum_line_fits.object_version,
            spectrum_line_fits.z_fit,
            spectrum_line_fits.z_fit_err,
            spectrum_line_fits.dv,
            spectrum_line_fits.dv_err,
            spectrum_line_fits.sigma_v,
            spectrum_line_fits.sigma_v_err,
            spectrum_line_fits.kin_source,
            spectrum_line_fits.n_lines,
            spectrum_line_fits.n_detected,
            spectrum_line_fits.n_broad,
            spectrum_line_fits.chi2,
            spectrum_line_fits.dof,
            spectrum_line_fits.lines,
            spectrum_line_fits.fit_version,
            spectrum_line_fits.cfpipe_version,
            spectrum_line_fits.f_lsf,
            spectrum_line_fits.spectrum_hash,
            spectrum_line_fits.fitted_at,
            spectrum_line_fits.created_at,
            spectrum_line_fits.updated_at,
            spectrum_line_fits.spectrum_science_hash
           FROM public.spectrum_line_fits
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
            dr_line_fits.lines,
            dr_line_fits.fit_version,
            dr_line_fits.cfpipe_version,
            dr_line_fits.f_lsf,
            dr_line_fits.spectrum_hash,
            dr_line_fits.fitted_at,
            dr_line_fits.created_at,
            dr_line_fits.updated_at,
            dr_line_fits.spectrum_science_hash
           FROM public.dr_line_fits) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "anon";

CREATE VIEW "public"."cat_nircam_images" WITH (security_invoker=true) AS  SELECT id,
    field,
    tile,
    filter,
    pixel_scale,
    extension,
    file_path,
    created_at,
    file_size,
    deploy_status,
    deployment_id,
    epoch
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            nircam_images.id,
            nircam_images.field,
            nircam_images.tile,
            nircam_images.filter,
            nircam_images.pixel_scale,
            nircam_images.extension,
            nircam_images.file_path,
            nircam_images.created_at,
            nircam_images.file_size,
            nircam_images.deploy_status,
            nircam_images.deployment_id,
            nircam_images.epoch
           FROM public.nircam_images
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_nircam_images.tag AS cat_tag,
            dr_nircam_images.id,
            dr_nircam_images.field,
            dr_nircam_images.tile,
            dr_nircam_images.filter,
            dr_nircam_images.pixel_scale,
            dr_nircam_images.extension,
            dr_nircam_images.file_path,
            dr_nircam_images.created_at,
            dr_nircam_images.file_size,
            dr_nircam_images.deploy_status,
            dr_nircam_images.deployment_id,
            dr_nircam_images.epoch
           FROM public.dr_nircam_images) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "anon";

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
    needs_review
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
            objects.needs_review
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
            dr_objects.needs_review
           FROM public.dr_objects) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_objects" FROM "anon";

CREATE VIEW "public"."cat_photometry_bands" WITH (security_invoker=true) AS  SELECT photometry_id,
    band,
    flux,
    flux_err,
    wav,
    snr,
    mag
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            object_photometry_bands.photometry_id,
            object_photometry_bands.band,
            object_photometry_bands.flux,
            object_photometry_bands.flux_err,
            object_photometry_bands.wav,
            object_photometry_bands.snr,
            object_photometry_bands.mag
           FROM public.object_photometry_bands
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_photometry_bands.tag AS cat_tag,
            dr_photometry_bands.photometry_id,
            dr_photometry_bands.band,
            dr_photometry_bands.flux,
            dr_photometry_bands.flux_err,
            dr_photometry_bands.wav,
            dr_photometry_bands.snr,
            dr_photometry_bands.mag
           FROM public.dr_photometry_bands) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "anon";

CREATE VIEW "public"."cat_photometry" WITH (security_invoker=true) AS  SELECT id,
    object_id,
    field,
    ra,
    "dec",
    catalog_name,
    catalog_id,
    match_distance_arcsec,
    photometry,
    photo_z,
    photo_z_err_lo,
    photo_z_err_hi,
    has_pz,
    created_at,
    updated_at
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            object_photometry.id,
            object_photometry.object_id,
            object_photometry.field,
            object_photometry.ra,
            object_photometry."dec",
            object_photometry.catalog_name,
            object_photometry.catalog_id,
            object_photometry.match_distance_arcsec,
            object_photometry.photometry,
            object_photometry.photo_z,
            object_photometry.photo_z_err_lo,
            object_photometry.photo_z_err_hi,
            object_photometry.has_pz,
            object_photometry.created_at,
            object_photometry.updated_at
           FROM public.object_photometry
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_photometry.tag AS cat_tag,
            dr_photometry.id,
            dr_photometry.object_id,
            dr_photometry.field,
            dr_photometry.ra,
            dr_photometry."dec",
            dr_photometry.catalog_name,
            dr_photometry.catalog_id,
            dr_photometry.match_distance_arcsec,
            dr_photometry.photometry,
            dr_photometry.photo_z,
            dr_photometry.photo_z_err_lo,
            dr_photometry.photo_z_err_hi,
            dr_photometry.has_pz,
            dr_photometry.created_at,
            dr_photometry.updated_at
           FROM public.dr_photometry) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "anon";

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
    science_hash
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
            spectra.science_hash
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
            dr_spectra.science_hash
           FROM public.dr_spectra) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "anon";

CREATE VIEW "public"."cat_spectrum_lines" WITH (security_invoker=true) AS  SELECT spectrum_id,
    line,
    component,
    wave_rest,
    flux,
    flux_err,
    snr,
    ew_rest,
    ew_rest_err,
    flags,
    blend_into
   FROM ( SELECT 'live'::text AS cat_src,
            ''::text AS cat_tag,
            spectrum_lines.spectrum_id,
            spectrum_lines.line,
            spectrum_lines.component,
            spectrum_lines.wave_rest,
            spectrum_lines.flux,
            spectrum_lines.flux_err,
            spectrum_lines.snr,
            spectrum_lines.ew_rest,
            spectrum_lines.ew_rest_err,
            spectrum_lines.flags,
            spectrum_lines.blend_into
           FROM public.spectrum_lines
        UNION ALL
         SELECT 'dr'::text AS cat_src,
            dr_spectrum_lines.tag AS cat_tag,
            dr_spectrum_lines.spectrum_id,
            dr_spectrum_lines.line,
            dr_spectrum_lines.component,
            dr_spectrum_lines.wave_rest,
            dr_spectrum_lines.flux,
            dr_spectrum_lines.flux_err,
            dr_spectrum_lines.snr,
            dr_spectrum_lines.ew_rest,
            dr_spectrum_lines.ew_rest_err,
            dr_spectrum_lines.flags,
            dr_spectrum_lines.blend_into
           FROM public.dr_spectrum_lines) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "anon";

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
    has_published_spectrum
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
            targets.has_published_spectrum
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
            dr_targets.has_published_spectrum
           FROM public.dr_targets) u
  WHERE ((cat_src =
        CASE
            WHEN (public.dr_browse_tag() IS NULL) THEN 'live'::text
            ELSE 'dr'::text
        END) AND (cat_tag = COALESCE(public.dr_browse_tag(), ''::text)));

REVOKE ALL ON TABLE "public"."cat_targets" FROM "anon";

COMMENT ON VIEW "public"."cat_fields" IS 'Catalog source view: fields for the live catalog, dr_fields rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_filter_options" IS 'Catalog source view: mv_filter_options for the live catalog, the browsed release''s dr_filter_options row otherwise.';

COMMENT ON VIEW "public"."cat_line_fits" IS 'Catalog source view: spectrum_line_fits for the live catalog, dr_line_fits rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_nircam_images" IS 'Catalog source view: nircam_images for the live catalog, dr_nircam_images rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_objects" IS 'Catalog source view: objects for the live catalog, dr_objects rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_photometry" IS 'Catalog source view: object_photometry for the live catalog, dr_photometry rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_photometry_bands" IS 'Catalog source view: object_photometry_bands for the live catalog, dr_photometry_bands rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_spectra" IS 'Catalog source view: spectra for the live catalog, dr_spectra rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_spectrum_lines" IS 'Catalog source view: spectrum_lines for the live catalog, dr_spectrum_lines rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

COMMENT ON VIEW "public"."cat_targets" IS 'Catalog source view: targets for the live catalog, dr_targets rows of the browsed release (dr_browse_tag()) otherwise. Read by the catalog RPCs; never written.';

REVOKE ALL ON TABLE "public"."cat_fields" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_fields" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_fields" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_fields" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_fields" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_fields" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_filter_options" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_filter_options" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_filter_options" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_filter_options" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_line_fits" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_line_fits" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_line_fits" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_line_fits" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_nircam_images" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_nircam_images" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_nircam_images" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_nircam_images" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_objects" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_objects" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_objects" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_objects" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_photometry" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_photometry" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_photometry" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_photometry" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_photometry_bands" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_photometry_bands" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_photometry_bands" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_photometry_bands" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectra" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectra" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectra" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_spectrum_lines" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_spectrum_lines" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_spectrum_lines" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_spectrum_lines" TO "service_role";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "authenticated";

GRANT SELECT ON TABLE "public"."cat_targets" TO "authenticated";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."cat_targets" TO "postgres";

REVOKE ALL ON TABLE "public"."cat_targets" FROM "service_role";

GRANT SELECT ON TABLE "public"."cat_targets" TO "service_role";
