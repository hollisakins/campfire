-- FitsGL tile-pyramid dataset registry (epic #337, Phase 3).
--
-- NOTE: `supabase db diff` also emitted spurious drop/recreate statements for
-- mv_filter_options, mv_programs_overview, nircam_reduction_progress, and
-- spectrum_flag_summary — the known migra limitation with (materialized) views
-- (AGENTS.md: "Materialized views ... are not tracked by the diff engine"). They
-- are unchanged here, and the recreate even omitted the matviews' unique indexes,
-- so those statements were removed. This migration only adds fitsgl_datasets.

  create table "public"."fitsgl_datasets" (
    "prefix" text not null,
    "field" text not null,
    "kind" text not null,
    "tile" text,
    "tiles" text[] not null,
    "pixel_scale" text not null,
    "fitsgl_json_url" text not null,
    "bands" text[] not null,
    "source_hashes" jsonb not null,
    "is_default" boolean not null default false,
    "schema_version" integer not null default 1,
    "deployed_at" timestamp with time zone not null default now()
      );


alter table "public"."fitsgl_datasets" enable row level security;

CREATE UNIQUE INDEX fitsgl_datasets_pkey ON public.fitsgl_datasets USING btree (prefix);

CREATE INDEX idx_fitsgl_datasets_field ON public.fitsgl_datasets USING btree (field);

CREATE UNIQUE INDEX uq_fitsgl_datasets_field_default ON public.fitsgl_datasets USING btree (field) WHERE (is_default = true);

alter table "public"."fitsgl_datasets" add constraint "fitsgl_datasets_pkey" PRIMARY KEY using index "fitsgl_datasets_pkey";

alter table "public"."fitsgl_datasets" add constraint "fitsgl_datasets_kind_check" CHECK ((kind = ANY (ARRAY['field'::text, 'tile'::text]))) not valid;

alter table "public"."fitsgl_datasets" validate constraint "fitsgl_datasets_kind_check";

-- Visibility helper: a FitsGL dataset is public iff every backing mosaic is
-- published (a mixed published+draft composite must stay hidden). SECURITY DEFINER
-- so it sees the draft nircam_images rows a non-admin's RLS would otherwise hide.
CREATE OR REPLACE FUNCTION public.fitsgl_dataset_is_public(
  p_field text, p_tiles text[], p_bands text[], p_pixel_scale text
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM nircam_images ni
    WHERE ni.field = p_field AND ni.tile = ANY (p_tiles)
      AND ni.filter = ANY (p_bands) AND ni.pixel_scale = p_pixel_scale
      AND ni.epoch = '' AND ni.deploy_status = 'published'
  ) AND NOT EXISTS (
    SELECT 1 FROM nircam_images ni
    WHERE ni.field = p_field AND ni.tile = ANY (p_tiles)
      AND ni.filter = ANY (p_bands) AND ni.pixel_scale = p_pixel_scale
      AND ni.epoch = '' AND ni.deploy_status <> 'published'
  );
$$;

grant execute on function public.fitsgl_dataset_is_public(text, text[], text[], text) to "authenticated";

grant delete on table "public"."fitsgl_datasets" to "anon";

grant insert on table "public"."fitsgl_datasets" to "anon";

grant references on table "public"."fitsgl_datasets" to "anon";

grant select on table "public"."fitsgl_datasets" to "anon";

grant trigger on table "public"."fitsgl_datasets" to "anon";

grant truncate on table "public"."fitsgl_datasets" to "anon";

grant update on table "public"."fitsgl_datasets" to "anon";

grant delete on table "public"."fitsgl_datasets" to "authenticated";

grant insert on table "public"."fitsgl_datasets" to "authenticated";

grant references on table "public"."fitsgl_datasets" to "authenticated";

grant select on table "public"."fitsgl_datasets" to "authenticated";

grant trigger on table "public"."fitsgl_datasets" to "authenticated";

grant truncate on table "public"."fitsgl_datasets" to "authenticated";

grant update on table "public"."fitsgl_datasets" to "authenticated";

grant delete on table "public"."fitsgl_datasets" to "service_role";

grant insert on table "public"."fitsgl_datasets" to "service_role";

grant references on table "public"."fitsgl_datasets" to "service_role";

grant select on table "public"."fitsgl_datasets" to "service_role";

grant trigger on table "public"."fitsgl_datasets" to "service_role";

grant truncate on table "public"."fitsgl_datasets" to "service_role";

grant update on table "public"."fitsgl_datasets" to "service_role";


  create policy "admin_fitsgl_datasets_all"
  on "public"."fitsgl_datasets"
  as permissive
  for all
  to authenticated
using (( SELECT public.is_admin() AS is_admin))
with check (( SELECT public.is_admin() AS is_admin));



  create policy "authenticated_select_fitsgl_datasets"
  on "public"."fitsgl_datasets"
  as permissive
  for select
  to authenticated
using ((( SELECT public.is_admin() AS is_admin) OR public.fitsgl_dataset_is_public(field, tiles, bands, pixel_scale)));



  create policy "service_role_fitsgl_datasets_all"
  on "public"."fitsgl_datasets"
  as permissive
  for all
  to service_role
using (true)
with check (true);
