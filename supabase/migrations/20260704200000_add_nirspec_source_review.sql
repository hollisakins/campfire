-- NIRSpec source-scoped review flags (P6, NIRSpec review loop, design §4.3). One
-- row per (observation, exposure_root, source_id) — UNIQUE on that triple. Holds the
-- two editable flag channels as jsonb mirroring the local reference/nirspec/<obs>/
-- TOMLs 1:1 so P7's pull serializes without transform:
--   stuck_shutters = [1,2,3]    ordinal list  (mirrors stuck_closed_shutters.toml)
--   bkg_overrides  = {"3":[1]}  {nod: [nods]} (mirrors nodded_background_overrides.toml;
--                               keys/values are exposure-sequence numbers, not indices)
-- Admin-only (RLS); web-editable (admins INSERT/UPDATE directly, unlike the
-- deploy-only intermediate tables); NOT deployed to OSN. Additive: a brand-new table
-- with no rows and no dependents; seed.sql is unaffected (the seed generator does not
-- emit this table, same as nirspec_rate_exposures/spectrum_exposures).
--
-- Mirrors the create idiom of 20260704180000_add_nirspec_rate_exposures.sql.
-- Grants match the schema file (authenticated + service_role only; no anon).

create sequence "public"."nirspec_source_review_id_seq";

create table "public"."nirspec_source_review" (
    "id" integer not null default nextval('public.nirspec_source_review_id_seq'::regclass),
    "observation" text not null,
    "exposure_root" text not null,
    "source_id" integer not null,
    "stuck_shutters" jsonb,
    "bkg_overrides" jsonb,
    "notes" text,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now()
);

alter table "public"."nirspec_source_review" enable row level security;

alter sequence "public"."nirspec_source_review_id_seq" owned by "public"."nirspec_source_review"."id";

CREATE UNIQUE INDEX nirspec_source_review_pkey ON public.nirspec_source_review USING btree (id);

CREATE UNIQUE INDEX nirspec_source_review_unique ON public.nirspec_source_review USING btree (observation, exposure_root, source_id);

alter table "public"."nirspec_source_review" add constraint "nirspec_source_review_pkey" PRIMARY KEY using index "nirspec_source_review_pkey";

alter table "public"."nirspec_source_review" add constraint "nirspec_source_review_unique" UNIQUE using index "nirspec_source_review_unique";

grant all on table "public"."nirspec_source_review" to "authenticated";

grant all on table "public"."nirspec_source_review" to "service_role";

create policy "admin_select_nirspec_source_review"
  on "public"."nirspec_source_review" as permissive for select to authenticated
  using (( SELECT public.is_admin() AS is_admin));

create policy "admin_insert_nirspec_source_review"
  on "public"."nirspec_source_review" as permissive for insert to authenticated
  with check (( SELECT public.is_admin() AS is_admin));

create policy "admin_update_nirspec_source_review"
  on "public"."nirspec_source_review" as permissive for update to authenticated
  using (( SELECT public.is_admin() AS is_admin))
  with check (( SELECT public.is_admin() AS is_admin));

-- Comments are not tracked by the migra diff engine (CLAUDE.md), so the table
-- comment declared in schemas/tables.sql must be applied by hand here to reach prod.
comment on table "public"."nirspec_source_review" is 'Editable flag channel for the NIRSpec nods review loop (P6). One row per (observation, exposure_root, source_id); admin-only, web-editable, NOT deployed. stuck_shutters/bkg_overrides jsonb mirror the reference/nirspec/<obs>/ TOMLs 1:1 for the P7 pull-back.';
