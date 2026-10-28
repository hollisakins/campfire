SET lock_timeout = '5s';
SET statement_timeout = '60s';

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

REVOKE ALL ON SCHEMA "public" FROM "campfire_reader";

GRANT USAGE ON SCHEMA "public" TO "campfire_reader";

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

REVOKE ALL ON TABLE "public"."dr_objects" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."dr_objects" TO "campfire_reader";

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

REVOKE ALL ON TABLE "public"."objects" FROM "campfire_reader";

GRANT SELECT ON TABLE "public"."objects" TO "campfire_reader";

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
