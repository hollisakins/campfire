create sequence "public"."spectrum_exposures_id_seq";

drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."nircam_reduction_progress";

drop view if exists "public"."spectrum_flag_summary";


  create table "public"."deploy_events" (
    "id" uuid not null default gen_random_uuid(),
    "actor" uuid,
    "action" text not null,
    "deployment_id" integer,
    "observation" text,
    "spectrum_id" text,
    "object_id" integer,
    "status_from" text,
    "status_to" text,
    "affected_count" integer,
    "host" text,
    "metadata" jsonb,
    "occurred_at" timestamp with time zone not null default now()
      );


alter table "public"."deploy_events" enable row level security;


  create table "public"."spectrum_exposures" (
    "id" integer not null default nextval('public.spectrum_exposures_id_seq'::regclass),
    "spectrum_id" integer not null,
    "exposure_ref" text not null,
    "root" text,
    "nod" integer,
    "detector" text,
    "source_id" integer,
    "grating" text,
    "stage" text not null default 'cal'::text,
    "review_status" text not null default 'pending'::text,
    "masking" text not null default 'none'::text,
    "notes" text,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now()
      );


alter table "public"."spectrum_exposures" enable row level security;

alter table "public"."deployments" add column "published_at" timestamp with time zone;

alter table "public"."deployments" add column "revoked_at" timestamp with time zone;

alter table "public"."deployments" add column "status" text not null default 'published'::text;

alter sequence "public"."spectrum_exposures_id_seq" owned by "public"."spectrum_exposures"."id";

CREATE UNIQUE INDEX deploy_events_pkey ON public.deploy_events USING btree (id);

CREATE INDEX idx_deploy_events_deployment_id ON public.deploy_events USING btree (deployment_id) WHERE (deployment_id IS NOT NULL);

CREATE INDEX idx_deploy_events_occurred_at ON public.deploy_events USING btree (occurred_at DESC);

CREATE INDEX idx_spectrum_exposures_exposure_ref ON public.spectrum_exposures USING btree (exposure_ref);

CREATE INDEX idx_spectrum_exposures_review ON public.spectrum_exposures USING btree (review_status) WHERE (review_status <> 'approved'::text);

CREATE INDEX idx_spectrum_exposures_spectrum_id ON public.spectrum_exposures USING btree (spectrum_id);

CREATE UNIQUE INDEX spectrum_exposures_pkey ON public.spectrum_exposures USING btree (id);

CREATE UNIQUE INDEX spectrum_exposures_unique ON public.spectrum_exposures USING btree (spectrum_id, exposure_ref);

alter table "public"."deploy_events" add constraint "deploy_events_pkey" PRIMARY KEY using index "deploy_events_pkey";

alter table "public"."spectrum_exposures" add constraint "spectrum_exposures_pkey" PRIMARY KEY using index "spectrum_exposures_pkey";

alter table "public"."deploy_events" add constraint "deploy_events_action_check" CHECK ((action = ANY (ARRAY['upload'::text, 'publish'::text, 'revoke'::text, 'recover'::text, 'supersede'::text, 'delete'::text]))) not valid;

alter table "public"."deploy_events" validate constraint "deploy_events_action_check";

alter table "public"."deploy_events" add constraint "deploy_events_deployment_id_fkey" FOREIGN KEY (deployment_id) REFERENCES public.deployments(id) ON DELETE SET NULL not valid;

alter table "public"."deploy_events" validate constraint "deploy_events_deployment_id_fkey";

alter table "public"."deployments" add constraint "deployments_status_check" CHECK ((status = ANY (ARRAY['in_prep'::text, 'published'::text, 'revoked'::text]))) not valid;

alter table "public"."deployments" validate constraint "deployments_status_check";

alter table "public"."spectrum_exposures" add constraint "spectrum_exposures_spectrum_id_fkey" FOREIGN KEY (spectrum_id) REFERENCES public.spectra(id) ON DELETE CASCADE not valid;

alter table "public"."spectrum_exposures" validate constraint "spectrum_exposures_spectrum_id_fkey";

alter table "public"."spectrum_exposures" add constraint "spectrum_exposures_unique" UNIQUE using index "spectrum_exposures_unique";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.get_lifecycle_status()
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_has_status_col boolean;
  v_has_target_flag boolean;
  v_reader_threaded boolean;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'spectra' AND column_name = 'deploy_status'
  ) INTO v_has_status_col;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'targets' AND column_name = 'has_published_spectrum'
  ) INTO v_has_target_flag;

  -- A representative reader RPC must carry the predicate parameter — proof that
  -- B1's reader-threading (not just the column) is deployed.
  SELECT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proname = 'get_filtered_object_ids'
      AND pg_get_function_arguments(p.oid) LIKE '%p_include_unpublished%'
  ) INTO v_reader_threaded;

  RETURN json_build_object(
    'enabled', (v_has_status_col AND v_has_target_flag AND v_reader_threaded),
    'version', 1,
    'checks', json_build_object(
      'spectra_deploy_status', v_has_status_col,
      'targets_has_published_spectrum', v_has_target_flag,
      'reader_p_include_unpublished', v_reader_threaded
    )
  );
