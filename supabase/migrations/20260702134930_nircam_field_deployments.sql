-- epic #261, N1 — NIRCam deploy first-class: field-scoped deployments.
-- deployments can anchor to a NIRCam FIELD (exactly one of observation/field,
-- via deployments_scope_check), recording the same provenance + draft/published/
-- revoked lifecycle. The storage_objects gate (RLS + filter_accessible_storage_keys
-- + get_storage_objects_for_sync) gains a field branch: a published field deployment
-- is public to EVERYONE (NIRCam fields span multiple programs), NIRSpec obs stay
-- program-scoped. set_deployment_status branches on field vs observation. (Spurious
-- view/matview drop+recreate churn stripped — migra artifact, definitions unchanged.)

drop policy "select_storage_objects_by_access" on "public"."storage_objects";

alter table "public"."deployments" add column "field" text;

alter table "public"."deployments" alter column "observation" drop not null;

alter table "public"."deployments" add constraint "deployments_scope_check" CHECK ((num_nonnulls(observation, field) = 1)) not valid;

alter table "public"."deployments" validate constraint "deployments_scope_check";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.filter_accessible_storage_keys(p_keys text[], p_program_slugs text[], p_include_unpublished boolean DEFAULT false)
 RETURNS TABLE(storage_key text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT so.storage_key
  FROM storage_objects so
  WHERE so.storage_key = ANY(p_keys)
    AND so.status = 'active'
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
    );
$function$
;

CREATE OR REPLACE FUNCTION public.get_storage_objects_for_sync(p_program_slugs text[], p_updated_since timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT 1000, p_offset integer DEFAULT 0, p_include_counts boolean DEFAULT true, p_include_unpublished boolean DEFAULT false)
 RETURNS TABLE(objects jsonb, total_count bigint, total_accessible_count bigint)
 LANGUAGE plpgsql
 STABLE
 SET plan_cache_mode TO 'force_custom_plan'
 SET statement_timeout TO '120s'
