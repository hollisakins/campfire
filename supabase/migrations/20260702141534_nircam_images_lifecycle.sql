-- epic #261, N2 — NIRCam mosaic lifecycle. nircam_images (the public mosaic index)
-- gains deploy_status (draft/published/revoked, default published) + deployment_id
-- (FK -> deployments), mirroring spectra: published mosaics are public to everyone
-- (a field spans multiple programs), draft/revoked are admin-only via the RLS.
-- set_deployment_status flips a field deployment's nircam_images.deploy_status.
-- (Spurious view/matview churn stripped — migra artifact, definitions unchanged.)

drop policy "authenticated_select_nircam" on "public"."nircam_images";

alter table "public"."nircam_images" add column "deploy_status" text not null default 'published'::text;

alter table "public"."nircam_images" add column "deployment_id" integer;

alter table "public"."nircam_images" add constraint "nircam_images_deploy_status_check" CHECK ((deploy_status = ANY (ARRAY['draft'::text, 'published'::text, 'revoked'::text]))) not valid;

alter table "public"."nircam_images" validate constraint "nircam_images_deploy_status_check";

alter table "public"."nircam_images" add constraint "nircam_images_deployment_id_fkey" FOREIGN KEY (deployment_id) REFERENCES public.deployments(id) ON DELETE SET NULL not valid;

alter table "public"."nircam_images" validate constraint "nircam_images_deployment_id_fkey";

set check_function_bodies = off;

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
  v_n_images integer;
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

  -- NIRCam field-scoped deployment (epic #261, N1/N2): exposure/mosaic FITS
  -- visibility rides deployment.status via the storage_objects gate; the public
  -- mosaic index (nircam_images) carries its own deploy_status, flipped here to
  -- match (mirrors how the observation path flips spectra.deploy_status).
  IF v_field IS NOT NULL THEN
    v_action := CASE WHEN p_to = 'revoked' THEN 'revoke'
                     WHEN p_to = 'draft' THEN 'upload' ELSE 'publish' END;
    UPDATE deployments SET
      status = p_to,
      published_at = CASE WHEN p_to = 'published' THEN now() ELSE published_at END,
      revoked_at = CASE WHEN p_to = 'revoked' THEN now() ELSE revoked_at END
    WHERE id = p_deployment_id;
    WITH flipped AS (
      UPDATE nircam_images SET deploy_status = p_to
      WHERE deployment_id = p_deployment_id AND deploy_status <> p_to
      RETURNING 1)
    SELECT count(*) INTO v_n_images FROM flipped;
    INSERT INTO deploy_events (actor, action, deployment_id, status_to, host, affected_count, metadata)
      VALUES (p_actor, v_action, p_deployment_id, p_to, p_host, v_n_images,
              jsonb_build_object('field', v_field));
    RETURN json_build_object(
      'deployment_id', p_deployment_id, 'field', v_field, 'status', p_to,
      'nircam_images', json_build_object('updated', v_n_images, 'action', v_action));
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

  create policy "authenticated_select_nircam"
  on "public"."nircam_images"
  as permissive
  for select
  to authenticated
using (((deploy_status = 'published'::text) OR ( SELECT public.is_admin() AS is_admin)));

