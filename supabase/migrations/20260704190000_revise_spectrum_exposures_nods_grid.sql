-- Revive + revise spectrum_exposures as the NIRSpec nods-renderer grid (P4, review
-- loop, design §2c/§4.2). The table was a dead epic-#210-B2 scaffold: its
-- spectrum_id NOT NULL FK to spectra was unsatisfiable for an intermediates-only
-- deploy (which runs BEFORE stage-3 combine makes any spectrum row), and nothing in
-- production ever read or wrote it (only the b2_lifecycle RLS test inserted a row).
-- So we DROP + RECREATE it, dropping the spectra FK and re-keying to observations.name
-- with UNIQUE (observation, exposure_root, nod, detector, source_id) + render columns
-- (exp_group from the pipeline CFEXPGRP stamp, storage_key, dims). Safe because the
-- table is empty; seed.sql is unaffected (generate_seed.py never emits it). Mirrors
-- the 20260704180000_add_nirspec_rate_exposures.sql create idiom.

drop table if exists "public"."spectrum_exposures" cascade;
drop sequence if exists "public"."spectrum_exposures_id_seq";

create sequence "public"."spectrum_exposures_id_seq";

create table "public"."spectrum_exposures" (
    "id" integer not null default nextval('public.spectrum_exposures_id_seq'::regclass),
    "observation" text not null,
    "exposure_root" text not null,
    "nod" text not null,
    "detector" text not null,
    "source_id" integer not null,
    "exp_group" integer,
    "grating" text,
    "filename" text not null,
    "storage_key" text,
    "image_width" integer,
    "image_height" integer,
    "stage" text not null default 'cal'::text,
    "review_status" text not null default 'pending'::text,
    "masking" text not null default 'none'::text,
    "notes" text,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now()
);

alter table "public"."spectrum_exposures" enable row level security;

alter sequence "public"."spectrum_exposures_id_seq" owned by "public"."spectrum_exposures"."id";

CREATE UNIQUE INDEX spectrum_exposures_pkey ON public.spectrum_exposures USING btree (id);

CREATE UNIQUE INDEX spectrum_exposures_unique ON public.spectrum_exposures USING btree (observation, exposure_root, nod, detector, source_id);

CREATE INDEX idx_spectrum_exposures_observation ON public.spectrum_exposures USING btree (observation);

CREATE INDEX idx_spectrum_exposures_review ON public.spectrum_exposures USING btree (review_status) WHERE (review_status <> 'approved'::text);

alter table "public"."spectrum_exposures" add constraint "spectrum_exposures_pkey" PRIMARY KEY using index "spectrum_exposures_pkey";

alter table "public"."spectrum_exposures" add constraint "spectrum_exposures_unique" UNIQUE using index "spectrum_exposures_unique";

grant all on table "public"."spectrum_exposures" to "authenticated";

grant all on table "public"."spectrum_exposures" to "service_role";

create policy "admin_select_spectrum_exposures"
  on "public"."spectrum_exposures" as permissive for select to authenticated
  using (( SELECT public.is_admin() AS is_admin));

create policy "admin_insert_spectrum_exposures"
  on "public"."spectrum_exposures" as permissive for insert to authenticated
  with check (( SELECT public.is_admin() AS is_admin));

create policy "admin_update_spectrum_exposures"
  on "public"."spectrum_exposures" as permissive for update to authenticated
  using (( SELECT public.is_admin() AS is_admin))
  with check (( SELECT public.is_admin() AS is_admin));