END;
$function$
;

CREATE OR REPLACE FUNCTION public.log_deploy_event(p_action text, p_actor uuid DEFAULT NULL::uuid, p_deployment_id integer DEFAULT NULL::integer, p_observation text DEFAULT NULL::text, p_affected_count integer DEFAULT NULL::integer, p_metadata jsonb DEFAULT NULL::jsonb, p_host text DEFAULT NULL::text)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_id uuid;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_action NOT IN ('upload', 'publish', 'revoke', 'recover', 'supersede', 'delete') THEN
    RAISE EXCEPTION 'Invalid deploy_event action: %', p_action;
  END IF;

  INSERT INTO deploy_events(actor, action, deployment_id, observation, affected_count, metadata, host)
  VALUES (p_actor, p_action, p_deployment_id, p_observation, p_affected_count, p_metadata, p_host)
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.recompute_has_published_spectrum(p_target_ids text[] DEFAULT NULL::text[], p_field text DEFAULT NULL::text)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_targets text[];
  v_n_targets int := 0;
  v_n_objects int := 0;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_target_ids IS NOT NULL THEN
    v_targets := p_target_ids;
  ELSIF p_field IS NOT NULL THEN
    SELECT array_agg(t.target_id) INTO v_targets FROM targets t WHERE t.field = p_field;
  ELSE
    RAISE EXCEPTION 'recompute_has_published_spectrum requires p_target_ids or p_field';
  END IF;

  IF v_targets IS NULL OR array_length(v_targets, 1) IS NULL THEN
    RETURN json_build_object('targets_updated', 0, 'objects_updated', 0);
  END IF;

  UPDATE targets t
  SET has_published_spectrum = EXISTS (
        SELECT 1 FROM spectra s
        WHERE s.target_id = t.target_id AND s.deploy_status = 'published'
      )
  WHERE t.target_id = ANY(v_targets);
  GET DIAGNOSTICS v_n_targets = ROW_COUNT;

  UPDATE objects o
  SET has_published_spectrum = EXISTS (
        SELECT 1 FROM targets t WHERE t.object_id = o.id AND t.has_published_spectrum
      )
  WHERE o.id IN (
    SELECT DISTINCT t2.object_id FROM targets t2
    WHERE t2.target_id = ANY(v_targets) AND t2.object_id IS NOT NULL
  );
  GET DIAGNOSTICS v_n_objects = ROW_COUNT;

  RETURN json_build_object('targets_updated', v_n_targets, 'objects_updated', v_n_objects);
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
  v_from text;
  v_action text;
  v_spectrum_ids integer[];
  v_result json;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_to NOT IN ('in_prep', 'published', 'revoked') THEN
    RAISE EXCEPTION 'Invalid status: %', p_to;
  END IF;

  SELECT observation INTO v_obs FROM deployments WHERE id = p_deployment_id;
  IF v_obs IS NULL THEN
    RAISE EXCEPTION 'Deployment % not found', p_deployment_id;
  END IF;

  -- publish: in_prep->published; revoke: published->revoked; in_prep: published->in_prep.
  v_from := CASE p_to WHEN 'published' THEN 'in_prep' WHEN 'revoked' THEN 'published' ELSE 'published' END;
  v_action := CASE p_to WHEN 'published' THEN 'publish' WHEN 'revoked' THEN 'revoke' ELSE 'upload' END;

  SELECT array_agg(s.id) INTO v_spectrum_ids
  FROM spectra s JOIN targets t ON s.target_id = t.target_id
  WHERE t.observation = v_obs AND s.deploy_status = v_from;

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

  IF p_to NOT IN ('in_prep', 'published', 'revoked') THEN
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

