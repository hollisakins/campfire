SET lock_timeout = '5s'; SET statement_timeout = '60s';

SET local check_function_bodies = off;

DROP POLICY "select_storage_objects_by_access" ON "public"."storage_objects";

CREATE OR REPLACE FUNCTION public.filter_accessible_storage_keys (
  p_keys                text[],
  p_program_slugs       text[],
  p_include_unpublished boolean DEFAULT false
)
  RETURNS TABLE (
    storage_key text
  )
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT so.storage_key
  FROM storage_objects so
  WHERE so.storage_key = ANY(p_keys)
    AND (
      (so.status = 'active'
       AND (
         p_include_unpublished
         OR (so.spectrum_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM spectra s
               JOIN targets t ON t.target_id = s.target_id
               WHERE s.spectrum_id = so.spectrum_id
                 AND s.deploy_status = 'published'
                 AND t.program_slug = ANY(p_program_slugs)))
         OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM deployments d
               LEFT JOIN observations o ON o.name = d.observation
               WHERE d.id = so.deployment_id
                 AND d.status = 'published'
                 -- NIRCam field deploy (epic #261, N1): multi-program, public to all
                 -- when published. NIRSpec obs deploy stays program-scoped.
                 AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))))
       ))
      OR
      (so.root = 'archive' AND so.status = 'archived'
       AND (
         p_include_unpublished
         OR (so.spectrum_id IS NOT NULL
             AND so.program_slug = ANY(p_program_slugs)
             AND EXISTS (
               SELECT 1 FROM spectra s
               WHERE s.spectrum_id = so.spectrum_id
                 AND s.deploy_status = 'published'))
       ))
    );
$function$;

CREATE OR REPLACE FUNCTION public.filter_link_storage_keys (
  p_keys           text[],
  p_program_slugs  text[],
  p_observation    text    DEFAULT NULL::text,
  p_field          text    DEFAULT NULL::text,
  p_include_drafts boolean DEFAULT false
)
  RETURNS TABLE (
    storage_key text
  )
  LANGUAGE sql
  STABLE
  AS $function$
  SELECT so.storage_key
  FROM storage_objects so
  WHERE so.storage_key = ANY(p_keys)
    -- A link is scoped to exactly one axis (share_links_scope_check). Neither
    -- set means no scope at all: authorize nothing rather than everything.
    AND (p_observation IS NOT NULL OR p_field IS NOT NULL)
    AND (
      (so.status = 'active'
       AND (
         (so.spectrum_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM spectra s
            WHERE s.spectrum_id = so.spectrum_id
              AND (s.deploy_status = 'published'
                   OR (s.deploy_status = 'draft' AND p_include_drafts))
              AND s.program_slug = ANY(p_program_slugs)
              AND s.observation = p_observation))
         OR (so.spectrum_id IS NULL AND so.deployment_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM deployments d
               LEFT JOIN observations o ON o.name = d.observation
               WHERE d.id = so.deployment_id
                 AND (d.status = 'published'
                      OR (d.status = 'draft' AND p_include_drafts))
                 -- NIRCam field deploy (epic #261, N1): multi-program, public to
                 -- all when published -- so the scope match on the next line is
                 -- the ONLY thing keeping a field link off every other field.
                 AND (d.field IS NOT NULL OR o.program_slug = ANY(p_program_slugs))
                 AND (d.observation = p_observation OR d.field = p_field)))
       ))
      OR
      -- Archive rows (P2): the link branch of the policy's archive disjunct --
      -- the row's own program and observation, and the live spectrum visible
      -- to this link. Observation-scoped links only: archive rows are NIRSpec.
      (so.root = 'archive' AND so.status = 'archived'
       AND so.spectrum_id IS NOT NULL
       AND so.program_slug = ANY(p_program_slugs)
       AND so.observation = p_observation
       AND EXISTS (
         SELECT 1 FROM spectra s
         WHERE s.spectrum_id = so.spectrum_id
           AND (s.deploy_status = 'published'
                OR (s.deploy_status = 'draft' AND p_include_drafts))))
    );
$function$;

CREATE POLICY "select_storage_objects_by_access" ON "public"."storage_objects"
  FOR SELECT
  TO "authenticated"
  USING
    ((( SELECT public.is_admin() AS is_admin) OR ((( SELECT (NOT public.is_link_account())) OR ( SELECT public.link_allows_download() AS link_allows_download)) AND (((status =
    'active'::text) AND (((spectrum_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM public.spectra s
  WHERE
    ((s.spectrum_id = storage_objects.spectrum_id) AND ((s.deploy_status = 'published'::text) OR ((s.deploy_status = 'draft'::text) AND ( SELECT public.link_sees_drafts() AS
    link_sees_drafts))) AND (s.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND
    (( SELECT (NOT public.is_link_account())) OR (s.observation = ( SELECT public.link_observation() AS link_observation))))))) OR ((spectrum_id IS NULL) AND (deployment_id IS
    NOT NULL) AND (EXISTS ( SELECT 1
   FROM (public.deployments d
     LEFT JOIN public.observations o ON ((o.name = d.observation)))
  WHERE
    ((d.id = storage_objects.deployment_id) AND ((d.status = 'published'::text) OR ((d.status = 'draft'::text) AND ( SELECT public.link_sees_drafts() AS link_sees_drafts))) AND
    ((d.field IS
    NOT NULL) OR (o.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))) AND
    (( SELECT (NOT public.is_link_account())) OR (d.observation = ( SELECT public.link_observation() AS link_observation)) OR (d.field = ( SELECT public.link_field() AS
    link_field))))))))) OR ((root = 'archive'::text) AND (status = 'archived'::text) AND (spectrum_id IS
    NOT NULL) AND (program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])) AND
    (( SELECT (NOT public.is_link_account())) OR (observation = ( SELECT public.link_observation() AS link_observation))) AND (EXISTS ( SELECT 1
   FROM public.spectra s
  WHERE
    ((s.spectrum_id = storage_objects.spectrum_id) AND ((s.deploy_status = 'published'::text) OR ((s.deploy_status = 'draft'::text) AND ( SELECT public.link_sees_drafts() AS
    link_sees_drafts)))))))))));
