SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE SCHEMA "reader";

CREATE VIEW "reader"."dr_objects" WITH (security_barrier=true) AS  WITH pub AS (
         SELECT COALESCE(array_agg(p.slug), '{}'::text[]) AS slugs
           FROM public.programs p
          WHERE p.is_public
        )
 SELECT o.tag,
    o.id,
    o.object_id,
    o.field,
    o.ra,
    o."dec",
    o.created_at,
        CASE
            WHEN a.use_stored THEN o.n_targets
            ELSE a.n_targets
        END AS n_targets,
        CASE
            WHEN a.use_stored THEN o.n_spectra
            ELSE a.n_spectra
        END AS n_spectra,
        CASE
            WHEN a.use_stored THEN o.programs
            ELSE a.programs
        END AS programs,
        CASE
            WHEN a.use_stored THEN o.gratings
            ELSE a.gratings
        END AS gratings,
        CASE
            WHEN a.use_stored THEN o.observations
            ELSE a.observations
        END AS observations,
        CASE
            WHEN a.use_stored THEN o.max_snr
            ELSE a.max_snr
        END AS max_snr,
        CASE
            WHEN a.use_stored THEN o.max_exposure_time
            ELSE a.max_exposure_time
        END AS max_exposure_time,
    o.photo_z,
    o.photo_z_err_lo,
    o.photo_z_err_hi,
    o.has_photometry,
    o.updated_at,
    o.redshift_auto,
    o.redshift_inspected,
    o.redshift_quality,
    o.inspected_used_auto,
    o.redshift,
    o.last_inspected_at,
    o.last_inspected_by,
    o.last_data_change_at,
    o.staleness_reason,
    o.reviewed_spectrum_ids,
    o.needs_review,
    o.version,
    o.is_active,
    o.has_published_spectrum,
        CASE
            WHEN a.use_stored THEN o.search_text
            ELSE a.search_text
        END AS search_text,
        CASE
            WHEN a.use_stored THEN o.evidence_programs
            ELSE ARRAY( SELECT e.e
               FROM unnest(o.evidence_programs) e(e)
              WHERE (e.e = ANY (a.programs))
              ORDER BY e.e)
        END AS evidence_programs,
        CASE
            WHEN (a.use_stored OR (EXISTS ( SELECT 1
               FROM public.dr_spectra s
              WHERE ((s.tag = o.tag) AND (s.id = o.z_source_spectrum_id) AND s.is_public AND (s.deploy_status = 'published'::text))))) THEN o.z_source_spectrum_id
            ELSE NULL::integer
        END AS z_source_spectrum_id,
    o.is_public
   FROM ((public.dr_objects o
     CROSS JOIN pub)
     CROSS JOIN LATERAL ( WITH m AS (
                 SELECT t.target_id,
                    t.program_slug,
                    t.observation
                   FROM public.dr_targets t
                  WHERE ((t.tag = o.tag) AND (t.object_id = o.id) AND t.is_public AND t.has_published_spectrum)
                ), sp AS (
                 SELECT s.grating,
                    s.signal_to_noise,
                    s.exposure_time
                   FROM public.dr_spectra s
                  WHERE ((s.tag = o.tag) AND (s.target_id IN ( SELECT m.target_id
                           FROM m)) AND s.is_public AND (s.deploy_status = 'published'::text))
                )
         SELECT ((o.programs <@ pub.slugs) AND (NOT (EXISTS ( SELECT 1
                   FROM public.dr_targets t
                  WHERE ((t.tag = o.tag) AND (t.object_id = o.id) AND ((NOT t.is_public) OR (NOT t.has_published_spectrum)))))) AND (NOT (EXISTS ( SELECT 1
                   FROM (public.dr_targets t
                     JOIN public.dr_spectra s ON (((s.tag = t.tag) AND (s.target_id = t.target_id))))
                  WHERE ((t.tag = o.tag) AND (t.object_id = o.id) AND ((NOT s.is_public) OR (s.deploy_status <> 'published'::text)))))) AND (o.n_spectra = ( SELECT count(*) AS count
                   FROM (public.dr_targets t
                     JOIN public.dr_spectra s ON (((s.tag = t.tag) AND (s.target_id = t.target_id))))
                  WHERE ((t.tag = o.tag) AND (t.object_id = o.id)))) AND (o.n_targets = ( SELECT count(*) AS count
                   FROM public.dr_targets t
                  WHERE ((t.tag = o.tag) AND (t.object_id = o.id))))) AS use_stored,
            COALESCE(( SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) AS array_agg
                   FROM m), '{}'::text[]) AS programs,
            COALESCE(( SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) AS array_agg
                   FROM sp
                  WHERE (sp.grating IS NOT NULL)), '{}'::text[]) AS gratings,
            COALESCE(( SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) AS array_agg
                   FROM m
                  WHERE (m.observation IS NOT NULL)), '{}'::text[]) AS observations,
            (( SELECT count(*) AS count
                   FROM m))::integer AS n_targets,
            (( SELECT count(*) AS count
                   FROM sp))::integer AS n_spectra,
            ( SELECT max(sp.signal_to_noise) AS max
                   FROM sp) AS max_snr,
            ( SELECT max(sp.exposure_time) AS max
                   FROM sp) AS max_exposure_time,
            concat_ws(' '::text, o.object_id, ( SELECT string_agg(DISTINCT m.target_id, ' '::text) AS string_agg
                   FROM m), ( SELECT string_agg(DISTINCT m.program_slug, ' '::text) AS string_agg
                   FROM m), ( SELECT string_agg(DISTINCT m.observation, ' '::text) AS string_agg
                   FROM m)) AS search_text) a)
  WHERE (o.is_public AND o.is_active AND o.has_published_spectrum AND (EXISTS ( SELECT 1
           FROM public.dr_releases r
          WHERE ((r.tag = o.tag) AND (r.state = 'published'::text)))));