-- NOTE (#228): db diff re-emits can_inspect()/handle_new_user() because the
-- declarative schema and the deployed migration differ on SET search_path
-- (pre-existing drift tracked in #228). Stripped here to keep this B2 migration
-- single-purpose; restore via the #228 fix, not this PR.

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

-- B2 (#218): migra recreates the matviews but does NOT track their unique indexes;
-- restore them (REFRESH ... CONCURRENTLY requires a unique index). Validated by
-- the check-mv-indexes CI job.
CREATE UNIQUE INDEX mv_filter_options_id ON public.mv_filter_options USING btree (id);
CREATE UNIQUE INDEX mv_programs_overview_slug ON public.mv_programs_overview USING btree (slug);

-- B2 (#218): security_invoker restored (migra drops the view option on recreate).
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


-- B2 (#218): security_invoker restored (migra drops the view option on recreate).
create or replace view "public"."spectrum_flag_summary" with (security_invoker = true) as  SELECT s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((s.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.spectra s
     CROSS JOIN public.flag_definitions fd)
  WHERE ((s.deploy_status = 'published'::text) OR public.is_admin())
  GROUP BY s.id, s.target_id, s.grating;


grant delete on table "public"."deploy_events" to "anon";

grant insert on table "public"."deploy_events" to "anon";

grant references on table "public"."deploy_events" to "anon";

grant select on table "public"."deploy_events" to "anon";

grant trigger on table "public"."deploy_events" to "anon";

grant truncate on table "public"."deploy_events" to "anon";

grant update on table "public"."deploy_events" to "anon";

grant delete on table "public"."deploy_events" to "authenticated";

grant insert on table "public"."deploy_events" to "authenticated";

grant references on table "public"."deploy_events" to "authenticated";

grant select on table "public"."deploy_events" to "authenticated";

grant trigger on table "public"."deploy_events" to "authenticated";

grant truncate on table "public"."deploy_events" to "authenticated";

grant update on table "public"."deploy_events" to "authenticated";

grant delete on table "public"."deploy_events" to "service_role";

grant insert on table "public"."deploy_events" to "service_role";

grant references on table "public"."deploy_events" to "service_role";

grant select on table "public"."deploy_events" to "service_role";

grant trigger on table "public"."deploy_events" to "service_role";

grant truncate on table "public"."deploy_events" to "service_role";

grant update on table "public"."deploy_events" to "service_role";

grant delete on table "public"."spectrum_exposures" to "anon";

grant insert on table "public"."spectrum_exposures" to "anon";

grant references on table "public"."spectrum_exposures" to "anon";

grant select on table "public"."spectrum_exposures" to "anon";

grant trigger on table "public"."spectrum_exposures" to "anon";

grant truncate on table "public"."spectrum_exposures" to "anon";

grant update on table "public"."spectrum_exposures" to "anon";

grant delete on table "public"."spectrum_exposures" to "authenticated";

grant insert on table "public"."spectrum_exposures" to "authenticated";

grant references on table "public"."spectrum_exposures" to "authenticated";

grant select on table "public"."spectrum_exposures" to "authenticated";

grant trigger on table "public"."spectrum_exposures" to "authenticated";

grant truncate on table "public"."spectrum_exposures" to "authenticated";

grant update on table "public"."spectrum_exposures" to "authenticated";

grant delete on table "public"."spectrum_exposures" to "service_role";

grant insert on table "public"."spectrum_exposures" to "service_role";

grant references on table "public"."spectrum_exposures" to "service_role";

grant select on table "public"."spectrum_exposures" to "service_role";

grant trigger on table "public"."spectrum_exposures" to "service_role";

grant truncate on table "public"."spectrum_exposures" to "service_role";

grant update on table "public"."spectrum_exposures" to "service_role";


  create policy "admin_select_deploy_events"
  on "public"."deploy_events"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));



  create policy "admin_deployments_update"
  on "public"."deployments"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));



  create policy "admin_insert_spectrum_exposures"
  on "public"."spectrum_exposures"
  as permissive
  for insert
  to authenticated
with check (( SELECT public.is_admin() AS is_admin));



  create policy "admin_select_spectrum_exposures"
  on "public"."spectrum_exposures"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));



  create policy "admin_update_spectrum_exposures"
  on "public"."spectrum_exposures"
  as permissive
  for update
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));




-- B2 (#218): COMMENT ON statements (migra does not track comments; hand-appended
-- so production, built from migrations, carries the same documentation as the
-- declarative schema files).
COMMENT ON TABLE "public"."spectrum_exposures" IS 'NIRSpec canonical spectrum-exposure intermediates (epic #210, B2). One logical row per (exposure,detector,source); child of spectra (FK to integer PK). Registered to storage_objects with product_type=''nirspec_spectrum_exposure''. Admin-only lifecycle (intermediates are never user-facing science).';
COMMENT ON COLUMN "public"."spectrum_exposures"."spectrum_id" IS 'FK to spectra.id (the stable integer PK, NOT the GENERATED spectra.spectrum_id text column which is not uniquely constrained). ON DELETE CASCADE: intermediates die with their parent spectrum and are rebuilt on the next deploy.';
COMMENT ON COLUMN "public"."spectrum_exposures"."exposure_ref" IS 'Stable (root,nod,detector,source) tuple identifying the input NIRSpec exposure. Matches storage_objects.exposure_ref for the registry join; backs the partial unique (product_type, exposure_ref) WHERE status=''active'' on storage_objects.';
COMMENT ON TABLE "public"."deploy_events" IS 'Append-only audit log for the intermediate-product lifecycle (epic #210, B2/B3). One row per action (upload/publish/revoke/recover/supersede/delete), written only by the lifecycle RPCs (SECURITY DEFINER). deployment_id is nullable (some events are not tied to a deployment). Admin-readable; never client-inserted.';
