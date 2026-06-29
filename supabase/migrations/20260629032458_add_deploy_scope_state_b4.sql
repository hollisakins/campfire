drop materialized view if exists "public"."mv_filter_options";

drop materialized view if exists "public"."mv_programs_overview";

drop view if exists "public"."nircam_reduction_progress";

drop view if exists "public"."spectrum_flag_summary";


  create table "public"."deploy_scope_state" (
    "scope_type" text not null,
    "scope_key" text not null,
    "version" integer not null default 0,
    "last_actor" uuid,
    "last_deploy_at" timestamp with time zone,
    "updated_at" timestamp with time zone not null default now()
      );


alter table "public"."deploy_scope_state" enable row level security;

CREATE UNIQUE INDEX deploy_scope_state_pkey ON public.deploy_scope_state USING btree (scope_type, scope_key);

alter table "public"."deploy_scope_state" add constraint "deploy_scope_state_pkey" PRIMARY KEY using index "deploy_scope_state_pkey";

alter table "public"."deploy_scope_state" add constraint "deploy_scope_state_scope_type_check" CHECK ((scope_type = ANY (ARRAY['observation'::text, 'field'::text]))) not valid;

alter table "public"."deploy_scope_state" validate constraint "deploy_scope_state_scope_type_check";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.claim_deploy_scope(p_scope_type text, p_scope_key text, p_expected_version integer, p_actor uuid DEFAULT NULL::uuid)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_new_version integer;
  v_current integer;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  IF p_scope_type NOT IN ('observation', 'field') THEN
    RAISE EXCEPTION 'Invalid scope_type: %', p_scope_type;
  END IF;

  -- CAS: insert at version 1 if the scope is new (expected 0); otherwise bump
  -- only when the stored version still matches what the caller read at start.
  INSERT INTO deploy_scope_state AS d (scope_type, scope_key, version, last_actor, last_deploy_at, updated_at)
  VALUES (p_scope_type, p_scope_key, 1, p_actor, now(), now())
  ON CONFLICT (scope_type, scope_key) DO UPDATE
    SET version = d.version + 1, last_actor = p_actor, last_deploy_at = now(), updated_at = now()
    WHERE d.version = p_expected_version
  RETURNING d.version INTO v_new_version;

  IF v_new_version IS NOT NULL THEN
    RETURN json_build_object('claimed', true, 'version', v_new_version, 'conflict', false);
  END IF;

  -- No row returned: an existing row's version != expected (concurrent deploy).
  SELECT version INTO v_current FROM deploy_scope_state
  WHERE scope_type = p_scope_type AND scope_key = p_scope_key;
  RETURN json_build_object('claimed', false, 'conflict', true,
                           'current', COALESCE(v_current, 0), 'expected', p_expected_version);
END;
$function$
;

CREATE OR REPLACE FUNCTION public.get_deploy_scope_version(p_scope_type text, p_scope_key text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_is_admin boolean;
  v_version integer;
BEGIN
  SELECT COALESCE(up.is_admin, false) INTO v_is_admin
  FROM user_profiles up WHERE up.user_id = auth.uid();
  IF NOT (COALESCE(v_is_admin, false) OR COALESCE(auth.role(), '') = 'service_role') THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT version INTO v_version FROM deploy_scope_state
  WHERE scope_type = p_scope_type AND scope_key = p_scope_key;
  RETURN COALESCE(v_version, 0);
END;
$function$
;

-- NOTE (#228): can_inspect()/handle_new_user() re-emitted by migra (pre-existing
-- SET search_path drift, tracked in #228). Stripped to keep this B4 migration
-- single-purpose.

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


-- B4 (#220): security_invoker restored (migra drops the view option on recreate).
-- B4 (#220): migra recreates the matviews but drops their unique indexes; restore
-- them (REFRESH CONCURRENTLY needs them). Validated by the check-mv-indexes CI.
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


-- B4 (#220): security_invoker restored (migra drops the view option on recreate).
create or replace view "public"."spectrum_flag_summary" with (security_invoker = true) as  SELECT s.id,
    s.target_id,
    s.grating,
    array_agg(DISTINCT fd.label) FILTER (WHERE ((fd.category = 'dq_flags'::text) AND ((s.dq_flags & fd.value) > 0))) AS dq_flags_labels
   FROM (public.spectra s
     CROSS JOIN public.flag_definitions fd)
  WHERE ((s.deploy_status = 'published'::text) OR public.is_admin())
  GROUP BY s.id, s.target_id, s.grating;


grant delete on table "public"."deploy_scope_state" to "anon";

grant insert on table "public"."deploy_scope_state" to "anon";

grant references on table "public"."deploy_scope_state" to "anon";

grant select on table "public"."deploy_scope_state" to "anon";

grant trigger on table "public"."deploy_scope_state" to "anon";

grant truncate on table "public"."deploy_scope_state" to "anon";

grant update on table "public"."deploy_scope_state" to "anon";

grant delete on table "public"."deploy_scope_state" to "authenticated";

grant insert on table "public"."deploy_scope_state" to "authenticated";

grant references on table "public"."deploy_scope_state" to "authenticated";

grant select on table "public"."deploy_scope_state" to "authenticated";

grant trigger on table "public"."deploy_scope_state" to "authenticated";

grant truncate on table "public"."deploy_scope_state" to "authenticated";

grant update on table "public"."deploy_scope_state" to "authenticated";

grant delete on table "public"."deploy_scope_state" to "service_role";

grant insert on table "public"."deploy_scope_state" to "service_role";

grant references on table "public"."deploy_scope_state" to "service_role";

grant select on table "public"."deploy_scope_state" to "service_role";

grant trigger on table "public"."deploy_scope_state" to "service_role";

grant truncate on table "public"."deploy_scope_state" to "service_role";

grant update on table "public"."deploy_scope_state" to "service_role";


  create policy "admin_select_deploy_scope_state"
  on "public"."deploy_scope_state"
  as permissive
  for select
  to authenticated
using (( SELECT public.is_admin() AS is_admin));




-- B4 (#220): COMMENT ON (migra does not track comments).
COMMENT ON TABLE "public"."deploy_scope_state" IS 'Optimistic-concurrency version per deploy scope (epic #210, B4). claim_deploy_scope does the compare-and-set so concurrent same-scope deploys are detected, not silently clobbered. Admin/internal.';