AS $function$
BEGIN
  RETURN QUERY
  WITH scoped AS (
    SELECT so.*
    FROM storage_objects so
    WHERE so.status = 'active'
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
      )
  ),
  matched AS MATERIALIZED (
    SELECT * FROM scoped
    WHERE (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
    ORDER BY scoped.storage_key
    LIMIT p_limit OFFSET p_offset
  ),
  total AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
      AND (p_updated_since IS NULL OR scoped.updated_at > p_updated_since)
  ),
  accessible AS (
    SELECT COUNT(*) AS cnt FROM scoped
    WHERE p_include_counts
  )
  SELECT
    COALESCE(jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'backend', m.backend,
        'bucket', m.bucket,
        'storage_key', m.storage_key,
        'content_hash', m.content_hash,
        'size_bytes', m.size_bytes,
        'content_type', m.content_type,
        'product_type', m.product_type,
        'instrument', m.instrument,
        'status', m.status,
        'observation', m.observation,
        'field', m.field,
        'spectrum_id', m.spectrum_id,
        'exposure_ref', m.exposure_ref,
        'deployment_id', m.deployment_id,
        'cfpipe_version', m.cfpipe_version,
        'created_at', m.created_at,
        'updated_at', m.updated_at
      )
    ), '[]'::jsonb),
    COALESCE((SELECT cnt FROM total), 0)::BIGINT,
    COALESCE((SELECT cnt FROM accessible), 0)::BIGINT
  FROM matched m;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.set_deployment_status(p_deployment_id integer, p_to text, p_actor uuid DEFAULT NULL::uuid, p_host text DEFAULT NULL::text)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_obs text;
  v_field text;
  v_action text;
  v_spectrum_ids integer[];
  v_result json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('draft', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid status: %', p_to;
  END IF;

  SELECT observation, field INTO v_obs, v_field FROM deployments WHERE id = p_deployment_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Deployment % not found', p_deployment_id;
  END IF;

  -- NIRCam field-scoped deployment (epic #261, N1): no spectra to flip — the
  -- exposure/mosaic FITS visibility rides deployment.status via the storage_objects
  -- gate, so flipping the deployment row is the whole transition. (N2 extends this
  -- to also flip nircam_images.deploy_status for the public mosaic index.)
  IF v_field IS NOT NULL THEN
    v_action := CASE WHEN p_to = 'revoked' THEN 'revoke'
                     WHEN p_to = 'draft' THEN 'upload' ELSE 'publish' END;
    UPDATE deployments SET
      status = p_to,
      published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
      revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
    WHERE id = p_deployment_id;
    INSERT INTO deploy_events (actor, action, deployment_id, status_to, host, metadata)
      VALUES (p_actor, v_action, p_deployment_id, p_to, p_host,
              jsonb_build_object('field', v_field));
    RETURN json_build_object(
      'deployment_id', p_deployment_id, 'field', v_field,
      'status', p_to, 'spectra', json_build_object('updated', 0, 'action', v_action));
  END IF;

  -- Which current statuses transition to p_to:
  --   p_to='published'  -> draft (first publish) OR revoked (recover) become visible
  --   p_to='revoked'    -> published spectra are hidden
  --   p_to='draft'    -> published spectra go back to draft
  -- The prior version matched only 'draft' for the published case, so recovering
  -- a REVOKED deployment flipped the deployment row but left its spectra revoked
  -- and hidden ("0 updated", silently inconsistent) — #233 review.
  SELECT array_agg(s.id) INTO v_spectrum_ids
  FROM spectra s JOIN targets t ON s.target_id = t.target_id
  WHERE t.observation = v_obs
    AND s.deploy_status = ANY (
      CASE p_to WHEN 'published' THEN ARRAY['draft', 'revoked']
                WHEN 'revoked'   THEN ARRAY['published']
                ELSE                  ARRAY['published'] END);

  -- Audit label: publishing previously-revoked spectra is a 'recover', not a
  -- first 'publish'. Computed before the transition (spectra still hold old status).
  v_action := CASE
    WHEN p_to = 'revoked' THEN 'revoke'
    WHEN p_to = 'draft' THEN 'upload'
    WHEN EXISTS (SELECT 1 FROM spectra s JOIN targets t ON s.target_id = t.target_id
                 WHERE t.observation = v_obs AND s.deploy_status = 'revoked')
      THEN 'recover'
    ELSE 'publish'
  END;

  UPDATE deployments SET
    status = p_to,
    published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
    revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
  WHERE id = p_deployment_id;

  IF v_spectrum_ids IS NOT NULL THEN
    v_result := public.set_spectra_deploy_status(
      p_spectrum_db_ids := v_spectrum_ids, p_to := p_to, p_action := v_action,
      p_actor := p_actor, p_deployment_id := p_deployment_id, p_host := p_host);
  ELSE
    v_result := json_build_object('updated', 0, 'action', v_action);
  END IF;

  RETURN json_build_object(
    'deployment_id', p_deployment_id, 'observation', v_obs,
    'status', p_to, 'spectra', v_result);
END;
$function$
;

  create policy "select_storage_objects_by_access"
  on "public"."storage_objects"
  as permissive
  for select
  to authenticated
using (((status = 'active'::text) AND (((spectrum_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM (public.spectra s
     JOIN public.targets t ON ((t.target_id = s.target_id)))
  WHERE ((s.spectrum_id = storage_objects.spectrum_id) AND (s.deploy_status = 'published'::text) AND (t.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[])))))) OR ((spectrum_id IS NULL) AND (deployment_id IS NOT NULL) AND (EXISTS ( SELECT 1
   FROM (public.deployments d
     LEFT JOIN public.observations o ON ((o.name = d.observation)))
  WHERE ((d.id = storage_objects.deployment_id) AND (d.status = 'published'::text) AND ((d.field IS NOT NULL) OR (o.program_slug = ANY (( SELECT public.accessible_program_slugs() AS accessible_program_slugs)::text[]))))))))));

