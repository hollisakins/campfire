-- NIRSpec rate-mask triage table (P2, NIRSpec review loop, design §3.2). The
-- detector-grain analogue of nircam_exposures, keyed on
-- (observation, exposure_root, detector). Admin-only (RLS); deploy-populated via a
-- split-ownership upsert (campfire.deploy.rate_exposures) that refreshes identity/
-- render columns without clobbering web review state. Additive: a brand-new table
-- with no rows and no dependents; seed.sql is unaffected (the seed generator does
-- not emit this table, same as nircam_exposures/spectrum_exposures).
--
-- Mirrors the create idiom of 20260629025904_add_intermediate_lifecycle_b2.sql.
-- Grants match the schema file (authenticated + service_role only; no anon).

create sequence "public"."nirspec_rate_exposures_id_seq";

create table "public"."nirspec_rate_exposures" (
    "id" integer not null default nextval('public.nirspec_rate_exposures_id_seq'::regclass),
    "observation" text not null,
    "exposure_root" text not null,
    "detector" text not null,
    "filename" text not null,
    "grating" text,
    "image_width" integer,
    "image_height" integer,
    "storage_key" text,
    "stage" text not null default 'rate'::text,
    "review_status" text not null default 'pending'::text,
    "masking" text not null default 'none'::text,
    "mask_regions" jsonb,
    "notes" text,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now()
);

alter table "public"."nirspec_rate_exposures" enable row level security;

alter sequence "public"."nirspec_rate_exposures_id_seq" owned by "public"."nirspec_rate_exposures"."id";

CREATE UNIQUE INDEX nirspec_rate_exposures_pkey ON public.nirspec_rate_exposures USING btree (id);

CREATE UNIQUE INDEX nirspec_rate_exposures_unique ON public.nirspec_rate_exposures USING btree (observation, exposure_root, detector);

CREATE INDEX idx_nirspec_rate_exposures_observation ON public.nirspec_rate_exposures USING btree (observation);

CREATE INDEX idx_nirspec_rate_exposures_review ON public.nirspec_rate_exposures USING btree (review_status) WHERE (review_status <> 'approved'::text);

alter table "public"."nirspec_rate_exposures" add constraint "nirspec_rate_exposures_pkey" PRIMARY KEY using index "nirspec_rate_exposures_pkey";

alter table "public"."nirspec_rate_exposures" add constraint "nirspec_rate_exposures_unique" UNIQUE using index "nirspec_rate_exposures_unique";

grant all on table "public"."nirspec_rate_exposures" to "authenticated";

grant all on table "public"."nirspec_rate_exposures" to "service_role";

create policy "admin_select_nirspec_rate_exposures"
  on "public"."nirspec_rate_exposures" as permissive for select to authenticated
  using (( SELECT public.is_admin() AS is_admin));

create policy "admin_insert_nirspec_rate_exposures"
  on "public"."nirspec_rate_exposures" as permissive for insert to authenticated
  with check (( SELECT public.is_admin() AS is_admin));

create policy "admin_update_nirspec_rate_exposures"
  on "public"."nirspec_rate_exposures" as permissive for update to authenticated
  using (( SELECT public.is_admin() AS is_admin))
  with check (( SELECT public.is_admin() AS is_admin));