CREATE VIEW "reader"."objects" WITH (security_barrier=true) AS  WITH pub AS (
         SELECT COALESCE(array_agg(p.slug), '{}'::text[]) AS slugs
           FROM public.programs p
          WHERE p.is_public
        )
 SELECT o.id,
    o.object_id,
    o.field,
    o.ra,
    o."dec",
    o.created_at,
        CASE
            WHEN a.use_stored THEN o.n_targets
            ELSE a.n_targets
        END AS n_targets,
        CASE
            WHEN a.use_stored THEN o.n_spectra
            ELSE a.n_spectra
        END AS n_spectra,
        CASE
            WHEN a.use_stored THEN o.programs
            ELSE a.programs
        END AS programs,
        CASE
            WHEN a.use_stored THEN o.gratings
            ELSE a.gratings
        END AS gratings,
        CASE
            WHEN a.use_stored THEN o.observations
            ELSE a.observations
        END AS observations,
        CASE
            WHEN a.use_stored THEN o.max_snr
            ELSE a.max_snr
        END AS max_snr,
        CASE
            WHEN a.use_stored THEN o.max_exposure_time
            ELSE a.max_exposure_time
        END AS max_exposure_time,
    o.photo_z,
    o.photo_z_err_lo,
    o.photo_z_err_hi,
    o.has_photometry,
    o.updated_at,
    o.redshift_auto,
    o.redshift_inspected,
    o.redshift_quality,
    o.inspected_used_auto,
    o.redshift,
    o.last_inspected_at,
    o.last_inspected_by,
    o.last_data_change_at,
    o.staleness_reason,
    o.reviewed_spectrum_ids,
    o.needs_review,
    o.version,
    o.is_active,
    o.has_published_spectrum,
        CASE
            WHEN a.use_stored THEN o.search_text
            ELSE a.search_text
        END AS search_text,
    o.is_public
   FROM ((public.objects o
     CROSS JOIN pub)
     CROSS JOIN LATERAL ( WITH m AS (
                 SELECT t.target_id,
                    t.program_slug,
                    t.observation
                   FROM public.targets t
                  WHERE ((t.object_id = o.id) AND t.is_public AND t.has_published_spectrum)
                ), sp AS (
                 SELECT s.grating,
                    s.signal_to_noise,
                    s.exposure_time
                   FROM public.spectra s
                  WHERE ((s.target_id IN ( SELECT m.target_id
                           FROM m)) AND s.is_public AND (s.deploy_status = 'published'::text))
                )
         SELECT ((o.programs <@ pub.slugs) AND (NOT (EXISTS ( SELECT 1
                   FROM public.targets t
                  WHERE ((t.object_id = o.id) AND ((NOT t.is_public) OR (NOT t.has_published_spectrum)))))) AND (NOT (EXISTS ( SELECT 1
                   FROM (public.targets t
                     JOIN public.spectra s ON ((s.target_id = t.target_id)))
                  WHERE ((t.object_id = o.id) AND ((NOT s.is_public) OR (s.deploy_status <> 'published'::text))))))) AS use_stored,
            COALESCE(( SELECT array_agg(DISTINCT m.program_slug ORDER BY m.program_slug) AS array_agg
                   FROM m), '{}'::text[]) AS programs,
            COALESCE(( SELECT array_agg(DISTINCT sp.grating ORDER BY sp.grating) AS array_agg
                   FROM sp
                  WHERE (sp.grating IS NOT NULL)), '{}'::text[]) AS gratings,
            COALESCE(( SELECT array_agg(DISTINCT m.observation ORDER BY m.observation) AS array_agg
                   FROM m
                  WHERE (m.observation IS NOT NULL)), '{}'::text[]) AS observations,
            (( SELECT count(*) AS count
                   FROM m))::integer AS n_targets,
            (( SELECT count(*) AS count
                   FROM sp))::integer AS n_spectra,
            ( SELECT max(sp.signal_to_noise) AS max
                   FROM sp) AS max_snr,
            ( SELECT max(sp.exposure_time) AS max
                   FROM sp) AS max_exposure_time,
            concat_ws(' '::text, o.object_id, ( SELECT string_agg(DISTINCT m.target_id, ' '::text) AS string_agg
                   FROM m), ( SELECT string_agg(DISTINCT m.program_slug, ' '::text) AS string_agg
                   FROM m), ( SELECT string_agg(DISTINCT m.observation, ' '::text) AS string_agg
                   FROM m)) AS search_text) a)
  WHERE (o.is_public AND o.is_active AND o.has_published_spectrum);

