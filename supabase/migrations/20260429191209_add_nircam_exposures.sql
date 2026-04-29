-- nircam_exposures: per-exposure tracking for NIRCam stage2 deliverables and
-- admin triage. Pipeline pushes rows via `campfire deploy nircam`; admins
-- review on /admin/nircam; pipeline pulls back exclusions via `pull` to a
-- contract file consumed by run_stage2/run_stage3.

create sequence "public"."nircam_exposures_id_seq";

create table "public"."nircam_exposures" (
    "id" integer not null default nextval('public.nircam_exposures_id_seq'::regclass),
    "field" text not null,
    "filter" text not null,
    "detector" text not null,
    "filename" text not null,
    "visit" text,
    "date_obs" timestamp without time zone,
    "ra_center" double precision,
    "dec_center" double precision,
    "stage" text not null default 'uncal'::text,
    "review_status" text not null default 'pending'::text,
    "masking" text not null default 'none'::text,
    "correction" text not null default 'none'::text,
    "png_path" text,
    "notes" text,
    "created_at" timestamp without time zone default now(),
    "updated_at" timestamp without time zone default now()
);

alter table "public"."nircam_exposures" enable row level security;

alter sequence "public"."nircam_exposures_id_seq" owned by "public"."nircam_exposures"."id";

CREATE INDEX idx_nircam_exposures_field_filter ON public.nircam_exposures USING btree (field, filter);

CREATE INDEX idx_nircam_exposures_review ON public.nircam_exposures USING btree (review_status) WHERE (review_status <> 'approved'::text);

CREATE UNIQUE INDEX nircam_exposures_pkey ON public.nircam_exposures USING btree (id);

CREATE UNIQUE INDEX nircam_exposures_unique ON public.nircam_exposures USING btree (field, filter, filename);

alter table "public"."nircam_exposures" add constraint "nircam_exposures_pkey" PRIMARY KEY using index "nircam_exposures_pkey";

alter table "public"."nircam_exposures" add constraint "nircam_exposures_unique" UNIQUE using index "nircam_exposures_unique";

create or replace view "public"."nircam_reduction_progress" as
SELECT field,
    filter,
    count(*) AS total,
    count(*) FILTER (WHERE (stage = 'uncal'::text)) AS at_uncal,
    count(*) FILTER (WHERE (stage = 'rate'::text)) AS at_rate,
    count(*) FILTER (WHERE (stage = 'cal'::text)) AS at_cal,
    count(*) FILTER (WHERE (stage = 'jhat'::text)) AS at_jhat,
    count(*) FILTER (WHERE (stage = 'crf'::text)) AS at_crf,
    count(*) FILTER (WHERE (review_status = 'pending'::text)) AS pending_review,
    count(*) FILTER (WHERE (review_status = 'approved'::text)) AS approved,
    count(*) FILTER (WHERE (review_status = 'excluded'::text)) AS excluded,
    count(*) FILTER (WHERE (masking = 'needed'::text)) AS needs_masking,
    count(*) FILTER (WHERE (correction = 'needed'::text)) AS needs_correction
   FROM public.nircam_exposures
  GROUP BY field, filter;

grant delete on table "public"."nircam_exposures" to "anon";
grant insert on table "public"."nircam_exposures" to "anon";
grant references on table "public"."nircam_exposures" to "anon";
grant select on table "public"."nircam_exposures" to "anon";
grant trigger on table "public"."nircam_exposures" to "anon";
grant truncate on table "public"."nircam_exposures" to "anon";
grant update on table "public"."nircam_exposures" to "anon";

grant delete on table "public"."nircam_exposures" to "authenticated";
grant insert on table "public"."nircam_exposures" to "authenticated";
grant references on table "public"."nircam_exposures" to "authenticated";
grant select on table "public"."nircam_exposures" to "authenticated";
grant trigger on table "public"."nircam_exposures" to "authenticated";
grant truncate on table "public"."nircam_exposures" to "authenticated";
grant update on table "public"."nircam_exposures" to "authenticated";

grant delete on table "public"."nircam_exposures" to "service_role";
grant insert on table "public"."nircam_exposures" to "service_role";
grant references on table "public"."nircam_exposures" to "service_role";
grant select on table "public"."nircam_exposures" to "service_role";
grant trigger on table "public"."nircam_exposures" to "service_role";
grant truncate on table "public"."nircam_exposures" to "service_role";
grant update on table "public"."nircam_exposures" to "service_role";

grant select on public."nircam_reduction_progress" to "authenticated";

create policy "admin_select_exposures"
  on "public"."nircam_exposures"
  as permissive
  for select
  to authenticated
using (public.is_admin());

create policy "admin_insert_exposures"
  on "public"."nircam_exposures"
  as permissive
  for insert
  to authenticated
with check (public.is_admin());

create policy "admin_update_exposures"
  on "public"."nircam_exposures"
  as permissive
  for update
  to authenticated
using (public.is_admin())
with check (public.is_admin());
