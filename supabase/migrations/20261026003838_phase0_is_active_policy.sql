SET lock_timeout = '5s';
SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP POLICY "select_comments_by_access" ON "public"."comments";

DROP POLICY "insert_audit_by_access" ON "public"."flag_audit_log";

DROP POLICY "select_audit_by_access" ON "public"."flag_audit_log";

DROP POLICY "select_list_members" ON "public"."object_list_members";

DROP POLICY "select_object_photometry_by_access" ON "public"."object_photometry";

DROP POLICY "select_objects_by_access" ON "public"."objects";

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
    -- Gate 2: verbatim select_objects_by_access (policies.sql) for every
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
    -- Gate 2b: verbatim select_objects_by_access (policies.sql), same callers.
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

CREATE POLICY "select_comments_by_access" ON "public"."comments"
  FOR SELECT
  TO "authenticated"
  USING ((( SELECT (NOT public.is_link_account())) AND (((target_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.targets t
  WHERE
    ((t.id = comments.target_id) AND (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND (t.has_published_spectrum OR (
    SELECT public.is_admin() AS is_admin)))))) OR ((target_id IS NULL) AND (object_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.objects o
  WHERE
    ((o.id = comments.object_id) AND (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND (o.has_published_spectrum OR ( SELECT
    public.is_admin() AS is_admin)) AND (o.is_active OR ( SELECT public.is_admin() AS is_admin)))))))));

CREATE POLICY "insert_audit_by_access" ON "public"."flag_audit_log"
  FOR INSERT
  TO "authenticated"
  WITH CHECK ((((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE
    ((t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND (t.has_published_spectrum OR ( SELECT public.is_admin() AS
    is_admin)))))) OR ((object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE
    ((o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND (o.has_published_spectrum OR ( SELECT public.is_admin() AS is_admin)) AND
    (o.is_active OR ( SELECT public.is_admin() AS is_admin)))))) OR ((spectrum_id IS NOT NULL) AND (spectrum_id IN ( SELECT s.id
   FROM (public.spectra s
     JOIN public.targets t ON ((t.target_id = s.target_id)))
  WHERE
    ((t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND ((s.deploy_status = 'published'::text) OR ( SELECT
    public.is_admin() AS is_admin))))))));

CREATE POLICY "select_audit_by_access" ON "public"."flag_audit_log"
  FOR SELECT
  TO "authenticated"
  USING ((( SELECT (NOT public.is_link_account())) AND (((target_id IS NOT NULL) AND (target_id IN ( SELECT t.id
   FROM public.targets t
  WHERE
    ((t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND (t.has_published_spectrum OR ( SELECT public.is_admin() AS
    is_admin)))))) OR ((object_id IS NOT NULL) AND (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE
    ((o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND (o.has_published_spectrum OR ( SELECT public.is_admin() AS is_admin)) AND
    (o.is_active OR ( SELECT public.is_admin() AS is_admin)))))) OR ((spectrum_id IS NOT NULL) AND (spectrum_id IN ( SELECT s.id
   FROM (public.spectra s
     JOIN public.targets t ON ((t.target_id = s.target_id)))
  WHERE
    ((t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND ((s.deploy_status = 'published'::text) OR ( SELECT
    public.is_admin() AS is_admin)))))))));

CREATE POLICY "select_list_members" ON "public"."object_list_members"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT (NOT public.is_link_account())) AND (list_id IN ( SELECT public.viewable_list_ids() AS viewable_list_ids)) AND (((object_id IS NULL) AND (list_id IN ( SELECT
    public.member_editable_list_ids() AS member_editable_list_ids))) OR (object_id IN ( SELECT o.id
   FROM public.objects o
  WHERE
    ((o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND (o.has_published_spectrum OR ( SELECT public.is_admin() AS is_admin)) AND
    (o.is_active OR ( SELECT public.is_admin() AS is_admin))))))));

CREATE POLICY "select_object_photometry_by_access" ON "public"."object_photometry"
  FOR SELECT
  TO "authenticated"
  USING (((object_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.objects o
  WHERE
    ((o.id = object_photometry.object_id) AND (o.programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND (o.has_published_spectrum OR ( SELECT
    public.is_admin() AS is_admin) OR ( SELECT public.link_sees_drafts() AS link_sees_drafts)) AND (o.is_active OR ( SELECT public.is_admin() AS is_admin)) AND
    (( SELECT (NOT public.is_link_account())) OR (o.observations && ARRAY[( SELECT public.link_observation() AS link_observation)])))))));

CREATE POLICY "select_objects_by_access" ON "public"."objects"
  FOR SELECT
  TO "authenticated"
  USING
    (((programs && ( SELECT public.accessible_program_slugs() AS accessible_program_slugs)) AND (has_published_spectrum OR ( SELECT public.is_admin() AS is_admin) OR ( SELECT
    public.link_sees_drafts() AS link_sees_drafts)) AND (is_active OR ( SELECT public.is_admin() AS is_admin)) AND
    (( SELECT (NOT public.is_link_account())) OR (observations && ARRAY[( SELECT public.link_observation() AS link_observation)]))));