CREATE POLICY "reader_select_catalog_matches" ON "public"."catalog_matches"
  FOR SELECT
  TO "campfire_reader"
  USING (((object_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.objects o
  WHERE (o.id = catalog_matches.object_id)))));

CREATE POLICY "reader_select_dr_catalog_matches" ON "public"."dr_catalog_matches"
  FOR SELECT
  TO "campfire_reader"
  USING (((object_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.dr_objects o
  WHERE ((o.tag = dr_catalog_matches.tag) AND (o.id = dr_catalog_matches.object_id))))));

CREATE POLICY "reader_select_dr_fields" ON "public"."dr_fields"
  FOR SELECT
  TO "campfire_reader"
  USING (((EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_fields.tag) AND (r.state = 'published'::text)))) AND (EXISTS ( SELECT 1
   FROM public.dr_nircam_images ni
  WHERE ((ni.tag = dr_fields.tag) AND (ni.field = dr_fields.name) AND (ni.deploy_status = 'published'::text))))));

CREATE POLICY "reader_select_dr_filter_options" ON "public"."dr_filter_options"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_filter_options.tag) AND (r.state = 'published'::text)))));

CREATE POLICY "reader_select_dr_line_fits" ON "public"."dr_line_fits"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.dr_spectra s
  WHERE ((s.tag = dr_line_fits.tag) AND (s.id = dr_line_fits.spectrum_id)))));

