alter table "public"."deployments" drop constraint "deployments_status_check";

alter table "public"."spectra" drop constraint "spectra_deploy_status_check";

-- B5: migrate existing rows to the renamed value BEFORE the new CHECK validates
-- (none in prod yet; idempotent).
UPDATE public.spectra SET deploy_status = 'draft' WHERE deploy_status = 'in_prep';
UPDATE public.deployments SET status = 'draft' WHERE status = 'in_prep';

drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."nircam_reduction_progress";

drop view if exists "public"."spectrum_flag_summary";

alter table "public"."deployments" alter column "deployed_by" drop not null;

alter table "public"."deployments" add constraint "deployments_status_check" CHECK ((status = ANY (ARRAY['draft'::text, 'published'::text, 'revoked'::text]))) not valid;

alter table "public"."deployments" validate constraint "deployments_status_check";

alter table "public"."spectra" add constraint "spectra_deploy_status_check" CHECK ((deploy_status = ANY (ARRAY['draft'::text, 'published'::text, 'revoked'::text]))) not valid;

alter table "public"."spectra" validate constraint "spectra_deploy_status_check";

set check_function_bodies = off;

-- NOTE (#228): can_inspect()/handle_new_user() re-emitted by migra (pre-existing
-- SET search_path drift, tracked in #228). Stripped to keep this migration single-purpose.

create materialized view "public"."mv_filter_options" as  SELECT 1 AS id,
    ARRAY( SELECT DISTINCT targets.field
           FROM public.targets
          WHERE targets.has_published_spectrum
          ORDER BY targets.field) AS fields,
    ARRAY( SELECT DISTINCT targets.observation
           FROM public.targets
          WHERE ((targets.observation IS NOT NULL) AND targets.has_published_spectrum)
          ORDER BY targets.observation) AS observations,
    ARRAY( SELECT DISTINCT spectra.grating
           FROM public.spectra
          WHERE (spectra.deploy_status = 'published'::text)
          ORDER BY spectra.grating) AS gratings;


create materialized view "public"."mv_programs_overview" as  SELECT p.slug,
    p.program_name,
    p.pi_name,
    p.description,
    p.is_public,
    p.cycle,
    COALESCE(stats.target_count, (0)::bigint) AS target_count,
    COALESCE(stats.gratings, ARRAY[]::text[]) AS gratings,
    COALESCE(stats.fields, ARRAY[]::text[]) AS fields,
    COALESCE(stats.observations, ARRAY[]::text[]) AS observations,
    COALESCE(pids.jwst_pids, ARRAY[]::integer[]) AS jwst_pids,
    COALESCE(pids.n_observations, (0)::bigint) AS n_observations,
    last_red.last_reduced_at
   FROM (((public.programs p
     LEFT JOIN ( SELECT t.program_slug,
            count(DISTINCT t.target_id) AS target_count,
            array_agg(DISTINCT s.grating ORDER BY s.grating) FILTER (WHERE (s.grating IS NOT NULL)) AS gratings,
            array_agg(DISTINCT t.field ORDER BY t.field) AS fields,
            array_agg(DISTINCT t.observation ORDER BY t.observation) AS observations
           FROM (public.targets t
             LEFT JOIN public.spectra s ON (((s.target_id = t.target_id) AND (s.deploy_status = 'published'::text))))
          GROUP BY t.program_slug) stats ON ((p.slug = stats.program_slug)))
     LEFT JOIN ( SELECT observations.program_slug,
            array_agg(DISTINCT observations.jwst_program_id ORDER BY observations.jwst_program_id) AS jwst_pids,
            count(*) AS n_observations
           FROM public.observations
          GROUP BY observations.program_slug) pids ON ((p.slug = pids.program_slug)))
     LEFT JOIN ( SELECT o.program_slug,
            max(d.reduced_at) AS last_reduced_at
           FROM (public.observations o
             JOIN public.deployments d ON ((d.observation = o.name)))
          WHERE (d.source_ids_filter IS NULL)
          GROUP BY o.program_slug) last_red ON ((p.slug = last_red.program_slug)));


-- B5: security_invoker restored (migra drops it on recreate).
-- B5: restore matview unique indexes migra drops (REFRESH CONCURRENTLY needs them).
CREATE UNIQUE INDEX mv_filter_options_id ON public.mv_filter_options USING btree (id);
CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview USING btree (slug);

create or replace view "public"."nircam_reduction_progress" with (security_invoker = true) as  SELECT field,
    filter,
    count(*) AS total,
    count(*) FILTER (WHERE (stage = 'uncal'::text)) AS at_uncal,
    count(*) FILTER (WHERE (stage = 'detector1'::text)) AS at_detector1,
    count(*) FILTER (WHERE (stage = 'persistence'::text)) AS at_persistence,
    count(*) FILTER (WHERE (stage = 'wisp'::text)) AS at_wisp,
    count(*) FILTER (WHERE (stage = 'striping'::text)) AS at_striping,
    count(*) FILTER (WHERE (stage = 'image2'::text)) AS at_image2,
    count(*) FILTER (WHERE (stage = 'edge'::text)) AS at_edge,
    count(*) FILTER (WHERE (stage = 'sky'::text)) AS at_sky,
    count(*) FILTER (WHERE (stage = 'diag_striping'::text)) AS at_diag_striping,
    count(*) FILTER (WHERE (stage = 'variance'::text)) AS at_variance,
    count(*) FILTER (WHERE (stage = 'wcs_shift'::text)) AS at_wcs_shift,
    count(*) FILTER (WHERE (stage = 'preview'::text)) AS at_preview,
    count(*) FILTER (WHERE (stage = 'jhat'::text)) AS at_jhat,
    count(*) FILTER (WHERE (stage = 'apply_mask'::text)) AS at_apply_mask,
    count(*) FILTER (WHERE (stage = 'bad_pixel'::text)) AS at_bad_pixel,
    count(*) FILTER (WHERE (stage = 'outlier'::text)) AS at_outlier,
    count(*) FILTER (WHERE (review_status = 'pending'::text)) AS pending_review,
    count(*) FILTER (WHERE (review_status = 'approved'::text)) AS approved,
    count(*) FILTER (WHERE (review_status = 'excluded'::text)) AS excluded,
    count(*) FILTER (WHERE (masking = 'needed'::text)) AS needs_masking,
    count(*) FILTER (WHERE (correction = 'needed'::text)) AS needs_correction
   FROM public.nircam_exposures
  GROUP BY field, filter;


CREATE OR REPLACE FUNCTION public.set_deployment_status(p_deployment_id integer, p_to text, p_actor uuid DEFAULT NULL::uuid, p_host text DEFAULT NULL::text)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_obs text;
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

  SELECT observation INTO v_obs FROM deployments WHERE id = p_deployment_id;
  IF v_obs IS NULL THEN
    RAISE EXCEPTION 'Deployment % not found', p_deployment_id;
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

CREATE OR REPLACE FUNCTION public.set_spectra_deploy_status(p_spectrum_db_ids integer[], p_to text, p_action text DEFAULT NULL::text, p_actor uuid DEFAULT NULL::uuid, p_deployment_id integer DEFAULT NULL::integer, p_host text DEFAULT NULL::text)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_action text;
  v_updated int := 0;
  v_target_ids text[];
  v_recompute json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('draft', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid deploy_status: %', p_to;
  END IF;

  v_action := COALESCE(p_action,
    CASE p_to WHEN 'published' THEN 'publish' WHEN 'revoked' THEN 'revoke' ELSE 'upload' END);
  IF v_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete') THEN
    RAISE EXCEPTION 'Invalid action: %', v_action;
  END IF;

  IF p_spectrum_db_ids IS NULL OR array_length(p_spectrum_db_ids, 1) IS NULL THEN
    RETURN json_build_object('updated', 0, 'action', v_action);
  END IF;

  SELECT array_agg(DISTINCT s.target_id) INTO v_target_ids
  FROM spectra s WHERE s.id = ANY(p_spectrum_db_ids);

  UPDATE spectra s SET deploy_status = p_to
  WHERE s.id = ANY(p_spectrum_db_ids) AND s.deploy_status <> p_to;
  GET DIAGNOSTICS v_updated = ROW_COUNT;

  IF v_target_ids IS NOT NULL THEN
    v_recompute := public.recompute_has_published_spectrum(p_target_ids := v_target_ids);
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, status_to, affected_count, host, metadata)
  VALUES (p_actor, v_action, p_deployment_id, p_to, v_updated, p_host,
          jsonb_build_object('n_targets', COALESCE(array_length(v_target_ids, 1), 0)));

  RETURN json_build_object('updated', v_updated, 'action', v_action, 'recompute', v_recompute);
END;
$function$
;

-- B5: security_invoker restored (migra drops it on recreate).
create or replace view "public"."spectrum_flag_summary" with (security_invoker = true) as  SELECT s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((s.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.spectra s
     CROSS JOIN public.flag_definitions fd)
  WHERE ((s.deploy_status = 'published'::text) OR public.is_admin())
  GROUP BY s.id, s.target_id, s.grating;