CREATE POLICY "reader_select_dr_lines" ON "public"."dr_lines"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.dr_spectra s
  WHERE ((s.tag = dr_lines.tag) AND (s.id = dr_lines.spectrum_id)))));

CREATE POLICY "reader_select_dr_manifest" ON "public"."dr_manifest"
  FOR SELECT
  TO "campfire_reader"
  USING (((EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_manifest.tag) AND (r.state = 'published'::text)))) AND ((program_slug IS NULL) OR (EXISTS ( SELECT 1
   FROM public.programs p
  WHERE ((p.slug = dr_manifest.program_slug) AND p.is_public))))));

CREATE POLICY "reader_select_dr_nircam_images" ON "public"."dr_nircam_images"
  FOR SELECT
  TO "campfire_reader"
  USING (((deploy_status = 'published'::text) AND (EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_nircam_images.tag) AND (r.state = 'published'::text))))));

CREATE POLICY "reader_select_dr_objects" ON "public"."dr_objects"
  FOR SELECT
  TO "campfire_reader"
  USING ((is_public AND is_active AND has_published_spectrum AND (EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_objects.tag) AND (r.state = 'published'::text))))));

CREATE POLICY "reader_select_dr_photometry" ON "public"."dr_photometry"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.dr_catalog_matches p
  WHERE ((p.tag = dr_photometry.tag) AND (p.id = dr_photometry.match_id)))));

CREATE POLICY "reader_select_dr_releases" ON "public"."dr_releases"
  FOR SELECT
  TO "campfire_reader"
  USING ((state = 'published'::text));

CREATE POLICY "reader_select_dr_spectra" ON "public"."dr_spectra"
  FOR SELECT
  TO "campfire_reader"
  USING ((is_public AND (deploy_status = 'published'::text) AND (EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_spectra.tag) AND (r.state = 'published'::text))))));

CREATE POLICY "reader_select_dr_targets" ON "public"."dr_targets"
  FOR SELECT
  TO "campfire_reader"
  USING ((is_public AND has_published_spectrum AND (EXISTS ( SELECT 1
   FROM public.dr_releases r
  WHERE ((r.tag = dr_targets.tag) AND (r.state = 'published'::text))))));

CREATE POLICY "reader_select_fields" ON "public"."fields"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.nircam_images ni
  WHERE ((ni.field = fields.name) AND (ni.deploy_status = 'published'::text)))));

CREATE POLICY "reader_select_line_fits" ON "public"."line_fits"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.spectra s
  WHERE (s.id = line_fits.spectrum_id))));

CREATE POLICY "reader_select_lines" ON "public"."lines"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.spectra s
  WHERE (s.id = lines.spectrum_id))));

CREATE POLICY "reader_select_nircam_images" ON "public"."nircam_images"
  FOR SELECT
  TO "campfire_reader"
  USING ((deploy_status = 'published'::text));

CREATE POLICY "reader_select_objects" ON "public"."objects"
  FOR SELECT
  TO "campfire_reader"
  USING ((is_public AND is_active AND has_published_spectrum));

CREATE POLICY "reader_select_observations" ON "public"."observations"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.programs p
  WHERE ((p.slug = observations.program_slug) AND p.is_public))));

CREATE POLICY "reader_select_photometry" ON "public"."photometry"
  FOR SELECT
  TO "campfire_reader"
  USING ((EXISTS ( SELECT 1
   FROM public.catalog_matches p
  WHERE (p.id = photometry.match_id))));

CREATE POLICY "reader_select_programs" ON "public"."programs"
  FOR SELECT
  TO "campfire_reader"
  USING (is_public);

CREATE POLICY "reader_select_spectra" ON "public"."spectra"
  FOR SELECT
  TO "campfire_reader"
  USING ((is_public AND (deploy_status = 'published'::text)));

CREATE POLICY "reader_select_targets" ON "public"."targets"
  FOR SELECT
  TO "campfire_reader"
  USING ((is_public AND has_published_spectrum));

COMMENT ON VIEW "reader"."dr_objects" IS 'dr_objects for the public SQL reader (campfire_reader): the frozen public objects of published releases, their member aggregates computed over the frozen public published members only. Same columns as public.dr_objects.';

COMMENT ON VIEW "reader"."objects" IS 'objects for the public SQL reader (campfire_reader): the public, active objects with a published spectrum, their member aggregates computed over public published members only. Same columns as public.objects.';

REVOKE ALL ON SCHEMA "public" FROM "campfire_reader";

GRANT USAGE ON SCHEMA "public" TO "campfire_reader";

GRANT USAGE ON SCHEMA "reader" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."catalog_matches" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."catalog_matches" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_catalog_matches" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_catalog_matches" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_fields" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_fields" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_filter_options" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_filter_options" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_line_fits" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_line_fits" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_lines" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_lines" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_manifest" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_manifest" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_nircam_images" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_nircam_images" TO "campfire_reader";

REVOKE ALL ("created_at") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("created_at") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("dec") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("dec") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("field") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("field") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("has_photometry") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("has_photometry") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("has_published_spectrum") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("has_published_spectrum") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("id") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("id") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("inspected_used_auto") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("inspected_used_auto") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("is_active") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("is_active") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("is_public") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("is_public") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("last_data_change_at") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("last_data_change_at") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("last_inspected_at") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("last_inspected_at") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("last_inspected_by") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("last_inspected_by") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("needs_review") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("needs_review") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("object_id") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("object_id") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("photo_z_err_hi") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("photo_z_err_hi") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("photo_z_err_lo") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("photo_z_err_lo") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("photo_z") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("photo_z") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("ra") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("ra") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("redshift_auto") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("redshift_auto") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("redshift_inspected") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("redshift_inspected") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("redshift_quality") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("redshift_quality") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("redshift") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("redshift") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("reviewed_spectrum_ids") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("reviewed_spectrum_ids") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("staleness_reason") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("staleness_reason") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("tag") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("tag") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("updated_at") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("updated_at") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ("version") ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ("version") ON TABLE "public"."dr_objects" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_photometry" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_photometry" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_releases" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_releases" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_spectra" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_spectra" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."dr_targets" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_targets" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."fields" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."fields" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."line_fits" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."line_fits" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."lines" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."lines" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."nircam_images" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."nircam_images" TO "campfire_reader";

REVOKE ALL ("created_at") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("created_at") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("dec") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("dec") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("field") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("field") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("has_photometry") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("has_photometry") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("has_published_spectrum") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("has_published_spectrum") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("id") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("id") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("inspected_used_auto") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("inspected_used_auto") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("is_active") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("is_active") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("is_public") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("is_public") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("last_data_change_at") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("last_data_change_at") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("last_inspected_at") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("last_inspected_at") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("last_inspected_by") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("last_inspected_by") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("needs_review") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("needs_review") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("object_id") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("object_id") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("photo_z_err_hi") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("photo_z_err_hi") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("photo_z_err_lo") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("photo_z_err_lo") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("photo_z") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("photo_z") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("ra") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("ra") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("redshift_auto") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("redshift_auto") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("redshift_inspected") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("redshift_inspected") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("redshift_quality") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("redshift_quality") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("redshift") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("redshift") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("reviewed_spectrum_ids") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("reviewed_spectrum_ids") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("staleness_reason") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("staleness_reason") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("updated_at") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("updated_at") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ("version") ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ("version") ON TABLE "public"."objects" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."observations" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."observations" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."photometry" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."photometry" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."programs" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."programs" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."spectra" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."spectra" TO "campfire_reader";

REVOKE ALL ON TABLE "public"."targets" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."targets" TO "campfire_reader";

GRANT SELECT ON TABLE "reader"."dr_objects" TO "campfire_reader";

GRANT SELECT ON TABLE "reader"."objects" TO "campfire_reader";
